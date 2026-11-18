"""共享总结缓存客户端：查 / 取 / 发。

设计见 docs/shared-summary-cache.md。所有网络失败都抛 ShareError，
由调用方降级（不能影响本地转写主流程）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import llm, pipeline

PIPELINE_VERSION = "0.2.0"
MAX_BUNDLE = 12 * 1024 * 1024
BUNDLE_FILES = ("retell.md", "transcript.md", "transcript.srt", "notes_map.md", "meta.json")
MAX_FRAMES = 40


class ShareError(RuntimeError):
    """共享服务的网络/协议错误；调用方应当降级而不是中断。"""


def prompt_hash() -> str:
    text = (llm.SYSTEM_FINAL + "\n" + llm.SYSTEM_NOTES).encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:12]


def lecture_key(play_url: str) -> str | None:
    """从 playVideo 链接的 JWT 里取 (hqyCourseId, hqySubId)。"""
    hint = pipeline.token_hint(play_url)
    course_id = str(hint.get("hqyCourseId") or "").strip()
    sub_id = str(hint.get("hqySubId") or "").strip()
    if not course_id or not sub_id:
        return None
    return f"{course_id}:{sub_id}"


@dataclass
class ShareConfig:
    server: str = ""
    token: str = ""
    name: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.server and self.token)

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "server": self.server,
            "name": self.name,
        }

    @classmethod
    def from_settings(cls, settings) -> "ShareConfig":
        return cls(
            server=(getattr(settings, "share_server", "") or "").strip().rstrip("/"),
            token=(getattr(settings, "share_token", "") or "").strip(),
            name=(getattr(settings, "share_name", "") or "").strip(),
        )


class ShareClient:
    def __init__(self, server: str, token: str, timeout=(3, 20)) -> None:
        self.base = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self.base + path
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ShareError(f"连不上共享服务：{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:200].replace("\n", " ")
            raise ShareError(f"共享服务返回 HTTP {resp.status_code}：{detail}")
        return resp

    def lookup(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        resp = self._request("POST", "/api/lookup", json={"keys": keys[:200]})
        return (resp.json() or {}).get("items") or {}

    def fetch(self, key: str) -> bytes:
        resp = self._request("GET", f"/api/summaries/{key}/bundle")
        data = resp.content
        if len(data) > MAX_BUNDLE:
            raise ShareError("共享包过大")
        return data

    def publish(self, key: str, meta: dict, blob: bytes) -> dict:
        payload = {
            "meta": meta,
            "bundle_b64": base64.b64encode(blob).decode("ascii"),
        }
        resp = self._request("POST", f"/api/summaries/{key}", json=payload)
        return resp.json() or {}

    def report(self, key: str, summary_id: int) -> dict:
        resp = self._request("POST", f"/api/summaries/{key}/report", json={"id": summary_id})
        return resp.json() or {}

    def delete(self, key: str, summary_id: int) -> dict:
        resp = self._request("DELETE", f"/api/summaries/{key}/{summary_id}")
        return resp.json() or {}


def register(server: str, invite: str, name: str, timeout=15) -> dict:
    """用邀请码换设备令牌；返回 {token, device}。"""
    url = server.rstrip("/") + "/api/register"
    try:
        resp = requests.post(url, json={"name": name, "invite": invite}, timeout=timeout)
    except requests.RequestException as exc:
        raise ShareError(f"连不上共享服务：{type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise ShareError(f"注册失败（HTTP {resp.status_code}）：{resp.text[:200]}")
    data = resp.json() or {}
    if not data.get("token"):
        raise ShareError("注册响应里没有令牌")
    return data


# --------------------------------------------------------------------------
# 打包 / 解包
# --------------------------------------------------------------------------
def _read_meta(out_dir: Path) -> dict:
    p = out_dir / "meta.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_publish(out_dir: Path, lecture: str) -> tuple[dict, bytes]:
    """把一个任务目录打成 zip，并生成要提交的 meta。"""
    out_dir = Path(out_dir)
    retell = out_dir / "retell.md"
    transcript = out_dir / "transcript.md"
    if not transcript.is_file():
        raise ShareError("没有 transcript.md，不能分享")
    if not retell.is_file():
        raise ShareError("没有 retell.md，不能分享")

    files: dict[str, bytes] = {}
    for name in BUNDLE_FILES:
        p = out_dir / name
        if p.is_file():
            files[name] = p.read_bytes()

    frames = sorted((out_dir / "frames").glob("*.jpg"))[:MAX_FRAMES]
    for f in frames:
        files[f"frames/{f.name}"] = f.read_bytes()

    transcript_bytes = files["transcript.md"]
    meta = {
        "lecture_key": lecture,
        "course_name": str(_read_meta(out_dir).get("course") or ""),
        "title": str(_read_meta(out_dir).get("title") or ""),
        "record_time": str(_read_meta(out_dir).get("record_time") or ""),
        "pipeline_version": PIPELINE_VERSION,
        "prompt_hash": prompt_hash(),
        "asr_model": str(_read_meta(out_dir).get("whisper_model") or ""),
        "llm_model": str(_read_meta(out_dir).get("llm_model") or ""),
        "duration_sec": _read_meta(out_dir).get("duration_sec"),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "has_frames": bool(frames),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    blob = buf.getvalue()
    if len(blob) > MAX_BUNDLE:
        raise ShareError(f"打包后 {len(blob) // 1024 // 1024} MB，超过共享上限")
    return meta, blob


def _safe_name(name: str) -> str | None:
    name = name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return None
    if name in BUNDLE_FILES or name == "meta.json":
        return name
    if name.startswith("frames/") and name.count("/") == 1:
        tail = name.split("/", 1)[1]
        if tail and all(c.isalnum() or c in "._-" for c in tail):
            return name
    return None


def unpack_bundle(blob: bytes, dest: Path, *, uploader: str, share_id: int | None = None) -> dict:
    """把共享包落到本地目录；只允许已知文件名，拒绝路径穿越。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "frames").mkdir(exist_ok=True)
    written: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _safe_name(info.filename)
                if name is None:
                    continue
                data = zf.read(info)
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                written.append(name)
    except zipfile.BadZipFile as exc:
        raise ShareError("共享包不是合法 zip") from exc
    if "transcript.md" not in written:
        raise ShareError("共享包里没有 transcript.md")

    meta = _read_meta(dest)
    meta.update(
        {
            "source": "shared",
            "shared_from": uploader,
            "shared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "share_id": share_id,
        }
    )
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"files": written, "meta": meta}
