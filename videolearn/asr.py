"""语音转写：本地 faster-whisper，或 OpenAI 兼容的云端 ASR。

云端后端只需要一个兼容 ``POST /audio/transcriptions`` 的服务：
- SiliconFlow：``https://api.siliconflow.cn/v1`` + ``FunAudioLLM/SenseVoiceSmall``
- Groq：``https://api.groq.com/openai/v1`` + ``whisper-large-v3-turbo``（建议 verbose_json）
- OpenAI：``https://api.openai.com/v1`` + ``whisper-1``
- 任何自建/代理的 OpenAI 兼容音频转写端点

本地模式仍是 faster-whisper；云端模式把每个音频块用 ffmpeg 编成 WAV 上传。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings

# 解码提示：偏向书面中文与术语，不改变“只转写听到的内容”这一约束
DEFAULT_PROMPT = (
    "以下是北京大学本科课堂录音。请用简体中文如实转写，保留专有名词、英文缩写与数学/专业术语。"
)

CLOUD_DEFAULT_BASE = "https://api.siliconflow.cn/v1"
CLOUD_DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B"
CLOUD_PROVIDERS = {
    "siliconflow": (CLOUD_DEFAULT_BASE, CLOUD_DEFAULT_MODEL),
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    "openai": ("https://api.openai.com/v1", "whisper-1"),
}


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


def _probe_duration(media_path: Path) -> float:
    from .media import find_ffmpeg, find_ffprobe

    ffprobe = find_ffprobe(find_ffmpeg())
    if not ffprobe:
        raise RuntimeError("云端分块需要 ffprobe（通常与 ffmpeg 一起安装）")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media_path)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"无法读取音频时长：{proc.stderr[:200]}") from exc


def _decode_range(media_path: Path, start: float, seconds: float, ffmpeg: str):
    import numpy as np

    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(media_path),
        "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _encode_wav(audio) -> bytes:
    """float32 16k mono → 16-bit PCM WAV bytes（OpenAI 兼容端点通用）。"""
    import numpy as np

    from .media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("云端 ASR 需要 ffmpeg 编码音频")
    pcm = np.asarray(audio, dtype=np.float32)
    if pcm.size == 0:
        return b""
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error",
        "-f", "f32le", "-ar", "16000", "-ac", "1", "-i", "-",
        "-f", "wav", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(cmd, input=pcm.tobytes(), capture_output=True, timeout=300)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg 编码云端音频失败：{proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def _clean_cloud_text(text: str) -> str:
    # SenseVoice 可能带 <|zh|><|NEUTRAL|> 之类标签
    text = re.sub(r"<\|[^|]*\|>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def split_text_segments(text: str, duration: float) -> list[Segment]:
    """云端只回文本时，按标点切成句，再按字数分配时间戳。

    时间戳是近似的（句级而不是词级），但足够生成 transcript.md / srt，
    也不会像"整块一个时间戳"那样丢掉分段结构。
    """
    text = _clean_cloud_text(text)
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if p.strip()]
    expanded: list[str] = []
    for part in parts:
        if len(part) <= 120:
            expanded.append(part)
            continue
        buf = ""
        for piece in re.split(r"(?<=[，,、：:])\s*", part):
            if not piece.strip():
                continue
            if buf and len(buf) + len(piece) > 100:
                expanded.append(buf.strip())
                buf = piece
            else:
                buf += piece
        if buf.strip():
            expanded.append(buf.strip())
    if not expanded:
        return []
    weights = [max(1, len(p)) for p in expanded]
    total = float(sum(weights))
    duration = max(0.0, float(duration or 0.0))
    out: list[Segment] = []
    cursor = 0.0
    for part, weight in zip(expanded, weights):
        end = cursor + duration * weight / total
        out.append(Segment(start=cursor, end=end, text=part))
        cursor = end
    return out


class AsrBackend(Protocol):
    name: str

    def transcribe(
        self,
        audio,
        *,
        language: str = "zh",
        beam_size: int = 1,
        vad: bool = True,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = False,
    ) -> list[Segment]: ...


class LocalWhisper:
    """faster-whisper，模型懒加载（流式只加载一次）。"""

    def __init__(self, model_name: str, threads: int = 8) -> None:
        self.model_name = model_name or "small"
        self.threads = max(1, min(16, int(threads or 8)))
        self._model = None

    @property
    def name(self) -> str:
        return f"local:{self.model_name}"

    def _ensure(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError(
                    "未安装 faster-whisper，请先执行: pip install -r requirements.txt"
                ) from e
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8", cpu_threads=self.threads
            )
        return self._model

    def transcribe(
        self,
        audio,
        *,
        language: str = "zh",
        beam_size: int = 1,
        vad: bool = True,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = False,
    ) -> list[Segment]:
        model = self._ensure()
        segments_iter, _info = model.transcribe(
            audio,
            language=language or "zh",
            beam_size=beam_size,
            vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 500} if vad else None,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt or DEFAULT_PROMPT,
        )
        out: list[Segment] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if text:
                out.append(Segment(float(seg.start), float(seg.end), text))
        return out


class CloudAsr:
    """OpenAI 兼容的 /audio/transcriptions 云端后端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 180.0,
        response_format: str = "json",
    ) -> None:
        self.base_url = (base_url or CLOUD_DEFAULT_BASE).rstrip("/")
        self.api_key = api_key.strip()
        self.model = model or CLOUD_DEFAULT_MODEL
        self.timeout = max(15.0, float(timeout or 180))
        self.response_format = (response_format or "json").strip()
        if not self.api_key:
            raise RuntimeError("云端 ASR 缺少 API Key（设置 ASR_API_KEY）")

    @property
    def name(self) -> str:
        return f"cloud:{self.model}"

    def _endpoint(self) -> str:
        return self.base_url + "/audio/transcriptions"

    def transcribe(
        self,
        audio,
        *,
        language: str = "zh",
        beam_size: int = 1,
        vad: bool = True,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = False,
    ) -> list[Segment]:
        import requests

        duration = len(audio) / 16000.0 if audio is not None else 0.0
        wav = _encode_wav(audio)
        if not wav:
            return []
        data = {"model": self.model}
        if language:
            data["language"] = language
        if self.response_format and self.response_format != "json":
            data["response_format"] = self.response_format
        # Whisper/Qwen 等端点支持 prompt；SenseVoice 不接受，跳过。
        if initial_prompt and "sensevoice" not in self.model.lower():
            data["prompt"] = initial_prompt

        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error = ""
        for attempt in range(3):
            files = {"file": ("audio.wav", wav, "audio/wav")}
            r = requests.post(
                self._endpoint(), headers=headers, data=data, files=files,
                timeout=(15, self.timeout),
            )
            if r.status_code == 200:
                return self._parse(r, duration)
            try:
                detail = r.json().get("error", {}).get("message") or r.text[:300]
            except ValueError:
                detail = r.text[:300]
            if r.status_code == 401:
                raise RuntimeError(f"云端 ASR API Key 无效：{detail}")
            if r.status_code == 402:
                raise RuntimeError(f"云端 ASR 余额不足（HTTP 402）：{detail}")
            last_error = f"HTTP {r.status_code}: {detail}"
            lowered = (detail or "").lower()
            # 有些端点不支持 response_format / language：去掉后重试一次。
            removed = False
            if "response_format" in data and ("response_format" in lowered or r.status_code in (400, 422)):
                data.pop("response_format", None)
                removed = True
            if "language" in data and ("language" in lowered or r.status_code in (400, 422)):
                data.pop("language", None)
                removed = True
            if "prompt" in data and ("prompt" in lowered or r.status_code in (400, 422)):
                data.pop("prompt", None)
                removed = True
            if not removed:
                break
        raise RuntimeError(f"云端 ASR 失败：{last_error}")

    @staticmethod
    def _parse(r, duration: float) -> list[Segment]:
        try:
            obj = r.json()
        except ValueError as exc:
            raise RuntimeError(f"云端 ASR 返回非 JSON：{r.text[:200]}") from exc
        segments = obj.get("segments") if isinstance(obj, dict) else None
        if isinstance(segments, list) and segments:
            out: list[Segment] = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                text = _clean_cloud_text(str(seg.get("text") or ""))
                if not text:
                    continue
                try:
                    start = float(seg.get("start") or 0.0)
                    end = float(seg.get("end") or start)
                except (TypeError, ValueError):
                    start, end = 0.0, duration
                out.append(Segment(start=max(0.0, start), end=max(start, end), text=text))
            if out:
                return out
        text = obj.get("text") if isinstance(obj, dict) else str(obj or "")
        return split_text_segments(str(text or ""), duration)


def make_backend(settings: Settings, *, threads: int = 8, streaming: bool = False) -> AsrBackend:
    mode = (settings.asr_backend or "local").strip().lower()
    if mode in ("cloud", "remote", "api", "siliconflow", "groq", "openai"):
        base = settings.asr_base_url
        model = settings.asr_model
        if mode in CLOUD_PROVIDERS:
            default_base, default_model = CLOUD_PROVIDERS[mode]
            base = base or default_base
            model = model or default_model
        fmt = settings.asr_response_format or "json"
        if ("groq" in (base or "").lower() or mode == "groq") and fmt == "json":
            fmt = "verbose_json"
        return CloudAsr(
            base, settings.asr_api_key, model,
            timeout=settings.asr_timeout,
            response_format=fmt,
        )
    return LocalWhisper(settings.whisper_model, threads=threads)


def transcribe(
    media_path: Path,
    model_name: str = "small",
    language: str = "zh",
    on_progress=None,
    time_offset: float = 0.0,
    initial_prompt: str | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[list[Segment], float]:
    """返回 (分段时间戳文本, 音频时长秒)。

    time_offset：素材在原始课程中的起点（配合 --start-seconds），用于让时间戳对齐原课。
    传入 settings 且 asr_backend=cloud 时，按 ASR_CHUNK_SECONDS 分块上传云端；
    本地模式保持整段解码。
    """
    if settings is not None and (settings.asr_backend or "local").lower() not in (
        "local", "", "whisper", "faster-whisper",
    ):
        backend = make_backend(settings, threads=min(16, os.cpu_count() or 4))
        duration = _probe_duration(Path(media_path))
        chunk = max(30.0, float(settings.asr_chunk_seconds or 300.0))
        segments: list[Segment] = []
        pos = 0.0
        from .media import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("云端 ASR 需要 ffmpeg 解码音频")
        while pos < duration - 0.2:
            take = min(chunk, duration - pos)
            audio = _decode_range(Path(media_path), pos, take, ffmpeg)
            part = backend.transcribe(
                audio,
                language=language or "zh",
                initial_prompt=initial_prompt,
            )
            for seg in part:
                segments.append(
                    Segment(
                        start=seg.start + time_offset + pos,
                        end=seg.end + time_offset + pos,
                        text=seg.text,
                    )
                )
            pos += take
            if on_progress:
                on_progress(len(segments), min(pos, duration), duration)
        return segments, duration

    backend = LocalWhisper(model_name, threads=min(16, os.cpu_count() or 4))
    audio = _load_audio(Path(media_path))
    duration = float(len(audio)) / 16000.0
    segments = backend.transcribe(
        audio,
        language=language or "zh",
        beam_size=5,
        vad=True,
        initial_prompt=initial_prompt,
        condition_on_previous_text=True,
    )
    for i, seg in enumerate(segments, 1):
        seg.start += time_offset
        seg.end += time_offset
        if on_progress:
            on_progress(i, seg.end - time_offset, duration)
    return segments, duration


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
