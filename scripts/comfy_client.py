# -*- coding: utf-8 -*-
"""本地 MiniMax H3 视频生成客户端（走 ComfyUI HTTP API）。

取代混元 / seedance 的云端调用：**动作与特效一律用本地的 H3 出片**。

为什么是这套配置（2026-10-07 在本机 3070 Ti 8GB 上落地）：
  · 本机 8GB 显存低于官方推荐的 12GB 门槛，靠三件事跑起来：
      64GB 内存（8-12GB 档的真瓶颈就是内存，不是显存）
      GGUF Q2/Q3 量化（不用 nvfp4 —— Ampere 上 nvfp4 只会模拟执行、零提速）
      小画布 + 短时长 + Turbo LoRA 步数
  · 启动参数必须 `--lowvram --disable-mmap`：
      --lowvram      把权重 offload 到内存（内存够，正是用武之地）
      --disable-mmap 绕开 Windows 上 >20GB safetensors 的崩溃（需 ComfyUI >= 0.31）
  · 不要装 SageAttention：30 系失败，且低显存下反而慢 2.5-3 倍。
  · 用 KJNodes 的两个**无损**节点压峰值显存：MiniMax H3 Low VRAM Attention、
    MiniMax H3 Chunk FeedForward —— 先用它们，再考虑降量化。
  · 采样固定 res_multistep + simple（官方模板的统一选择）；
    基座 20 步，挂上 Turbo LoRA 后 8 步（4 步也可用）。

硬约束（来自官方文档与工作流自带说明，均已在本机核对）：
  · 尺寸必须是 32 的倍数；短边 768 封顶（本地任何显卡都出不了 2K）
  · 时长按 17k+5 帧网格 @24fps 取整（1s=39 帧，2s=56 帧，5s=124 帧）
  · 视频与音频是联合生成的，音频无法单独关闭；我们只抽画面帧，忽略音轨

用法：
    from comfy_h3_client import ComfyH3Client
    c = ComfyH3Client()
    c.health()                                    # 确认服务在跑
    out = c.generate("first.png", "last.png", "the character walks in place ...",
                     seconds=2, megapixels=0.2, steps=8, out_path="clip.mp4")
"""
from __future__ import annotations

import json
import math
import pathlib
import time
import urllib.parse
import urllib.request
import uuid

BASE_URL = "http://127.0.0.1:8188"

# 已下载并验证过字节数的模型文件（见 .workbuddy/refs/minimax-h3-8gb-优化方案.md）
# 🔴 扩散模型**只能用 safetensors 量化版，不能用 GGUF**（2026-10-07 实测确认）：
#    city96 的 ComfyUI-GGUF 把扩散模型对着 IMG_ARCH_LIST 白名单校验
#    （flux/sd1/sdxl/sd3/aura/hidream/cosmos/ltxv/hyvid/wan/lumina2/qwen_image），
#    **里面没有 H3**，upstream issue #471 至今未支持 → UnetLoaderGGUF 必然报
#    "This model is not currently supported - (Unknown model architecture!)"。
#    （unsloth 的 GGUF 另有问题：缺 `general.architecture` 元数据，它是给 stablediffusion.cpp 的。）
#    所以走 convrot 量化 safetensors + 原生 UNETLoader。
UNET = "MiniMax_H3_FL2VA_pruned_mixed_int4_int8_convrot.safetensors"
# 文本编码器同样必须用 safetensors：qwen3vl 虽然在 TXT_ARCH_LIST 里，但
# unsloth 的 qwen3vl_32b_minimax_h3-Q2_K_M.gguf 缺 `general.architecture`，同样被拒。
# nvfp4 AWQ 是官方给 <48GB 显存档的推荐件（原生 CLIPLoader 的 device 默认 cpu，
# 15.7GB 编码器放内存、不占显存，每条提示词只跑一次）。
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
TURBO_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
GGUF_CLIP_UNUSABLE = "qwen3vl_32b_minimax_h3-Q2_K_M.gguf"   # 保留记录：不可用于 ComfyUI

FPS = 24
GRID = 17          # 时长必须落在 17k+5 上


def snap_length(seconds: float) -> int:
    """秒 -> 合法的帧数（17k+5 @24fps）。与官方工作流的 Math Expression 同一公式。"""
    n = max(5, round(seconds * FPS))
    return n + (5 - n % GRID) % GRID


def snap_size(megapixels: float, aspect: str = "1:1") -> tuple[int, int]:
    """按目标像素数算宽高，对齐到 32 的倍数。1:1 @0.2MP -> 448x448。"""
    ar = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16}[aspect]
    total = megapixels * 1_000_000
    h = math.sqrt(total / ar)
    w = h * ar
    def snap(v: float) -> int:
        return max(32, int(round(v / 32)) * 32)
    return snap(w), snap(h)


class ComfyH3Error(RuntimeError):
    """出片链路上的可读错误（连不上 / 缺节点 / 生成失败）。"""
    pass


class ComfyH3Client:
    def __init__(self, base_url: str = BASE_URL, *, unet: str = UNET, clip: str = CLIP,
                 vae_video: str = VAE_VIDEO, vae_audio: str = VAE_AUDIO,
                 lora: str = TURBO_LORA):
        self.base = base_url.rstrip("/")
        self.unet, self.clip = unet, clip
        self.vae_video, self.vae_audio = vae_video, vae_audio
        self.lora = lora
        self.client_id = uuid.uuid4().hex

    # ---------- HTTP 小工具 ----------

    def _get(self, path: str, timeout: int = 30):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, timeout: int = 60):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def health(self) -> dict:
        """返回 {ready, version, vram_mb, ram_mb, device, node_count}。"""
        try:
            stats = self._get("/system_stats", timeout=8)
        except Exception as e:
            raise ComfyH3Error(f"ComfyUI 没在跑？{self.base} —— {type(e).__name__}: {e}") from e
        sysinfo = (stats.get("system") or {})
        dev = (stats.get("devices") or [{}])[0]
        oi = self._get("/object_info", timeout=30)
        missing = [n for n in ("UnetLoaderGGUF", "CLIPLoaderGGUF", "MiniMaxH3ImageToVideo",
                               "MiniMaxLowVRAMAttention", "MiniMaxChunkFeedForward")
                   if n not in oi]
        return {"ready": not missing, "version": sysinfo.get("comfyui_version"),
                "vram_mb": dev.get("vram_total"), "ram_mb": sysinfo.get("ram_total"),
                "device": dev.get("name"), "node_count": len(oi), "missing_nodes": missing}

    def upload_image(self, path: str | pathlib.Path, *, subfolder: str = "h3in") -> str:
        """把首/尾帧上传到 ComfyUI 的 input 目录，返回可在 LoadImage 里引用的名字。"""
        p = pathlib.Path(path)
        if not p.exists():
            raise ComfyH3Error(f"找不到图片：{p}")
        boundary = "----comfyh3" + uuid.uuid4().hex
        body = b""
        for name, value in (("subfolder", subfolder), ("overwrite", "true"), ("type", "input")):
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                 f"filename=\"{p.name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
        body += p.read_bytes() + b"\r\n" + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.base + "/upload/image", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            info = json.loads(r.read().decode("utf-8"))
        sub = info.get("subfolder") or ""
        return f"{sub}/{info['name']}" if sub else info["name"]

    # ---------- 工作流 ----------

    def build(self, prompt: str, *, first_frame: str | None = None, last_frame: str | None = None,
              width: int | None = None, height: int | None = None,
              seconds: float = 2.0, megapixels: float = 0.2, aspect: str = "1:1",
              length: int | None = None, steps: int = 8, seed: int | None = None,
              lora_strength: float = 1.0, use_lora: bool = True,
              low_vram_nodes: bool = True, filename_prefix: str = "video/h3_action") -> dict:
        """构造 ComfyUI API 格式的工作流（FL2VA 首尾帧）。"""
        if width is None or height is None:
            width, height = snap_size(megapixels, aspect)
        if length is None:
            length = snap_length(seconds)

        g: dict = {}
        g["unet"] = {"class_type": "UNETLoader",
                     "inputs": {"unet_name": self.unet, "weight_dtype": "default"}}
        m: list = ["unet", 0]
        if use_lora:
            g["lora"] = {"class_type": "LoraLoaderModelOnly",
                         "inputs": {"model": m, "lora_name": self.lora,
                                    "strength_model": lora_strength}}
            m = ["lora", 0]
        # KJNodes 的两个无损降显存节点：先加它们，再考虑降量化
        if low_vram_nodes:
            g["lowvram_attn"] = {"class_type": "MiniMaxLowVRAMAttention",
                                 "inputs": {"model": m, "head_chunks": 4}}
            g["chunk_ff"] = {"class_type": "MiniMaxChunkFeedForward",
                             "inputs": {"model": ["lowvram_attn", 0], "chunks": 2,
                                        "seq_threshold": 4096}}
            m = ["chunk_ff", 0]
        g["clip"] = {"class_type": "CLIPLoader",
                     "inputs": {"clip_name": self.clip, "type": "minimax"}}
        g["vae_v"] = {"class_type": "VAELoader", "inputs": {"vae_name": self.vae_video}}
        g["vae_a"] = {"class_type": "VAELoader", "inputs": {"vae_name": self.vae_audio}}

        h3_inputs: dict = {"clip": ["clip", 0], "vae": ["vae_v", 0], "prompt": prompt,
                           "width": width, "height": height, "length": length}
        if first_frame:
            g["img_first"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
            h3_inputs["first_frame"] = ["img_first", 0]
        if last_frame:
            g["img_last"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
            h3_inputs["last_frame"] = ["img_last", 0]
        g["h3"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": h3_inputs}

        g["noise"] = {"class_type": "RandomNoise",
                      "inputs": {"noise_seed": seed if seed is not None else int(time.time() * 1000) % 2**31}}
        g["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
        g["sched"] = {"class_type": "BasicScheduler",
                      "inputs": {"model": m, "scheduler": "simple", "steps": steps, "denoise": 1.0}}
        g["guider"] = {"class_type": "BasicGuider", "inputs": {"model": m, "conditioning": ["h3", 0]}}
        g["sample"] = {"class_type": "SamplerCustomAdvanced",
                       "inputs": {"noise": ["noise", 0], "guider": ["guider", 0],
                                  "sampler": ["sampler", 0], "sigmas": ["sched", 0],
                                  "latent_image": ["h3", 1]}}
        g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae_v", 0]}}
        g["decode_a"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["sample", 0], "vae": ["vae_a", 0]}}
        g["video"] = {"class_type": "CreateVideo",
                      "inputs": {"images": ["decode", 0], "fps": float(FPS), "audio": ["decode_a", 0]}}
        g["save"] = {"class_type": "SaveVideo",
                     "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix,
                                "format": "auto", "codec": "auto"}}
        return {"prompt": g, "_meta": {"width": width, "height": height, "length": length,
                                       "seconds": round(length / FPS, 2), "steps": steps}}

    # ---------- 提交 / 等待 / 下载 ----------

    def submit(self, workflow: dict) -> str:
        r = self._post("/prompt", {"prompt": workflow["prompt"], "client_id": self.client_id})
        if "prompt_id" not in r:
            raise ComfyH3Error(f"提交失败：{r}")
        return r["prompt_id"]

    def wait(self, prompt_id: str, *, interval: float = 5.0, timeout_s: float = 7200,
             on_tick=None) -> dict:
        """轮询 /history 直到完成。返回 history 条目。"""
        t0 = time.time()
        while True:
            try:
                h = self._get(f"/history/{prompt_id}", timeout=30).get(prompt_id)
            except Exception:
                h = None
            if h:
                status = (h.get("status") or {}).get("status_str")
                if status in ("success", "error"):
                    if status == "error":
                        msgs = (h.get("status") or {}).get("messages") or []
                        raise ComfyH3Error(f"生成失败：{msgs}")
                    return h
            el = time.time() - t0
            if el > timeout_s:
                raise ComfyH3Error(f"等待超时（{timeout_s:.0f}s）")
            if on_tick:
                on_tick(f"  生成中… 已等待 {el:.0f}s")
            time.sleep(interval)

    def outputs(self, entry: dict) -> list[dict]:
        """从 history 条目里取输出文件描述（filename/subfolder/type）。"""
        out = []
        for node_out in (entry.get("outputs") or {}).values():
            for key in ("videos", "gifs", "images"):
                for item in (node_out.get(key) or []):
                    if isinstance(item, dict) and "filename" in item:
                        out.append(item)
        return out

    def download(self, item: dict, out_path: str | pathlib.Path) -> pathlib.Path:
        q = urllib.parse.urlencode({"filename": item["filename"],
                                    "subfolder": item.get("subfolder", ""),
                                    "type": item.get("type", "output")})
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{self.base}/view?{q}", timeout=600) as r:
            out.write_bytes(r.read())
        return out

    def generate(self, first_frame, last_frame=None, prompt: str = "", *, out_path,
                 on_tick=None, **kw) -> pathlib.Path:
        """上传首尾帧 -> 提交 -> 等待 -> 下载 mp4。返回落盘路径。"""
        wf = self.build(prompt, first_frame=None, last_frame=None, **kw)
        f1 = self.upload_image(first_frame)
        f2 = self.upload_image(last_frame) if last_frame else None
        # 用上传后的文件名重建工作流
        wf = self.build(prompt, first_frame=f1, last_frame=f2, **kw)
        meta = wf["_meta"]
        if on_tick:
            on_tick(f"  画布 {meta['width']}x{meta['height']} / {meta['length']} 帧"
                    f"（{meta['seconds']}s @{FPS}fps）/ {meta['steps']} 步")
        pid = self.submit(wf)
        if on_tick:
            on_tick(f"  已提交 prompt_id={pid}")
        h = self.wait(pid, on_tick=on_tick)
        items = self.outputs(h)
        if not items:
            raise ComfyH3Error(f"没有输出文件：{json.dumps(h.get('outputs'), ensure_ascii=False)[:400]}")
        return self.download(items[0], out_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="本地 MiniMax H3 视频生成（ComfyUI 后端）")
    ap.add_argument("--health", action="store_true", help="只检查服务与节点是否就绪")
    ap.add_argument("--first", help="首帧图片")
    ap.add_argument("--last", help="尾帧图片（不传则只用首帧）")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--megapixels", type=float, default=0.2)
    ap.add_argument("--aspect", default="1:1")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--seed", type=int)
    ap.add_argument("-o", "--out", default="out_h3.mp4")
    a = ap.parse_args()

    c = ComfyH3Client()
    info = c.health()
    print(json.dumps(info, ensure_ascii=False, indent=1))
    if a.health:
        raise SystemExit(0 if info["ready"] else 1)
    if not a.first:
        raise SystemExit("需要 --first")
    t0 = time.time()
    p = c.generate(a.first, a.last, a.prompt, out_path=a.out, on_tick=print,
                   seconds=a.seconds, megapixels=a.megapixels, aspect=a.aspect, steps=a.steps, seed=a.seed)
    print(f"完成：{p}  用时 {time.time()-t0:.1f}s")
