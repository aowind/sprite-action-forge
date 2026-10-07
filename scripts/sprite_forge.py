# -*- coding: utf-8 -*-
"""立绘 → 全套动作精灵表（本地 MiniMax H3 出片，全自动）。

一条命令跑完：绿幕首帧 → 本地 H3 出片 → 抽帧 → 抠绿 → **逐帧对齐** → 选窗 → 拼表。

    python sprite_forge.py --art 少侠.png --char hero --actions idle,run,attack,skill,hurt

产出（默认写 ./_gen_actions/）：
    <char>_<action>.webp   精灵表（每帧等宽、内容不裁切、跨动作人物等高）
    <char>_<action>.json   meta：帧数/fps/loop/content_h_ratio/baseline_ratio/选窗质量

设计要点（每一条都是踩过坑才定下来的，改之前先看 references/ 里的说明）：
  · **时长配方**：循环动作给 2 秒（56 帧）—— 1 秒两端起停包络占满、没有稳定的中段循环窗
  · **帧几何**：画布尺寸由内容实际伸展范围**反推**，上下左右对称留白 → 数学上保证不裁切
  · **对齐语义**：脚底用**整段平移**（保留起伏），水平按动作类型选"逐帧居中/整段平移"
  · **提示词**：怪物必须给具体到身体部位的措辞（通用措辞会让模型生成静止插画）
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import shutil
import subprocess
import sys
from urllib.error import URLError

import numpy as np
from PIL import Image, ImageChops, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ============================================================ 抠绿参数
# 判据要严、宁漏勿误：角色身上本来就有青绿/墨绿，判宽了会在身上抠出洞。
# 幕布是**纯亮绿**（g≈255, r=b≈0），所以用"g 明显高于 max(r,b)" + "g 本身够亮"两条。
GREEN = (0, 255, 0)
GREEN_MARGIN = 46
GREEN_MIN = 110
ALPHA_MIN = 8          # 判定"这里有像素"的阈值（>0 会把极淡的抗锯齿也算进来）

# ============================================================ 动作配方
# loop    : 循环动作（取中段稳定窗）；False = 一次性（攻击/技能/挨打）
# seconds : 出片时长。循环动作**必须 2 秒左右**，1 秒没有稳定的中段循环窗（实测）
# frames  : 最终取几帧；fps: 播放帧率
# center  : 水平对齐方式。循环类逐帧居中（消原地跑漂移）；
#           一次性类整段平移（**保留前冲/踉跄的位移**，居中会把位移压平）
# tail    : 取视频**尾段**选窗 —— 用来避开"快速挥砍必然带出的运动拖尾"
# no_fx   : 提示词里追加"禁止一切画面特效"（特效要单独出一条精灵，别烘进角色帧）
ACTIONS = {
    "idle":   dict(loop=True,  center=True,  seconds=2.0, frames=8,  fps=6,  draw=0.5, no_fx=False,
                   prompt="{subj} stands idle in place, a subtle breathing loop: chest rises and "
                          "falls, a slight weight shift, one slow blink. Feet stay planted.",
                   neg="walking, running, stepping, travelling"),
    "walk":   dict(loop=True,  center=True,  seconds=2.0, frames=10, fps=10, draw=1.0, no_fx=False,
                   prompt="{subj} walks in place like on a treadmill, a steady repeating walk "
                          "cycle at a relaxed pace, heel-to-toe steps, arms swinging opposite "
                          "the legs, a slight up-and-down bob. Stays centered, does not travel.",
                   neg="running, standing still, turning around"),
    "run":    dict(loop=True,  center=True,  seconds=2.0, frames=10, fps=12, draw=1.0, no_fx=False,
                   prompt="{subj} runs in place like on a treadmill, a steady repeating run "
                          "cycle: knees high, arms pumping, torso leaning forward, both feet "
                          "leave the ground between steps. Stays centered, does not travel.",
                   neg="walking, standing still, slowing down"),
    "attack": dict(loop=False, center=False, seconds=1.0, frames=8,  fps=12, draw=1.0, no_fx=True,
                   prompt="{subj} performs one quick strike, then returns to the starting ready "
                          "pose. Snappy anticipation, fast motion forward, short follow-through.",
                   neg="walking, running, standing idle, multiple strikes"),
    "skill":  dict(loop=False, center=False, seconds=1.5, frames=10, fps=10, draw=1.0, no_fx=True,
                   tail=True,
                   prompt="{subj} slowly raises the weapon up overhead, holds it high while "
                          "gathering power, the stance steady and feet planted, then lowers it "
                          "back to the ready pose. Slow controlled movement, small range of "
                          "motion, no fast swing, no big arc.",
                   neg="walking, running, big fast swing, spinning, ordinary strike"),
    "hurt":   dict(loop=False, center=False, seconds=0.7, frames=6,  fps=12, draw=0.6, no_fx=False,
                   prompt="{subj} takes a hit: flinches backward, head snaps back, staggers half "
                          "a step, then recovers to the ready pose.",
                   neg="walking, running, attacking, staying still"),
}

NO_FX = ("Absolutely no visual effects: no energy trail, no glowing arc, no crescent light "
         "sweep, no light streaks, no particles, no impact flash, no dust, no smoke, "
         "no colored haze or mist, no motion blur, no speed lines, no aura, "
         "no glow around the weapon or the body. "
         "The weapon is plain, clean and fully visible with a sharp simple silhouette. "
         "The background stays pure flat solid green with nothing drawn on it at all.")

LOCKED = ("Locked static camera, no camera movement. Flat pure green screen background, "
          "no ground shadow, {style}, same character design as the first frame. "
          "Silent, no audio, no music, no speech.")


def _fs(p) -> str:
    return str(pathlib.Path(p).resolve())


# ============================================================ 绿幕首帧
def build_green_frame(art: pathlib.Path, out: pathlib.Path, *,
                      size: int = 768, content_ratio: float = 0.72,
                      foot_ratio: float = 0.92, tol: int = 26) -> Image.Image:
    """立绘 → 绿幕首帧（H3 的首帧）。

    顺序很关键：**先 flood fill 再缩放**。反过来（先缩再填）会引入新的抗锯齿白边。
    四角 flood fill 只把**与边界连通的**背景变绿 —— 角色内部的白（眼白/衣服）不会被误伤。
    """
    im = Image.open(art)
    if im.mode != "RGB":
        # 带 alpha 的立绘：把透明区直接铺成绿（比认颜色更准）
        bg = Image.new("RGB", im.size, GREEN)
        bg.paste(im, (0, 0), im.split()[-1] if im.mode in ("RGBA", "LA") else None)
        view = bg
    else:
        view = im.convert("RGB")

    # 降到中等尺寸再 flood fill（纯 Python 实现，小图快十几倍）
    if view.height > 760:
        k = 760 / view.height
        view = view.resize((max(1, round(view.width * k)), 760), Image.LANCZOS)

    vw, vh = view.size
    for s in ((0, 0), (vw - 1, 0), (0, vh - 1), (vw - 1, vh - 1)):
        ImageDraw.floodfill(view, s, GREEN, thresh=tol)
    view = _kill_fringe(view)

    # 按"非绿"裁到主体
    non_green = ImageChops.invert(_green_mask(view))
    bb = non_green.getbbox()
    if bb:
        view = view.crop(bb)

    # 贴到绿幕画布：主体占高度 content_ratio，水平居中，脚底落在 foot_ratio
    target_h = int(size * content_ratio)
    k2 = target_h / view.height
    view = view.resize((max(1, round(view.width * k2)), target_h), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), GREEN)
    canvas.paste(view, (int(size * 0.5 - view.width * 0.5),
                        size - int(size * (1 - foot_ratio)) - view.height))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"  首帧 {out.name} {size}x{size}  主体 {view.size}  {out.stat().st_size // 1024}KB")
    return canvas


def _green_mask(im: Image.Image) -> Image.Image:
    """纯亮绿幕布的掩膜（给 flood fill 后的清边用）。"""
    a = np.array(im.convert("RGB")).astype(np.int16)
    g = a[..., 1]
    mask = (g - np.maximum(a[..., 0], a[..., 2]) > 60) & (g > 150)
    return Image.fromarray((mask * 255).astype(np.uint8), "L")


def _kill_fringe(im: Image.Image) -> Image.Image:
    """去掉白边上残留的浅色描边（纯白像素若 8 邻域有绿，就吃掉它）。"""
    a = np.array(im.convert("RGB"))
    white = np.all(a > 238, axis=2)
    if not white.any():
        return im
    gm = np.array(_green_mask(im)) > 0
    grown = np.zeros_like(gm)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            grown |= np.roll(np.roll(gm, dy, 0), dx, 1)
    a[white & grown] = GREEN
    return Image.fromarray(a, "RGB")


# ============================================================ 抠绿
def key_out(im: Image.Image):
    """绿幕 → RGBA。返回 (rgba, 绿色占比)。含 despill（压掉边缘的绿反光）。"""
    a = np.array(im.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    green = (g - np.maximum(r, b) > GREEN_MARGIN) & (g > GREEN_MIN)
    alpha = np.where(green, 0, 255).astype(np.uint8)
    # despill：边缘被幕布染绿 → 把 g 压到和 max(r,b) 一个水平
    over = (~green) & (g > np.maximum(r, b))
    a2 = a.copy()
    a2[..., 1] = np.where(over, np.maximum(r, b), g)
    rgba = np.dstack([a2.clip(0, 255).astype(np.uint8), alpha])
    return Image.fromarray(rgba, "RGBA"), float(green.mean())


def hole_ratio(rgba: Image.Image) -> float:
    """体检：**被主体轮廓围住的**透明像素占比（"身上透绿"的真正判据）。

    ⚠️ 两个错误判据都踩过：① 取画面中间一块（主体本来就窄，大半是背景，占比虚高）；
    ② 取 bbox 矩形（bbox 是矩形、主体是细长条，框内照样一大半是背景）。
    正确做法：对"非绿"掩膜**填洞** —— 从图像边界 flood fill，填不到的透明区才是"身上透绿"。
    """
    al = np.array(rgba)[..., 3]
    solid = al > ALPHA_MIN
    if not solid.any():
        return 0.0
    h, w = solid.shape
    free = ~solid
    # 从边界 flood fill（迭代式，不引 scipy）
    reach = np.zeros_like(free)
    stack = []
    for x in range(w):
        for y in (0, h - 1):
            if free[y, x]:
                stack.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if free[y, x]:
                stack.append((y, x))
    while stack:
        y, x = stack.pop()
        if reach[y, x] or not free[y, x]:
            continue
        reach[y, x] = True
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and free[ny, nx] and not reach[ny, nx]:
                stack.append((ny, nx))
    inside_hole = free & ~reach
    return float(inside_hole.sum()) / float(solid.sum() + inside_hole.sum())


# ============================================================ 抽帧
def _ffmpeg() -> str:
    """找 ffmpeg，按这个顺序试：
        ① 环境变量 SPRITE_FFMPEG（显式指定）
        ② imageio-ffmpeg（自带一份 ffmpeg 二进制，最省事）
           —— 顺带把"随包依赖"目录加进 sys.path：很多项目把 imageio_ffmpeg
              放在 `<项目>/tools/_py/` 这种地方（实测就是这么用的），光靠 pip 装的位置找不到
        ③ 系统 PATH 里的 ffmpeg
    三条都不通时给一句能照做的报错，别抛 ModuleNotFoundError。
    """
    import os
    env = os.environ.get("SPRITE_FFMPEG")
    if env and pathlib.Path(env).exists():
        return env
    for cand in (HERE / "_py", HERE.parent / "_py",
                 pathlib.Path.cwd() / "_py", pathlib.Path.cwd() / "tools" / "_py"):
        if cand.is_dir() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise SystemExit(
        "❌ 找不到 ffmpeg。任选一种：\n"
        "     ① pip install imageio-ffmpeg        （推荐，自带一份，不用装系统包）\n"
        "     ② 把 imageio_ffmpeg 放到 _py/ 目录下（随包依赖，配合脚本自动搜索）\n"
        "     ③ 装系统 ffmpeg 并确保在 PATH 里\n"
        "     ④ SPRITE_FFMPEG=/path/to/ffmpeg 显式指定")


def extract(video: pathlib.Path, fps: int = 24, tmp: pathlib.Path | None = None):
    """用 ffmpeg 抽帧（原生 24fps 抽满，后面由选窗决定采样）。"""
    tmp = tmp or (video.parent / "_frames")
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob("*.png"):
        f.unlink()
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error",
                    "-i", str(video), "-vf", f"fps={fps}", str(tmp / "f_%04d.png")],
                   check=True)
    return sorted(tmp.glob("f_*.png"))


# ============================================================ 帧几何
def _measure(frames):
    out = []
    for f in frames:
        a = np.array(f)[..., 3]
        ys, xs = np.where(a > ALPHA_MIN)
        out.append((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                   if len(ys) else None)
    return out


def align_frames(frames, *, center: bool = True, content_ratio: float = 0.78,
                 pad_frac_x: float = 0.06):
    """统一帧几何：整段等比缩放 + 脚底对齐 + （按需）水平居中，**数学上保证不裁切内容**。

    🔴 三个必须这么做的理由（都踩过）：
      1. 画布尺寸要由**内容实际伸展范围反推**。
         早期写法把"内容中心"对到画布坐标 cx，而画布宽按 `cx ± half_w` 算、原点却在 0
         → 内容中心落在 cx 而不是 W/2，**超出部分被直接裁掉**。
         实测：idle 6/6 帧右边缘被切、怪物系全部左边被切 —— 表现就是"人只显示一半"。
      2. 垂直要用**上下对称留白**。写成 `baseline = H*U/span` 时，D=0 会让基线落在最底边、
         底边距为 0 → 实测 23 帧全贴边被裁。
      3. 脚底对齐要用**整段同一个 dy**。逐帧对齐脚底会把跳跃/腾空的高度差压平。
      4. `center` 按动作区分：循环类逐帧居中（消原地跑漂移）；一次性类整段平移
         （保留前冲位移，逐帧居中会把它压平）。
    """
    boxes = _measure(frames)
    hs = [b[3] - b[1] for b in boxes if b]
    if not hs:
        return frames, {}
    H0 = frames[0].height
    k = (content_ratio * H0) / float(np.median(hs))
    if abs(k - 1.0) > 0.01:
        frames = [f.resize((max(1, round(f.width * k)), max(1, round(f.height * k))),
                           Image.LANCZOS) for f in frames]
        boxes = _measure(frames)
    live = [b for b in boxes if b]
    base = float(np.median([b[3] for b in live]))
    if center:
        refs = [((b[0] + b[2]) / 2 if b else 0) for b in boxes]
    else:
        refs = [f.width / 2 for f in frames]      # 原图中心 → 保留帧间水平位移
    L = max([refs[i] - boxes[i][0] for i in range(len(frames)) if boxes[i]] or [0])
    R = max([boxes[i][2] - refs[i] for i in range(len(frames)) if boxes[i]] or [0])
    U = max([base - boxes[i][1] for i in range(len(frames)) if boxes[i]] or [0])
    D = max([boxes[i][3] - base for i in range(len(frames)) if boxes[i]] or [0])
    span_x, span_y = max(L + R, 1.0), max(U + D, 1.0)
    H = max(1, int(round(span_y / content_ratio)))
    baseline_y = (H - span_y) / 2 + U              # 上下对称留白
    W = max(1, int(round(span_x / (1 - 2 * pad_frac_x))))
    anchor_x = (W - span_x) / 2 + L
    dy = int(round(baseline_y - base))             # 整段同一个 dy → 保留起伏
    out = []
    for i, f in enumerate(frames):
        c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        c.paste(f, (int(round(anchor_x - refs[i])), dy), f)
        out.append(c)
    chk = _measure(out)
    touch = sum(1 for b in chk if b and (b[0] <= 0 or b[1] <= 0 or b[2] >= W or b[3] >= H))
    if touch:
        print(f"  ⚠️ 对齐后有 {touch} 帧贴边（可能被裁）—— 检查 align_frames")
    med_h = float(np.median([b[3] - b[1] for b in chk if b])) if chk else 0.0
    return out, {"canvas": [W, H], "baseline": round(baseline_y, 1), "centered": center,
                 "content_h_ratio": round(med_h / H, 4),
                 "baseline_ratio": round(baseline_y / H, 4),
                 "touch_edges": touch,
                 "spread_center": round(float(np.std([(b[0] + b[2]) / 2 for b in live])), 2),
                 "spread_bottom": round(float(np.std([b[3] for b in live])), 2)}


# ============================================================ 选窗
def pick_window(frames, action: str, n_want: int, spec: dict):
    """选窗 → 采样 n_want 帧。

    循环动作用 seam/baseline 最优窗（`loop_check` 的算法），并给出**质量分**：
      score < 0.5 干净 / 0.5~0.8 看一眼 / > 0.8 步幅超出窗，要扩窗口或重新生成。
    一次性动作取"离静止姿势最远"的活动窗；`tail=True` 时取尾段（避开运动拖尾）。
    """
    import loop_check as lc
    smalls = []
    for f in frames:
        im = f.convert("RGBA")
        k = lc.SMALL / max(im.width, im.height)
        if k < 1:
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BOX)
        a = np.array(im).astype(np.float32)
        smalls.append((a[..., :3] * (a[..., 3:] / 255), a[..., 3] / 255))
    n = len(smalls)
    if n_want >= n:
        return list(range(n)), {"mode": "all"}

    if spec.get("loop"):
        best = lc.loop_window(smalls, 6, max(8, n // 2), first=1,
                              min_base=0.05 if action == "idle" else lc.BASE_MIN)
        if best is None:
            best = lc.loop_window(smalls, 6, max(8, n // 2), first=1, min_base=0)
        if best:
            score, s, P, seam, base = best
            idx = sorted(set(min(s + round(k * P / n_want), n - 1) for k in range(n_want)))
            return idx, {"mode": "loop", "start": s, "period": P,
                         "score": round(float(score), 3),
                         "seam": round(float(seam), 2), "baseline": round(float(base), 2)}

    if spec.get("tail"):
        s0 = int(n * 0.68)
        idx = sorted(set(min(s0 + round(k * (n - 1 - s0) / max(1, n_want - 1)), n - 1)
                         for k in range(n_want)))
        return idx, {"mode": "tail", "start": s0, "end": n - 1}

    rest = smalls[0]
    d = np.array([float(np.abs(f[0] - rest[0]).mean() + lc.ALPHA_W * np.abs(f[1] - rest[1]).mean())
                  for f in smalls])
    thr = max(1.0, 0.12 * float(d.max()))
    act = np.where(d > thr)[0]
    s = max(0, int(act.min()) - 1) if len(act) else 0
    e = min(n - 1, int(act.max()) + 2) if len(act) else n - 1
    idx = sorted(set(min(s + round(k * (e - s) / max(1, n_want - 1)), n - 1)
                     for k in range(n_want)))
    return idx, {"mode": "active-window", "start": s, "end": e}


def build_sheet(frames, frame_h: int = 300) -> Image.Image:
    """等高等比缩放到 frame_h，横向拼表。总宽 = 单帧宽 × 帧数，注意别顶穿 GPU 纹理上限。"""
    scaled = []
    for f in frames:
        k = frame_h / f.height
        scaled.append(f.resize((max(1, round(f.width * k)), frame_h), Image.LANCZOS))
    sheet = Image.new("RGBA", (sum(s.width for s in scaled), frame_h), (0, 0, 0, 0))
    x = 0
    for s in scaled:
        sheet.paste(s, (x, 0), s)
        x += s.width
    return sheet


# ============================================================ 主流程
def produce(char: str, action: str, art: pathlib.Path, out_dir: pathlib.Path, *,
            style: str = "clean stylized game character art", prompts: dict | None = None,
            seconds=None, megapixels=0.2, steps=8, seed=None, frame_h=300,
            reuse_video=False, keep_work=False) -> dict:
    from comfy_client import ComfyH3Client, ComfyH3Error
    spec = dict(ACTIONS[action])
    if prompts:
        spec.update({k: v for k, v in prompts.items() if k in ("prompt", "neg", "tail", "seconds")})
    sec = seconds if seconds is not None else spec["seconds"]
    work = out_dir / "_work" / f"{char}_{action}"
    work.mkdir(parents=True, exist_ok=True)
    first_png, mp4 = work / "first_frame.png", work / "clip.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 66}\n=== {char} · {action}  ({'循环' if spec['loop'] else '一次性'}) ===")
    if not reuse_video:
        build_green_frame(art, first_png)
        text = spec["prompt"].format(subj=style) + " " + LOCKED.format(style=style)
        if spec.get("no_fx"):
            text += " " + NO_FX
        if spec.get("neg"):
            text += f" Avoid: {spec['neg']}."
        print(f"[1/4] 绿幕首帧就绪\n[2/4] 本地 H3 出片 {sec}s / {megapixels}MP / {steps} 步 …")
        try:
            ComfyH3Client().generate(first_png, None, text, out_path=mp4,
                                     seconds=sec, megapixels=megapixels, steps=steps,
                                     seed=seed, filename_prefix=f"video/{char}_{action}")
        except URLError as e:
            raise SystemExit(
                f"❌ 连不上本地 ComfyUI（{e}）。先把服务起起来：\n"
                f"     bash scripts/ensure_comfy.sh\n"
                f"   （本机还没装 ComfyUI / H3 模型的话，看 SKILL.md 的「环境要求」）")
        except ComfyH3Error as e:
            raise SystemExit(f"❌ 生成失败：{e}")
        print(f"      -> {mp4.name} {mp4.stat().st_size // 1024}KB")
    else:
        if not mp4.exists():
            raise SystemExit(f"❌ --reuse-video 但 {mp4} 不存在")

    pngs = extract(mp4, 24, work / "_frames")
    kept, holes = [], []
    for p in pngs:
        rgba, gr = key_out(Image.open(p))
        if gr > 0.98:                     # 整幅都是绿 = 没抠到人物
            continue
        kept.append(rgba)
        holes.append(hole_ratio(rgba))
    print(f"[3/4] 抽出 {len(pngs)} 帧 → 抠绿成功 {len(kept)} 帧"
          f"（主体内部透绿 {np.mean(holes) if holes else 0:.2%}，异常要查）")
    if len(kept) < spec["frames"]:
        raise SystemExit(f"❌ 有效帧不足（{len(kept)} < 需要 {spec['frames']}）")

    aligned, astat = align_frames(kept, center=spec["center"])
    idx, wstat = pick_window(aligned, action, spec["frames"], spec)
    sheet = build_sheet([aligned[i] for i in idx], frame_h=frame_h)
    if sheet.width > 8000:
        raise SystemExit(f"❌ 精灵表宽 {sheet.width} 超 GPU 纹理上限，调小 --frame-h")
    dst = out_dir / f"{char}_{action}.webp"
    sheet.save(dst, "WEBP", quality=92, method=6)
    meta = {"char": char, "action": action, "frames": len(idx),
            "cell": [round(sheet.width / len(idx)), sheet.height],
            "sheet": [sheet.width, sheet.height], "fps": spec["fps"],
            "loop": bool(spec["loop"]), "draw": spec["draw"],
            "content_h_ratio": astat.get("content_h_ratio"),
            "baseline_ratio": astat.get("baseline_ratio"),
            "window": wstat, "align": astat,
            "h3": {"seconds": sec, "megapixels": megapixels, "steps": steps, "seed": seed}}
    (out_dir / f"{char}_{action}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    verdict = ""
    if wstat.get("mode") == "loop" and "score" in wstat:
        s = wstat["score"]
        verdict = ("CLEAN ✅" if s < 0.5 else "CHECK ⚠️（看一眼）" if s < 0.8
                   else "WEAK ❌（步幅超出窗：加长 --seconds 或重新生成）")
    print(f"[4/4] 选窗 {wstat}\n✅ {dst.name}  {sheet.width}x{sheet.height}  "
          f"{len(idx)} 帧 @{spec['fps']}fps  {verdict}")
    if not keep_work:
        for f in (work / "_frames").glob("*.png"):
            f.unlink()
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="立绘 → 全套动作精灵表（本地 H3）")
    ap.add_argument("--art", required=True, help="立绘（人物/怪物的设定图，白底或透明底）")
    ap.add_argument("--char", required=True, help="角色 id（决定输出文件名）")
    ap.add_argument("--actions", required=True,
                    help=f"要做的动作，逗号分隔，可选：{','.join(ACTIONS)}")
    ap.add_argument("--out", default="./_gen_actions")
    ap.add_argument("--style", default="clean stylized game character art",
                    help="画风描述（会拼进提示词，如 'clean papercraft cut-out style'）")
    ap.add_argument("--prompts", help="角色专属提示词覆盖（JSON：{action: {prompt/neg/tail}}）")
    ap.add_argument("--seconds", type=float, help="覆盖出片时长（循环动作建议 ≥2.0）")
    ap.add_argument("--megapixels", type=float, default=0.2)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--frame-h", type=int, default=300)
    ap.add_argument("--reuse-video", action="store_true", help="复用已有 clip.mp4，只重跑后半段")
    ap.add_argument("--keep-work", action="store_true", help="保留抽出的帧（排查用）")
    a = ap.parse_args()

    art = pathlib.Path(a.art)
    if not art.exists():
        raise SystemExit(f"❌ 立绘不存在：{art}")
    overrides = {}
    if a.prompts:
        overrides = json.loads(pathlib.Path(a.prompts).read_text(encoding="utf-8"))
    out_dir = pathlib.Path(a.out)

    done, fail = [], []
    for act in [x.strip() for x in a.actions.split(",") if x.strip()]:
        if act not in ACTIONS:
            print(f"  ⏭  未知动作 {act}（可选 {list(ACTIONS)}）")
            continue
        try:
            produce(a.char, act, art, out_dir, style=a.style,
                    prompts=overrides.get(act), seconds=a.seconds,
                    megapixels=a.megapixels, steps=a.steps, seed=a.seed,
                    frame_h=a.frame_h, reuse_video=a.reuse_video, keep_work=a.keep_work)
            done.append(act)
        except SystemExit as e:
            print(f"  ⏭  {act} 失败：{e}")
            fail.append(act)
    print(f"\n完成 {len(done)}/{len(done) + len(fail)}  ->  {out_dir}")
    if fail:
        print(f"   失败：{fail}（可加 --reuse-video 重跑后半段）")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
