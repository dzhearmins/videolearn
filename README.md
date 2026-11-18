# pku-videolearn

北大教学网（course.pku.edu.cn）课程回放 → 文字稿 → AI 重述，个人学习用途。

> 只处理你本人有权限访问的课程；不绕过 DRM；不分享、不传播下载内容。

## 它做什么

```
学号+密码 ──IAAA OAuth──> 教学网 SSO Cookie
                              │
                    videoList.action（课程回放列表）
                              │
                    playVideo.action?token=...
                              │  iframe → 302
                    course_id / sub_id / app_id / auth_data
                              │
        yjapise .../get-sub-info-by-auth-data
                              │
              m3u8（标准 AES-128 或明文）／直链 mp4
                              │
        ffmpeg 解码 → faster-whisper 流式分块 → transcript.partial.md → transcript.md
                              │ 并发
                    DeepSeek stream → stream_restated.md（初稿）
                              │
              （默认）眼睛：分步思考 → 批量截图 → 读板书/PPT → eyes_analysis
                              │
        final_pass：完整转写 + 初稿 + 视觉结论 → retell.md（唯一终稿）
```

## 安装

需要 Python 3.10+（本机 3.14 已验证可安装 ctranslate2/av wheel）。

```powershell
cd D:\projects\videolearn
python -m pip install -r requirements.txt
```

FFmpeg **必需**（转写前解码音频；TS 流无损封装为 MP4 也用它）：

```powershell
winget install --id Gyan.FFmpeg -e --source winget
```

## 配置

复制 `.env.example` 为 `.env`，填写：

```ini
PKU_STUDENT_ID=你的学号
PKU_PASSWORD=你的密码
DEEPSEEK_API_KEY=sk-...
```

- `.env`、`.state/`、`output/` 均已在 `.gitignore` 中。
- 密码只用于 `iaaa.pku.edu.cn/iaaa/oauthlogin.do`（RSA PKCS#1 v1.5 加密后提交），不会写日志。
- 首次登录成功后会缓存 Cookie 到 `.state/cookies.json`，之后自动复用；失效会自动重登。

## 使用

```powershell
# 0) 环境自检（不输出任何密钥）
python -m videolearn doctor

# 1) 先验证登录
python -m videolearn login

# 2) 交互式：列课程 → 选课 → 列回放 → 选一节 → 流式转写+初稿 → 眼睛 → final_pass
python -m videolearn run

# 跳过课程选择（按课程名关键词）
python -m videolearn run --course 高等代数

# 直接给某一节的 playVideo URL（token 有效期很短，需现取）
python -m videolearn run --url "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=..." --title "第 3 次课"

# 只要文字稿，不留视频
python -m videolearn run --no-video

# 只取某一段做小样（从第 600 秒起 90 秒），验证效果或精听
python -m videolearn run --course 高等代数 --start-seconds 600 --limit-seconds 90

# 关闭眼睛 / 关闭流式（回退批量转写），或调整流式分块
python -m videolearn run --course 高等代数 --no-eyes
python -m videolearn run --course 高等代数 --no-stream
python -m videolearn run --course 高等代数 --chunk-seconds 240 --overlap-seconds 5 --asr-threads 8

# 复用同目录已有的眼睛分析，不再调用 LLM
python -m videolearn run --course 高等代数 --reuse-eyes
```

查看课程列表：`python -m videolearn courses`

## 默认两段式流水线

`run` 默认执行：

1. **流式阶段**：ASR 按 `--chunk-seconds`（默认 240s、重叠 5s）产出，LLM 用 `stream=True` 逐 delta 写出初稿；`transcript.partial.md` 与 `stream_restated.md` 边产边落盘。请求固定带 `thinking=disabled`，否则首正文会被 reasoning 拖到 20–60s。
2. **眼睛阶段（默认开启）**：在完整转写上跑“思考 → 批量取帧 → 观察”循环，产出 `eyes_analysis.json/.md`。
3. **final_pass（全量总结）**：流式结束后，从磁盘读回**完整转写**与**流式初稿**，重新做一次完整重述；初稿只作覆盖度参考，最终 `retell.md` 只有这一份总结，`stream_restated.md` 改名为 `stream_draft.superseded.md` 并标记仅作审计。

计时分开记录：首正文时延、流式阶段时长、final_pass 时长、端到端时长写入 `stream_metrics.json` 与 `meta.json` 的 `stream` 字段。

## 眼睛功能（默认开启）

开启后，重述前运行一个“思考 → 批量取帧 → 观察 → 留存 → 再思考”的循环：

1. **思考**：LLM 结合转写与 `previous_steps`，先输出 `analysis`（想确认什么、还缺什么），再选择动作。
2. **取帧**：动作 `get_frames` 一次可给 1–4 个时间点；ffmpeg 按请求顺序抽 1280px JPEG 到 `frames/`。
3. **观察**：这一批图按时间顺序一次性发给视觉模型，要求逐帧 `### HH:MM:SS` + `### 综合分析`，图前紧跟时间标签，避免张冠李戴。
4. **留存**：每一步的 `analysis`、取帧清单、`observation` 立即写入 `eyes_analysis.json` / `eyes_analysis.md`，并进入下一轮 `previous_steps`；后续分析直接读这些结果。
5. **收尾**：模型可随时 `finish(summary)`；达到 `--max-steps` 或 `--max-shots` 时，系统基于已有分析再生成一次最终视觉结论。
6. **写稿**：逐帧描述 + 最终结论进入重述；模型可在正文用 `[[SHOT:HH-MM-SS]]` 指定插图位置，未被引用的截图统一附在文末「黑板与 PPT 截图」。

要求：`VISION_MODEL`（默认同 `LLM_MODEL`）必须支持图片输入；`deepseek-flash` 支持，`deepseek-v4-pro` 仅文本。眼睛默认开启，`--no-eyes` 关闭；每轮约 1 次决策 + 1 次批量看图；`--max-shots` 默认 8（硬上限 20），`--max-steps` 默认 4（硬上限 8），单批最多 4 张。截图会随转写一起发送到同一 LLM 端点，请知悉。

## 上下文防超限

| 措施 | 状态 | 位置 |
|---|---|---|
| 转写按时间戳分块，块间 400 字重叠 | 已实现 | `llm.chunk_transcript` |
| 单次输入预算 + 超限截断（保留头尾、标注截断字数） | 已实现 | `llm.max_input_chars` / `llm.fit_text` |
| 笔记总长超预算时分层摘要（最多 3 轮，仍超再截断） | 已实现 | `llm._compress_notes` / `llm.group_notes` |
| 截图条数硬上限 20 | 已实现 | `llm.MAX_SHOTS_HARD` / `vision.run_eyes` |
| 服务端报 context 超限后缩输入重试 | 已实现 | `llm.ContextWindowError` + `eyes_decide` / `observe_frames` / `retell` |
| 单次 prompt 体积可自查 | 已实现 | `context_report.json`；`VIDEOLRN_DEBUG=1` 时逐次打印 |
| 真正的 tokenizer 精确计数 | 未实现 | 用 1 token/字符的保守启发式 |
| 输出被 `max_tokens` 截断后自动续写 | 未实现 | 只记录 `finish_reason=length` |
| 图片按体积自适应压缩 | 未实现 | 固定 1280px JPEG |

预算由 `VIDEOLRN_MAX_INPUT_TOKENS` 控制，默认 60000（按 1 token/字符，即单次输入 ≤ 60000 字）。超限时先分层摘要，最后仍超才截断；每次截断/调用体积/重试都写进 `context_report.json`。`--max-shots` 超过 20 会被压到 20。

仍存的缺口：估算不是真 tokenizer（偏保守，可能过早压缩）；planner 的转写超长时保留头尾、可能漏中间，超长课建议用 `--start-seconds/--limit-seconds` 分段；输出 8192 token 上限只记录不续写。

## 流式转写重述可行性（实测）

在完整视频 `output/高等代数（I）/2026-11-05_080000_课堂回放/video.mp4`（D=7162.4s）上实测：

```powershell
python -m videolearn.stream_bench --video "<完整视频.mp4>" `
  --chunk-seconds 240 --overlap-seconds 5 --model base --threads 8
```

做法：ASR 线程按 240s 块（重叠 5s）产出，LLM 线程用 `stream=True` 逐 delta 消费，队列 `maxsize=1` 形成背压。**关键前提是请求里带 `"thinking": {"type": "disabled"}`**；默认 reasoning 模式下 `deepseek-flash` 会先输出大段 reasoning，首正文 token 明显延后。

| 指标 | 实测值 |
|---|---|
| 视频时长 D | 7162.4s |
| 分块 | 31 × 240s（重叠 5s） |
| 流式总耗时 | 297.6s（4.2% D） |
| 批式基线总耗时 | 362.1s（5.1% D） |
| 首正文 token 早于下一块 ASR 结束 | 28/29（97%） |
| 重复句占比 | 去重前 10/339（2.95%）→ 去重后 9/338（2.66%） |
| 句级覆盖率 <0.6 / <0.4 | 55/303（平均 78.2%）/ 20/303（其中 4 句为转写质量元评论） |
| 峰值 RSS | 0.53GB |
| ASR 纯耗时合计 | 295.0s |
| 批式 LLM 合计 | 67.2s |
| 全量总结 final_pass | 414.8s（5.8% D） |
| 端到端（流式+final_pass） | 712.4s（9.9% D） |

结论：**离线处理场景可行**——加上独立 final_pass 后端到端 712.4s（9.9% D），仍远小于视频时长；首正文与下一块 ASR 的重叠、去重比例均达标。final_pass 是流水线末尾的独立阶段，不计入首 token 时延。55/303 的“丢句”是抽象改写造成的措辞差异（含口语反问、模型对转写质量的元评论），不是 55 句事实缺失；报告列出了示例，平均字符 bigram 覆盖率 78.2%。本次用 `base` ASR 是为了在可接受时间内跑完整 2 小时；生产用 `small` 时 ASR 更慢，重叠只会更容易，但总耗时未测。报告与逐块时间戳见同目录 `stream_bench.md`，原始数据见 `stream_bench.jsonl`。

## 输出

```
output/
└─ 高等代数（I）/
   └─ 2026-11-03_101000_课堂回放/
      ├─ video.mp4        # 回放（--no-video 时删除）
      ├─ transcript.md    # 带 [HH:MM:SS] 的完整文字稿
      ├─ transcript.srt   # 字幕
      ├─ transcript.txt   # 纯文本
      ├─ transcript.partial.md  # 流式阶段边产边写
      ├─ stream_events.jsonl    # 每块 asr/llm 计时
      ├─ stream_metrics.json    # 首正文/流式/final_pass/端到端
      ├─ stream_draft.superseded.md  # 流式初稿（仅审计，已被取代）
      ├─ retell.md        # final_pass 的唯一最终总结（含截图）
      ├─ notes_map.md     # 分段精读笔记
      ├─ frames/          # 眼睛截图（HH-MM-SS.jpg）
      ├─ eyes_analysis.json  # 分步思考/取帧/观察/最终结论（可复用）
      ├─ eyes_analysis.md    # 同上的人类可读版
      ├─ shots.json       # 每张截图的时间/原因/视觉描述
      ├─ visual_notes.md  # 视觉笔记汇总
      ├─ context_report.json  # 上下文事件：分块/截断/调用体积/超限重试
      └─ meta.json
```

没有 `DEEPSEEK_API_KEY` 时会生成 `prompt.md`，可粘贴到任意网页版大模型。

本机 `output/高等代数（I）/` 下有真实小样：`..._sample600+90s/`（纯转写+重述）、`..._sample600+60s/`（**分步眼睛**，含 `eyes_analysis.json/.md`、`frames/` 与内嵌图重述）、`..._sample600+120s/`（旧版一次性取帧的眼睛结果），可直接对照看效果；`output/` 被 gitignore，仅本机可见。

## 安全与合规

- 支持**标准 HLS AES-128**（RFC 8216，key 由同一账号会话从校方资源服务器取回，按分片解密，等同官方网页播放器行为）。
- **不处理** SAMPLE-AES / Widevine / 任何 DRM；检测到会立即中止并说明原因。
- 仅使用你自己的账号、仅下载你有权限的课程，供个人复习；**视频画面本身带有“未经授权不得翻录转载”的版权提示，请勿再分发**。
- 日志中不输出密码、token、Cookie、`auth_data`。
- `.env`、`.state/`、`output/` 均已 gitignore。

## 已知限制

- CPU 转写较慢：实测 29 秒音频用 18 秒（约 0.6× 实时），1 小时课约 35–60 分钟；`medium` 更准但更慢（`.env` 的 `VIDEOLRN_WHISPER_MODEL`）。
- 数学公式是 ASR 的弱项，转写里的公式可能失真；`retell.md` 会标注存疑处，请对照视频核对。
- **眼睛功能**受截图分辨率与模型视觉能力限制：小字、手写连笔、教师遮挡都可能读错；`shots.json` 保留每张图的描述与原因，重要公式请点开原图核对。
- 默认流式 ASR 用 `beam_size=1` 且请求关闭 thinking；与批式 `beam_size=5` 相比转写质量可能略低，但换来首正文低延迟。final_pass 会额外耗时（完整视频实测 414.8s），不想等可用 `--no-stream` 或对短片段运行。
- 一次批量看图依赖端点支持多图；`deepseek-flash` 实测 4 张/次，其他模型可能只接受单图。
- 为避免浪费，已取过的时间点 10 秒内不允许重复取帧；代理只能换相邻时间点，不能回看同一帧。历史步骤只回灌文字分析与观察，不回灌图片。
- 截图会发送到与转写相同的 LLM 端点；若你对此敏感，不要开启 `--eyes`。
- playVideo 的 JWT 签名由教学网签发，示例 token 可能过期/无效；以 `run` 列表里的实时链接为准。
- 课程回放列表接口若改版，`videolearn/blackboard.py` 的解析选择器需要同步调整。
- 国内网络下模型下载慢，建议在 `.env` 设置 `HF_ENDPOINT=https://hf-mirror.com`。

## 故障排查

- **IAAA 登录失败 [E21]**：尝试次数过多，等 30 分钟；不要反复试密码。
- **“回放 token 已失效”**：playVideo 的 JWT 由教学网短期签发，回到 `run` 列表重新选即可。
- **`retell.md` 为空 / 报“空正文”**：`deepseek-flash` 等推理模型会先消耗 reasoning token；把 `.env` 的 `LLM_MODEL` 改为 `deepseek-chat`，或减少单次输入。
- **ASR 术语错得多**（如“矩阵→巨镇”）：设置 `VIDEOLRN_ASR_PROMPT` 补充本课术语，或换 `medium` 模型。
- **找不到 ffmpeg**：重开终端让 PATH 生效，然后 `python -m videolearn doctor` 复核。
- **转写很慢**：CPU 正常现象；`--limit-seconds 90` 可先验证效果。

## 开发

```powershell
python -m pytest -q
```

结构：`iaaa.py`（认证）→ `blackboard.py`（列表/播放地址）→ `media.py`（HLS/MP4）→ `asr.py` → `llm.py`，`pipeline.py` 串联，`cli.py` 交互。
