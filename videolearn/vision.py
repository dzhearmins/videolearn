"""“眼睛”代理：思考 → 一次取多帧 → 观察 → 留存 → 再思考。

每一步：
1. `llm.eyes_decide` 先输出 analysis，再决定 get_frames（时间点列表）或 finish；
2. `extract_frames` 按请求顺序批量抽帧；
3. `llm.observe_frames` 把多张图按时间顺序一次发给视觉模型；
4. 本步分析、取帧清单、逐帧观察立即写入 eyes_analysis.json / .md，
   并进入下一步的 previous_steps；最后把 summary 交给重述。

只有显式开启（CLI --eyes）才会运行。截图保存在输出目录 frames/ 下。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import llm
from .config import Settings
from .media import find_ffmpeg

FRAME_WIDTH = 1280
MAX_BATCH = 4


@dataclass
class EyesResult:
    shots: list[dict]
    summary: str
    status: str
    analysis_json: Path
    analysis_md: Path


def _extract_one_frame(video: Path, seconds: float, dest: Path, width: int = FRAME_WIDTH) -> Path:
    """从视频指定秒数抽一帧 JPEG（秒数为素材内相对时间）。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("眼睛功能需要 ffmpeg，请先安装")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, seconds):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "4",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"抽帧失败（{seconds:.1f}s）: {msg}")
    return dest


def extract_frames(
    video: Path,
    requests_: list[dict],
    frames_dir: Path,
    time_offset: float = 0.0,
    width: int = FRAME_WIDTH,
) -> list[dict]:
    """按请求顺序批量抽帧；返回与请求同序的帧列表（失败的跳过并打印原因）。"""
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for req in requests_:
        seconds = float(req["seconds"])
        local = max(0.0, seconds - float(time_offset))
        key = llm.frame_key(seconds)
        path = frames_dir / f"{key}.jpg"
        try:
            _extract_one_frame(video, local, path, width)
        except Exception as e:  # noqa: BLE001
            print(f"      跳过 {req['time']}：{type(e).__name__}: {e}")
            continue
        out.append(
            {
                "key": key,
                "time": llm.format_timecode(seconds),
                "seconds": seconds,
                "focus": req.get("focus", ""),
                "file": f"frames/{key}.jpg",
                "path": path,
            }
        )
    return out


def normalize_frame_requests(
    raw_times,
    start: float,
    end: float,
    observed: list[float],
    remaining: int,
    max_batch: int = MAX_BATCH,
) -> list[dict]:
    """校验/去重模型请求的时间点：范围内、与已取帧相隔 ≥10 秒、单次不超过上限。"""
    accepted: list[dict] = []
    accepted_seconds: list[float] = []
    for item in raw_times or []:
        if isinstance(item, str):
            time_s, focus = item, ""
        elif isinstance(item, dict):
            time_s = str(item.get("time", ""))
            focus = str(item.get("focus", "")).strip()
        else:
            continue
        seconds = llm.parse_shot_time(time_s)
        if seconds is None:
            continue
        if seconds < start - 5 or seconds > end + 5:
            continue
        if any(abs(seconds - o) < 10 for o in observed):
            continue
        if any(abs(seconds - a) < 10 for a in accepted_seconds):
            continue
        accepted.append(
            {
                "seconds": seconds,
                "time": llm.format_timecode(seconds),
                "focus": focus[:120],
            }
        )
        accepted_seconds.append(seconds)
        if len(accepted) >= min(max_batch, remaining):
            break
    return accepted


def _split_observations(text: str, frames: list[dict]) -> dict[str, str]:
    """把视觉模型输出按 `### HH:MM:SS` 切分并映射回各帧。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^###\s*(\d{2}:\d{2}:\d{2})", stripped)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
        elif stripped.startswith("### 综合分析"):
            current = None
        elif current is not None:
            sections[current].append(line)

    out = {f["time"]: "\n".join(sections.get(f["time"], [])).strip() for f in frames}
    if frames and not any(out.values()):
        out[frames[0]["time"]] = text.strip()
    return out


def load_eyes_analysis(out_dir: Path) -> dict | None:
    p = Path(out_dir) / "eyes_analysis.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _render_analysis_md(payload: dict) -> str:
    lines = [
        f"# 眼睛分析：{payload.get('title', '')}",
        "",
        f"- 状态：{payload.get('status', '')}",
        f"- 范围：{payload.get('time_offset', 0):.0f}s 起，时长 {payload.get('duration_sec', 0):.1f}s",
        f"- 截图：{len(payload.get('shots', []))} 张",
        "",
    ]
    for step in payload.get("steps", []):
        lines += [f"## 步骤 {step.get('step')}", "", "### 思考/分析", step.get("analysis", ""), ""]
        if step.get("frames"):
            lines.append("### 取帧")
            for f in step["frames"]:
                focus = f"（{f['focus']}）" if f.get("focus") else ""
                lines.append(f"- {f['time']} → {f['file']}{focus}")
            lines.append("")
        if step.get("observation"):
            lines += ["### 观察", step["observation"], ""]
        if step.get("error"):
            lines += ["### 错误", step["error"], ""]
    lines += ["## 最终结论", payload.get("summary", "") or "（无）", ""]
    return "\n".join(lines)


def save_eyes_analysis(out_dir: Path, payload: dict) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / "eyes_analysis.json"
    md = out_dir / "eyes_analysis.md"
    j.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(_render_analysis_md(payload), encoding="utf-8")
    return j, md


def _history(steps: list[dict]) -> str:
    lines: list[str] = []
    for s in steps:
        lines.append(f"Step {s['step']} 分析：{s.get('analysis', '')[:800]}")
        action = s.get("action") or {}
        if action.get("type") == "get_frames":
            times = [
                (t.get("time", "") if isinstance(t, dict) else str(t))
                for t in action.get("times", [])
            ]
            lines.append("取帧：" + ", ".join(times))
        elif action.get("type") == "finish":
            lines.append("结束")
        if s.get("observation"):
            lines.append("观察：" + s["observation"][:2500])
        if s.get("error"):
            lines.append("错误：" + s["error"])
    return "\n".join(lines)


def _payload(
    title: str,
    video_path: Path,
    duration: float,
    time_offset: float,
    max_shots: int,
    max_steps: int,
    status: str,
    summary: str,
    steps: list[dict],
    shots: list[dict],
) -> dict:
    return {
        "version": 1,
        "title": title,
        "video": str(video_path),
        "duration_sec": round(duration, 1),
        "time_offset": time_offset,
        "max_shots": max_shots,
        "max_steps": max_steps,
        "status": status,
        "summary": summary,
        "steps": steps,
        "shots": shots,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_eyes(
    settings: Settings,
    title: str,
    video_path: Path,
    transcript_path: Path,
    out_dir: Path,
    duration: float,
    time_offset: float = 0.0,
    max_shots: int = 8,
    max_steps: int = 4,
    reuse: bool = False,
    on_progress=None,
) -> EyesResult:
    """执行“思考→批量取帧→观察”循环；每步分析都落盘并进入下一步上下文。"""
    out_dir = Path(out_dir)
    analysis_json = out_dir / "eyes_analysis.json"
    analysis_md = out_dir / "eyes_analysis.md"

    if reuse:
        data = load_eyes_analysis(out_dir)
        if data:
            print("      复用上次眼睛分析（未调用 LLM）")
            return EyesResult(
                shots=data.get("shots") or [],
                summary=data.get("summary", ""),
                status=data.get("status", "reused"),
                analysis_json=analysis_json,
                analysis_md=analysis_md,
            )
        print("      没有可复用的眼睛分析，重新执行")

    requested = max_shots
    max_shots = llm.clamp_max_shots(max_shots)
    if max_shots != requested:
        print(f"      截图数 {requested} 超过硬上限，按 {max_shots} 执行")
    max_steps = max(1, min(int(max_steps), 8))

    text = Path(transcript_path).read_text(encoding="utf-8")
    start = float(time_offset)
    end = start + float(duration)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink(missing_ok=True)

    steps: list[dict] = []
    shots: list[dict] = []
    observed: list[float] = []
    summary = ""
    status = "max_steps"

    for step in range(1, max_steps + 1):
        remaining = max_shots - len(shots)
        if remaining <= 0:
            status = "max_frames"
            break
        max_batch = min(MAX_BATCH, remaining)

        try:
            decision = llm.eyes_decide(
                settings,
                title,
                text,
                start,
                end,
                _history(steps),
                remaining,
                max_batch,
            )
        except Exception as e:  # noqa: BLE001
            steps.append(
                {
                    "step": step,
                    "analysis": "",
                    "action": {},
                    "frames": [],
                    "observation": "",
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            status = "error"
            save_eyes_analysis(
                out_dir,
                _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
            )
            break

        analysis = decision["analysis"]
        action = decision["action"]
        if action.get("type") == "finish":
            summary = str(action.get("summary", "")).strip() or "（模型结束但未给总结）"
            steps.append(
                {
                    "step": step,
                    "analysis": analysis,
                    "action": action,
                    "frames": [],
                    "observation": "",
                }
            )
            status = "finished"
            save_eyes_analysis(
                out_dir,
                _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
            )
            break

        requests_ = normalize_frame_requests(
            action.get("times"), start, end, observed, remaining, max_batch
        )
        if not requests_:
            steps.append(
                {
                    "step": step,
                    "analysis": analysis,
                    "action": action,
                    "frames": [],
                    "observation": "",
                    "error": "请求的时间点无效、重复或超出范围",
                }
            )
            save_eyes_analysis(
                out_dir,
                _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
            )
            continue

        frames = extract_frames(video_path, requests_, frames_dir, start)
        if not frames:
            steps.append(
                {
                    "step": step,
                    "analysis": analysis,
                    "action": action,
                    "frames": [],
                    "observation": "",
                    "error": "抽帧全部失败",
                }
            )
            save_eyes_analysis(
                out_dir,
                _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
            )
            continue

        try:
            observation = llm.observe_frames(settings, title, frames)
        except Exception as e:  # noqa: BLE001
            steps.append(
                {
                    "step": step,
                    "analysis": analysis,
                    "action": action,
                    "frames": [{"time": f["time"], "file": f["file"], "focus": f["focus"]} for f in frames],
                    "observation": "",
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            save_eyes_analysis(
                out_dir,
                _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
            )
            continue

        per_frame = _split_observations(observation, frames)
        for f in frames:
            shots.append(
                {
                    "key": f["key"],
                    "time": f["time"],
                    "seconds": f["seconds"],
                    "reason": f["focus"] or "AI 选取的板书时刻",
                    "file": f["file"],
                    "description": per_frame.get(f["time"]) or observation,
                }
            )
            observed.append(f["seconds"])

        steps.append(
            {
                "step": step,
                "analysis": analysis,
                "action": action,
                "frames": [
                    {"time": f["time"], "file": f["file"], "focus": f["focus"]}
                    for f in frames
                ],
                "observation": observation,
            }
        )
        save_eyes_analysis(
            out_dir,
            _payload(title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots),
        )
        if on_progress:
            on_progress(step, len(shots), max_shots)

    if not summary:
        try:
            summary = llm.eyes_summarize(settings, title, steps)
        except Exception as e:  # noqa: BLE001
            last = steps[-1] if steps else {}
            summary = (last.get("observation") or last.get("analysis") or "未获得视觉信息")[:1500]
            print(f"      最终视觉结论生成失败：{type(e).__name__}: {e}")

    payload = _payload(
        title, video_path, duration, start, max_shots, max_steps, status, summary, steps, shots
    )
    save_eyes_analysis(out_dir, payload)

    shots_payload = {
        "title": title,
        "video": str(video_path),
        "duration_sec": round(duration, 1),
        "time_offset": start,
        "max_shots": max_shots,
        "max_steps": max_steps,
        "status": status,
        "summary": summary,
        "shots": shots,
    }
    (out_dir / "shots.json").write_text(
        json.dumps(shots_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [f"# 视觉笔记：{title}", "", f"状态：{status}", ""]
    for s in shots:
        lines.append(f"## {s['time']}（{s['reason']}）")
        lines.append("")
        lines.append(s["description"])
        lines.append("")
    (out_dir / "visual_notes.md").write_text("\n".join(lines), encoding="utf-8")

    return EyesResult(
        shots=shots,
        summary=summary,
        status=status,
        analysis_json=analysis_json,
        analysis_md=analysis_md,
    )
