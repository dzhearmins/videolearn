#!/usr/bin/env bash
# macOS / Linux 启动脚本（Windows 用 run.bat）。默认启动本机网页界面。
#   ./run.sh                     网页界面（默认端口 8765）
#   ./run.sh --port 8123         网页界面，指定端口
#   ./run.sh --no-browser        网页界面，不自动开浏览器
#   ./run.sh web --port 8123     同上（显式子命令）
#   ./run.sh doctor              命令行：环境自检（login / courses / run 同理）
#   ./run.sh cli                 旧版交互式命令行
#   ./run.sh cli run --course 高等代数
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi

case "${1:-}" in
  "")
    exec "$PY" -m videolearn web
    ;;
  --cli|cli)
    shift
    exec "$PY" -m videolearn "$@"
    ;;
  -*)
    exec "$PY" -m videolearn web "$@"
    ;;
  web|login|courses|doctor|run)
    exec "$PY" -m videolearn "$@"
    ;;
  *)
    exec "$PY" -m videolearn "$@"
    ;;
esac
