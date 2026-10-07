# -*- coding: utf-8 -*-
"""攻击/技能特效精灵 —— **独立于角色**，游戏里叠加播放。

    python fx_forge.py --fx slash --out ./_gen_actions/fx
    python fx_forge.py --all --out ./_gen_actions/fx

🔴 为什么特效必须独立（否则一定会出问题）：
    特效烘在角色帧里的后果 —— 挥剑的光弧把**单帧宽度撑大**（实测 322px vs 站立 174px），
    而且和角色焊死：没法单独调位置、时长、缩放，也没法让特效"晚于动作"出现。

技术选择：**黑底 + alpha=亮度**
  · 特效在**纯黑底**上生成（不绿幕）—— 光效不需要抠色，亮度天然就是 alpha
  · alpha = 像素亮度（减去 floor 去掉视频压缩的底噪）→ 黑=全透明，亮=不透明，光晕边缘自然半透明
  · 游戏端用 `globalCompositeOperation='lighter'`（加法）叠加：黑底加 0 等于没画，亮的才是光

`delay_ratio`：特效在动作的哪个时刻才开始播（占动作时长比例）。
    跟手的（刀光）给 0；要"等招式演到末段才落"的（落雷）给 0.8 ——
    正好和"动作播完才结算伤害"的时序对齐。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 内置特效库。prompt 要点：把**该特效的物理特征**全部点名 ——
# ⚠️ 写"能量柱/vertical pillar of energy"这种抽象说法会生成一根模糊竖条（实测很差）。
FX_LIB = {
    "slash": dict(
        seconds=0.7, megapixels=0.2, steps=8,
        prompt=("A single bright white crescent sword slash arc, a thin curved energy blade "
                "trail sweeping from upper right to lower left, glowing white core with a warm "
                "yellow edge, sharp clean shape, centered in the frame, isolated on pure black "
                "background, no character, no hands, no sword, no text, no numbers, "
                "no camera movement. The arc sweeps across in one quick motion."),
        offset_x=0.62, offset_y=-0.52, scale=1.25, delay_ratio=0.0,
    ),
    "burst": dict(
        seconds=1.2, megapixels=0.2, steps=8,
        prompt=("A single lightning bolt striking straight down from the very top of the frame "
                "to the ground at the center: one jagged zigzag streak with one or two sharp "
                "forked branches, a blinding white hot core wrapped in electric cyan glow, "
                "and at the instant of impact a flat radial shockwave ring bursting outward "
                "along the ground with sharp radial light spokes. "
                "High contrast, crisp glowing edges, dramatic, isolated on pure black "
                "background, no character, no hands, no weapon, no text, no numbers, "
                "no camera movement."),
        offset_x=0.45, offset_y=-0.78, scale=1.9, delay_ratio=0.80,
    ),
    "impact": dict(
        seconds=0.6, megapixels=0.2, steps=8,
        prompt=("A sharp white impact flash burst: a compact radial starburst of straight light "
                "spikes around a blinding white core, fast expanding then fading, "
                "sharp clean edges, isolated on pure black background, no character, no hands, "
                "no weapon, no text, no numbers, no camera movement."),
        offset_x=0.55, offset_y=-0.45, scale=1.0, delay_ratio=0.5,
    ),
}

NEG = "character, person, hands, weapon, text, watermark, background scenery, fog, cloud, smoke"


def luminance_alpha(im: Image.Image, floor: int = 12) -> Image.Image:
    """黑底 → RGBA：alpha = 亮度（减 floor 去底噪），颜色保持。

    floor 不能省：视频压缩会把"纯黑"压出 3~10 的噪点，不砍掉整张精灵会蒙一层灰。
    """
    a = np.array(im.convert("RGB")).astype(np.float32)
    lum = a.max(axis=2)
    alpha = np.clip((lum - floor) / (255.0 - floor), 0, 1) * 255.0
    return Image.fromarray(np.dstack([a, alpha]).astype(np.uint8), "RGBA")


def build(fx: str, out_dir: pathlib.Path, *, seconds=None, megapixels=None, steps=8,
          reuse_video=False, frame_h: int = 300, n_want: int = 8) -> dict:
    from comfy_client import ComfyH3Client, ComfyH3Error
    from sprite_forge import build_sheet, extract

    spec = FX_LIB[fx]
    sec = seconds if seconds is not None else spec["seconds"]
    mp = megapixels if megapixels is not None else spec["megapixels"]
    work = out_dir / "_work" / fx
    work.mkdir(parents=True, exist_ok=True)
    black, mp4 = work / "black.png", work / "clip.mp4"

    print(f"\n{'=' * 62}\n=== 特效 · {fx} ===")
    if not reuse_video:
        Image.new("RGB", (448, 448), (0, 0, 0)).save(black)
        print(f"[1/3] 黑底首帧\n[2/3] H3 出片 {sec}s / {mp}MP / {steps} 步 …")
        try:
            ComfyH3Client().generate(black, None, spec["prompt"] + f" Avoid: {NEG}.",
                                     out_path=mp4, seconds=sec, megapixels=mp, steps=steps,
                                     filename_prefix=f"video/fx_{fx}")
        except URLError as e:
            raise SystemExit(
                f"❌ 连不上本地 ComfyUI（{e}）。先起服务：\n"
                f"     bash scripts/ensure_comfy.sh")
        except ComfyH3Error as e:
            raise SystemExit(f"❌ 生成失败：{e}")
        print(f"      -> {mp4.name} {mp4.stat().st_size // 1024}KB")
    else:
        if not mp4.exists():
            raise SystemExit(f"❌ --reuse-video 但 {mp4} 不存在")

    frames = [luminance_alpha(Image.open(p)) for p in extract(mp4, 24, work / "_frames")]
    cover = [float((np.array(f)[..., 3] > 24).mean()) for f in frames]
    act = np.where(np.array(cover) > max(0.01, max(cover) * 0.15))[0]
    s, e = (int(act.min()), int(act.max())) if len(act) else (0, len(frames) - 1)
    idx = sorted(set(min(s + round(k * (e - s) / max(1, n_want - 1)), len(frames) - 1)
                     for k in range(n_want)))
    sheet = build_sheet([frames[i] for i in idx], frame_h=frame_h)
    dst = out_dir / f"{fx}.webp"
    sheet.save(dst, "WEBP", quality=92, method=6)
    meta = {"fx": fx, "frames": len(idx), "fps": 12, "loop": False,
            "cell": [round(sheet.width / len(idx)), sheet.height],
            "sheet": [sheet.width, sheet.height],
            "offset_x": spec["offset_x"], "offset_y": spec["offset_y"], "scale": spec["scale"],
            "delay_ratio": spec["delay_ratio"],
            "h3": {"seconds": sec, "megapixels": mp, "steps": steps}}
    (out_dir / f"{fx}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    print(f"[3/3] {len(frames)} 帧 → 取 {len(idx)} 帧（最亮覆盖 {max(cover):.1%}）")
    print(f"✅ {dst.name}  {sheet.width}x{sheet.height}  delay={spec['delay_ratio']}")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="攻击/技能特效精灵（独立于角色）")
    ap.add_argument("--fx", choices=list(FX_LIB))
    ap.add_argument("--all", action="store_true", help=f"全部：{list(FX_LIB)}")
    ap.add_argument("--out", default="./_gen_actions/fx")
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--megapixels", type=float)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--frame-h", type=int, default=300)
    ap.add_argument("--reuse-video", action="store_true")
    a = ap.parse_args()
    todo = list(FX_LIB) if a.all else ([a.fx] if a.fx else [])
    if not todo:
        raise SystemExit("需要 --fx 或 --all")
    for f in todo:
        try:
            build(f, pathlib.Path(a.out), seconds=a.seconds, megapixels=a.megapixels,
                  steps=a.steps, reuse_video=a.reuse_video, frame_h=a.frame_h)
        except SystemExit as e:
            print(f"  ⏭ 跳过 {f}：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
