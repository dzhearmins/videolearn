# 云端 ASR：把转写从本机 faster-whisper 换到第三方

> 状态：已在远端内测启用（SiliconFlow `Qwen/Qwen3-ASR-1.7B`）。
> 45 秒真实数学课程任务通过，Web 任务日志里 `asr_seconds ≈ 5.2 s`。
> 相关代码：`videolearn/asr.py` 的 `CloudAsr` / `make_backend`，
> `videolearn/streaming.py` 的 ASR 后端注入。

## 1. 支持哪些服务

所有兼容 **OpenAI `/audio/transcriptions`** 的服务都可以，用同一套环境变量：

| 服务 | `ASR_BASE_URL` | `ASR_MODEL` | 备注 |
|---|---|---|---|
| SiliconFlow | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-ASR-1.7B` | 国内可达，中文好；默认 |
| SiliconFlow | 同上 | `FunAudioLLM/SenseVoiceSmall` | 更便宜，术语偶有错 |
| Groq | `https://api.groq.com/openai/v1` | `whisper-large-v3-turbo` | 很快，带 verbose_json 时间戳；国内网络可能不稳 |
| OpenAI | `https://api.openai.com/v1` | `whisper-1` | 需要能访问 OpenAI |
| 自建/代理 | 你的 `/v1` 地址 | 你的模型名 | 只要接口兼容即可 |

10.6 秒中文样例（同一段话，含“矩阵的初等变换与行列式”）实测：

| 模型 | 响应时间 | 结果 |
|---|---|---|
| `Qwen/Qwen3-ASR-1.7B` | 0.52 s | 全对 |
| `XingChenAGI/XingChenASR-V3.2` | 1.66 s | 全对 |
| `FunAudioLLM/SenseVoiceSmall` | 1.26 s | “与行列式”听成“宇航列式” |
| `XingChenAGI/Xing-Audio4.0-ASR-Preview` | 1.73 s | 输出英文，不适合中文课 |

`Qwen3-ASR-1.7B` 在样例上最快也最准，因此默认用它；具体价格/额度以服务商
控制台为准。

## 2. 配置

在项目根目录 `.env`（远端是 `/home/dzr/videolearn-beta/.env`）：

```bash
VIDEOLRN_ASR_BACKEND=cloud
ASR_BASE_URL=https://api.siliconflow.cn/v1
ASR_MODEL=Qwen/Qwen3-ASR-1.7B
ASR_API_KEY=sk-...
ASR_LANGUAGE=zh
ASR_TIMEOUT=180
ASR_CHUNK_SECONDS=300
ASR_RESPONSE_FORMAT=json
```

网页右上角「设置」里也有同样四项：转写方式、服务地址、模型、API Key；
远端会话里填的只存内存，不写服务器 `.env`。

本地模式不受影响：`VIDEOLRN_ASR_BACKEND=local` 或删掉这一行即回到
faster-whisper。

## 3. 它怎么工作

- 本地 `/ 云` 共用同一个 `AsrBackend` 接口；`streaming.py` 不再直接 new
  `WhisperModel`，而是 `asr.make_backend(settings)`。
- 云端按 `ASR_CHUNK_SECONDS`（默认 300 s）切块，用 ffmpeg 把 PCM 编成
  16 kHz 单声道 WAV（300 s 约 9.6 MB）上传。
- 服务返回 `segments`（Whisper verbose_json）就直接用；只返回 `text`
  （Qwen/SenseVoice）就按中文标点切句，再按字数比例分配时间戳。
  因此时间戳是句级近似，不是词级。
- 音频块只上传给 ASR 服务商，不上传视频；服务器仍只短暂保留视频。
- 云端失败不会自动偷偷回退本地（避免你不知道走了哪条路）；错误会直接
  显示在任务日志里。

## 4. 隐私与成本

- 课堂音频会离开服务器，发给所选服务商；敏感课程请先确认对方的数据政策。
- 只发音频，不发视频、Cookie、学号密码。
- 按音频分钟计费；先跑 90 秒小样确认效果，再跑整节课。
- 服务器 `.env` 里的共享 Key 所有内测会话都能用（前端只显示打码值）。
  不想共用就删掉 `ASR_API_KEY` 并让每个人在设置里填自己的。

## 5. 排错

| 现象 | 处理 |
|---|---|
| `HTTP 401` | Key 错/过期 |
| `HTTP 402` | 服务商余额不足，去控制台充值或换 Key |
| `HTTP 400` 且提到 `response_format`/`language` | 适配层会自动去掉这两个参数重试一次；仍失败看返回正文 |
| `ffmpeg 编码云端音频失败` | 检查 ffmpeg 是否在 PATH；云端需要它把 PCM 编成 WAV |
| 时间戳很粗 | 云端只回文本时的预期行为；换支持 `verbose_json` 的 Whisper 端点可得到句级/词级时间戳 |
| 想回本地 | `.env` 改 `VIDEOLRN_ASR_BACKEND=local`，重启服务 |

## 6. 验收记录

- 10.6 秒中文样例：`asr.transcribe(settings=cloud)` 成功，返回 2 个句级
  时间段，文本全对（Qwen3-ASR-1.7B）。
- 同一段 45 秒高等代数课堂（10:00 起）：
  - 本地 `base`：23.4 s，130 字，只有 1 个粗时间段，句子有缺漏；
  - 云端 Qwen3-ASR：6.5 s，221 字，按句切分，内容更完整、带标点。
- 45 秒真实课程 Web 任务：状态 `done`，`stream_metrics.json` 中
  `asr_model=cloud:Qwen/Qwen3-ASR-1.7B`、`asr_seconds=5.199`，
  服务峰值内存约 300 MB（不再加载本地 whisper）。
- 本地 93 个测试全过（含 6 个云端适配层测试）。
- 账户余额不足时接口返回 HTTP 402；适配层会直接提示“余额不足”，
  不会静默回退本地。
