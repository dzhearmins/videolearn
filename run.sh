#!/usr/bin/env sh
# macOS / Linux 启动脚本（Windows 用 run.bat）
# 用法： ./run.sh           等价于 python -m videolearn
#        ./run.sh web       本机网页界面
#        ./run.sh doctor    环境自检
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi
exec "$PY" -m videolearn "$@"
