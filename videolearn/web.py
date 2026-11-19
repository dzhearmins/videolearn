"""本机网页界面（方案 C）：只监听 127.0.0.1，用一次性令牌保护接口。

设计约束（见 docs/centralized-service-architecture.md §5 C）：
- 不开公网、不做多用户、不托管任何凭据；
- 不引入 Web 框架，避免为了一个本地界面增加安装负担（stdlib http.server）；
- 复用现有 pipeline.run_for_video，把它的 print 输出切成行，用长轮询回放给浏览器；
- 不改 streaming.py，另一 agent 正在改它；这里只调用 pipeline。

用法：
    python -m videolearn web
    python -m videolearn web --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import dataclass, field, replace
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import blackboard, iaaa, media, pipeline
from . import share as sharelib
from .config import HOME, ROOT, Settings, load_settings
from .session import AuthError, BBClient
from .term import soften_console

PLAY_URL_PREFIX = "https://course.pku.edu.cn/"
LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\]):\d+$")
LOCAL_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
FRAME_RE = re.compile(r"^frames/[0-9A-Za-z._-]+\.(jpg|jpeg|png)$")
ENV_KEY_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

# 允许下载/预览的产物（相对输出目录）
ARTIFACT_NAMES = {
    "retell.md",
    "transcript.md",
    "transcript.srt",
    "transcript.txt",
    "notes_map.md",
    "visual_notes.md",
    "eyes_analysis.md",
    "eyes_analysis.json",
    "prompt.md",
    "meta.json",
    "stream_metrics.json",
    "context_report.json",
}
MAX_FRAMES = 40
MAX_BODY = 64 * 1024
MAX_LIVE_BYTES = 512 * 1024

# 可以被"视频存储"管理的文件名（只认这些，绝不按调用方给的任意路径删）
VIDEO_NAMES = ("video.mp4", "video.ts", "video.m4a")

# 设置页字段 → Settings 属性（远端会话不经过 .env 时用）
SETTING_FIELDS = {
    "PKU_STUDENT_ID": "student_id",
    "PKU_PASSWORD": "password",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "LLM_MODEL": "llm_model",
    "LLM_BASE_URL": "llm_base_url",
    "VIDEOLRN_WHISPER_MODEL": "whisper_model",
    "VIDEOLRN_AUTO_CLEAN_VIDEO": "auto_clean_video",
    "VIDEOLRN_ASR_BACKEND": "asr_backend",
    "ASR_BASE_URL": "asr_base_url",
    "ASR_API_KEY": "asr_api_key",
    "ASR_MODEL": "asr_model",
}

# 长任务运行过程中，任务页要边跑边读的产物（先出现的在前）
LIVE_ARTIFACTS = (
    ("draft", "stream_restated.md", "流式初稿"),
    ("partial", "transcript.partial.md", "边转写文字稿"),
    ("draft_final", "stream_draft.superseded.md", "流式初稿（定稿）"),
    ("retell", "retell.md", "最终重述"),
    ("notes", "notes_map.md", "分段笔记"),
    ("transcript", "transcript.md", "完整文字稿"),
)
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


class ApiError(Exception):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# 输出捕获：pipeline 用 print 报进度，这里切成行供浏览器长轮询
# --------------------------------------------------------------------------
class LineSink(io.TextIOBase):
    def __init__(self) -> None:
        self._buf = ""
        self._cv = threading.Condition()
        self.lines: list[str] = []
        self.state = "running"

    def write(self, s: str) -> int:  # noqa: D102
        if not s:
            return 0
        with self._cv:
            self._buf += s.replace("\r", "")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self.lines.append(line.rstrip())
            self._cv.notify_all()
        return len(s)

    def flush(self) -> None:  # noqa: D102
        pass

    def finish(self, state: str) -> None:
        with self._cv:
            rest = self._buf.strip()
            if rest:
                self.lines.append(rest)
                self._buf = ""
            self.state = state
            self._cv.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[list[str], int, str]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while len(self.lines) <= after and self.state == "running":
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return self.lines[after:], len(self.lines), self.state


@dataclass
class Job:
    id: str
    params: dict
    state: str = "queued"
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    out_dir: Path | None = None
    share: dict | None = None
    clean: dict | None = None
    sink: LineSink = field(default_factory=LineSink)

    def _fresh(self, path: Path) -> bool:
        """只认本次任务开始之后写过的文件。

        同一节课重跑会复用同一个输出目录，上一次的 retell.md 还在盘上；
        不过滤的话，任务刚启动就会把旧结果当成"最终结果"展示。
        """
        if self.started is None:
            return True
        try:
            return path.stat().st_mtime >= self.started - 2.0
        except OSError:
            return False

    def artifacts(self) -> list[dict]:
        if not self.out_dir or not self.out_dir.is_dir():
            return []
        items: list[dict] = []
        for name in sorted(ARTIFACT_NAMES):
            f = self.out_dir / name
            if f.is_file() and self._fresh(f):
                items.append(self._item(name, f))
        frames = sorted((self.out_dir / "frames").glob("*.jpg"))[:MAX_FRAMES]
        for f in frames:
            if self._fresh(f):
                items.append(self._item(f"frames/{f.name}", f))
        return items

    def _item(self, rel: str, f: Path) -> dict:
        return {
            "name": rel,
            "bytes": f.stat().st_size,
            # 相对 URL：本地挂在 / 下、内测挂在 /<secret>/ 下都能解析
            "url": f"api/jobs/{self.id}/file?name={urllib.parse.quote(rel)}",
        }

    def summary(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "course_key": self.params.get("course_key"),
            "lecture_key": self.params.get("lecture_key"),
            "params": {
                k: self.params.get(k)
                for k in (
                    "course_name",
                    "title",
                    "time",
                    "eyes",
                    "stream",
                    "limit_seconds",
                    "start_seconds",
                    "from_shared",
                )
            },
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "out_dir": str(self.out_dir) if self.out_dir else None,
            "share": self.share,
            "clean": self.clean,
            "artifacts": self.artifacts(),
        }

    def live(self) -> dict:
        """返回流式阶段正在写的文件内容，供任务页边跑边看。"""
        files: list[dict] = []
        out_dir = self.out_dir
        if out_dir and out_dir.is_dir():
            for key, name, label in LIVE_ARTIFACTS:
                path = out_dir / name
                if not path.is_file() or not self._fresh(path):
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                text = data[:MAX_LIVE_BYTES].decode("utf-8", "replace")
                if key == "draft_final":
                    text = _strip_leading_notice(text)
                files.append(
                    {
                        "key": key,
                        "name": name,
                        "label": label,
                        "bytes": len(data),
                        "truncated": len(data) > MAX_LIVE_BYTES,
                        "text": text,
                    }
                )
        meta: dict = {}
        frames: list[dict] = []
        out_dir = self.out_dir
        if out_dir and out_dir.is_dir():
            meta_path = out_dir / "meta.json"
            if meta_path.is_file() and self._fresh(meta_path):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
            frames_dir = out_dir / "frames"
            if frames_dir.is_dir():
                for f in sorted(frames_dir.glob("*.jpg"))[:MAX_FRAMES]:
                    if not self._fresh(f):
                        continue
                    rel = f"frames/{f.name}"
                    frames.append(
                        {
                            "name": rel,
                            "bytes": f.stat().st_size,
                            "url": f"api/jobs/{self.id}/file?name={urllib.parse.quote(rel)}",
                        }
                    )
        return {
            "id": self.id,
            "state": self.state,
            "out_dir": str(out_dir) if out_dir else None,
            "files": files,
            "frames": frames,
            "eyes": {
                "enabled": bool(self.params.get("eyes")),
                "done": bool(meta.get("eyes")),
                "status": meta.get("eyes_status"),
                "shots": len(meta.get("shots") or []),
                "vision_model": meta.get("vision_model"),
            },
        }


def env_path() -> Path:
    """当前部署的 .env：VIDEOLRN_HOME 优先，否则回到项目根目录。

    本地模式仍然写项目根目录的 .env；远端内测把 HOME 指到实例目录，
    代码目录可以只读。
    """
    home = os.environ.get("VIDEOLRN_HOME")
    return (Path(home).expanduser().resolve() if home else ROOT) / ".env"


# --------------------------------------------------------------------------
# .env 读写
# --------------------------------------------------------------------------
def _dotenv_quote(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+\-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """只改动给定键，保留其余行与注释；原子替换。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = ENV_KEY_RE.match(line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={_dotenv_quote(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={_dotenv_quote(value)}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _mask_id(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 7) + value[-3:]


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return value[:4] + "…" + value[-4:]


# --------------------------------------------------------------------------
# 产物路径白名单
# --------------------------------------------------------------------------
def artifact_path(out_dir: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/").strip()
    if rel.startswith("/") or ".." in rel.split("/"):
        return None
    if rel not in ARTIFACT_NAMES and not FRAME_RE.match(rel):
        return None
    root = out_dir.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    return p


def _trim_memory() -> None:
    """把 glibc 空闲堆还给内核；whisper 跑完一轮后 RSS 会涨，连续任务容易顶到上限。"""
    if os.name != "posix":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:  # noqa: BLE001 - 非 glibc 平台忽略
        pass


def _self_rss_mb() -> float:
    if os.name != "posix":
        return 0.0
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


_WIN_LOCKS: dict[str, threading.Lock] = {}
_WIN_LOCKS_GUARD = threading.Lock()


class JobLock:
    """跨进程的单任务锁。

    远端内测可能同时开多个实例，但服务器只有 2 核 / 1.6 GB，两个 whisper
    同时跑会互相拖慢甚至 OOM。POSIX 用 flock；Windows 没有 fcntl，退化为
    进程内按路径互斥（本地通常只有一个实例）。
    """

    def __init__(self, path: "Path | str | None") -> None:
        self.path = Path(path) if path else None
        self._fh = None
        self._win_lock: threading.Lock | None = None

    def acquire(self) -> bool:
        if self.path is None:
            return True
        try:
            import fcntl
        except ImportError:  # Windows
            key = str(self.path.resolve())
            with _WIN_LOCKS_GUARD:
                lock = _WIN_LOCKS.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                return False
            self._win_lock = lock
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"{os.getpid()} {int(time.time())}\n")
            self._fh.flush()
            return True
        except OSError:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            return False

    def release(self) -> None:
        if self._win_lock is not None:
            self._win_lock.release()
            self._win_lock = None
            return
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


# --------------------------------------------------------------------------
# 应用状态
# --------------------------------------------------------------------------
class App:
    def __init__(
        self,
        key: str,
        settings: Settings,
        *,
        persist: bool = True,
        job_lock: "JobLock | None" = None,
    ) -> None:
        self.key = key
        self.settings = settings
        # persist=True：本地模式，设置写 .env；False：远端内测的会话，设置只留在内存。
        self.persist = persist
        self.job_lock = job_lock
        self.sid = ""
        self.last_seen = time.time()
        self.share = sharelib.ShareConfig.from_settings(settings)
        self.jobs: dict[str, Job] = {}
        self.current: str | None = None
        self._lock = threading.Lock()
        self._auth_cache: tuple[float, bool] = (0.0, False)
        self._auth_lock = threading.Lock()

    # ---- 认证 ----
    def ensured_client(self, force: bool = False) -> BBClient:
        client = BBClient(self.settings.state_dir)
        if not force and client.load_cookies() and iaaa.is_authenticated(client):
            return client
        self.settings.require_pku_credentials()
        iaaa.login(client, self.settings.student_id, self.settings.password)
        self._auth_cache = (time.monotonic(), True)
        return client

    def authenticated(self, check: bool = False) -> bool:
        ts, ok = self._auth_cache
        if not check or time.monotonic() - ts < 60:
            return ok
        if not (self.settings.state_dir / "cookies.json").exists():
            self._auth_cache = (time.monotonic(), False)
            return False
        with self._auth_lock:
            ts, ok = self._auth_cache
            if time.monotonic() - ts < 60:
                return ok
            client = BBClient(self.settings.state_dir, timeout=4)
            ok = bool(client.load_cookies() and iaaa.is_authenticated(client))
            self._auth_cache = (time.monotonic(), ok)
            return ok

    # ---- 状态 ----
    def status(self, check: bool = True) -> dict:
        s = self.settings
        active = self.jobs.get(self.current) if self.current else None
        return {
            "student_id": _mask_id(s.student_id),
            "has_password": bool(s.password),
            "api_key": _mask_key(s.llm_key),
            "llm_model": s.llm_model,
            "whisper_model": s.whisper_model,
            "asr_backend": s.asr_backend,
            "asr_base_url": s.asr_base_url,
            "asr_model": s.asr_model,
            "asr_api_key": _mask_key(s.asr_api_key),
            "ffmpeg": media.find_ffmpeg() is not None,
            "authenticated": self.authenticated(check=check),
            "share": self.share.describe(),
            "auto_clean_video": self.settings.auto_clean_video,
            "active": active.summary() if active else None,
            "jobs": [j.summary() for j in sorted(self.jobs.values(), key=lambda x: x.created, reverse=True)[:10]],
        }

    def reload_settings(self) -> None:
        if not self.persist:
            return
        self.settings = load_settings()
        self.share = sharelib.ShareConfig.from_settings(self.settings)
        self._auth_cache = (0.0, False)

    def apply_settings(self, updates: dict[str, str]) -> None:
        """保存设置：本地写 .env；远端会话只改内存（密码不落盘）。"""
        if self.persist:
            update_env_file(env_path(), updates)
            for key, value in updates.items():
                os.environ[key] = value
            self.reload_settings()
            return
        for key, value in updates.items():
            field_name = SETTING_FIELDS.get(key)
            if not field_name:
                continue
            if field_name == "auto_clean_video":
                setattr(self.settings, field_name, value == "1")
            elif field_name in ("llm_base_url", "asr_base_url"):
                setattr(self.settings, field_name, value.rstrip("/"))
            elif field_name == "asr_backend":
                setattr(self.settings, field_name, value.strip().lower() or "local")
            else:
                setattr(self.settings, field_name, value)
            if field_name == "llm_model" and not os.getenv("VISION_MODEL"):
                self.settings.vision_model = value
        self.share = sharelib.ShareConfig.from_settings(self.settings)
        self._auth_cache = (0.0, False)

    # ---- 共享 ----
    def share_client(self):
        if not self.share.enabled:
            return None
        return sharelib.ShareClient(self.share.server, self.share.token)

    def lookup_shared(self, keys: list[str]) -> dict[str, dict]:
        client = self.share_client()
        if client is None or not keys:
            return {}
        client.timeout = (2, 5)  # 列表页不能因为共享服务慢而卡住
        try:
            return client.lookup(keys)
        except sharelib.ShareError:
            return {}

    def register_share(self, server: str, invite: str, name: str) -> dict:
        server = (server or "").strip().rstrip("/")
        name = (name or "").strip()
        if not server.startswith(("http://", "https://")):
            raise ApiError(400, "共享服务地址必须以 http:// 或 https:// 开头")
        if not invite:
            raise ApiError(400, "缺少邀请码")
        try:
            data = sharelib.register(server, invite.strip(), name or "同学")
        except sharelib.ShareError as exc:
            raise ApiError(400, str(exc)) from exc
        updates = {
            "SHARE_SERVER": server,
            "SHARE_TOKEN": data["token"],
            "SHARE_NAME": name or "同学",
        }
        if self.persist:
            update_env_file(env_path(), updates)
            for k, v in updates.items():
                os.environ[k] = v
            self.reload_settings()
        else:
            self.settings.share_server = server
            self.settings.share_token = data["token"]
            self.settings.share_name = name or "同学"
            self.share = sharelib.ShareConfig.from_settings(self.settings)
        return self.share.describe()

    # ---- 任务 ----
    def start_job(self, params: dict) -> Job:
        with self._lock:
            if self.current:
                raise ApiError(409, "已有任务在跑，请等它结束或刷新页面查看进度。")
            # 连续跑多个任务后 ctranslate2 的堆会留在进程里；先归还，仍偏高就拒绝，
            # 避免 1.6 GB 的机器被拖进 swap。
            if _self_rss_mb() > 650:
                _trim_memory()
                if _self_rss_mb() > 650:
                    raise ApiError(
                        503,
                        "服务器内存紧张（前面任务还没完全释放），请过几分钟再试。",
                    )
            if self.job_lock is not None and not self.job_lock.acquire():
                raise ApiError(
                    409,
                    "服务器上另一位内测同学正在跑任务（只有 2 核，任务需要排队）。"
                    "请过几分钟再试。",
                )
            job = Job(id=secrets.token_hex(4), params=params)
            self.jobs[job.id] = job
            self.current = job.id
        try:
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
        except BaseException:
            with self._lock:
                self.jobs.pop(job.id, None)
                self.current = None
            if self.job_lock is not None:
                self.job_lock.release()
            raise
        return job

    def _run(self, job: Job) -> None:
        job.state = "running"
        job.started = time.time()
        p = job.params
        try:
            threads = int(p.get("asr_threads") or os.getenv("VIDEOLRN_ASR_THREADS") or 0)
            if not threads:
                threads = os.cpu_count() or 4
            threads = max(1, min(16, threads))
            if p.get("from_shared"):
                self._run_shared(job)
                job.state = "done"
                return
            client = self.ensured_client()
            with contextlib.redirect_stdout(job.sink):
                result = pipeline.run_for_video(
                    client,
                    self.settings,
                    title=p["title"],
                    time_text=p["time"],
                    play_url=p["play_url"],
                    course_name=p["course_name"],
                    limit_seconds=p.get("limit_seconds"),
                    start_seconds=float(p.get("start_seconds") or 0.0),
                    eyes=bool(p.get("eyes")),
                    stream=bool(p.get("stream", True)),
                    chunk_seconds=float(p.get("chunk_seconds", 240.0)),
                    overlap_seconds=float(p.get("overlap_seconds", 5.0)),
                    asr_threads=threads,
                    on_out_dir=lambda path: setattr(job, "out_dir", path),
                )
            job.out_dir = result.out_dir
            job.state = "done"
            if self.share.enabled and not p.get("limit_seconds"):
                job.share = {"state": "uploading"}
                threading.Thread(target=self._auto_share, args=(job,), daemon=True).start()
            else:
                job.share = {"state": "off" if not self.share.enabled else "skipped"}
            if self.settings.auto_clean_video:
                job.clean = {"state": "pending"}
                threading.Thread(target=self._auto_clean, args=(job,), daemon=True).start()
        except BaseException as exc:  # noqa: BLE001 - 含 SystemExit(缺少凭据)
            job.error = f"{type(exc).__name__}: {exc}"
            job.state = "failed"
            job.sink.write(f"\n[错误] {job.error}\n")
        finally:
            job.finished = time.time()
            job.sink.finish(job.state)
            with self._lock:
                self.current = None
            if self.job_lock is not None:
                self.job_lock.release()
            _trim_memory()

    def _run_shared(self, job: Job) -> None:
        """命中共享：只下载 zip + 解包，不登录教学网、不下载视频、不跑 ASR。"""
        p = job.params
        key = p.get("lecture_key") or ""
        client = self.share_client()
        if client is None:
            raise sharelib.ShareError("共享服务未配置")
        client.timeout = (5, 60)  # 复制一份 zip 可能几 MB
        info = client.lookup([key]).get(key)
        if not info:
            raise sharelib.ShareError("共享已被删除或没有命中")
        job.sink.write(
            f"[share] 命中：{info['uploader']} 于 {info.get('updated_at') or info['created_at']} 生成"
            f"（ASR {info.get('asr_model') or '?'}）\n"
        )
        blob = client.fetch(key)
        out_dir = _shared_out_dir(self.settings.output_dir, p)
        result = sharelib.unpack_bundle(
            blob, out_dir, uploader=info["uploader"], share_id=info.get("id")
        )
        job.out_dir = out_dir
        job.sink.write(f"[share] 已复制 {len(result['files'])} 个文件到 {out_dir}\n")
        job.sink.write("[share] 没有下载视频，也没有跑语音识别\n")

    def _auto_share(self, job: Job) -> None:
        """总结完成后自动上传；任何失败都只记日志，不影响本地结果。"""
        p = job.params
        try:
            if p.get("_auto_shared"):
                return
            p["_auto_shared"] = True
            key = str(p.get("lecture_key") or "")
            if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}:[0-9A-Za-z_-]{1,64}", key):
                job.share = {"state": "skipped", "message": "缺少课程标识"}
                return
            if p.get("limit_seconds"):
                job.share = {"state": "skipped", "message": "小样不上传"}
                return
            if not job.out_dir:
                job.share = {"state": "skipped", "message": "没有产物目录"}
                return
            meta, blob = sharelib.build_publish(job.out_dir, key)
            client = self.share_client()
            if client is None:
                job.share = {"state": "off"}
                return
            client.timeout = (5, 30)
            result = client.publish(key, meta, blob)
            updated = bool(result.get("updated"))
            job.share = {"state": "done", "bytes": len(blob), "updated": updated}
            job.sink.write(
                f"[share] 已自动{'更新' if updated else '上传'}共享（{max(1, len(blob) // 1024)} KB）\n"
            )
        except BaseException as exc:  # noqa: BLE001 - 上传失败不能影响本地结果
            job.share = {"state": "failed", "message": f"{type(exc).__name__}: {exc}"}
            job.sink.write(f"[share] 自动上传失败（本地结果不受影响）：{type(exc).__name__}: {exc}\n")

    # ---- 视频存储 ----
    def storage(self) -> dict:
        """列出 output/ 下所有已下载的视频；这是首页"视频存储"的唯一数据源。"""
        root = self.settings.output_dir
        items: list[dict] = []
        if root.is_dir():
            for p in root.rglob("video.*"):
                if not p.is_file() or p.name not in VIDEO_NAMES:
                    continue
                try:
                    st = p.stat()
                    rel = p.relative_to(root).as_posix()
                    rel_dir = p.parent.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                parts = [x for x in rel_dir.split("/") if x]
                items.append(
                    {
                        "rel": rel,
                        "course": parts[0] if len(parts) >= 2 else "",
                        "title": parts[-1] if parts else "",
                        "bytes": st.st_size,
                        "mtime": st.st_mtime,
                        "in_use": False,
                    }
                )
        items.sort(key=lambda x: x["mtime"], reverse=True)

        active = self.jobs.get(self.current) if self.current else None
        active_dir = active.out_dir.resolve() if active and active.out_dir else None
        for item in items:
            item_dir = (root / item["rel"]).parent.resolve()
            item["in_use"] = bool(active_dir and item_dir == active_dir)

        total = sum(i["bytes"] for i in items)
        try:
            free = shutil.disk_usage(root if root.exists() else root.parent).free
        except OSError:
            free = 0
        return {
            "items": items,
            "total_bytes": total,
            "free_bytes": free,
            "output_dir": str(root),
            "auto_cleanup": self.settings.auto_clean_video,
        }

    def delete_video(self, rel: str) -> dict:
        """只删"当前列表里真实存在"的视频；不信任调用方给的任意路径。"""
        rel = (rel or "").strip()
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            raise ApiError(400, "路径不合法")
        root = self.settings.output_dir.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or target.name not in VIDEO_NAMES:
            raise ApiError(400, "路径不合法")
        if rel not in {i["rel"] for i in self.storage()["items"]}:
            raise ApiError(404, "文件不存在")

        active = self.jobs.get(self.current) if self.current else None
        if active and active.out_dir and target.parent == active.out_dir.resolve():
            raise ApiError(409, "这个视频正被当前任务使用，等它结束再删")

        try:
            size = target.stat().st_size
            target.unlink()
        except FileNotFoundError:
            size = 0
        return {"freed_bytes": size, "storage": self.storage()}

    def _auto_clean(self, job: Job) -> None:
        """任务完成后删除它自己下载的视频（只在设置开启时调用）。"""
        try:
            if not job.out_dir:
                return
            freed = 0
            for name in VIDEO_NAMES:
                p = job.out_dir / name
                if p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
            job.clean = {"state": "done" if freed else "skipped", "freed_bytes": freed}
            if freed:
                job.sink.write(
                    f"[clean] 已自动删除视频，释放 {max(1, freed // 1024 // 1024)} MB\n"
                )
        except BaseException as exc:  # noqa: BLE001 - 清理失败不能影响任务结果
            job.clean = {"state": "failed", "message": f"{type(exc).__name__}: {exc}"}
            job.sink.write(f"[clean] 自动删除视频失败：{type(exc).__name__}: {exc}\n")


# --------------------------------------------------------------------------
# 远端内测：每个浏览器一个隔离会话
# --------------------------------------------------------------------------
SESSION_COOKIE = "vl_sid"
SESSION_SID_RE = re.compile(r"^[0-9A-Za-z_-]{16,64}$")
SESSION_TTL = 7 * 24 * 3600  # Cookie 有效期；内存会话随进程重启清空


class SessionStore:
    """给每个浏览器发一个 sid，凭 sid 分配独立的 Settings / output / jobs。

    远端内测的关键约束：
    - 设置（含 PKU 密码、API Key）只存在进程内存，不写 .env、不落盘；
    - 每个会话的 output/ 与 state/ 相互隔离，A 看不到 B 的课程、任务和产物；
    - 所有会话共享一个 JobLock，服务器一次只跑一个任务。
    """

    def __init__(
        self,
        key: str,
        base_settings: Settings,
        root: Path,
        *,
        cookie_path: str = "/",
        job_lock: JobLock | None = None,
    ) -> None:
        self.key = key
        self.base = base_settings
        self.root = Path(root)
        self.cookie_path = cookie_path if cookie_path.startswith("/") else "/" + cookie_path
        if not self.cookie_path.endswith("/"):
            self.cookie_path += "/"
        self.job_lock = job_lock
        self.sessions: dict[str, App] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def get(self, sid: str | None) -> tuple[App, str, bool]:
        """返回 (会话 App, sid, 是否新建)。sid 不合法或不存在时新建。"""
        sid = (sid or "").strip()
        with self._lock:
            app = self.sessions.get(sid) if SESSION_SID_RE.fullmatch(sid) else None
            if app is not None:
                app.last_seen = time.time()
                return app, sid, False
            if not SESSION_SID_RE.fullmatch(sid):
                sid = secrets.token_urlsafe(24)
            app = self._create(sid)
            self.sessions[sid] = app
            # 防御性上限：会话太多时丢掉最久没动的（文件保留，不删用户产物）
            if len(self.sessions) > 200:
                oldest = min(self.sessions.values(), key=lambda a: a.last_seen)
                self.sessions.pop(oldest.sid, None)
            return app, sid, True

    def _create(self, sid: str) -> App:
        home = self.root / sid
        output = home / "output"
        state = home / "state"
        output.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        for path in (home, state, output):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        # 只清空 PKU 凭据；LLM Key / 模型 / 共享配置可来自服务器级 .env，
        # 作为内测的统一默认值（个人可在设置里覆盖）。
        settings = replace(
            self.base,
            student_id="",
            password="",
            state_dir=state,
            output_dir=output,
        )
        app = App(self.key, settings, persist=False, job_lock=self.job_lock)
        app.sid = sid
        return app


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _index_html(
    key: str,
    *,
    notice: str = "",
    sample_seconds: int = 600,
    sample_start_seconds: int = 0,
) -> bytes:
    from importlib.resources import files

    raw = files("videolearn.webui").joinpath("index.html").read_text(encoding="utf-8")
    # 占位符单独命名，避免替换到 window.__VL_KEY__ 这类标识符内部
    notice_json = json.dumps(notice, ensure_ascii=False).replace("<", "\\u003c")
    raw = raw.replace("__VL_KEY_JSON__", json.dumps(key))
    raw = raw.replace("__VL_NOTICE_JSON__", notice_json)
    raw = raw.replace("__VL_SAMPLE_SECONDS__", str(max(10, int(sample_seconds))))
    raw = raw.replace(
        "__VL_SAMPLE_START_SECONDS__", str(max(0, int(sample_start_seconds)))
    )
    return raw.encode("utf-8")


def _strip_leading_notice(text: str) -> str:
    """去掉 superseded 文件顶部的说明引用块，只留正文。"""
    lines = text.splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    return "\n".join(lines).lstrip("\n")


def _shared_out_dir(root: Path, params: dict) -> Path:
    """给共享复制品选一个不与已有结果冲突的目录（与原 pipeline 命名一致）。"""
    stamp = str(params.get("time") or "").strip().replace(":", "").replace(" ", "_")
    if not stamp:
        stamp = time.strftime("%Y-%m-%d_%H%M")
    title = pipeline._display_title(str(params.get("title") or "课堂回放"))
    base = root / pipeline.safe_name(str(params.get("course_name") or "课程"))
    out_dir = base / f"{pipeline.safe_name(stamp)}_{pipeline.safe_name(title, '回放')}"
    candidate = out_dir
    n = 2
    while candidate.exists() and any(candidate.iterdir()):
        candidate = out_dir.with_name(f"{out_dir.name}_shared{n}")
        n += 1
    return candidate


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VideoLearnWeb/0.1"
    sys_version = ""

    # ---- 基础 ----
    @property
    def app(self) -> App:
        store = getattr(self.server, "sessions", None)
        if store is None:
            return self.server.app  # type: ignore[attr-defined]
        app, sid, new = store.get(self._sid_value())
        if new:
            self._new_sid = sid
        return app

    def _sid_value(self) -> str:
        raw = self.headers.get("Cookie") or ""
        try:
            jar = SimpleCookie(raw)
            return jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else ""
        except Exception:  # noqa: BLE001
            return ""

    def _sid_cookie_header(self) -> None:
        sid = getattr(self, "_new_sid", None)
        if not sid:
            return
        store = getattr(self.server, "sessions", None)
        path = store.cookie_path if store is not None else "/"
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={sid}; Path={path}; Max-Age={SESSION_TTL}; "
            "HttpOnly; SameSite=Lax",
        )

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        return

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._sid_cookie_header()
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._sid_cookie_header()
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return bool(LOCAL_HOST_RE.match((self.headers.get("Host") or "").strip()))

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-VL-Key") or ""
        if not supplied:
            raw = self.headers.get("Cookie") or ""
            try:
                jar = SimpleCookie(raw)
                supplied = jar["vl_key"].value if "vl_key" in jar else ""
            except Exception:  # noqa: BLE001
                supplied = ""
        return bool(supplied) and secrets.compare_digest(supplied, self.app.key)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or bool(LOCAL_ORIGIN_RE.match(origin))

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"error": "bad_host"}, 403)
            return False
        if not self._origin_ok():
            self._json({"error": "bad_origin"}, 403)
            return False
        if not self._token_ok():
            self._json({"error": "unauthorized"}, 403)
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大")
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "请求体不是合法 JSON") from exc
        if not isinstance(obj, dict):
            raise ApiError(400, "请求体必须是对象")
        return obj

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        self._new_sid = None
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/":
                self._index(query.get("k", [None])[0])
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if not self._guard():
                return
            if path == "/api/status":
                check = (query.get("check", ["0"])[0] or "0") == "1"
                self._json(self.app.status(check=check))
            elif path == "/api/courses":
                self._courses()
            elif path == "/api/videos":
                self._videos(query)
            elif path == "/api/jobs":
                self._jobs_list(query)
            elif path == "/api/storage":
                self._json(self.app.storage())
            elif path.startswith("/api/jobs/"):
                self._job_route(path, query)
            else:
                self._json({"error": "not_found"}, 404)
        except SystemExit as exc:
            # require_pku_credentials / 缺少配置时抛的是 SystemExit，不能让它穿透请求线程
            self._json({"error": str(exc) or "缺少配置", "need_config": True}, 400)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except AuthError as exc:
            self._json({"error": str(exc), "need_login": True}, 401)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        self._new_sid = None
        try:
            parsed = urllib.parse.urlparse(self.path)
            if not self._guard():
                return
            body = self._body()
            if parsed.path == "/api/settings":
                self._settings(body)
            elif parsed.path == "/api/login":
                self._login()
            elif parsed.path == "/api/share/register":
                self._share_register(body)
            elif parsed.path == "/api/jobs":
                self._start(body)
            elif parsed.path == "/api/storage/delete":
                rel = str(body.get("rel") or "")
                self._json({"ok": True, **self.app.delete_video(rel)})
            else:
                self._json({"error": "not_found"}, 404)
        except SystemExit as exc:
            # require_pku_credentials / 缺少配置时抛的是 SystemExit，不能让它穿透请求线程
            self._json({"error": str(exc) or "缺少配置", "need_config": True}, 400)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except AuthError as exc:
            self._json({"error": str(exc), "need_login": True}, 401)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---- 页面 ----
    def _index(self, key: str | None) -> None:
        supplied = key or ""
        authorized = (supplied and secrets.compare_digest(supplied, self.app.key)) or self._token_ok()
        if not authorized:
            if getattr(self.server, "sessions", None) is not None:
                message = "未授权：请从管理员发给你的内测链接打开，或联系管理员。"
            else:
                message = (
                    "未授权。请使用启动时终端里打印的 "
                    "http://127.0.0.1:<port>/?k=... 链接打开。"
                )
            self._text(message, 403)
            return
        body = _index_html(
            self.app.key,
            notice=getattr(self.server, "notice", ""),
            sample_seconds=getattr(self.server, "sample_seconds", 600),
            sample_start_seconds=getattr(self.server, "sample_start_seconds", 0),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        # 远端模式由 nginx 注入 X-VL-Key；本地模式才把 ?k 存成 Cookie。
        if supplied and getattr(self.server, "sessions", None) is None:
            self.send_header(
                "Set-Cookie",
                f"vl_key={self.app.key}; Path=/; HttpOnly; SameSite=Strict",
            )
        self._sid_cookie_header()
        self.end_headers()
        self.wfile.write(body)

    # ---- 接口 ----
    def _courses(self) -> None:
        client = self.app.ensured_client()
        courses = blackboard.list_courses(client)
        self._json(
            {
                "authenticated": True,
                "courses": [{"key": c.key, "name": c.name, "term": c.term} for c in courses],
            }
        )

    def _videos(self, query: dict) -> None:
        course_key = (query.get("course", [""])[0] or "").strip()
        if not re.fullmatch(r"_?\d+_\d+", course_key):
            raise ApiError(400, "课程 key 格式不对")
        client = self.app.ensured_client()
        videos = blackboard.list_videos(client, course_key)
        out = []
        keys: list[str] = []
        for v in videos:
            hint = pipeline.token_hint(v.play_url)
            key = (
                f"{hint.get('hqyCourseId')}:{hint.get('hqySubId')}"
                if hint.get("hqyCourseId") and hint.get("hqySubId")
                else None
            )
            if key:
                keys.append(key)
            out.append(
                {
                    "title": v.title,
                    "time": v.time,
                    "teacher": v.teacher,
                    "play_url": v.play_url,
                    "lecture_key": key,
                }
            )
        shared = self.app.lookup_shared(keys)
        for item in out:
            info = shared.get(item["lecture_key"] or "")
            if info:
                item["shared"] = info
        self._json({"videos": out})

    def _settings(self, body: dict) -> None:
        updates: dict[str, str] = {}
        if "auto_cleanup" in body and body["auto_cleanup"] is not None:
            updates["VIDEOLRN_AUTO_CLEAN_VIDEO"] = "1" if body["auto_cleanup"] else "0"
        mapping = {
            "student_id": "PKU_STUDENT_ID",
            "password": "PKU_PASSWORD",
            "api_key": "DEEPSEEK_API_KEY",
            "llm_model": "LLM_MODEL",
            "whisper_model": "VIDEOLRN_WHISPER_MODEL",
            "llm_base_url": "LLM_BASE_URL",
            "asr_backend": "VIDEOLRN_ASR_BACKEND",
            "asr_base_url": "ASR_BASE_URL",
            "asr_model": "ASR_MODEL",
            "asr_api_key": "ASR_API_KEY",
        }
        for field_name, env_key in mapping.items():
            value = body.get(field_name)
            if value is None:
                continue
            value = str(value).strip() if field_name != "password" else str(value)
            if field_name in ("password", "api_key", "asr_api_key") and value == "":
                continue  # 留空表示不修改
            updates[env_key] = value
        if not updates:
            raise ApiError(400, "没有需要保存的字段")
        self.app.apply_settings(updates)
        self._json({"ok": True, "status": self.app.status(check=False)})

    def _login(self) -> None:
        client = self.app.ensured_client(force=True)
        courses = blackboard.list_courses(client)
        self._json({"ok": True, "courses": len(courses)})

    def _share_register(self, body: dict) -> None:
        share = self.app.register_share(
            str(body.get("server") or ""),
            str(body.get("invite") or ""),
            str(body.get("name") or ""),
        )
        self._json({"ok": True, "share": share})

    def _start(self, body: dict) -> None:
        play_url = str(body.get("play_url") or "").strip()
        if not play_url.startswith(PLAY_URL_PREFIX):
            raise ApiError(400, "播放链接必须来自 course.pku.edu.cn")
        title = str(body.get("title") or "课堂回放").strip()[:120]
        time_text = str(body.get("time") or "").strip()[:40]
        course_name = str(body.get("course_name") or "课程").strip()[:80]
        course_key = str(body.get("course_key") or "").strip()
        if course_key and not re.fullmatch(r"_?\d+_\d+", course_key):
            raise ApiError(400, "课程 key 格式不对")
        lecture_key = str(body.get("lecture_key") or "").strip()[:64]
        from_shared = bool(body.get("from_shared")) and bool(lecture_key)
        if from_shared and not self.app.share.enabled:
            raise ApiError(400, "共享服务未配置，不能使用共享结果")
        limit_seconds = body.get("limit_seconds")
        if limit_seconds is not None:
            try:
                limit_seconds = float(limit_seconds)
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "小样时长必须是数字") from exc
            if not 10 <= limit_seconds <= 3600:
                raise ApiError(400, "小样时长需在 10–3600 秒之间")
        start_seconds = body.get("start_seconds")
        if start_seconds is None or limit_seconds is None:
            start_seconds = 0.0
        else:
            try:
                start_seconds = float(start_seconds)
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "起点必须是数字") from exc
            if not 0 <= start_seconds <= 86400:
                raise ApiError(400, "起点需在 0–86400 秒之间")
        params = {
            "play_url": play_url,
            "title": title,
            "time": time_text or time.strftime("%Y-%m-%d_%H%M"),
            "course_name": course_name,
            "course_key": course_key,
            "lecture_key": lecture_key,
            "from_shared": from_shared,
            "eyes": bool(body.get("eyes", True)),
            "stream": bool(body.get("stream", True)),
            "limit_seconds": limit_seconds,
            "start_seconds": start_seconds,
        }
        job = self.app.start_job(params)
        self._json({"job": job.summary()}, 202)

    def _jobs_list(self, query: dict) -> None:
        course = (query.get("course", [""])[0] or "").strip()
        jobs = sorted(self.app.jobs.values(), key=lambda j: j.created, reverse=True)
        if course:
            jobs = [j for j in jobs if j.params.get("course_key") == course]
        self._json({"jobs": [j.summary() for j in jobs[:50]]})

    def _job_route(self, path: str, query: dict) -> None:
        parts = [p for p in path.split("/") if p]
        # /api/jobs/<id>[/log|/file]
        if len(parts) < 3:
            raise ApiError(404, "not_found")
        job_id = parts[2]
        job = self.app.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "任务不存在")
        if len(parts) == 3:
            self._json(job.summary())
            return
        action = parts[3]
        if action == "log":
            try:
                after = max(0, int((query.get("after", ["0"])[0] or 0)))
                wait = min(max(0.0, float((query.get("wait", ["20"])[0] or 20))), 25.0)
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "after/wait 参数不合法") from exc
            lines, cursor, state = job.sink.wait(after, wait)
            self._json({"lines": lines, "cursor": cursor, "state": state})
            return
        if action == "live":
            self._json(job.live())
            return
        if action == "file":
            name = (query.get("name", [""])[0] or "")
            out_dir = job.out_dir
            if not out_dir:
                raise ApiError(404, "任务还没有产物")
            p = artifact_path(out_dir, name)
            if p is None:
                raise ApiError(404, "文件不存在或不允许访问")
            data = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                if "charset" not in ctype:
                    ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._sid_cookie_header()
            self.end_headers()
            self.wfile.write(data)
            return
        raise ApiError(404, "not_found")


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: App


def bind(host: str, port: int) -> Server:
    last: OSError | None = None
    candidates = [port] if port else [0]
    if port:
        candidates += list(range(port + 1, port + 21))
    for candidate in candidates:
        try:
            return Server((host, candidate), Handler)
        except OSError as exc:  # 端口占用，继续试下一个
            last = exc
    raise SystemExit(f"无法绑定 {host}:{port}：{last}")


def main(argv: list[str] | None = None) -> int:
    soften_console()
    parser = argparse.ArgumentParser(
        prog="videolearn web",
        description="网页界面：浏览器里选课、跑转写、看重述（只监听回环地址）",
    )
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765，占用时自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--key", default=os.getenv("VIDEOLRN_WEB_KEY", ""), help="固定访问令牌")
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="远端内测模式：每个浏览器独立会话，设置只存内存，不写 .env",
    )
    parser.add_argument(
        "--sessions-root",
        default=os.getenv("VIDEOLRN_SESSIONS", ""),
        help="会话目录（默认 <VIDEOLRN_HOME>/sessions）",
    )
    parser.add_argument(
        "--cookie-path",
        default=os.getenv("VIDEOLRN_COOKIE_PATH", "/"),
        help="会话 Cookie 的 Path（例如 /vl-xxxx/）",
    )
    parser.add_argument(
        "--job-lock",
        default=os.getenv("VIDEOLRN_JOB_LOCK", ""),
        help="跨进程任务锁文件（默认 <sessions-root>/.job.lock）",
    )
    parser.add_argument("--public-url", default=os.getenv("VIDEOLRN_PUBLIC_URL", ""))
    parser.add_argument(
        "--notice",
        default=os.getenv("VIDEOLRN_WEB_NOTICE", ""),
        help="页面顶部提示（内测环境用来写 HTTP/小样等注意事项）",
    )
    parser.add_argument(
        "--sample-seconds",
        type=int,
        default=int(os.getenv("VIDEOLRN_SAMPLE_SECONDS", "600") or 600),
        help="「只跑前 N 秒」的默认值（本地 600；2 核服务器建议 90–120）",
    )
    parser.add_argument(
        "--sample-start-seconds",
        type=int,
        default=int(os.getenv("VIDEOLRN_SAMPLE_START_SECONDS", "0") or 0),
        help="小样默认起点秒数（远端建议 600，跳过课堂开头可能安静的部分）",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    key = args.key or secrets.token_urlsafe(18)
    # 固定只监听回环地址：公网入口由 nginx 反代，并负责 Basic 认证。
    server = bind("127.0.0.1", args.port)
    server.notice = args.notice  # type: ignore[attr-defined]
    server.sample_seconds = args.sample_seconds  # type: ignore[attr-defined]
    server.sample_start_seconds = args.sample_start_seconds  # type: ignore[attr-defined]
    if args.sessions:
        root = Path(args.sessions_root) if args.sessions_root else HOME / "sessions"
        lock_path = Path(args.job_lock) if args.job_lock else root / ".job.lock"
        server.sessions = SessionStore(  # type: ignore[attr-defined]
            key,
            settings,
            root,
            cookie_path=args.cookie_path,
            job_lock=JobLock(lock_path),
        )
    else:
        server.app = App(key, settings)  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{server.server_port}/?k={key}"

    print()
    print("  北京大学回放课程 helper · 网页界面")
    if args.sessions:
        print("  模式：远端内测（每个浏览器独立会话；设置只存内存）")
        print(f"  公网入口： {args.public_url or '(由 nginx 反代)'}")
        # 不打印令牌：它已经写进 web.env，避免落到 journal / 日志里
        print("  访问令牌：已从 VIDEOLRN_WEB_KEY 读取")
    else:
        print(f"  打开： {url}")
    print("  （访问令牌别发给无关的人；Ctrl+C 结束）")
    print(flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
