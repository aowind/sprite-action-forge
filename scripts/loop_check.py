# -*- coding: utf-8 -*-
"""loop_check.py —— 2D 动作帧序列「循环质量」量化判据（偷师 scenario-sprite-animation）。

对一组按序命名的帧图（PNG/WebP，带或不带 alpha）自动：
  1. 内容裁切归一：按所有帧的联合包围盒裁切再比较（整幅大图上缩会把人物动作稀释掉）
  2. 找最优循环窗 [s, s+P)：接缝差 seam vs 窗内帧差均值 baseline，score = seam/baseline
  3. 尺寸漂移检测：循环窗内人物高度中位数 vs 首帧，超 [0.88, 1.12] 报警
  4. 静止/非序列闸门：整体运动量低于阈值直接判「不是动作序列」，不硬编循环窗
  5.（可选 --gif）按最优窗输出循环预览 GIF；（可选 --emit）把最优窗导成重编号序列

判读标准（来自 scenario 实测）：
  score < 0.5          干净，直接用
  0.5 ~ 0.8            看 GIF 再定
  > 0.8（walk/run）    步幅超出搜索窗，要么扩 --pmax 重搜，要么重生成

用法：
  python tools/loop_check.py _gen_idle_walk/frame_%d.png --count 10
  python tools/loop_check.py _gen_idle_walk/combo_%d.png --count 10 --gif out.gif
  python tools/loop_check.py _gen_idle_walk/ --emit _out/loop7      # 目录模式自然排序
  python tools/loop_check.py frames/ --cycle idle --crop none       # 关掉内容裁切

算法出处：scenario-labs/skills cycle_sheets.py 的 loop_window()（MIT）。保留其两个关键设计：
  - 比较用「预乘 alpha 的小图」：alpha 差加权 x80（轮廓漂移比颜色漂移更伤循环）
  - baseline 用「窗内相邻帧差均值」而非全序列：循环好不好只跟窗内自己比
相对原版的两处改动：内容裁切归一（原版靠固定 960 方画布 + 人物固定占 58%，我们没这保证）；
静止闸门（原版不检查，对「参考图对」这类非序列输入也照样返回一个窗）。
"""
import argparse
import glob
import os
import re
import shutil
import sys

import numpy as np
from PIL import Image

# ---------- 参数 ----------

SMALL = 120          # 比较用缩略图最长边
ALPHA_W = 80         # alpha 差加权：轮廓漂移 >> 颜色漂移
BASE_MIN = 0.35      # 低于此 baseline 视为「窗内没在动」
SEAM_CLEAN = 0.5     # score 判读线
SEAM_WARN = 0.8
DRIFT_OK = (0.88, 1.12)   # 人物高度漂移容忍区间

MIN_MOTION = 2.0     # 整体帧差均值低于此值 -> 判定不是动作序列
SUBTLE_MOTION = 8.0  # 低于此值 -> 动作很轻（idle 级），循环判读仅供参考

# 循环周期搜索范围（帧数）。scenario 按 24fps 视频调；我们是直接出帧，
# 一帧一步，周期≈帧数，默认覆盖 walk 8 / run 6-8 / idle 4-8。
RANGES = {"walk": (4, 9), "run": (4, 9), "idle": (3, 8), "attack": None}


# ---------- 载入与归一 ----------

def content_box(paths, alpha_thr=128):
    """所有帧的联合内容包围盒。有 alpha 用 alpha；全不透明则用「与边框底色不同」。"""
    boxes = []
    opaque = True
    for p in paths:
        al = np.array(Image.open(p).convert("RGBA"))[..., 3]
        if (al < alpha_thr).mean() > 0.02:
            opaque = False
            ys, xs = np.where(al >= alpha_thr)
            boxes.append((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    if not opaque:
        return (int(min(b[0] for b in boxes)), int(min(b[1] for b in boxes)),
                int(max(b[2] for b in boxes)), int(max(b[3] for b in boxes)))
    # 不透明背景：边框颜色中位数，全序列差值并集
    mask = None
    for p in paths:
        rgb = np.array(Image.open(p).convert("RGB")).astype(np.int16)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(border, 0)
        spread = np.abs(border - bg).max(1)
        tol = max(40, int(np.percentile(spread, 90)) + 25)
        m = np.abs(rgb - bg).max(-1) > tol
        mask = m if mask is None else (mask | m)
    ys, xs = np.where(mask)
    return None if not len(ys) else (int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1))


def load_small(paths, box=None):
    """读帧 -> 预乘 alpha 的小图 (rgb*a, a/255)。box 给定先裁切，再等比缩到最长边 SMALL。"""
    out = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        if box:
            im = im.crop(box)
        k = SMALL / max(im.width, im.height)
        if k < 1:
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BOX)
        a = np.array(im).astype(np.float32)
        out.append((a[..., :3] * (a[..., 3:] / 255), a[..., 3] / 255))
    return out


def dist(a, b):
    """两帧差异：RGB 均值绝对差 + alpha 差 x ALPHA_W。"""
    return float(np.abs(a[0] - b[0]).mean() + ALPHA_W * np.abs(a[1] - b[1]).mean())


def motion_parts(sm):
    """(rgb 项均值, alpha 项均值)——分开报，好看出动作信号到底在哪。
    实测：combo 类「参考图对」的全部动作信号只在 alpha 里（RGB 逐像素相同），
    alpha 项 x80 的加权正是为这种情况准备的。"""
    rgb = float(np.mean([np.abs(sm[i][0] - sm[i + 1][0]).mean() for i in range(len(sm) - 1)]))
    alp = float(np.mean([np.abs(sm[i][1] - sm[i + 1][1]).mean() for i in range(len(sm) - 1)]))
    return rgb, alp * ALPHA_W


# ---------- 核心扫窗 ----------

def loop_window(sm, pmin, pmax, first=1, min_base=BASE_MIN):
    """最紧循环窗 [s, s+P)：返回 (score, s, P, seam, baseline) 或 None。"""
    n = len(sm)
    step = [dist(sm[i], sm[i + 1]) for i in range(n - 1)]
    best = None
    for P in range(pmin, pmax + 1):
        for s in range(first, n - P):
            seam = dist(sm[s], sm[s + P])
            base = float(np.mean(step[s:s + P]))
            if base < min_base:
                continue
            score = seam / base
            if best is None or score < best[0] - 1e-9:
                best = (score, s, P, seam, base)
    return best


def fig_heights(paths):
    """每帧人物（非透明像素）高度，原生像素。"""
    out = []
    for p in paths:
        al = np.array(Image.open(p).convert("RGBA"))[..., 3]
        ys = np.where(al.any(1))[0]
        out.append(float(ys.max() - ys.min() + 1) if len(ys) else 0.0)
    return out


# ---------- 输入展开 ----------

def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def expand_paths(target, count=None, start=1):
    """%d 模式 + --count 展开（起始编号由 --start 决定，视频抽帧常是 0 基）；
    否则目录/通配符按名称自然排序（frame_2 在 frame_10 前）。"""
    if "%d" in target:
        if not count:
            sys.exit("--count required when pattern contains %d")
        return [target % i for i in range(start, start + count)]
    if os.path.isdir(target):
        files = glob.glob(os.path.join(target, "*.png")) + glob.glob(os.path.join(target, "*.webp"))
    else:
        files = glob.glob(target)
    files = sorted(files, key=natural_key)
    if count:
        files = files[:count]
    if not files:
        sys.exit(f"no frames found for {target}")
    return files


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="2D 动作帧序列循环质量检测")
    ap.add_argument("target", help="帧序列：'dir/frame_%%d.png' + --count，或目录/通配符")
    ap.add_argument("--count", type=int, help="配合 %%d 模式的帧数")
    ap.add_argument("--start", type=int, default=1,
                    help="配合 %%d 模式的起始编号（视频抽帧常是 0 基，用 --start 0）")
    ap.add_argument("--cycle", default="walk", choices=list(RANGES),
                    help="动作类型，决定周期搜索窗（默认 walk；attack 不循环只做检测）")
    ap.add_argument("--crop", default="auto", choices=["auto", "none"],
                    help="auto=按联合内容包围盒裁切再比较（默认）")
    ap.add_argument("--gif", metavar="OUT.gif", help="按最优循环窗输出预览 GIF")
    ap.add_argument("--emit", metavar="DIR", help="把最优窗导出为 DIR/01.png..（丢掉接缝重复帧）")
    ap.add_argument("--fps", type=int, default=10, help="GIF 帧率（默认 10，须整除 100）")
    ap.add_argument("--pmin", type=int, help="覆盖周期搜索下界")
    ap.add_argument("--pmax", type=int, help="覆盖周期搜索上界")
    ap.add_argument("--min-motion", type=float, default=MIN_MOTION,
                    help=f"帧差均值低于此值判为非动作序列（默认 {MIN_MOTION}）")
    a = ap.parse_args()

    paths = expand_paths(a.target, a.count, a.start)
    n = len(paths)
    print(f"{n} frames: {os.path.basename(paths[0])} .. {os.path.basename(paths[-1])}")
    if n < 5:
        sys.exit(f"need at least 5 frames for a loop check, got {n}")

    box = content_box(paths) if a.crop == "auto" else None
    sm = load_small(paths, box)
    if box:
        w0, h0 = Image.open(paths[0]).size
        print(f"content box: {box} -> crop {box[2]-box[0]}x{box[3]-box[1]} of {w0}x{h0}")

    # 帧差曲线
    step_all = [dist(sm[i], sm[i + 1]) for i in range(n - 1)]
    motion = float(np.mean(step_all))
    rgb_m, alp_m = motion_parts(sm)
    print("frame-to-frame deltas:", " ".join(f"{d:.1f}" for d in step_all))
    print(f"motion level     : {motion:.2f}  (= rgb {rgb_m:.2f} + alpha*{ALPHA_W} {alp_m:.2f})")

    # 静止闸门：不是动作序列就别硬编循环窗
    if motion < a.min_motion:
        print("=" * 60)
        print(f"NOT AN ANIMATION SEQUENCE: motion {motion:.2f} < {a.min_motion}.")
        print("这些帧几乎没有差异 —— 可能是参考图/拼图输入，或生成时角色没动。")
        print("先确认输入是「同一动作的连续帧」；确实是轻微动作的话用 --min-motion 0.5 强制分析。")
        return
    if motion < SUBTLE_MOTION:
        print(f"[note] subtle motion ({motion:.2f}); loop verdicts are unreliable at this level,"
              " verify with --gif")

    # 循环窗
    rng = RANGES[a.cycle]
    best = None
    if rng is not None:
        if a.pmin or a.pmax:
            rng = (a.pmin or rng[0], a.pmax or rng[1])
        min_base = 0.05 if a.cycle == "idle" else BASE_MIN
        best = loop_window(sm, *rng, min_base=min_base)
        if best is None:
            best = loop_window(sm, *rng, min_base=0)
            if best:
                print("[note] window motion below floor, relaxed; verify with --gif")

    # 尺寸漂移
    hs = fig_heights(paths)

    print("=" * 60)
    if best:
        score, s, P, seam, base = best
        export = list(range(s, s + P))          # 尾帧≈首帧，导出丢掉接缝那一帧
        verdict = ("CLEAN  (<0.5)" if score < SEAM_CLEAN else
                   "CHECK  (0.5-0.8, eyeball the GIF)" if score < SEAM_WARN else
                   f"WEAK   (>{SEAM_WARN}: stride exceeds window; widen --pmax or regenerate)")
        print(f"best loop window : frames {s+1}..{s+P+1}  (period {P})")
        print(f"score seam/baseline = {score:.3f}   seam={seam:.2f} baseline={base:.2f}")
        print(f"verdict          : {verdict}")
        print(f"export frames    : {os.path.basename(paths[export[0]])} .. "
              f"{os.path.basename(paths[export[-1]])}  ({len(export)} frames, "
              f"drop seam duplicate {os.path.basename(paths[s+P])})")
        if hs[0] > 0:
            win_h = [hs[i] for i in range(s, s + P + 1) if hs[i] > 0]
            drift = float(np.median(win_h)) / hs[0]
            ok = DRIFT_OK[0] <= drift <= DRIFT_OK[1]
            print(f"size vs first    : {drift:.2f}x   "
                  f"{'OK' if ok else 'DRIFT: figure moved toward/away from camera, regenerate'}")
    else:
        print("no loop window found (clip too short, or not a looping cycle)")
    if hs[0] == 0:
        print("[warn] first frame has no opaque pixels?")

    # 导出
    if a.emit:
        idx = export if best else list(range(n))
        os.makedirs(a.emit, exist_ok=True)
        for k, i in enumerate(idx, 1):
            ext = os.path.splitext(paths[i])[1]
            shutil.copyfile(paths[i], os.path.join(a.emit, f"{k:02d}{ext}"))
        print(f"emitted          : {a.emit}/  ({len(idx)} frames, renumbered from 01)")

    # GIF 预览
    if a.gif:
        idx = export if best else list(range(n))
        seq = [Image.open(paths[i]).convert("RGBA") for i in idx]
        w = max(im.width for im in seq); h = max(im.height for im in seq)
        frames_out = []
        for im in seq:
            c = Image.new("RGBA", (w, h), (40, 42, 54, 255))
            c.alpha_composite(im, ((w - im.width) // 2, (h - im.height) // 2))
            if h > 512:                       # 大图缩到 512 高，预览别几十兆
                k = 512 / h
                c = c.resize((max(1, round(w * k)), 512), Image.LANCZOS)
            elif h < 512:                     # 小精灵整数倍放大，保持像素感
                z = max(1, 512 // h)
                c = c.resize((w * z, h * z), Image.NEAREST)
            frames_out.append(c.convert("RGB"))
        if 100 % a.fps != 0:
            print(f"[warn] fps={a.fps} not a divisor of 100; GIF will be re-timed on the 10ms grid")
        frames_out[0].save(a.gif, save_all=True, append_images=frames_out[1:],
                           duration=round(1000 / a.fps), loop=0)
        print(f"preview GIF      : {a.gif}  ({len(frames_out)} frames @ {a.fps}fps)")


if __name__ == "__main__":
    main()
