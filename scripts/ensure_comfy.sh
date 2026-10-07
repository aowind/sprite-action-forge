#!/bin/bash
# 确保本地 ComfyUI（MiniMax H3 出片服务）在跑：**有则什么都不做，无则补起**。
#
# 🔴 绝不"先杀占端口的进程再起" —— 多会话/多任务环境下那等于踩掉别人正在跑的服务。
#
# 用法：
#   bash ensure_comfy.sh
#   COMFY_DIR=/path/to/ComfyUI COMFY_PORT=8188 bash ensure_comfy.sh
set -u
PORT="${COMFY_PORT:-8188}"

# 自动探测 ComfyUI 安装位置（不假设路径 —— 技能要能跨项目/跨机器用）
pick_comfy_dir() {
  if [ -n "${COMFY_DIR:-}" ]; then echo "$COMFY_DIR"; return; fi
  for d in "$HOME/ComfyUI" /d/ComfyUI /c/ComfyUI /opt/ComfyUI "$PWD/ComfyUI" \
           "$HOME/comfy/ComfyUI" "$HOME/Documents/ComfyUI"; do
    if [ -d "$d" ] && { [ -d "$d/.venv" ] || [ -d "$d/venv" ] || [ -f "$d/main.py" ]; }; then
      echo "$d"; return
    fi
  done
  echo ""
}
COMFY_DIR="$(pick_comfy_dir)"

if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/system_stats"; then
  echo "ComfyUI 已在跑（端口 $PORT）"
  exit 0
fi

if [ -z "$COMFY_DIR" ]; then
  echo "❌ 没找到 ComfyUI 安装目录（自动找过 ~/ComfyUI、/d/ComfyUI、/opt/ComfyUI 等）"
  echo "   用 COMFY_DIR=/你的/ComfyUI bash ensure_comfy.sh 指定"
  exit 1
fi
PY="$COMFY_DIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$COMFY_DIR/venv/bin/python"
[ -x "$PY" ] || PY="python"
if [ ! -x "$PY" ] && ! command -v python >/dev/null 2>&1; then
  echo "❌ 找不到 ComfyUI 的 python（试过 .venv/Scripts、venv/bin、系统 python）"
  exit 1
fi

echo "ComfyUI 未运行，补起…（8GB 显存用 --lowvram + 无损压显存节点）"
cd "$COMFY_DIR" || exit 1
rm -f _startup.log
( "$PY" main.py --lowvram --disable-mmap --port "$PORT" --disable-auto-launch \
    > _startup.log 2>&1 & )

for i in $(seq 1 60); do
  if curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/system_stats"; then
    echo "✅ ComfyUI 就绪（等待 $((i * 2))s）"
    exit 0
  fi
  sleep 2
done
echo "❌ 启动超时，看 $COMFY_DIR/_startup.log"
exit 1
