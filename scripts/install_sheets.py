# -*- coding: utf-8 -*-
"""把生成好的精灵表装进 web 目录，并生成清单 `actions.json`。

    python install_sheets.py --src ./_gen_actions --dest ./public/assets/actions
    python install_sheets.py --src ./_gen_actions --dest ./public/assets/actions \
        --fx-src ./_gen_actions/fx --fx-map '{"attack":"slash","skill":"burst"}'

三条铁律（都是踩过坑才加的）：

1. **覆盖前强制备份**。很多项目**不是 git 仓库**，覆盖素材不可逆 ——
   踩过一次：抽帧脚本直接覆盖了在用的跑动表（23 帧 → 10 帧），而游戏代码里还写着 23，
   跑动直接坏掉，只能靠别处的同名副本手动捞回来。
   备份到 `<src>/_backup_public/<时间戳>__<文件名>`，**别删**，它是"选哪一版"的依据。

2. **比例以实际素材为准，不信 meta**。meta 是生成时的测量值，而素材会被重生成/回退。
   两者一旦不一致，游戏就会按错误的尺寸与基线画（人物突然变大变小、脚悬空）。
   `measure_sheet()` 直接从**最终要装进去的 webp** 量。

3. **帧数/帧率写进清单，不在游戏代码里手写**。
   "帧数写在代码里"是素材替换后的定时炸弹（改了素材忘了改常量 = 人物被压成一条缝）。

命名约定（务必在生产时就按这个给 `--char` 起名，装配零配置）：
    hero  /  mob_boar  /  mob_bandit  …  →  hero_idle.webp / mob_boar_run.webp
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import time

import numpy as np
from PIL import Image

BACKUP_KEEP = 40          # 每类素材最多留几个历史备份


def measure_sheet(path: pathlib.Path, frames: int):
    """从最终素材量出 (人物高占比, 脚底线占比)。"""
    im = Image.open(path).convert("RGBA")
    n = max(1, frames)
    cellw = im.width / n
    h = im.height
    a = np.array(im)
    hs, bs = [], []
    for i in range(n):
        cell = a[:, int(round(i * cellw)):int(round((i + 1) * cellw))]
        al = cell[..., 3]
        ys, _xs = np.where(al > 8)
        if not len(ys):
            continue
        hs.append(ys.max() - ys.min() + 1)
        bs.append(ys.max() + 1)
    if not hs:
        return None
    return round(float(np.median(hs)) / h, 4), round(float(np.median(bs)) / h, 4)


def _backup(dst: pathlib.Path, backup_root: pathlib.Path, stamp: str) -> bool:
    if not dst.exists():
        return False
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dst, backup_root / f"{stamp}__{dst.name}")
    return True


def _prune(backup_root: pathlib.Path) -> None:
    """同一素材的历史备份只留最近几份，别让目录无限膨胀。"""
    if not backup_root.exists():
        return
    groups: dict[str, list[pathlib.Path]] = {}
    for p in backup_root.glob("*__*"):
        groups.setdefault(p.name.split("__", 1)[1], []).append(p)
    for files in groups.values():
        for p in sorted(files)[:-BACKUP_KEEP]:
            p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="装配精灵表 + 生成 actions.json")
    ap.add_argument("--src", required=True, help="生产脚本的输出目录（含 <char>_<action>.json/webp）")
    ap.add_argument("--dest", required=True, help="web 目录（游戏按相对路径加载）")
    ap.add_argument("--fx-src", help="特效精灵目录（fx_forge.py 的输出）")
    ap.add_argument("--fx-map", default='{}', help='动作→特效，如 \'{"attack":"slash"}\'')
    ap.add_argument("--move", default='{}', help='角色→移动动作，如 \'{"mob_bandit":"walk"}\'')
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    src, dest = pathlib.Path(a.src), pathlib.Path(a.dest)
    backup_root = src / "_backup_public"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fx_map, move_map = json.loads(a.fx_map), json.loads(a.move)

    metas = sorted(p for p in src.glob("*.json") if p.name not in ("actions.json",))
    if not metas:
        raise SystemExit(f"❌ {src} 里没有 <char>_<action>.json，先跑 sprite_forge.py")

    manifest: dict = {"version": 1, "generated": time.strftime("%F %T"), "chars": {}}
    installed, backed = [], []

    for mp in metas:
        meta = json.loads(mp.read_text(encoding="utf-8"))
        char, action = meta["char"], meta["action"]
        webp = src / f"{char}_{action}.webp"
        if not webp.exists():
            print(f"  ⏭  {mp.name} 没有对应 webp")
            continue
        name = f"{char}_{action}.webp"
        dst = dest / name
        if not a.dry_run:
            if _backup(dst, backup_root, stamp):
                backed.append(name)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(webp, dst)
        meas = None if a.dry_run else measure_sheet(dst, meta["frames"])
        entry = manifest["chars"].setdefault(
            char, {"move": move_map.get(char, "run"), "actions": {}})
        entry["actions"][action] = {
            "file": name, "frames": meta["frames"], "fps": meta["fps"],
            "loop": meta.get("loop", False), "draw": meta.get("draw", 1.0),
            "content_h_ratio": (meas[0] if meas else meta.get("content_h_ratio")),
            "baseline_ratio": (meas[1] if meas else meta.get("baseline_ratio")),
        }
        installed.append(f"{name} ({meta['frames']}帧 @{meta['fps']}fps)")

    # ---- 特效 ----
    fx_installed = []
    if a.fx_src:
        fxsrc = pathlib.Path(a.fx_src)
        fx_block = {}
        for jp in sorted(fxsrc.glob("*.json")):
            m = json.loads(jp.read_text(encoding="utf-8"))
            name = m["fx"]
            wp = fxsrc / f"{name}.webp"
            if not wp.exists():
                continue
            out_name = f"fx_{name}.webp"
            if not a.dry_run:
                _backup(dest / out_name, backup_root, stamp)
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wp, dest / out_name)
            fx_installed.append(f"{out_name} ({m['frames']}帧 @{m['fps']}fps)")
            fx_block[name] = {"file": out_name, "frames": m["frames"], "fps": m["fps"],
                              "offset_x": m.get("offset_x", 0.6),
                              "offset_y": m.get("offset_y", -0.5),
                              "scale": m.get("scale", 1.2),
                              "delay_ratio": m.get("delay_ratio", 0.0)}
        if fx_block:
            manifest["fx"] = fx_block
            manifest["fxMap"] = {k: v for k, v in fx_map.items() if v in fx_block}

    if not a.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "actions.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        _prune(backup_root)

    print(f"\n{'（dry-run）' if a.dry_run else ''}装了 {len(installed)} 张动作表 -> {dest}")
    for s in installed:
        print("   ", s)
    if fx_installed:
        print(f"   特效 {len(fx_installed)} 条：" + "、".join(fx_installed))
        print(f"   动作→特效 {manifest.get('fxMap', {})}")
    if backed:
        print(f"\n覆盖前已备份 {len(backed)} 个原文件 -> {backup_root}/{stamp}__*")
    print(f"\n清单 -> {dest / 'actions.json'}")
    for k, v in manifest["chars"].items():
        print(f"   {k:14s} move={v['move']:5s} 动作={list(v['actions'].keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
