# pku-videolearn 0.1.0

北大教学网（course.pku.edu.cn）课程回放 → 文字稿 → AI 重述。本地运行、自带网页界面，可选同班共享总结。

> 只处理你本人有权限访问的课程；不绕过 DRM；不分享、不传播下载的视频内容。

## 它做什么

```
学号+密码 ──IAAA OAuth──> 教学网 SSO Cookie
                              │
                    videoList.action（课程回放列表）
                              │
                    playVideo.action?token=...
                              │
                    m3u8（标准 AES-128 或明文）／直链 mp4
                              │
            ffmpeg 解码 → faster-whisper 分块转写 → transcript.md
                              │ 并发
                    DeepSeek 流式初稿 → stream_restated.md
                              │
            （默认）眼睛：思考 → 批量截图 → 读板书/PPT → eyes_analysis
                              │
            final_pass：完整转写 + 初稿 + 视觉结论 → retell.md（唯一终稿）
```

- **网页界面**：浏览器里选课、看实时初稿、看最终结果、清理视频存储。
- **命令行**：`login` / `courses` / `doctor` / `run`，适合脚本化或排障。
- **共享总结（可选）**：同班有人跑过同一节课时，直接复制结果，不下载视频、不跑 ASR；
  你跑完的总结会自动上传，同一节课只保留最新一版。

## 快速开始

全新环境先完成[安装](#安装)：clone 仓库、装 Python 依赖、装 FFmpeg，然后回到项目目录运行。

### Windows

```bat
run.bat
```

### macOS / Linux

```bash
chmod +x run.sh   # 首次执行需要
./run.sh
```

默认启动本机网页界面，浏览器会打开 `http://127.0.0.1:8765/?k=...`（终端里也会打印）。
这个链接带一次性访问令牌，不要发给别人。

一点环境都没有？Windows 双击 `start.bat`，macOS/Linux 运行 `./start.sh`（首次 `chmod +x start.sh`）：
它们会自动下载 Python 3.12、FFmpeg（脚本单独拷走时连源码一起下载），然后启动网页界面。

首次使用：点右上角「设置」，填学号、密码、DeepSeek API Key，保存即写入 `.env`；
也可以直接复制 `.env.example` 为 `.env` 手动填写。

## 安装

需要 Python 3.10+ 和 FFmpeg（含 `ffprobe`）。

### 1. 获取代码

```bash
git clone https://github.com/dzhearmins/videolearn.git
cd videolearn
```

没装 git 的话，在仓库页面点 **Code → Download ZIP**，解压后进入目录即可。

### 2. 安装 Python 依赖

```bash
python -m pip install -r requirements.txt
```

### 3. 安装 FFmpeg

FFmpeg **必需**（解码音频、抽帧、TS 转封装），并且要带 `ffprobe`：

| 平台 | 安装 |
|---|---|
| Windows | `winget install --id Gyan.FFmpeg -e --source winget` |
| macOS | `brew install ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |

也可以用 `pip install -e .` 安装出 `videolearn` 和 `videolearn-web` 两个命令；
直接 `python -m videolearn ...` 不需要安装。

## 配置

配置来自项目根目录的 `.env`（已 gitignore），设置页保存时会写这个文件。

| 变量 | 必填 | 说明 |
|---|---|---|
| `PKU_STUDENT_ID` | 是 | 学号 |
| `PKU_PASSWORD` | 是 | 密码（只用于 IAAA 登录，不写日志） |
| `DEEPSEEK_API_KEY` | 建议 | 任意 OpenAI 兼容端点的 key；不填时只导出 `prompt.md` |
| `LLM_BASE_URL` | 否 | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` | 否 | 默认 `deepseek-chat` |
| `VISION_MODEL` | 否 | 眼睛用的视觉模型，默认同 `LLM_MODEL`，必须支持图片 |
| `VIDEOLRN_WHISPER_MODEL` | 否 | `tiny/base/small/medium/large-v3`，默认 `small` |
| `VIDEOLRN_ASR_PROMPT` | 否 | 本课术语，帮助 ASR 断句（不会让模型编内容） |
| `VIDEOLRN_MAX_INPUT_TOKENS` | 否 | 单次 LLM 输入预算，默认 60000（按 1 token/字符保守估算） |
| `VIDEOLRN_AUTO_CLEAN_VIDEO` | 否 | `1` 时任务完成后自动删除视频，默认 `0` |
| `SHARE_SERVER` / `SHARE_TOKEN` / `SHARE_NAME` | 否 | 共享服务地址 / 设备令牌 / 昵称，注册后自动写入 |
| `HF_ENDPOINT` | 否 | 国内网络建议 `https://hf-mirror.com`，加速模型下载 |
| `VIDEOLRN_DEBUG` | 否 | `1` 时打印更多排障信息 |

运行时状态：Cookie 缓存在 `.state/cookies.json`（首次登录后自动复用，失效自动重登）；
所有产物写在 `output/`。`.env`、`.state/`、`output/` 都不进 git。

## 网页界面

启动后：

1. 左侧选课程 → 选回放 → 「分析回放」（可勾选「眼睛」「流式初稿」「只跑前 600 秒」）。
2. 任务页边跑边看：流式初稿、边转写文字稿、眼睛截图、最终结果（重述 / 分段笔记 / 完整文字稿）
   可预览和下载，底部是运行日志。
3. 首页「视频存储」列出 `output/` 下所有已下载视频（体积、时间、是否正在使用），可逐个删除；
   设置里可以打开「任务完成后自动删除视频」（默认关闭，只删视频，文字稿/截图都保留）。
4. 设置页还能改模型、填共享服务。服务只监听 `127.0.0.1`，所有接口要求令牌。

## 命令行

在项目根目录执行（`run.bat` / `run.sh` 会自动切到项目根目录）：

```text
usage: videolearn [-h] [--model MODEL] {login,courses,doctor,web,run} ...

  login      强制重新登录（刷新 .state/cookies.json）
  courses    列出可选课程
  doctor     检查环境与配置（不输出任何密钥）
  web        本机网页界面（浏览器里选课、跑转写、看重述）
  run        交互式选择课程/回放并生成文字稿与重述
```

`run` 的常用参数（完整见 `python -m videolearn run --help`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--course 关键词` | — | 按课程名跳过课程选择 |
| `--url URL` `--title` `--time` | — | 直接给某一节的 playVideo 链接（token 很短，需现取） |
| `--limit-seconds N` / `--start-seconds N` | — | 只取某一段做小样，先验证效果 |
| `--eyes` / `--no-eyes` | 开启 | 是否让模型看板书/PPT 截图 |
| `--max-shots N` / `--max-steps N` | 8 / 4 | 眼睛最多截图数（硬上限 20）/ 最多思考轮数（硬上限 8） |
| `--stream` / `--no-stream` | 开启 | 流式 ASR+初稿；关闭则批量转写 |
| `--chunk-seconds` / `--overlap-seconds` / `--asr-threads` | 240 / 5 / 8 | 流式分块、重叠与线程数 |
| `--no-video` | — | 完成后删除视频，只留文字稿/截图 |
| `--reuse-eyes` | — | 复用同目录已有的眼睛分析，不再调 LLM |

示例：

```bash
python -m videolearn doctor                 # 环境自检，不输出密钥
python -m videolearn login                  # 先验证登录
python -m videolearn courses                # 列课程
python -m videolearn run                    # 交互式跑一节
python -m videolearn run --course 高等代数    # 跳过课程选择
python -m videolearn run --course 高等代数 --start-seconds 600 --limit-seconds 90
python -m videolearn run --course 高等代数 --no-video
python -m videolearn web --port 8123 --no-browser
```

`run.bat` / `./run.sh` 默认就是 `web`；要进旧命令行用 `run.bat cli` 或 `./run.sh cli`，
例如 `./run.sh cli run --course 高等代数`。子命令（`doctor` / `courses` / `login` / `run`）可以直接透传。

## 共享总结（可选）

**加入**：设置 → 共享总结 → 填服务地址、邀请码、昵称 → 「注册 / 更新共享」。

- 注册后，回放列表里别人跑过的课会显示 `已有 · 同学A · 日期`，点「用共享」直接复制结果
  （不下载视频、不跑 ASR，几秒完成）。
- 你自己跑完的总结会**自动上传**，不需要点任何按钮；同一节课只保留**最新一版**，
  换更强模型重跑会覆盖旧版（方便后续升级后拿到更高质量的结果）。
- 小样（`--limit-seconds`）和从共享复制来的结果不会上传。
- 服务端只存文字和截图，不存视频/音频，也接触不到你的学号密码；设备令牌可随时撤销。

示例部署：`http://39.97.231.204/vl`（邀请码向发起人要）。

**自建服务**（可选，Python 标准库，无额外依赖）：

```bash
python -m share_server serve --host 127.0.0.1 --port 8787   # 默认数据目录 ~/videolearn-share/data
python -m share_server invite --count 3                     # 生成一次性邀请码
python -m share_server devices                              # 查看设备
python -m share_server revoke 3                             # 撤销某个设备
python -m share_server stats                                # 条目数/占用
```

用 nginx/Caddy 反代到公网时建议加 HTTPS；服务默认只监听回环地址。

## 输出

```
output/
└─ 课程名/
   └─ 2026-11-03_101000_课堂回放/
      ├─ video.mp4                    # 回放（转封装失败时为 .ts；--no-video / 自动清理后删除）
      ├─ transcript.md                # 带 [HH:MM:SS] 的完整文字稿
      ├─ transcript.srt / .txt        # 字幕 / 纯文本
      ├─ transcript.partial.md        # 流式阶段边产边写
      ├─ stream_events.jsonl          # 每块 ASR/LLM 计时
      ├─ stream_metrics.json          # 首正文/流式/final_pass/端到端
      ├─ stream_draft.superseded.md   # 流式初稿（仅审计，已被终稿取代）
      ├─ retell.md                    # 最终重述（含可选的板书截图）
      ├─ notes_map.md                 # 分段精读笔记
      ├─ frames/                      # 眼睛截图（HH-MM-SS.jpg）
      ├─ eyes_analysis.json / .md     # 分步思考/取帧/观察/结论
      ├─ shots.json / visual_notes.md # 每张图的来源与视觉笔记
      ├─ context_report.json          # 分块/截断/调用体积/超限重试
      └─ meta.json
```

没有配置 API Key 时不会生成 `retell.md`，而是生成 `prompt.md`，可粘贴到任意网页版大模型。

## 平台支持

Windows 已实机验证；macOS / Linux 代码已做平台分支（ffmpeg/ffprobe 查找、文件名按字节截断、
`LANG=C` 下中文输出不崩），但没有实机跑过。细节和安装命令见
[docs/cross-platform.md](docs/cross-platform.md)。

- Python 3.10+；Windows 上 3.14 可用。macOS/Linux 若 `ctranslate2` 没有对应 wheel，
  用 3.11/3.12 的虚拟环境最稳。
- Apple Silicon 没有 Metal 加速：固定 CPU + int8，能跑但不用 GPU。
- FFmpeg 必须带 `ffprobe`，否则流式转写会失败（批量转写仍可用，加 `--no-stream`）。

## 安全与合规

- 支持标准 HLS AES-128（key 由同一账号会话取回，按分片解密）；**不处理** SAMPLE-AES / Widevine / 任何 DRM。
- 只使用你自己的账号、只下载你有权限的课程，供个人复习；视频画面带有“未经授权不得翻录转载”提示，请勿再分发。
- 密码只用于 IAAA 登录（RSA PKCS#1 v1.5 加密后提交），日志不输出密码、token、Cookie、`auth_data`。
- 网页界面只监听 `127.0.0.1`；共享服务只接收文字/截图，设备令牌可撤销。

## 已知限制

- **CPU 转写慢**：`small` 在一台 i7-13700H 笔记本上约 3–5× 实时（含模型加载），2 小时课约 25–40 分钟；
  想更快用 `--limit-seconds` 先验证，或换 `base`。
- 数学公式是 ASR 的弱项，转写可能失真；`retell.md` 会标注存疑处，请对照视频核对。
- 眼睛受截图分辨率与视觉模型能力限制，小字、连笔、遮挡都可能读错；重要公式请点开原图核对。
- 一次批量看图依赖端点支持多图；不同模型支持数量不同。
- 流式 ASR 用 `beam_size=1` 且关闭 thinking，转写质量可能略低于批量；final_pass 会额外耗时。
- 超长课（单次输入超预算）会分层摘要或截断，建议用 `--start-seconds/--limit-seconds` 分段跑。
- playVideo 的 JWT 由教学网短期签发，示例链接会过期；以 `courses` / 网页列表里的实时链接为准。

## 故障排查

| 现象 | 处理 |
|---|---|
| IAAA 登录失败 `[E21]` | 尝试次数过多，等 30 分钟；不要反复试密码 |
| “回放 token 已失效” | 回列表重新取链接，token 很短 |
| `retell.md` 为空 / 报“空正文” | 推理模型会先消耗 reasoning token；换 `LLM_MODEL=deepseek-chat` 或减小单次输入 |
| ASR 术语错得多 | 设置 `VIDEOLRN_ASR_PROMPT` 补充本课术语，或换 `medium` |
| 找不到 ffmpeg / ffprobe | 重开终端让 PATH 生效，再跑 `python -m videolearn doctor` |
| 网页打不开 / 401 | 必须用终端里打印的 `?k=...` 链接；服务只在 127.0.0.1 |
| 共享注册失败 | 检查服务地址、邀请码是否已用过、网络是否可达 |

## 开发

```bash
python -m pytest -q          # 全部测试
python -m compileall -q videolearn
```

版本 `0.1.0`；入口点：`videolearn = videolearn.cli:entry`、`videolearn-web = videolearn.web:main`。

结构：`iaaa.py`（认证）→ `blackboard.py`（课程/播放地址）→ `media.py`（HLS/MP4/ffmpeg）
→ `asr.py` / `streaming.py`（转写）→ `llm.py`（重述/眼睛决策）→ `vision.py`（截图）
→ `pipeline.py`（编排）→ `cli.py` / `web.py`（两种入口）；`share.py` 是共享客户端，
`share_server/` 是只存文字共享服务。

更多设计说明：[docs/cross-platform.md](docs/cross-platform.md)、
[docs/shared-summary-cache.md](docs/shared-summary-cache.md)、
[docs/centralized-service-architecture.md](docs/centralized-service-architecture.md)。
