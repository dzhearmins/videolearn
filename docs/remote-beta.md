# 远端内测：把网页界面挂到服务器上

> 状态：已部署（2026-11-19）。入口是 `http://<服务器>/<秘密路径>/`，
> 路径与 Basic 账号由管理员单独发放，不写进仓库。
> 相关实现：`videolearn/web.py` 的 `--sessions`、`SessionStore`、`JobLock`。

## 1. 它是什么

本机模式（`python -m videolearn web`）仍然只监听 `127.0.0.1`，行为不变。
远端模式只是多开一个开关：

```bash
VIDEOLRN_HOME=/srv/videolearn \
  python -m videolearn web --sessions --port 8766 \
  --cookie-path /<秘密路径>/ --sample-seconds 90 --sample-start-seconds 600 \
  --notice "内测环境：HTTP 明文，请勿使用主账号密码"
```

`--sample-start-seconds 600` 让「只跑前 90 秒」从第 10 分钟开始，
避开课堂开头经常出现的静音/调试画面；页面上仍可按次改「起点 分钟」。

nginx 把 `/<秘密路径>/` 反代到 `127.0.0.1:8766/`，并负责 Basic 认证；
应用仍然只监听回环地址，公网不直接暴露 Python 的端口。

## 2. 每个浏览器一个隔离会话

`--sessions` 会按 `vl_sid` Cookie 分配独立的：

- `Settings`（学号、密码、API Key、模型选择）
- `.state/cookies.json` 所在的会话目录
- `output/` 产物目录（课程名/回放名/视频/文字稿）
- 任务列表与「当前任务」

**学号/密码/API Key 只存在进程内存里**，不写 `.env`、不落盘；
服务重启后需要重新填。服务器级 `.env` 只放共享默认值
（例如统一的 LLM Key、`VIDEOLRN_WHISPER_MODEL=base`），
个人在设置里改了只影响自己的会话。

所有会话共享一把文件锁（`sessions/.job.lock`）：服务器只有 2 核，
两个 whisper 同时跑会互相拖慢甚至 OOM，所以一次只跑一个任务，
第二个请求会收到 409，提示过几分钟再试。

进程还有三道内存保护：任务结束调用 `malloc_trim(0)` 归还空闲堆；
启动新任务前 RSS > 650 MB 时先 trim，仍高就返回 503；
systemd 限制 `MemoryHigh=750M`、`MemoryMax=900M`、`MemorySwapMax=0`，
宁可 cgroup 杀掉服务重启，也不让 1.6 GB 的整机被 swap 拖死。
`VIDEOLRN_LLM_TIMEOUT=300` 把单次 LLM 读超时从 900 s 收紧到 5 分钟，
避免一个卡住的请求占住唯一的任务槽 15 分钟。

## 3. 安全边界（重要）

- **当前只有 HTTP（80），没有 TLS。** Basic 密码、PKU 学号/密码、
  DeepSeek Key 在链路上都是明文。只适合可信小圈子内测；
  不要把主账号密码发给不信任的人，能申请专用测试账号最好。
- 入口有两层：不可猜的路径 + nginx Basic 认证；
  Basic 密码每人一个，可单独撤销。
- 应用令牌由 nginx 通过 `X-VL-Key` 注入，不出现在 URL 里；
  Python 端口只监听 `127.0.0.1`。
- 服务端 `.env` 里的 `DEEPSEEK_API_KEY` 是共享默认值，所有会话都能用它
  跑任务（但看不到完整 Key，接口只回打码值）。不想共用就删掉这一行并重启。
- 服务器上仍会短暂落盘视频（任务结束默认自动删除，`VIDEOLRN_AUTO_CLEAN_VIDEO=1`）；
  文字稿/截图会留在 `sessions/<sid>/output/`，由会话主人通过「视频存储」清理。

## 4. 运维命令

```bash
# 状态 / 日志
systemctl status videolearn-web
journalctl -u videolearn-web -f

# 重启（改了代码或 web.env 后）
systemctl restart videolearn-web
nginx -t && nginx -s reload

# Basic 账号：每行 user:apr1-hash；加人/删人都直接改这个文件
printf 'alice:%s\n' "$(openssl passwd -apr1 '新密码')" \
  >> /www/server/nginx/conf/videolearn-beta.htpasswd

# 看会话目录占用
du -sh /home/dzr/videolearn-beta/sessions/*
```

添加账号不需要 reload nginx；改路径或应用令牌需要同时改
`web.env` / nginx 配置并重启。

## 5. 资源与体验

实测（阿里云 2 vCPU / 1.6 GB，`base` int8，2 线程）：

| 项目 | 数值 |
|---|---|
| 模型加载 | 4–22 s（缓存命中约 4 s） |
| 流式 ASR（beam=1） | 约 0.3–0.7× 音频时长 |
| 103 s 中文样本 | 约 27–32 s |
| 峰值 RSS | 420–570 MB |
| 服务空闲 | 约 35–60 MB |
| 45 s 小样（真实课程、Web API 全流程） | 约 60 s，产出 transcript/retell/notes 齐全 |
| 连续第二个 45 s 任务 | 峰值约 770 MB，无重启；trim 后回落到约 500 MB |
| 90 s 小样（估算） | 约 2 分钟 |

真实样例会先下载 3–4 MB 的 45 秒 HLS 分片；任务结束自动删视频。
课堂开头 45 秒可能是静音/调试画面（实测第一段无语音），
所以默认小样从第 10 分钟开始。

**云端 ASR**：已启用（OpenAI 兼容端点，SiliconFlow
`Qwen/Qwen3-ASR-1.7B`，见 [cloud-asr.md](cloud-asr.md)）。
45 秒真实数学课程 ASR 从本地 `base` 的约 23 s 降到约 5–7 s，
且分句/标点更完整；服务不再加载本地 whisper，峰值内存约 300 MB。
想回本地就把 `VIDEOLRN_ASR_BACKEND` 改成 `local` 并重启。

因此远端默认「只跑前 90 秒」；600 秒小样约 3–7 分钟，完整 2 小时课
要半小时以上，且会下载约 1 GB 视频（任务结束自动删）。
`base` 的中文术语识别一般（例如「矩阵」可能写成「矩正」），
要质量可在设置里换 `small`，但更慢、内存更高（服务有 900 MB 上限）。

## 6. 已知限制

- 没有 HTTPS；这是当前云安全组只开放 80 端口导致的，不是代码问题。
- 没有账号体系：Basic 账号只是入口，会话之间互不可见，但没有「注册/找回密码」。
- 服务重启会清空所有会话内存里的凭据；Cookie 还在，但需要重新填学号/密码。
- 没有队列：同一时刻只能跑一个任务，第二个人收到 409。
- 先用**不存在的假账号**探测 IAAA 可达性（返回 E01），再用 `.env` 里的账号从服务器侧
  做了一次真实登录与 45 秒样例，均成功；第一次使用仍建议先跑 90 秒小样。

## 7. 部署验收（2026-11-19）

- IAAA / 教学网 / 媒体域名从服务器可达；假账号返回 E01，真实账号一次登录成功。
- 真实课程 45 秒 HLS 样例：下载 3.8 MB → 流式 ASR 26.4 s → 初稿 32.4 s →
  final pass 24.4 s，端到端 59.8 s，`transcript/retell/notes_map` 齐全。
- 通过 Web API 跑同一份 45 秒样例（会话隔离 + 任务锁 + 产物接口）状态 `done`。
- 两个 Basic 用户/浏览器会话的学号、API Key、任务列表互不可见；设置只存内存，
  服务器 `.env` 未被改写。
- nginx 无认证 401、错误密码 401、正确密码 200；`/vl` 共享服务仍正常。
- 页面内联 JS 通过 `node --check`。
