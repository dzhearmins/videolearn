"""流式 ASR + LLM 重述基准。

用法：
  $env:PYTHONPATH='D:\\projects\\videolearn'; $env:PYTHONIOENCODING='utf-8'
  python -m videolearn.stream_bench --video "<mp4>" --chunk-seconds 240 --model base

产出：视频同目录 stream_bench.jsonl / stream_bench.md
"""

from __future__ import annotations

import argparse
import json
import platform
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
import requests

from .config import Settings, load_settings
from .media import find_ffmpeg, find_ffprobe

SYSTEM_RESTATE = (
    "你是课堂助教。把这段课堂转写改写成连贯、忠实的中文重述：保留定义、定理、公式（LaTeX）"
    "与关键推导，删除口语和重复；不要编造转写里没有的内容。只输出本段重述，不要标题。"
)


def _now() -> float:
    return time.monotonic()


def decode_chunk(video: Path, start: float, seconds: float, ffmpeg: str) -> np.ndarray:
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(video),
        "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
    return np.frombuffer(proc.stdout, dtype=np.float32)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 6]


def norm(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】]", "", text)


def dedupe_first_sentence(text: str, prev_tail: str) -> tuple[str, bool]:
    sents = split_sentences(text)
    if not sents or not prev_tail:
        return text, False
    head = norm(sents[0])[:40]
    if head and head in norm(prev_tail):
        rest = text.replace(sents[0], "", 1).strip()
        if rest:  # 不能把唯一一句删空
            return rest, True
    return text, False


def post_json(
    settings: Settings,
    model: str,
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    reasoning_effort: str | None = None,
    disable_thinking: bool = False,
):
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    elif reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    return requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.llm_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(15, 600),
        stream=stream,
    )


def _messages(chunk_text: str, prev_tail: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_RESTATE},
        {
            "role": "user",
            "content": (
                f"上一段结尾（仅用于衔接，不要重复）：\n{prev_tail[-300:]}\n\n"
                f"本段转写：\n{chunk_text}\n\n请输出这一段的重述。"
            ),
        },
    ]


def stream_restate(
    settings: Settings,
    model: str,
    chunk_text: str,
    prev_tail: str,
    reasoning_effort: str | None = None,
    disable_thinking: bool = False,
) -> tuple[str, float | None, float | None, float | None, str]:
    messages = _messages(chunk_text, prev_tail)
    first_content: float | None = None
    first_any: float | None = None
    finish = ""
    parts: list[str] = []
    with post_json(
        settings, model, messages, 8192, True, reasoning_effort, disable_thinking
    ) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            data = line[5:].strip() if line.startswith("data:") else ""
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content") and first_any is None:
                    first_any = _now()
                piece = delta.get("content") or ""
                if piece:
                    if first_content is None:
                        first_content = _now()
                    if first_any is None:
                        first_any = first_content
                    parts.append(piece)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            except (json.JSONDecodeError, AttributeError):
                continue
    return "".join(parts).strip(), first_content, first_any, _now(), finish


def batch_restate(
    settings: Settings,
    model: str,
    chunk_text: str,
    prev_tail: str,
    reasoning_effort: str | None = None,
    disable_thinking: bool = False,
) -> tuple[str, float, str]:
    t0 = _now()
    r = post_json(
        settings, model, _messages(chunk_text, prev_tail), 8192, False,
        reasoning_effort, disable_thinking,
    )
    dt = _now() - t0
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    return (
        ((choice.get("message") or {}).get("content") or "").strip(),
        dt,
        choice.get("finish_reason") or "",
    )


@dataclass
class Event:
    idx: int
    start: float
    end: float
    text: str = ""
    t_asr_start: float = 0.0
    t_asr_end: float = 0.0
    t_llm_first: float | None = None
    t_llm_first_reasoning: float | None = None
    t_llm_end: float | None = None
    chars_in: int = 0
    chars_out: int = 0
    deduped: bool = False
    first_before_next: bool | None = None
    reasoning_before_next: bool | None = None
    final_text: str = ""
    baseline_text: str = ""
    baseline_seconds: float = 0.0
    baseline_finish: str = ""
    extra: dict = field(default_factory=dict)


class RssSampler:
    def __init__(self) -> None:
        self.peak = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        proc = psutil.Process()
        while not self.stop.is_set():
            try:
                self.peak = max(self.peak, proc.memory_info().rss)
            except psutil.Error:
                pass
            self.stop.wait(0.5)

    def start(self) -> "RssSampler":
        self.thread.start()
        return self

    def finish(self) -> int:
        self.stop.set()
        self.thread.join(timeout=2)
        return self.peak


def _sql_row(e: Event, t0: float) -> dict:
    return {
        "idx": e.idx,
        "chunk_start_sec": round(e.start, 1),
        "chunk_end_sec": round(e.end, 1),
        "t_asr_start": round(e.t_asr_start - t0, 3),
        "t_asr_end": round(e.t_asr_end - t0, 3),
        "t_llm_first_token": round(e.t_llm_first - t0, 3) if e.t_llm_first else None,
        "t_llm_first_reasoning": round(e.t_llm_first_reasoning - t0, 3)
        if e.t_llm_first_reasoning
        else None,
        "t_llm_end": round(e.t_llm_end - t0, 3) if e.t_llm_end else None,
        "chars_in": e.chars_in,
        "chars_out": e.chars_out,
        "first_token_before_next_asr_end": e.first_before_next,
        "reasoning_before_next_asr_end": e.reasoning_before_next,
        "deduped_leading_sentence": e.deduped,
        "baseline_chars": len(e.baseline_text),
        "baseline_seconds": round(e.baseline_seconds, 3),
        "baseline_finish": e.baseline_finish,
        "stream_text": e.final_text,
        "baseline_text": e.baseline_text,
    }


def run(args) -> int:
    video = Path(args.video).resolve()
    if not video.exists():
        raise SystemExit(f"视频不存在：{video}")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("需要 ffmpeg")
    settings = load_settings()
    if not settings.llm_key:
        raise SystemExit("需要 DEEPSEEK_API_KEY")
    model = args.llm_model or settings.llm_model

    ffprobe = find_ffprobe(ffmpeg)
    if not ffprobe:
        raise SystemExit("需要 ffprobe（通常与 ffmpeg 一起安装）")
    dur = float(
        subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    step = max(30.0, args.chunk_seconds - args.overlap_seconds)
    starts = []
    s = float(args.start_seconds)
    while s < dur - 1:
        starts.append(s)
        s += step
    if args.max_chunks:
        starts = starts[: args.max_chunks]

    out_dir = video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "stream_bench.jsonl"
    md = out_dir / "stream_bench.md"
    print(
        f"[bench] video={video.name} D={dur:.0f}s chunks={len(starts)} "
        f"chunk={args.chunk_seconds}s overlap={args.overlap_seconds}s asr={args.model} llm={model}",
        flush=True,
    )

    t0 = _now()
    rss = RssSampler().start()
    q: queue.Queue = queue.Queue(maxsize=1)
    events: list[Event] = []
    lock = threading.Lock()

    def asr_worker() -> None:
        from faster_whisper import WhisperModel

        wat = _now()
        wm = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
        print(f"[asr] model loaded in {_now() - wat:.1f}s", flush=True)
        for i, start in enumerate(starts):
            take = min(args.chunk_seconds + args.overlap_seconds, dur - start)
            if take <= 0.5:
                break
            t_asr_start = _now()
            audio = decode_chunk(video, start, take, ffmpeg)
            segs, _info = wm.transcribe(
                audio,
                language="zh",
                beam_size=1,
                vad_filter=args.vad,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
            )
            parts = []
            for seg in segs:
                if i > 0 and seg.start < args.overlap_seconds:
                    continue
                parts.append(seg.text.strip())
            text = "".join(parts)
            ev = Event(
                idx=i,
                start=start,
                end=min(start + args.chunk_seconds, dur),
                text=text,
                t_asr_start=t_asr_start,
                t_asr_end=_now(),
                chars_in=len(text),
            )
            with lock:
                events.append(ev)
            print(
                f"[asr] #{i} {start:.0f}-{ev.end:.0f}s "
                f"asr={ev.t_asr_end - ev.t_asr_start:.1f}s "
                f"rtf={(ev.t_asr_end - ev.t_asr_start) / max(1.0, ev.end - start):.3f} "
                f"chars={len(text)}",
                flush=True,
            )
            q.put(ev)
        q.put(None)

    def llm_worker() -> None:
        prev_tail = ""
        while True:
            ev = q.get()
            if ev is None:
                break
            if not ev.text.strip():
                ev.t_llm_end = ev.t_asr_end
                ev.extra["finish"] = "empty"
                ev.extra["prev_tail"] = prev_tail
                print(f"[llm] #{ev.idx} skip empty", flush=True)
                continue
            ev.extra["prev_tail"] = prev_tail
            text, first, first_any, end, finish = stream_restate(
                settings, model, ev.text, prev_tail, args.reasoning_effort,
                args.thinking == "disabled",
            )
            text, deduped = dedupe_first_sentence(text, prev_tail)
            ev.t_llm_first = first
            ev.t_llm_first_reasoning = first_any
            ev.t_llm_end = end
            ev.chars_out = len(text)
            ev.deduped = deduped
            ev.final_text = text
            ev.extra["finish"] = finish
            prev_tail = (prev_tail + "\n" + text)[-args.context_chars:]
            print(
                f"[llm] #{ev.idx} first={((first - t0) if first else -1):.1f}s "
                f"end={(end - t0):.1f}s chars={len(text)} dedup={deduped}",
                flush=True,
            )

    ta = threading.Thread(target=asr_worker, daemon=True)
    tl = threading.Thread(target=llm_worker, daemon=True)
    ta.start()
    tl.start()
    ta.join()
    tl.join()
    stream_wall = _now() - t0

    for i, ev in enumerate(events):
        if i + 1 < len(events):
            if ev.t_llm_first is not None:
                ev.first_before_next = ev.t_llm_first < events[i + 1].t_asr_end
            if ev.t_llm_first_reasoning is not None:
                ev.reasoning_before_next = (
                    ev.t_llm_first_reasoning < events[i + 1].t_asr_end
                )

    prev_tail = ""
    base_t0 = _now()
    for ev in events:
        if not ev.text.strip():
            ev.baseline_text = ""
            ev.baseline_finish = "empty"
            continue
        text, dt, finish = batch_restate(
            settings, model, ev.text, ev.extra.get("prev_tail", ""), args.reasoning_effort,
            args.thinking == "disabled",
        )
        ev.baseline_text = text
        ev.baseline_seconds = dt
        ev.baseline_finish = finish
        print(f"[base] #{ev.idx} {dt:.1f}s chars={len(text)}", flush=True)
    base_llm_wall = _now() - base_t0
    peak_rss = rss.finish()

    rows = [_sql_row(e, t0) for e in events]
    with jsonl.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    stream_text = "\n\n".join(e.final_text for e in events)
    baseline_text = "\n\n".join(e.baseline_text for e in events)
    (out_dir / "stream_restated.md").write_text(stream_text, encoding="utf-8")
    (out_dir / "batch_restated.md").write_text(baseline_text, encoding="utf-8")

    overlap_n = sum(1 for e in events if e.first_before_next)
    overlap_den = sum(1 for e in events if e.first_before_next is not None)
    overlap_pct = overlap_n * 100 / max(1, overlap_den)
    reasoning_overlap_n = sum(1 for e in events if e.reasoning_before_next)
    reasoning_overlap_den = sum(1 for e in events if e.reasoning_before_next is not None)
    reasoning_overlap_pct = reasoning_overlap_n * 100 / max(1, reasoning_overlap_den)
    dedup_n = sum(1 for e in events if e.deduped)

    def bigrams(s: str) -> set[str]:
        s = norm(s)
        return {s[i : i + 2] for i in range(max(0, len(s) - 1))}

    def jaccard(a: str, b: str) -> float:
        ba, bb = bigrams(a), bigrams(b)
        if not ba or not bb:
            return 0.0
        return len(ba & bb) / len(ba | bb)

    lost: list[str] = []
    lost_loose: list[str] = []
    lost_loose_meta = 0
    base_sent_total = 0
    cover_sum = 0.0
    cover_n = 0
    meta_kw = ("转写", "无法", "请提供", "内容不完整", "语义不清", "无法忠实")
    for e in events:
        base_sents = split_sentences(e.baseline_text)
        base_sent_total += len(base_sents)
        stream_bg = bigrams(e.final_text)
        for s in base_sents:
            sb = bigrams(s)
            cover = len(sb & stream_bg) / max(1, len(sb))
            cover_sum += cover
            cover_n += 1
            if cover < 0.6:
                lost.append(s)
            if cover < 0.4:
                lost_loose.append(s)
                if any(k in s for k in meta_kw):
                    lost_loose_meta += 1
    avg_cover = cover_sum / max(1, cover_n)

    all_stream_sents = [s for e in events for s in split_sentences(e.final_text)]
    seen: set[str] = set()
    dup_lines = 0
    for s in all_stream_sents:
        key = norm(s)
        if key in seen:
            dup_lines += 1
        else:
            seen.add(key)
    dup_rate = dup_lines * 100 / max(1, len(all_stream_sents))
    length_dev = (len(stream_text) - len(baseline_text)) / max(1, len(baseline_text)) * 100

    asr_sum = sum(e.t_asr_end - e.t_asr_start for e in events)
    baseline_total = asr_sum + base_llm_wall
    vm = psutil.virtual_memory()
    cpu = platform.processor() or platform.uname().processor or "unknown"

    def fmt_t(x: float | None) -> str:
        return "-" if x is None else f"{x:.1f}"

    lines = [
        f"# 流式 ASR+LLM 重述基准：{video.name}",
        "",
        "## 环境与配置",
        "",
        f"- 视频时长 D：{dur:.1f}s（{dur / 60:.1f} 分钟）",
        f"- 分块：{args.chunk_seconds:.0f}s，重叠：{args.overlap_seconds:.0f}s，块数：{len(events)}",
        f"- ASR：faster-whisper `{args.model}`，int8，{args.threads} 线程，VAD={args.vad}",
        f"- LLM：`{model}`（stream=True，逐 delta）",
        f"- CPU：{cpu}，{psutil.cpu_count(logical=True)} 逻辑核，RAM {vm.total / 1e9:.1f}GB",
        "",
        "## 汇总",
        "",
        "| 指标 | 流式 | 批式基线 |",
        "|---|---|---|",
        f"| 总耗时（s） | {stream_wall:.1f} | {baseline_total:.1f} |",
        f"| 总耗时 / 视频时长 | {stream_wall / dur * 100:.1f}% | {baseline_total / dur * 100:.1f}% |",
        f"| 输出字符数 | {len(stream_text)} | {len(baseline_text)} |",
        f"| 长度偏差 | — | {length_dev:+.1f}% |",
        "",
        f"- 首正文 token 早于下一块 ASR 结束：**{overlap_n}/{overlap_den}（{overlap_pct:.0f}%）**",
        f"- 首 reasoning token 早于下一块 ASR 结束：**{reasoning_overlap_n}/{reasoning_overlap_den}（{reasoning_overlap_pct:.0f}%）**",
        f"- 重叠首句去重：{dedup_n} 次；重复句占比：去重前 {dup_lines + dedup_n}/{len(all_stream_sents) + dedup_n}（{(dup_lines + dedup_n) / max(1, len(all_stream_sents) + dedup_n) * 100:.2f}%）→ 去重后 {dup_lines}/{len(all_stream_sents)}（{dup_rate:.2f}%）",
        f"- 批式有、流式无的句子（字符 bigram 覆盖率<0.6）：**{len(lost)}/{base_sent_total}**，平均覆盖率 {avg_cover * 100:.1f}%",
        f"- 更宽松阈值（覆盖率<0.4）：**{len(lost_loose)}/{base_sent_total}**，其中模型对转写质量的元评论 {lost_loose_meta} 句",
        f"- 峰值 RSS：{peak_rss / 1e9:.2f}GB",
        f"- ASR 纯耗时合计：{asr_sum:.1f}s；批式 LLM 合计：{base_llm_wall:.1f}s",
        "",
    ]
    if lost:
        lines += ["丢失句示例：", ""]
        lines += [f"- {s[:100]}" for s in lost[:5]]
        lines.append("")
    lines += [
        "## 分块时间戳（相对本次运行开始，秒）",
        "",
        "| # | 区间 | asr_start | asr_end | llm_any | llm_first | llm_end | in | out | 正文早于下一块 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['idx']} | {row['chunk_start_sec']:.0f}-{row['chunk_end_sec']:.0f}s | "
            f"{row['t_asr_start']:.1f} | {row['t_asr_end']:.1f} | "
            f"{fmt_t(row['t_llm_first_reasoning'])} | {fmt_t(row['t_llm_first_token'])} | "
            f"{fmt_t(row['t_llm_end'])} | {row['chars_in']} | {row['chars_out']} | "
            f"{'Y' if row['first_token_before_next_asr_end'] else 'N'} |"
        )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        f"[bench] wall={stream_wall:.1f}s ratio={stream_wall / dur * 100:.1f}% "
        f"baseline={baseline_total:.1f}s overlap_content={overlap_n}/{overlap_den} "
        f"overlap_reasoning={reasoning_overlap_n}/{reasoning_overlap_den} "
        f"dup={dup_rate:.2f}% lost={len(lost)}/{base_sent_total} "
        f"cover={avg_cover * 100:.1f}% rss={peak_rss / 1e9:.2f}GB",
        flush=True,
    )
    print(f"[bench] {jsonl}", flush=True)
    print(f"[bench] {md}", flush=True)
    return 0


def main() -> int:
    from .term import soften_console

    soften_console()
    p = argparse.ArgumentParser(description="流式 ASR+LLM 重述基准")
    p.add_argument("--video", required=True)
    p.add_argument("--chunk-seconds", type=float, default=240)
    p.add_argument("--start-seconds", type=float, default=0.0)
    p.add_argument("--overlap-seconds", type=float, default=5)
    p.add_argument("--context-chars", type=int, default=600)
    p.add_argument("--model", default="base", help="faster-whisper 模型")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--llm-model", default="")
    p.add_argument("--reasoning-effort", default="", choices=["", "low", "high", "max"])
    p.add_argument("--thinking", default="disabled", choices=["disabled", "default"])
    p.add_argument("--no-vad", dest="vad", action="store_false", help="关闭 VAD")
    p.set_defaults(vad=True)
    p.add_argument("--max-chunks", type=int, default=0, help="仅用于冒烟测试")
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
