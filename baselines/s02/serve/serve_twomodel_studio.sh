#!/bin/bash
# Two-model arm on the M5 Ultra Studio: one llama-server instance per side, each on its own port.
# Same build and sampling as serve_gemma26b_llamacpp_studio.sh (llama.cpp b11399, temperature 1.0,
# top-k 64, top-p 0.95, thinking off with --reasoning-budget 0), 16,384 tokens of context per slot.
# TOPK overrides top-k for a model whose generation_config differs (Qwen3.8-Flash-Next: TOPK=20).
# Stops only the server already on this port (by its PID), never by pattern, so the other side
# keeps running.
# usage: serve_twomodel_studio.sh <port> <gguf path> <alias> <parallel slots>
set -u
TOPK="${TOPK:-64}"
PORT="${1:?port}"; GGUF="${2:?gguf}"; ALIAS="${3:?alias}"; NP="${4:?parallel slots}"
LOG="$HOME/cb-convert/llama-server-twomodel-${PORT}-$(date +%Y%m%d_%H%M%S).log"
OLD=$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN)
if [ -n "$OLD" ]; then kill "$OLD"; sleep 3; fi
nohup "$HOME/src/llama.cpp/build/bin/llama-server" -m "$GGUF" --alias "$ALIAS" \
  -ngl 99 -np "$NP" -c $((16384 * NP)) --jinja \
  --temp 1.0 --top-k "$TOPK" --top-p 0.95 --min-p 0 --reasoning-budget 0 \
  --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 < /dev/null &
echo "port $PORT -> $LOG"
