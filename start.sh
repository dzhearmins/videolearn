#!/usr/bin/env bash
# pku-videolearn 一键启动（macOS / Linux）。
#   - 在本仓库里运行：直接用当前源码
#   - 单独拷走运行：自动下载源码 tar.gz
#   - 没有 Python 3.10+：用 uv 装一个私有的 Python 3.12（不需要系统权限）
#   - 没有 FFmpeg：macOS 用 Homebrew/静态版，Linux 用 johnvansickle 静态版
#   - 最后启动本机网页界面，额外参数原样转发（如 ./start.sh --port 8123）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/pku-videolearn"
APP=""
UV=""
VENV_PY=""

say() { printf '%s\n' "$*"; }
fail() {
  say ""
  say "启动失败：$*"
  say "请检查网络/代理，或手动安装 Python 3.10+ 与 FFmpeg 后运行 ./run.sh。"
  exit 1
}

fetch() {
  local url="$1" out="$2"
  say "    $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --connect-timeout 20 --max-time 1800 -o "$out" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$out" "$url"
  else
    fail "需要 curl 或 wget"
  fi
}

say "============================================"
say " pku-videolearn - one-click bootstrap"
say "============================================"
say ""

# ---------------- 1. 源码 ----------------
if [[ -f "$HERE/videolearn/__main__.py" ]]; then
  APP="$HERE"
elif [[ -f "$DATA/app/videolearn/__main__.py" ]]; then
  APP="$DATA/app"
else
  say "[1/4] 下载源码 ..."
  mkdir -p "$DATA"
  fetch "https://github.com/dzhearmins/videolearn/archive/refs/heads/main.tar.gz" "$DATA/app.tar.gz" || fail "源码下载失败"
  rm -rf "$DATA/app" "$DATA/videolearn-main"
  tar -xzf "$DATA/app.tar.gz" -C "$DATA"
  [[ -d "$DATA/videolearn-main" ]] || fail "源码包解压失败"
  mv "$DATA/videolearn-main" "$DATA/app"
  APP="$DATA/app"
fi
say "    app: $APP"

# ---------------- 2. Python ----------------
find_python() {
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

if PY="$(find_python)"; then
  say "[2/4] Python: $("$PY" -V 2>&1)"
  if [[ -x "$DATA/venv/bin/python" ]] &&
     ! "$DATA/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    rm -rf "$DATA/venv"
  fi
  if [[ ! -x "$DATA/venv/bin/python" ]]; then
    say "    创建虚拟环境 ..."
    "$PY" -m venv "$DATA/venv" || fail "python3 -m venv 失败（Debian/Ubuntu 可能需要 sudo apt install python3-venv）"
  fi
  VENV_PY="$DATA/venv/bin/python"
else
  say "[2/4] 没有 Python 3.10+，用 uv 装一个私有 Python 3.12 ..."
  mkdir -p "$DATA/bin"
  if [[ ! -x "$DATA/bin/uv" ]]; then
    case "$(uname -s):$(uname -m)" in
      Linux:x86_64|Linux:amd64) asset="uv-x86_64-unknown-linux-gnu.tar.gz" ;;
      Linux:aarch64|Linux:arm64) asset="uv-aarch64-unknown-linux-gnu.tar.gz" ;;
      Darwin:arm64) asset="uv-aarch64-apple-darwin.tar.gz" ;;
      Darwin:x86_64) asset="uv-x86_64-apple-darwin.tar.gz" ;;
      *) fail "不支持的平台：$(uname -s) $(uname -m)" ;;
    esac
    fetch "https://github.com/astral-sh/uv/releases/latest/download/$asset" "$DATA/uv.tar.gz" || fail "uv 下载失败"
    tar -xzf "$DATA/uv.tar.gz" -C "$DATA/bin" || fail "uv 解压失败"
    found="$(find "$DATA/bin" -maxdepth 3 -type f -name uv | head -1)"
    [[ -n "$found" ]] || fail "找不到 uv 可执行文件"
    cp "$found" "$DATA/bin/uv"
    chmod +x "$DATA/bin/uv"
    UV="$DATA/bin/uv"
  else
    UV="$DATA/bin/uv"
  fi
  "$UV" venv --python 3.12 "$DATA/venv" || fail "uv 创建 Python 环境失败"
  VENV_PY="$DATA/venv/bin/python"
fi

# ---------------- 3. 依赖 ----------------
if [[ -z "$UV" ]] && command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
fi
if "$VENV_PY" -c 'import faster_whisper, requests, bs4, cryptography, dotenv, numpy' >/dev/null 2>&1; then
  say "[3/4] Python 依赖已安装"
else
  say "[3/4] 安装 Python 依赖（首次几分钟）..."
  INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  if [[ -n "$UV" ]]; then
    "$UV" pip install --python "$VENV_PY" -r "$APP/requirements.txt" --index-url "$INDEX" --timeout 60 ||
      "$UV" pip install --python "$VENV_PY" -r "$APP/requirements.txt" --timeout 60 ||
      fail "安装依赖失败"
  else
    if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
      "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 || fail "虚拟环境里没有 pip，也无法运行 ensurepip"
    fi
    "$VENV_PY" -m pip install --disable-pip-version-check --no-input -r "$APP/requirements.txt" -i "$INDEX" --timeout 60 ||
      "$VENV_PY" -m pip install --disable-pip-version-check --no-input -r "$APP/requirements.txt" --timeout 60 ||
      fail "安装依赖失败"
  fi
fi

# ---------------- 4. FFmpeg ----------------
say "[4/4] 检查 FFmpeg ..."
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  say "    已安装"
else
  mkdir -p "$DATA/bin"
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        say "    Homebrew 安装 ffmpeg ..."
        brew install ffmpeg || fail "brew install ffmpeg 失败"
      else
        say "    下载 macOS 静态版 ffmpeg / ffprobe ..."
        for b in ffmpeg ffprobe; do
          url="$("$VENV_PY" -c "import json,urllib.request;print(json.load(urllib.request.urlopen('https://evermeet.cx/ffmpeg/info/$b/release'))['download']['zip']['url'])")" ||
            fail "查询 evermeet 下载地址失败"
          fetch "$url" "$DATA/$b.zip" || fail "$b 下载失败"
          unzip -o -q "$DATA/$b.zip" -d "$DATA/bin" || fail "解压 $b 失败"
          chmod +x "$DATA/bin/$b"
          xattr -dr com.apple.quarantine "$DATA/bin/$b" 2>/dev/null || true
        done
        say "    注意：evermeet 只有 Intel 版，Apple Silicon 需要 Rosetta"
      fi
      ;;
    Linux)
      case "$(uname -m)" in
        x86_64|amd64) asset="ffmpeg-release-amd64-static.tar.xz" ;;
        aarch64|arm64) asset="ffmpeg-release-arm64-static.tar.xz" ;;
        *) fail "不支持的架构：$(uname -m)" ;;
      esac
      say "    下载 Linux 静态版 ffmpeg / ffprobe ..."
      fetch "https://johnvansickle.com/ffmpeg/releases/$asset" "$DATA/ffmpeg.tar.xz" || fail "ffmpeg 下载失败"
      tar -xJf "$DATA/ffmpeg.tar.xz" -C "$DATA" || fail "ffmpeg 解压失败"
      d="$(find "$DATA" -maxdepth 1 -type d -name 'ffmpeg-*-static' | head -1)"
      [[ -n "$d" ]] || fail "找不到 ffmpeg 解压目录"
      cp "$d/ffmpeg" "$d/ffprobe" "$DATA/bin/" || fail "复制 ffmpeg 失败"
      chmod +x "$DATA/bin/ffmpeg" "$DATA/bin/ffprobe"
      ;;
    *)
      fail "不支持的平台：$(uname -s)"
      ;;
  esac
  export PATH="$DATA/bin:$PATH"
fi

# ---------------- 5. 启动 ----------------
say ""
say "启动网页界面（Ctrl+C 停止）..."
cd "$APP"
exec "$VENV_PY" -m videolearn web "$@"
