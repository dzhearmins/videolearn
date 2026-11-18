"""语音转写：faster-whisper（CPU int8）。

无 GPU 的机器上默认 small；可用 VIDEOLRN_WHISPER_MODEL=medium 提升质量（更慢）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 解码提示：偏向书面中文与术语，不改变“只转写听到的内容”这一约束
DEFAULT_PROMPT = (
    "以下是北京大学本科课堂录音。请用简体中文如实转写，保留专有名词、英文缩写与数学/专业术语。"
)


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _fmt_ts(seconds: float, srt: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _load_audio(media_path: Path):
    """用 ffmpeg 解码为 16kHz 单声道 float32。

    不依赖 PyAV：faster-whisper 1.2.1 与 PyAV 19 的 open() 签名不兼容。
    """
    import numpy as np

    from .media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ASR 需要 ffmpeg 解码音频。请安装 FFmpeg 后重试："
            "winget install --id Gyan.FFmpeg -e --source winget"
        )
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error",
        "-i", str(media_path),
        "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"ffmpeg 解码音频失败: {msg}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def transcribe(
    media_path: Path,
    model_name: str = "small",
    language: str = "zh",
    on_progress=None,
    time_offset: float = 0.0,
    initial_prompt: str | None = None,
) -> tuple[list[Segment], float]:
    """返回 (分段时间戳文本, 音频时长秒)。

    time_offset：素材在原始课程中的起点（配合 --start-seconds），用于让时间戳对齐原课。
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("未安装 faster-whisper，请先执行: pip install -r requirements.txt") from e

    threads = min(16, os.cpu_count() or 4)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    audio = _load_audio(media_path)
    segments_iter, info = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=initial_prompt or DEFAULT_PROMPT,
        condition_on_previous_text=True,
    )

    segments: list[Segment] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start=float(seg.start) + time_offset,
                end=float(seg.end) + time_offset,
                text=text,
            )
        )
        if on_progress:
            on_progress(len(segments), float(seg.end), float(info.duration or 0))

    return segments, float(info.duration or 0)


def write_transcripts(segments: list[Segment], out_dir: Path, title: str, duration: float) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "transcript.md"
    srt = out_dir / "transcript.srt"
    txt = out_dir / "transcript.txt"

    lines = [f"# 课堂文字稿：{title}", ""]
    if segments and segments[0].start > 1.0:
        lines.append(f"片段：从 {_fmt_ts(segments[0].start)} 起，时长 {_fmt_ts(duration)}")
    else:
        lines.append(f"时长：{_fmt_ts(duration)}")
    lines.append("")
    for s in segments:
        lines.append(f"[{_fmt_ts(s.start)}] {s.text}")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")

    srt_lines = []
    for i, s in enumerate(segments, 1):
        srt_lines += [str(i), f"{_fmt_ts(s.start, True)} --> {_fmt_ts(s.end, True)}", s.text, ""]
    srt.write_text("\n".join(srt_lines), encoding="utf-8")

    txt.write_text("\n".join(s.text for s in segments), encoding="utf-8")

    return {"md": md, "srt": srt, "txt": txt}
