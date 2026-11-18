"""默认流式：ASR 与初稿重述并发，边转写边出稿并落盘中间产物。

产物：
- transcript.partial.md：ASR 边产边写的文字稿（带崩溃恢复价值）
- transcript.md/.srt/.txt：完整文字稿（流式结束后由同一批 segments 写出）
- stream_restated.md：流式初稿（final_pass 之后会被标记 superseded）
- stream_events.jsonl：每块 asr/llm 计时
- stream_metrics.json：首 token 与流式总时长

注意：final_pass 不在此模块内调用，避免二次总结污染流式输出。
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from . import asr, llm
from .config import Settings
from .media import find_ffmpeg, find_ffprobe

SYSTEM_RESTATE = (
    "你是课堂助教。把这段课堂转写改写成连贯、忠实的中文重述：保留定义、定理、公式（LaTeX）"
    "与关键推导，删除口语和重复；不要编造转写里没有的内容。只输出本段重述，不要标题。"
)


@dataclass
class StreamResult:
    transcript_md: Path
    transcript_srt: Path
    transcript_txt: Path
    partial_md: Path
    draft_md: Path
    events_jsonl: Path
    metrics_json: Path
    chunks: int
    first_token_sec: float | None
    stream_duration_sec: float
    asr_seconds: float
    duration_sec: float
    time_offset: float


def _now() -> float:
    return time.monotonic()


def _fmt(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


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


def dedupe_first_sentence(text: str, prev_tail: str) -> tuple[str, bool]:
    parts = [
        p.strip()
        for p in re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
        if len(p.strip()) >= 6
    ]
    if not parts or not prev_tail:
        return text, False
    strip_re = r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】]"
    head = re.sub(strip_re, "", parts[0])[:40]
    tail = re.sub(strip_re, "", prev_tail)
    if head and head in tail:
        rest = text.replace(parts[0], "", 1).strip()
        if rest:
            return rest, True
    return text, False


def stream_restate(
    settings: Settings, chunk_text: str, prev_tail: str
) -> tuple[str, float | None, float, str]:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_RESTATE},
            {
                "role": "user",
                "content": (
                    f"上一段结尾（仅用于衔接，不要重复）：\n{prev_tail[-300:]}\n\n"
                    f"本段转写：\n{chunk_text}\n\n请输出这一段的重述。"
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 8192,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    first: float | None = None
    finish = ""
    parts: list[str] = []
    with requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.llm_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(15, 600),
        stream=True,
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
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    if first is None:
                        first = _now()
                    parts.append(piece)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            except (json.JSONDecodeError, AttributeError):
                continue
    return "".join(parts).strip(), first, _now(), finish


def run_streaming(
    settings: Settings,
    video_path: Path,
    out_dir: Path,
    title: str,
    start_seconds: float = 0.0,
    limit_seconds: float | None = None,
    chunk_seconds: float = 240.0,
    overlap_seconds: float = 5.0,
    threads: int = 8,
    on_progress=None,
) -> StreamResult:
    """并发跑 ASR 与初稿重述；返回完整文字稿、初稿与计时。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("流式需要 ffmpeg")
    if not settings.llm_key:
        raise RuntimeError("流式需要 LLM Key")

    ffprobe = find_ffprobe(ffmpeg)
    if not ffprobe:
        raise RuntimeError("流式需要 ffprobe（通常与 ffmpeg 一起安装）")
    total_dur = float(
        subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    time_offset = float(start_seconds or 0.0)
    # 下载时若指定了 start_seconds，视频文件本身已从该点裁剪，解码要从 0 开始；
    # 时间戳则加上 time_offset，保持与原课一致。
    local_limit = float(limit_seconds) if limit_seconds else total_dur
    end_limit = min(local_limit, total_dur)
    step = max(30.0, chunk_seconds - overlap_seconds)
    starts: list[float] = []
    s = 0.0
    while s < end_limit - 1:
        starts.append(s)
        s += step

    transcript_md = out_dir / "transcript.md"
    transcript_srt = out_dir / "transcript.srt"
    transcript_txt = out_dir / "transcript.txt"
    partial_md = out_dir / "transcript.partial.md"
    draft_md = out_dir / "stream_restated.md"
    events_jsonl = out_dir / "stream_events.jsonl"
    metrics_json = out_dir / "stream_metrics.json"

    t0 = _now()
    q: queue.Queue = queue.Queue(maxsize=1)
    events: dict[int, dict] = {}
    segments: list[asr.Segment] = []
    first_token: float | None = None
    errors: list[str] = []
    lock = threading.Lock()

    def asr_body() -> None:
        from faster_whisper import WhisperModel

        wm = WhisperModel(
            settings.whisper_model, device="cpu", compute_type="int8", cpu_threads=threads
        )
        partial_md.write_text(f"# 流式文字稿：{title}\n\n", encoding="utf-8")
        for i, chunk_start in enumerate(starts):
            take = min(chunk_seconds + overlap_seconds, end_limit - chunk_start)
            if take <= 0.5:
                break
            t_asr = _now()
            audio = decode_chunk(video_path, chunk_start, take, ffmpeg)
            segs, _info = wm.transcribe(
                audio,
                language="zh",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                initial_prompt=settings.asr_prompt or None,
            )
            parts: list[str] = []
            for seg in segs:
                if i > 0 and seg.start < overlap_seconds:
                    continue
                text = (seg.text or "").strip()
                if not text:
                    continue
                abs_start = time_offset + chunk_start + float(seg.start)
                abs_end = time_offset + chunk_start + float(seg.end)
                segments.append(asr.Segment(start=abs_start, end=abs_end, text=text))
                parts.append(text)
                with partial_md.open("a", encoding="utf-8") as f:
                    f.write(f"[{_fmt(abs_start)}] {text}\n\n")
            chunk_text = "".join(parts)
            t_end = _now()
            ev = {
                "idx": i,
                "chunk_start": chunk_start,
                "chunk_end": min(chunk_start + chunk_seconds, end_limit),
                "t_asr_start": t_asr,
                "t_asr_end": t_end,
                "chars_in": len(chunk_text),
                "text": chunk_text,
            }
            with lock:
                events[i] = ev
            print(
                f"[asr] #{i} {chunk_start:.0f}-{ev['chunk_end']:.0f}s "
                f"asr={t_end - t_asr:.1f}s chars={len(chunk_text)}",
                flush=True,
            )
            q.put(ev)

    def asr_worker() -> None:
        try:
            asr_body()
        except Exception as e:  # noqa: BLE001
            errors.append(f"asr: {type(e).__name__}: {e}")
            print(f"[asr] 失败：{type(e).__name__}: {e}", flush=True)
        finally:
            q.put(None)

    def llm_worker() -> None:
        nonlocal first_token
        prev_tail = ""
        draft_md.write_text(f"# 流式阶段草稿（仅供 final_pass 参考）：{title}\n\n", encoding="utf-8")
        while True:
            ev = q.get()
            if ev is None:
                break
            if not ev["text"].strip():
                ev["t_llm_first"] = None
                ev["t_llm_end"] = _now()
                ev["chars_out"] = 0
                continue
            try:
                text, first, end, finish = stream_restate(
                    settings, ev["text"], prev_tail
                )
            except Exception as e:  # noqa: BLE001
                errors.append(f"llm #{ev['idx']}: {type(e).__name__}: {e}")
                print(f"[llm] #{ev['idx']} 失败：{type(e).__name__}: {e}", flush=True)
                ev["t_llm_first"] = None
                ev["t_llm_end"] = _now()
                ev["chars_out"] = 0
                ev["finish"] = f"error:{type(e).__name__}"
                continue
            text, deduped = dedupe_first_sentence(text, prev_tail)
            ev["t_llm_first"] = first
            ev["t_llm_end"] = end
            ev["chars_out"] = len(text)
            ev["finish"] = finish
            ev["deduped"] = deduped
            if first is not None:
                with lock:
                    if first_token is None or first < first_token:
                        first_token = first
            prev_tail = (prev_tail + "\n" + text)[-600:]
            with draft_md.open("a", encoding="utf-8") as f:
                f.write(
                    f"## {_fmt(time_offset + ev['chunk_start'])}–"
                    f"{_fmt(time_offset + ev['chunk_end'])}\n\n{text}\n\n"
                )
            print(
                f"[llm] #{ev['idx']} first={((first - t0) if first else -1):.1f}s "
                f"end={(end - t0):.1f}s chars={len(text)}",
                flush=True,
            )
            if on_progress:
                on_progress(ev["idx"] + 1, len(starts))

    ta = threading.Thread(target=asr_worker, daemon=True)
    tl = threading.Thread(target=llm_worker, daemon=True)
    ta.start()
    tl.start()
    ta.join()
    tl.join()
    stream_end = _now()

    if errors:
        print(f"[stream] 警告：{len(errors)} 个错误；首个：{errors[0]}", flush=True)
    if not segments:
        detail = f"（{errors[0]}）" if errors else ""
        raise RuntimeError(f"流式转写没有识别到任何语音内容{detail}")
    clip_duration = max(1.0, end_limit)
    paths = asr.write_transcripts(segments, out_dir, title, clip_duration)

    ordered = [events[i] for i in sorted(events)]
    with events_jsonl.open("w", encoding="utf-8") as f:
        for ev in ordered:
            f.write(
                json.dumps(
                    {
                        "idx": ev["idx"],
                        "chunk_start_sec": round(ev["chunk_start"], 1),
                        "chunk_end_sec": round(ev["chunk_end"], 1),
                        "t_asr_start": round(ev["t_asr_start"] - t0, 3),
                        "t_asr_end": round(ev["t_asr_end"] - t0, 3),
                        "t_llm_first_token": round(ev["t_llm_first"] - t0, 3)
                        if ev.get("t_llm_first")
                        else None,
                        "t_llm_end": round(ev["t_llm_end"] - t0, 3)
                        if ev.get("t_llm_end")
                        else None,
                        "chars_in": ev["chars_in"],
                        "chars_out": ev.get("chars_out", 0),
                        "deduped_leading_sentence": ev.get("deduped", False),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    asr_sec = sum(ev["t_asr_end"] - ev["t_asr_start"] for ev in ordered)
    metrics = {
        "chunks": len(ordered),
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "asr_model": settings.whisper_model,
        "llm_model": settings.llm_model,
        "first_token_sec": round(first_token - t0, 3) if first_token else None,
        "stream_duration_sec": round(stream_end - t0, 3),
        "asr_seconds": round(asr_sec, 3),
        "duration_sec": round(clip_duration, 1),
        "time_offset": time_offset,
    }
    metrics_json.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    return StreamResult(
        transcript_md=paths["md"],
        transcript_srt=paths["srt"],
        transcript_txt=paths["txt"],
        partial_md=partial_md,
        draft_md=draft_md,
        events_jsonl=events_jsonl,
        metrics_json=metrics_json,
        chunks=len(ordered),
        first_token_sec=metrics["first_token_sec"],
        stream_duration_sec=metrics["stream_duration_sec"],
        asr_seconds=metrics["asr_seconds"],
        duration_sec=clip_duration,
        time_offset=time_offset,
    )
