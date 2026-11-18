"""LLM：课堂笔记 / 重述，以及“眼睛”功能的取帧规划与看图描述。

默认走 DeepSeek 的 OpenAI 兼容接口；可通过 LLM_BASE_URL/LLM_MODEL 替换。
视觉模型由 VISION_MODEL 指定，默认与 LLM_MODEL 相同（deepseek-flash 支持图片输入）。

上下文保护：
- 转写按时间戳分块（带重叠），每块远低于单次预算；
- 单次输入按 VIDEOLRN_MAX_INPUT_TOKENS 限制（默认 60000，保守按 1 token/字符）；
- 笔记超预算时分层摘要，最后仍超才截断；
- 服务端报 context 超限时缩输入重试；
- 所有分块/截断/调用体积写入 context_report.json。
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from .config import Settings

# 上下文保护参数：保守按 1 token/字符估算，可用环境变量覆盖
DEFAULT_MAX_INPUT_TOKENS = 60000
CHUNK_MAX_CHARS = 6000
CHUNK_OVERLAP_CHARS = 400
MAX_SHOTS_HARD = 20

# 本次 run 的上下文事件（截断/调用体积/超限重试），由 pipeline 写入 context_report.json
CONTEXT_TRACE: list[dict] = []

SYSTEM_NOTES = (
    "你是北京大学数学课程的助教。输入是课堂录音的自动转写，可能有同音错字、口语和断句错误。"
    "请整理成结构化课堂笔记：只依据转写内容，不编造；保留定义、定理、公式与推导，公式用 LaTeX；"
    "对明显转写错误在括号内标“疑为…”，不确定处标“（听不清）”；用中文。"
)

SYSTEM_FINAL = (
    "你是北京大学数学课程的资深助教。你会收到同一节课按时间切分的多段笔记。"
    "请合并成一份完整的课堂重述，严格忠于原意，不虚构内容。输出两部分：\n"
    "## A. 知识点精讲\n按讲解顺序组织，保留定义/定理/公式（LaTeX）与例题步骤，标注易错点。\n"
    "## B. 老师口吻重讲\n用连贯、自然的中文把整节课重新讲一遍，像一对一辅导，适合通勤时听/复习；"
    "不要逐字复述，但不要遗漏主线与关键推导。\n"
    "若输入有 <visual_notes>，你可以把截图内容写进正文，并在相应位置单独一行写 "
    "[[SHOT:HH-MM-SS]]（只能是 visual_notes 中出现过的时间点），系统会替换成图片；"
    "禁止编造未提供的截图。若 <visual_notes> 已确认黑板/PPT 内容，应以截图为准并在正文采用，"
    "不要把这些已确认的内容写成“缺失/听不清”。若输入有 <visual_summary>，那是分步视觉分析的总结，"
    "应与逐帧观察一起采用。若输入有 <stream_draft>，那是流式阶段的第一版重述：只把它当作覆盖度参考，"
    "最终输出必须更完整、更准确，并以完整转写与截图为准。最终稿不要出现“流式初稿”“stream_draft”"
    "“visual_notes”等内部阶段名，也不要描述这些阶段；只输出最终重述。"
    "若输入本身有矛盾或缺失，明确写出“转写缺失/存疑”，不要自行补全。"
)

EYES_SYSTEM = (
    "你是课堂视频的“眼睛”代理。你可以调用 get_frames 一次获取多帧课堂截图，观察结果会返回给你。"
    "每一步必须：1) 先写 analysis（基于转写与已有观察，说明想确认什么、还缺什么）；"
    "2) 再选 action：get_frames 或 finish。\n"
    "严格输出 JSON："
    '{"analysis":"...","action":{"type":"get_frames","times":[{"time":"HH:MM:SS","focus":"..."}]}}\n'
    "或："
    '{"analysis":"...","action":{"type":"finish","summary":"..."}}\n'
    "约束：时间必须在给定范围内、与已取帧相隔至少 10 秒；单次和累计帧数遵守用户给出的上限；"
    "信息足够或达到上限时必须 finish；只依据转写与观察，不要编造没看到的画面。"
)

VISION_SYSTEM = (
    "你是课堂助教，正在看课堂视频截图。转写可能不准，必须以图像为准。"
    "只描述图中确实可见的课程内容：黑板/PPT 上的文字与公式（尽量逐字，公式用 LaTeX）、"
    "图形表格、定理编号、矩阵形状。不要根据转写补图里没有的内容；看不清就写“看不清”。"
    "忽略画面中的时钟、日期、版权字幕等与课程内容无关的元素，不要转写它们。"
    "用中文，300 字以内。"
)

SUMMARY_SYSTEM = (
    "你在做课堂笔记的压缩合并。把输入的多段笔记合并成一段更紧凑的笔记，"
    "保留定义、定理、公式（LaTeX）、例题步骤与时间点，删除重复和口语；"
    "不得新增原文没有的内容。用中文，不超过 1500 字。"
)


EYES_SUMMARY_SYSTEM = (
    "你在为课堂视频的“眼睛”分析收尾。根据分步分析与逐帧观察，写最终视觉结论："
    "已确认的板书/PPT 内容、仍未看清或存疑之处、对理解本节课最关键的信息。"
    "只依据给出的观察，不编造；用中文，300 字以内。"
)


class LLMError(RuntimeError):
    pass


class ContextWindowError(LLMError):
    """服务端明确报上下文超限。上层应缩小输入后重试。"""


def reset_trace() -> None:
    CONTEXT_TRACE.clear()


def _trace(**event) -> None:
    event.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    CONTEXT_TRACE.append(event)


def estimate_tokens(text: str) -> int:
    """保守估算：按 1 token/字符计（中文实际常低于此值），宁多勿少。"""
    return max(1, len(text))


def max_input_chars() -> int:
    """单次输入的字符预算；VIDEOLRN_MAX_INPUT_TOKENS 可覆盖。"""
    try:
        tokens = int(os.getenv("VIDEOLRN_MAX_INPUT_TOKENS", str(DEFAULT_MAX_INPUT_TOKENS)))
    except ValueError:
        tokens = DEFAULT_MAX_INPUT_TOKENS
    return max(2000, tokens)


def fit_text(text: str, budget_chars: int, label: str = "") -> str:
    """超预算时保留头尾、截断中间，并记录截断事实；保证结果不超过预算。"""
    budget_chars = max(80, budget_chars)
    if len(text) <= budget_chars:
        return text
    reserve = min(80, budget_chars // 4)
    head = max(1, int((budget_chars - reserve) * 0.65))
    tail = max(0, budget_chars - reserve - head)
    removed = max(0, len(text) - head - tail)
    marker = f"\n…[上下文保护：中间截断 {removed} 字符]…\n"
    out = text[:head] + marker + (text[-tail:] if tail else "")
    if len(out) > budget_chars:  # 极端预算下兜底
        out = out[:budget_chars]
    _trace(
        kind="truncate",
        label=label,
        original_chars=len(text),
        kept_chars=len(out),
        removed_chars=removed,
    )
    if os.getenv("VIDEOLRN_DEBUG"):
        print(f"[ctx] {label or 'text'}: 截断 {removed} 字符")
    return out


def clamp_max_shots(value: int) -> int:
    return max(0, min(int(value), MAX_SHOTS_HARD))


def group_notes(notes: list[str], budget_chars: int) -> list[list[str]]:
    """把笔记按字符预算分组，供分层摘要。"""
    groups: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for note in notes:
        if cur and size + len(note) > budget_chars:
            groups.append(cur)
            cur, size = [], 0
        cur.append(note)
        size += len(note) + 1
    if cur:
        groups.append(cur)
    return groups


def _messages_chars(messages: list[dict]) -> int:
    """估算消息文本体积；图片按固定 token 折算，避免把 base64 长度当成文本。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += len(part.get("text") or "")
                elif part.get("type") == "image_url":
                    total += 1200
    return total


@dataclass
class Chunk:
    index: int
    start_hint: str
    end_hint: str
    text: str


def chunk_transcript(
    text: str,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """按行聚合，尽量在分段时间戳边界切块；相邻块带少量重叠。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    reserve = min(overlap_chars, max_chars // 4) if overlap_chars > 0 else 0
    target = max(20, max_chars - reserve)

    base: list[list[str]] = []
    buf: list[str] = []
    size = 0
    for ln in lines:
        if buf and size + len(ln) + 1 > target:
            base.append(buf)
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        base.append(buf)

    chunks: list[Chunk] = []
    prev: list[str] | None = None
    for i, body in enumerate(base):
        content = list(body)
        if i > 0 and prev and overlap_chars > 0:
            ov: list[str] = []
            n = 0
            for ln in reversed(prev):
                if n + len(ln) + 1 > overlap_chars:
                    break
                ov.insert(0, ln)
                n += len(ln) + 1
            body_chars = sum(len(x) + 1 for x in body)
            while ov and sum(len(x) + 1 for x in ov) + body_chars > max_chars:
                ov.pop(0)
            content = ov + body

        first = content[0]
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", first)
        start = m.group(1) if m else ""
        last = content[-1]
        m2 = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", last)
        end = m2.group(1) if m2 else ""
        text_out = "\n".join(content)
        if len(text_out) > max_chars:  # 单行本身超长时硬截断
            text_out = fit_text(text_out, max_chars, f"chunk_{i + 1}")
        chunks.append(Chunk(i + 1, start, end, text_out))
        prev = body

    _trace(
        kind="chunking",
        chunks=len(chunks),
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        total_chars=sum(len(c.text) for c in chunks),
        est_max_tokens=max((estimate_tokens(c.text) for c in chunks), default=0),
    )
    return chunks


def _chat_messages(
    settings: Settings,
    messages: list[dict],
    max_tokens: int = 8192,
    model: str | None = None,
    disable_thinking: bool = False,
) -> str:
    if not settings.llm_key:
        raise LLMError("缺少 DEEPSEEK_API_KEY")
    model_name = model or settings.llm_model
    base = settings.llm_base_url.rstrip("/")
    url = base + "/chat/completions"
    prompt_chars = _messages_chars(messages)
    prompt_tokens = max(1, prompt_chars)
    image_count = sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if isinstance(p, dict) and p.get("type") == "image_url"
    )

    if os.getenv("VIDEOLRN_DEBUG"):
        print(
            f"[llm] {model_name} input≈{prompt_tokens} tokens, images={image_count}, "
            f"max_out={max_tokens}"
        )
    _trace(
        kind="call",
        model=model_name,
        est_input_tokens=prompt_tokens,
        max_output_tokens=max_tokens,
        images=image_count,
        has_image=image_count > 0,
    )

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.llm_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(15, 900),
    )
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = r.text[:200]
        lowered = detail.lower()
        if r.status_code in (400, 413) and any(
            k in lowered
            for k in ("context", "too long", "maximum context", "token limit", "length limit")
        ):
            _trace(kind="context_error", model=model_name, detail=detail[:300])
            raise ContextWindowError(f"上下文超限: HTTP {r.status_code} {detail}")
        raise LLMError(f"LLM 请求失败: HTTP {r.status_code} {detail}")
    try:
        data = r.json()
        choice = data["choices"][0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError("LLM 返回格式异常") from e

    if finish == "length":
        _trace(kind="output_truncated", model=model_name, max_output_tokens=max_tokens)
        if os.getenv("VIDEOLRN_DEBUG"):
            print(f"[llm] 警告：输出被 max_tokens={max_tokens} 截断")

    content = content.strip()
    if not content:
        raise LLMError(
            f"LLM 返回了空正文（finish_reason={finish}）。"
            "若使用推理模型（如 deepseek-flash/reasoner），推理 token 可能耗尽预算；"
            "可改用 LLM_MODEL=deepseek-chat 或减小输入分块。"
        )
    return content


def _chat(settings: Settings, system: str, user: str, max_tokens: int = 8192) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        return _chat_messages(settings, messages, max_tokens=max_tokens)
    except LLMError as e:
        # 推理模型偶尔把 8192 token 预算全花在 reasoning 上，正文为空。
        # 不改变内容，只关闭 thinking 重试一次，保证长课 notes/final 不会直接失败。
        if "空正文" not in str(e):
            raise
        _trace(kind="retry_without_thinking", model=settings.llm_model)
        if os.getenv("VIDEOLRN_DEBUG"):
            print("[llm] 空正文，禁用 thinking 重试")
        return _chat_messages(
            settings, messages, max_tokens=max_tokens, disable_thinking=True
        )


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise LLMError("模型没有返回 JSON")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}") from e


def parse_shot_time(value: str) -> float | None:
    """接受 HH:MM:SS / MM:SS / SS，返回秒。"""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def format_timecode(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def frame_key(seconds: float) -> str:
    return format_timecode(seconds).replace(":", "-")


def eyes_decide(
    settings: Settings,
    title: str,
    transcript_text: str,
    start_offset: float,
    end_offset: float,
    history: str,
    remaining: int,
    max_batch: int,
) -> dict:
    """代理的一步：先分析，再决定取哪些帧或结束。返回 {analysis, action}。"""
    budget = max_input_chars()
    transcript = fit_text(transcript_text, int(budget * 0.55), "eyes_transcript")
    history_fit = fit_text(history, int(budget * 0.3), "eyes_history") if history else ""
    user = (
        f"课程：{title}\n"
        f"视频范围：{format_timecode(start_offset)}–{format_timecode(end_offset)}\n"
        f"剩余可截帧数：{remaining}；单次最多 {max_batch} 帧。\n\n"
        f"<transcript>\n{transcript}\n</transcript>\n\n"
        f"<previous_steps>\n{history_fit or '（尚无）'}\n</previous_steps>\n\n"
        "请输出 JSON。"
    )
    for attempt in range(2):
        try:
            raw = _chat(settings, EYES_SYSTEM, user, max_tokens=4096)
            data = _extract_json(raw)
            analysis = str(data.get("analysis", "")).strip()
            action = data.get("action")
            if not isinstance(action, dict) or action.get("type") not in (
                "get_frames",
                "finish",
            ):
                raise LLMError("action 必须是 get_frames 或 finish")
            return {
                "analysis": analysis or "（模型未给出分析）",
                "action": action,
            }
        except ContextWindowError:
            raise
        except LLMError as e:
            if attempt == 0:
                user += f"\n\n上次输出不合法（{e}），请只输出严格 JSON。"
                continue
            raise
    raise LLMError("眼睛决策失败")


def _build_frame_messages(title: str, frames: list[dict]) -> list[dict]:
    """构造多图消息：每张图前紧跟它的时间标签，保证顺序与时间戳对齐。"""
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"课程：{title}\n"
                f"以下按时间顺序提供 {len(frames)} 张课堂截图。每张图前标注时间与关注点。\n"
            ),
        }
    ]
    for i, f in enumerate(frames, 1):
        content.append(
            {
                "type": "text",
                "text": (
                    f"\n### 截图 {i}｜时间 {f['time']}\n"
                    f"关注点：{f.get('focus') or '板书/PPT 内容'}\n"
                ),
            }
        )
        b64 = base64.b64encode(Path(f["path"]).read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            }
        )
    content.append(
        {
            "type": "text",
            "text": (
                "\n请严格按时间顺序输出：每个时间点以 `### HH:MM:SS` 开头，"
                "逐字转写黑板/PPT 的公式与文字；最后以 `### 综合分析` 汇总。"
                "看不清写“看不清”，不要根据转写补画面。"
            ),
        }
    )
    return [
        {"role": "system", "content": VISION_SYSTEM},
        {"role": "user", "content": content},
    ]


def observe_frames(settings: Settings, title: str, frames: list[dict]) -> str:
    """一次把多帧截图发给视觉模型，返回逐帧观察 + 综合分析。"""
    messages = _build_frame_messages(title, frames)
    return _chat_messages(
        settings, messages, max_tokens=8192, model=settings.vision_model
    )


def eyes_summarize(settings: Settings, title: str, steps: list[dict]) -> str:
    """达到步数/帧数上限时，基于已有分步分析生成最终视觉结论。"""
    parts: list[str] = []
    for s in steps:
        parts.append(f"Step {s.get('step')} 分析：{s.get('analysis', '')[:500]}")
        if s.get("observation"):
            parts.append(f"观察：{s['observation'][:2000]}")
        if s.get("error"):
            parts.append(f"错误：{s['error']}")
    user = (
        f"课程：{title}\n" + "\n".join(parts) + "\n\n请输出最终视觉结论。"
    )
    budget = int(max_input_chars() * 0.6)
    return _chat(
        settings,
        EYES_SUMMARY_SYSTEM,
        fit_text(user, budget, "eyes_summary"),
        max_tokens=4096,
    )


def _image_markdown(shot: dict) -> str:
    reason = str(shot.get("reason", "")).replace("\n", " ").strip()
    caption = f"{shot['time']} 课堂截图" + (f"：{reason}" if reason else "")
    return f"![{shot['time']} 课堂截图](frames/{shot['key']}.jpg)\n\n> {caption}"


_SHOT_MARKER_RE = re.compile(r"\[\[SHOT:\s*([0-9:：\-]+)\s*\]\]")


def render_shot_images(text: str, shots: list[dict]) -> str:
    """替换模型写的 [[SHOT:...]]（兼容冒号/短横线、全角冒号）；未用到的截图放文末图集。"""
    by_key = {shot["key"]: shot for shot in shots}
    used: set[str] = set()

    def repl(m: re.Match) -> str:
        raw = m.group(1).replace("：", ":").replace("-", ":")
        seconds = parse_shot_time(raw)
        if seconds is None:
            return m.group(0)
        shot = by_key.get(frame_key(seconds))
        if not shot:
            return m.group(0)
        used.add(shot["key"])
        return _image_markdown(shot)

    text = _SHOT_MARKER_RE.sub(repl, text)

    rest = [s for s in shots if s["key"] not in used]
    if rest:
        text = text.rstrip() + "\n\n## 黑板与 PPT 截图\n\n"
        for shot in rest:
            text += _image_markdown(shot) + "\n\n"
    return text


def build_prompt(title: str, transcript_text: str) -> str:
    chunks = chunk_transcript(transcript_text)
    parts = [f"# 课程：{title}\n", f"共 {len(chunks)} 段。以下是按时间切分的课堂转写：\n"]
    for c in chunks:
        parts.append(f"\n--- 第 {c.index} 段（{c.start_hint}–{c.end_hint}）---\n")
        parts.append(c.text)
    parts.append("\n\n" + SYSTEM_FINAL)
    prompt = "\n".join(parts)
    budget = max_input_chars()
    if len(prompt) > budget:
        prompt = (
            f"> 提示：本文件约 {len(prompt)} 字，超过单次预算 {budget} 字；"
            "网页版粘贴时请分段，或先调低转写长度。\n\n" + prompt
        )
    return prompt


def _compress_notes(settings: Settings, notes: list[str], budget_chars: int) -> str:
    """笔记总长超预算时，分层摘要合并；最多 3 轮，最后仍超则截断。"""
    combined = "\n\n".join(notes)
    rounds = 0
    while len(combined) > budget_chars and len(notes) > 1 and rounds < 3:
        groups = group_notes(notes, max(2000, budget_chars // 2))
        if len(groups) <= 1:
            break
        new_notes: list[str] = []
        for group in groups:
            joined = "\n\n".join(group)
            try:
                summary = _chat(
                    settings,
                    SUMMARY_SYSTEM,
                    f"<notes>\n{joined}\n</notes>\n\n请压缩合并。",
                    max_tokens=4096,
                )
            except ContextWindowError:
                summary = _chat(
                    settings,
                    SUMMARY_SYSTEM,
                    f"<notes>\n{fit_text(joined, max(1000, len(joined) // 2), 'merge_retry')}\n</notes>\n\n请压缩合并。",
                    max_tokens=4096,
                )
            new_notes.append(summary)
        notes = new_notes
        combined = "\n\n".join(notes)
        rounds += 1
        _trace(
            kind="merge_round",
            round=rounds,
            groups=len(groups),
            chars=len(combined),
        )
    if len(combined) > budget_chars:
        combined = fit_text(combined, budget_chars, "notes_final")
    return combined


def retell(
    settings: Settings,
    title: str,
    transcript_text: str,
    out_dir: Path,
    on_progress=None,
    visual_notes: list[dict] | None = None,
    shots: list[dict] | None = None,
    visual_summary: str = "",
    draft_text: str = "",
) -> Path | None:
    """生成 retell.md；无 Key 时生成 prompt.md 并返回 None。

    visual_notes: [{"time","description"}]，来自“眼睛”功能。
    shots: 截图元数据，用于把 [[SHOT:...]] 替换成图片并在文末附图集。
    visual_summary: 眼睛代理分步分析的最终结论，会与逐帧观察一起进入最终 prompt。
    draft_text: 流式初稿全文；作为覆盖度参考，最终输出以完整转写为准。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_transcript(transcript_text)

    if not settings.llm_key:
        prompt = build_prompt(title, transcript_text)
        p = out_dir / "prompt.md"
        p.write_text(prompt, encoding="utf-8")
        return None

    budget = max_input_chars()
    notes: list[str] = []
    for c in chunks:
        if on_progress:
            on_progress("notes", c.index, len(chunks))
        user = (
            f"课程：{title}\n本段范围：{c.start_hint}–{c.end_hint}\n"
            f"<transcript>\n{c.text}\n</transcript>\n\n"
            "请输出这一段的课堂笔记。"
        )
        try:
            notes.append(_chat(settings, SYSTEM_NOTES, user))
        except ContextWindowError:
            short = fit_text(c.text, max(1500, len(c.text) // 2), f"chunk_retry_{c.index}")
            user = (
                f"课程：{title}\n本段范围：{c.start_hint}–{c.end_hint}\n"
                f"<transcript>\n{short}\n</transcript>\n\n"
                "请输出这一段的课堂笔记。"
            )
            notes.append(_chat(settings, SYSTEM_NOTES, user))

    notes_budget = max(2000, int(budget * (0.5 if draft_text else 0.65)))
    merged_notes = _compress_notes(settings, notes, notes_budget)

    draft_block = ""
    if draft_text:
        draft_block = (
            "<stream_draft>\n"
            + fit_text(draft_text, max(1000, int(budget * 0.25)), "stream_draft")
            + "\n</stream_draft>\n\n"
        )

    visual_parts: list[str] = []
    if visual_notes:
        lines = ["<visual_notes>"]
        for v in visual_notes:
            lines.append(f"[{v['time']}] {v['description']}")
        lines.append("</visual_notes>")
        visual_parts.append("\n".join(lines))
    if visual_summary:
        visual_parts.append(f"<visual_summary>\n{visual_summary}\n</visual_summary>")
    visual_block = ""
    if visual_parts:
        visual_block = fit_text(
            "\n\n".join(visual_parts) + "\n\n",
            max(1000, int(budget * 0.25)),
            "visual_notes",
        )

    def build_final(notes_payload: str) -> str:
        return (
            f"课程：{title}\n\n{draft_block}{visual_block}<notes>\n{notes_payload}\n</notes>\n\n"
            "请合并成完整重述。"
        )

    final_user = build_final(merged_notes)
    if len(final_user) > budget:
        merged_notes = fit_text(merged_notes, max(1000, int(budget * 0.5)), "final_fit")
        final_user = build_final(merged_notes)

    _trace(
        kind="final_prompt",
        chars=len(final_user),
        est_tokens=estimate_tokens(final_user),
        notes_chars=len(merged_notes),
        visual_chars=len(visual_block),
    )
    if on_progress:
        on_progress("final", 1, 1)
    try:
        merged = _chat(settings, SYSTEM_FINAL, final_user, max_tokens=8192)
    except ContextWindowError:
        merged_notes = fit_text(
            merged_notes, max(1500, len(merged_notes) // 2), "final_retry"
        )
        merged = _chat(settings, SYSTEM_FINAL, build_final(merged_notes), max_tokens=8192)

    if not merged.strip():
        raise LLMError("重述内容为空，未写入 retell.md")
    if shots:
        merged = render_shot_images(merged, shots)

    md = out_dir / "retell.md"
    md.write_text(
        f"# AI 重述：{title}\n\n> 由课堂录音转写自动生成，可能存在错误；请对照原视频核对公式与结论。\n\n{merged}\n",
        encoding="utf-8",
    )
    (out_dir / "notes_map.md").write_text("\n\n".join(notes), encoding="utf-8")
    return md
