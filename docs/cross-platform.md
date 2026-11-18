# macOS / Linux 兼容性说明

> 状态：代码已做跨平台分支；Windows 已实机验证，macOS/Linux 只做了静态检查（本机没有对应硬件）。
> 相关技能：`cross-platform-python-port-sweep`。

## 能不能跑

**能，前提是装好 Python 3.10+ 和 ffmpeg（含 ffprobe）。**
代码里没有 `msvcrt` / `winreg` / `os.startfile` / `SIGKILL` / `fcntl` 这类平台独占调用；
真正会挡住的只有三处，已全部改成"Windows 分支保留 + POSIX 分支新增"：

| 位置 | 原来 | 现在 |
|---|---|---|
| `media.find_ffmpeg()` | 只找 WinGet / `C:\ffmpeg` | PATH 优先，其次 `/opt/homebrew/bin`、`/usr/local/bin`、`/usr/bin`、`/snap/bin`；Windows 分支原样保留 |
| `streaming.py` / `stream_bench.py` 找 ffprobe | 硬编码 `with_name("ffprobe.exe")` | 新增 `media.find_ffprobe()`：先看 ffmpeg 同目录（按平台加 `.exe`），再退回 PATH；找不到时报"流式需要 ffprobe"而不是 `FileNotFoundError` |
| `pipeline.safe_name()` | 按字符截 80 | Windows 不变；POSIX 按 UTF-8 **字节**截到 160，避免 ext4 255 字节文件名上限（中文一字 3 字节） |

另外两处小改动：

- `videolearn/term.py`：`soften_console()` 把 stdout/stderr 的编码错误降级为 `replace`。
  POSIX 的 `LANG=C`（cron、容器、部分 SSH 默认环境）下打印中文不会再抛 `UnicodeEncodeError`。
  在 `cli.entry`、`web.main`、`stream_bench.main` 入口调用。
- `run.sh`：macOS/Linux 的启动脚本（Windows 继续用 `run.bat`）。

## 安装

### macOS

```bash
brew install ffmpeg python@3.12
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh doctor
```

### Debian / Ubuntu

```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh doctor
```

`./run.sh` 默认启动本机网页界面（与 `run.bat` 一致），参数原样透传：

```bash
./run.sh            # 本机网页界面（默认，http://127.0.0.1:8765）
./run.sh --port 8123  # 网页界面，指定端口
./run.sh doctor     # 环境自检，会打印 ffmpeg / ffprobe
./run.sh cli        # 旧版交互式命令行（等价于 python -m videolearn）
```

`run.bat` 用法完全相同；`run.bat cli run --course 高等代数` 可以带参数进入旧命令行。

首次执行如果提示 permission denied：`chmod +x run.sh`（Windows 上创建的文件没有可执行位）。

## 已知限制（不是代码问题，是依赖问题）

- **Python 版本**：`ctranslate2` 的 wheel 通常滞后于最新 CPython。
  本机 Windows 是 3.14 且可用，但 macOS/Linux 上如果 `pip install faster-whisper` 找不到 wheel，
  用 3.11/3.12 的虚拟环境最稳。
- **架构**：macOS 的 Apple Silicon、Linux 的 x86_64 都有 ctranslate2 轮子；
  Linux aarch64（树莓派等）不保证。
- **Apple Silicon 没有 Metal 加速**：本项目固定 `device="cpu", compute_type="int8"`，
  M 系列芯片能跑但不会用 GPU。想用 GPU 需要改 `asr.py` / `streaming.py` 里的设备参数（未做）。
- **ffmpeg 必须带 ffprobe**：Homebrew/apt 的 ffmpeg 包自带；手工编译的要注意。
- **文件名长度**：已按字节截断；如果项目目录本身路径很深，Linux 上仍可能触到 PATH_MAX，建议放在 `~/` 下。

## 已经静态核对过的点

- 没有顶层 `import msvcrt` / `win32*` / `fcntl` / `pwd` / `grp`。
- 没有 `shell=True` / `os.system` / `taskkill` / `os.startfile`。
- 所有文本读写都显式 `encoding="utf-8"`；二进制写入用 `"wb"`。
- 路径拼接全部走 `pathlib.Path`，没有 `os.sep` 或字符串拼反斜杠。
- `webbrowser.open` 自带平台分发（Windows `os.startfile` / macOS `open` / Linux `xdg-open`），无需自己写。
- `chmod(0o600)` 在 Windows 上失败已被 `except OSError` 吞掉，POSIX 上生效。

## 验证方式

```bash
python -m compileall -q videolearn        # 任意平台都应通过
python -m pytest -q                       # 51 个测试
./run.sh web                              # 打开 http://127.0.0.1:8765/?k=...
```

如果 `doctor` 里 ffmpeg 找到了但 ffprobe 没有，流式会失败、批量转写仍可用
（`--no-stream`）；把 ffprobe 补上即可。
