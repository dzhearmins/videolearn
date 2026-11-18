"""编排：回放页面 → 媒体文件 → 文字稿 → AI 重述。"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import asr, blackboard, llm, media, streaming, vision
from .config import Settings
from .session import BBClient


def safe_name(name: str, fallback: str = "未命名") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return (name or fallback)[:80]


def token_hint(play_url: str) -> dict:
    """从 playVideo URL 的 JWT payload 读取 recordTime 等（仅解码，不校验/伪造）。"""
    try:
        token = dict(
            part.split("=", 1) for part in urlparse(play_url).query.split("&") if "=" in part
        ).get("token", "")
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@dataclass
class RunResult:
    out_dir: Path
    media: Path
    transcript_md: Path
    srt: Path
    retell_md: Path | None
    prompt_md: Path | None
    eyes_analysis: Path | None = None


def _display_title(title: str) -> str:
    """教学网无标题的回放首列是“时间: 2026-11-03 10:10:00”，避免用它当标题。"""
    if re.match(r"^时间\s*[:：]", title.strip()):
        return "课堂回放"
    return title.strip()


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def run_for_video(
    client: BBClient,
    settings: Settings,
    *,
    title: str,
    time_text: str,
    play_url: str,
    course_name: str = "课程",
    keep_video: bool = True,
    limit_seconds: float | None = None,
    start_seconds: float | None = None,
    eyes: bool = False,
    max_shots: int = 8,
    max_steps: int = 4,
    reuse_eyes: bool = False,
    stream: bool = True,
    chunk_seconds: float = 240.0,
    overlap_seconds: float = 5.0,
    asr_threads: int = 8,
) -> RunResult:
    pipeline_t0 = time.monotonic()
    stamp = time_text.strip().replace(":", "").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d_%H%M")
    title = _display_title(title)
    if limit_seconds:
        suffix = f"_sample{int(start_seconds or 0)}+{int(limit_seconds)}s"
    else:
        suffix = ""
    out_dir = settings.output_dir / safe_name(course_name) / f"{safe_name(stamp)}_{safe_name(title, '回放')}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    llm.reset_trace()

    steps = 5 if eyes else 4
    counter = {"n": 0}

    def stage(label: str) -> None:
        counter["n"] += 1
        print(f"\n[{counter['n']}/{steps}] {label}", end="", flush=True)
        print(" …" if not label.endswith("…") else "")

    stage("解析播放地址")
    playback = blackboard.resolve_playback(client, play_url)
    kind = "m3u8" if playback.is_m3u8 else "直链文件"
    print(f"      媒体类型：{kind}")

    stage("下载媒体")
    video_path = out_dir / "video.mp4"
    last = {"pct": -1}

    if playback.is_m3u8:
        def on_seg(done: int, total: int, nbytes: int) -> None:
            pct = int(done * 100 / max(total, 1))
            if pct >= last["pct"] + 10 or done == total:
                last["pct"] = pct
                print(f"      分片 {done}/{total}  {pct}%  {_fmt_size(nbytes)}", flush=True)

        video_path = media.download_hls(
            client, playback.contents, video_path, on_seg,
            max_seconds=limit_seconds, start_seconds=start_seconds,
        )
    else:
        def on_bytes(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct >= last["pct"] + 10 or (total and done == total):
                last["pct"] = pct
                print(f"      {_fmt_size(done)}/{_fmt_size(total)}  {pct}%", flush=True)

        video_path = media.download_direct_mp4(client, playback.contents, video_path, on_bytes)

    if not video_path.exists() or video_path.stat().st_size == 0:
        raise RuntimeError("媒体下载结果为空")

    stream_result = None
    draft_md: Path | None = None
    fallback = False
    if stream:
        stage("流式转写 + 初稿重述（边转写边出稿）")
        try:
            stream_result = streaming.run_streaming(
                settings,
                video_path,
                out_dir,
                title,
                start_seconds=float(start_seconds or 0.0),
                limit_seconds=limit_seconds,
                chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds,
                threads=asr_threads,
                on_progress=lambda i, n: print(f"      初稿 {i}/{n} 块", flush=True),
            )
            paths = {
                "md": stream_result.transcript_md,
                "srt": stream_result.transcript_srt,
                "txt": stream_result.transcript_txt,
            }
            duration = stream_result.duration_sec
            draft_md = stream_result.draft_md
            print(
                f"      文字稿：{paths['md']}（流式 {stream_result.chunks} 块，"
                f"{stream_result.stream_duration_sec:.1f}s）"
            )
        except Exception as e:  # noqa: BLE001
            print(f"      流式失败（{type(e).__name__}: {e}），回退批量转写")
            fallback = True
            stream = False

    if not stream:
        if fallback:
            print("      回退批量语音转写 …")
        else:
            stage("语音转写（CPU，可能需要与视频等长的时间）")
        last_min = {"v": 0}

        def on_asr(_count: int, end: float, duration: float) -> None:
            minute = int(end // 60)
            if minute >= last_min["v"] + 1:
                last_min["v"] = minute
                pct = f" ({end / duration * 100:.0f}%)" if duration else ""
                print(f"      已转写 {minute} 分钟{pct}", flush=True)

        segments, duration = asr.transcribe(
            video_path, settings.whisper_model,
            on_progress=on_asr, time_offset=float(start_seconds or 0.0),
            initial_prompt=settings.asr_prompt or None,
        )
        if not segments:
            raise RuntimeError("没有识别到任何语音内容")
        paths = asr.write_transcripts(segments, out_dir, title, duration)
        print(f"      文字稿：{paths['md']}")

    shots: list[dict] = []
    eyes_summary = ""
    eyes_analysis: Path | None = None
    eyes_status = ""
    if eyes:
        if not settings.llm_key:
            print("      未配置 LLM Key，眼睛功能跳过。")
        else:
            stage("眼睛：思考 → 批量取帧 → 观察")
            result = vision.run_eyes(
                settings,
                title,
                video_path,
                paths["md"],
                out_dir,
                duration,
                time_offset=float(start_seconds or 0.0),
                max_shots=max_shots,
                max_steps=max_steps,
                reuse=reuse_eyes,
                on_progress=lambda step, n, cap: print(
                    f"      步骤 {step}：累计 {n}/{cap} 帧", flush=True
                ),
            )
            shots = result.shots
            eyes_summary = result.summary
            eyes_analysis = result.analysis_json
            eyes_status = result.status
            if shots:
                print(f"      已保存 {len(shots)} 张截图到 frames/，视觉笔记：visual_notes.md")
            print(f"      眼睛分析：{result.analysis_md}（状态：{result.status}）")

    stage("全量总结（final pass，以完整转写为准）")
    transcript_text = paths["md"].read_text(encoding="utf-8")
    draft_text = (
        draft_md.read_text(encoding="utf-8") if draft_md and draft_md.exists() else ""
    )
    final_t0 = time.monotonic()
    retell_md = llm.retell(
        settings,
        title,
        transcript_text,
        out_dir,
        on_progress=lambda phase, i, n: print(f"      {phase} {i}/{n}", flush=True),
        visual_notes=shots,
        shots=shots,
        visual_summary=eyes_summary,
        draft_text=draft_text,
    )
    final_pass_sec = time.monotonic() - final_t0
    print(f"      全量总结耗时：{final_pass_sec:.1f}s")

    draft_superseded: Path | None = None
    if retell_md and draft_md and draft_md.exists():
        draft_superseded = out_dir / "stream_draft.superseded.md"
        draft_superseded.write_text(
            "> 本文件是流式初稿，已被 retell.md 的全量总结取代，仅作审计。\n\n"
            + draft_md.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        draft_md.unlink()
    if retell_md:
        print(f"      重述：{retell_md}")
        prompt_md = None
    else:
        prompt_md = out_dir / "prompt.md"
        print(f"      未配置 LLM Key，已导出提示词：{prompt_md}")

    report = {
        "max_input_tokens": llm.max_input_chars(),
        "max_shots_hard_cap": llm.MAX_SHOTS_HARD,
        "trace": llm.CONTEXT_TRACE,
    }
    (out_dir / "context_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    truncations = sum(1 for e in llm.CONTEXT_TRACE if e.get("kind") == "truncate")
    print(f"      上下文报告：{len(llm.CONTEXT_TRACE)} 条事件，截断 {truncations} 次 → context_report.json")

    meta = {
        "course": course_name,
        "title": title,
        "record_time": time_text,
        "media_type": "m3u8" if playback.is_m3u8 else "mp4",
        "media_file": video_path.name,
        "duration_sec": round(duration, 1),
        "whisper_model": settings.whisper_model,
        "llm_model": settings.llm_model if retell_md else None,
        "vision_model": settings.vision_model if shots else None,
        "eyes": bool(eyes),
        "eyes_status": eyes_status or None,
        "eyes_max_steps": max_steps if eyes else None,
        "eyes_analysis": eyes_analysis.name if eyes_analysis else None,
        "max_shots": max_shots if eyes else None,
        "shots": [
            {"time": s["time"], "file": s["file"], "reason": s["reason"]}
            for s in shots
        ],
        "context_report": "context_report.json",
        "stream": {
            "enabled": bool(stream),
            "chunks": stream_result.chunks if stream_result else None,
            "first_token_sec": stream_result.first_token_sec if stream_result else None,
            "stream_duration_sec": stream_result.stream_duration_sec
            if stream_result
            else None,
            "asr_seconds": stream_result.asr_seconds if stream_result else None,
            "final_pass_sec": round(final_pass_sec, 3),
            "end_to_end_sec": round(time.monotonic() - pipeline_t0, 3),
            "draft_superseded": bool(draft_superseded),
            "draft_file": draft_superseded.name if draft_superseded else None,
        },
        "limited_seconds": limit_seconds,
        "start_seconds": start_seconds,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if stream_result:
        print(
            f"      流式指标：首正文 {stream_result.first_token_sec or 0:.1f}s；"
            f"流式阶段 {stream_result.stream_duration_sec:.1f}s；"
            f"final pass {final_pass_sec:.1f}s；"
            f"端到端 {time.monotonic() - pipeline_t0:.1f}s → stream_metrics.json"
        )

    if not keep_video:
        video_path.unlink(missing_ok=True)
        print("      已按配置删除视频，仅保留音频与文字稿")

    return RunResult(
        out_dir=out_dir,
        media=video_path,
        transcript_md=paths["md"],
        srt=paths["srt"],
        retell_md=retell_md,
        prompt_md=prompt_md,
        eyes_analysis=eyes_analysis,
    )
