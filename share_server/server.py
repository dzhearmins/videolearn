"""共享总结服务：只存文字产物的"班级笔记柜"。

设计见 docs/shared-summary-cache.md：
- 不接触任何北大凭据，不存视频/音频；
- 设备令牌（邀请码注册）鉴权，不看学号；
- 只做三件事：查某个 lecture_key 有没有、取 zip、传 zip。

单文件 stdlib 实现，SQLite + 本地磁盘，方便在裸机上直接跑。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KEY_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}:[0-9A-Za-z_-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BODY = 12 * 1024 * 1024       # 单个上传包上限 12 MB
MAX_NAME = 32
MAX_TEXT = 200


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    """SQLite + blob 目录；所有方法线程安全。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.data_dir / "blobs"
        self.blob_dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.data_dir / "share.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS devices(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              token_hash TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen TEXT,
              revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS invites(
              code TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              used_by INTEGER,
              used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS summaries(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              lecture_key TEXT NOT NULL UNIQUE,
              uploader_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              course_name TEXT,
              title TEXT,
              record_time TEXT,
              pipeline_version TEXT,
              prompt_hash TEXT,
              asr_model TEXT,
              llm_model TEXT,
              duration_sec REAL,
              transcript_sha256 TEXT NOT NULL,
              has_frames INTEGER NOT NULL DEFAULT 0,
              bytes INTEGER NOT NULL,
              bundle_path TEXT NOT NULL,
              hidden INTEGER NOT NULL DEFAULT 0,
              report_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_summaries_key ON summaries(lecture_key);
            CREATE TABLE IF NOT EXISTS reports(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              summary_id INTEGER NOT NULL,
              device_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(summary_id, device_id)
            );
            """
        )
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """旧库（UNIQUE(lecture_key, transcript_sha256)，同课多版本）迁到"一课一条、最新覆盖"。"""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(summaries)")}
        if "updated_at" not in cols:
            self.db.execute("ALTER TABLE summaries ADD COLUMN updated_at TEXT")
            self.db.execute("UPDATE summaries SET updated_at=created_at WHERE updated_at IS NULL")

        need_rebuild = False
        for idx in self.db.execute("PRAGMA index_list(summaries)").fetchall():
            info = self.db.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            if [r["name"] for r in info] == ["lecture_key", "transcript_sha256"]:
                need_rebuild = True
                break
        if not need_rebuild:
            return

        self.db.executescript(
            """
            BEGIN;
            ALTER TABLE summaries RENAME TO summaries_old;
            CREATE TABLE summaries(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              lecture_key TEXT NOT NULL UNIQUE,
              uploader_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              course_name TEXT,
              title TEXT,
              record_time TEXT,
              pipeline_version TEXT,
              prompt_hash TEXT,
              asr_model TEXT,
              llm_model TEXT,
              duration_sec REAL,
              transcript_sha256 TEXT NOT NULL,
              has_frames INTEGER NOT NULL DEFAULT 0,
              bytes INTEGER NOT NULL,
              bundle_path TEXT NOT NULL,
              hidden INTEGER NOT NULL DEFAULT 0,
              report_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO summaries(
              id, lecture_key, uploader_id, created_at, updated_at, course_name, title, record_time,
              pipeline_version, prompt_hash, asr_model, llm_model, duration_sec,
              transcript_sha256, has_frames, bytes, bundle_path, hidden, report_count)
            SELECT o.id, o.lecture_key, o.uploader_id, o.created_at, COALESCE(o.updated_at, o.created_at),
                   o.course_name, o.title, o.record_time, o.pipeline_version, o.prompt_hash,
                   o.asr_model, o.llm_model, o.duration_sec, o.transcript_sha256,
                   o.has_frames, o.bytes, o.bundle_path, o.hidden, o.report_count
              FROM summaries_old o
             WHERE o.id = (SELECT id FROM summaries_old
                            WHERE lecture_key=o.lecture_key
                            ORDER BY created_at DESC, id DESC LIMIT 1);
            DROP TABLE summaries_old;
            COMMIT;
            """
        )

    # ---------------- 设备 ----------------
    def create_invite(self, code: str | None = None) -> str:
        code = code or secrets.token_urlsafe(9)
        with self.lock:
            self.db.execute(
                "INSERT INTO invites(code, created_at) VALUES(?,?)", (code, utcnow())
            )
            self.db.commit()
        return code

    def register(self, name: str, invite: str | None, open_register: bool) -> tuple[str, dict]:
        name = (name or "").strip()[:MAX_NAME]
        if not name:
            raise ApiError(400, "昵称不能为空")
        token = secrets.token_urlsafe(24)
        with self.lock:
            if not open_register:
                row = self.db.execute(
                    "SELECT * FROM invites WHERE code=? AND used_by IS NULL", (invite or "",)
                ).fetchone()
                if row is None:
                    raise ApiError(403, "邀请码无效或已被使用")
            cur = self.db.execute(
                "INSERT INTO devices(token_hash, name, created_at) VALUES(?,?,?)",
                (sha256_bytes(token.encode()), name, utcnow()),
            )
            device_id = int(cur.lastrowid)
            if not open_register:
                self.db.execute(
                    "UPDATE invites SET used_by=?, used_at=? WHERE code=?",
                    (device_id, utcnow(), invite),
                )
            self.db.commit()
        return token, {"id": device_id, "name": name}

    def device_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        th = sha256_bytes(token.encode())
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM devices WHERE token_hash=? AND revoked=0", (th,)
            ).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE devices SET last_seen=? WHERE id=?", (utcnow(), row["id"]))
            self.db.commit()
        return dict(row)

    def list_devices(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,name,created_at,last_seen,revoked FROM devices ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def revoke(self, device_id: int) -> int:
        with self.lock:
            cur = self.db.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
            self.db.commit()
        return cur.rowcount

    # ---------------- 总结 ----------------
    def lookup(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        with self.lock:
            rows = self.db.execute(
                f"""SELECT s.*, d.name AS uploader FROM summaries s
                    JOIN devices d ON d.id=s.uploader_id
                    WHERE s.hidden=0 AND s.lecture_key IN ({q})
                    ORDER BY s.created_at DESC""",
                keys,
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            out[r["lecture_key"]] = {
                "id": r["id"],
                "uploader": r["uploader"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "title": r["title"],
                "course_name": r["course_name"],
                "asr_model": r["asr_model"],
                "llm_model": r["llm_model"],
                "pipeline_version": r["pipeline_version"],
                "prompt_hash": r["prompt_hash"],
                "duration_sec": r["duration_sec"],
                "bytes": r["bytes"],
                "has_frames": bool(r["has_frames"]),
                "count": 1,
            }
        return out

    def add_summary(self, key: str, uploader_id: int, meta: dict, blob: bytes) -> dict:
        """同一节课只保留最新一份：已有记录就原地覆盖，旧 blob 删除。"""
        digest = sha256_bytes(blob)
        rel = Path("blobs") / f"{key.replace(':', '_')}_{digest[:16]}.zip"
        dest = self.data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".tmp-{secrets.token_hex(8)}")
        tmp.write_bytes(blob)
        now = utcnow()
        values = (
            uploader_id,
            now,
            _clip(meta.get("course_name"), MAX_TEXT),
            _clip(meta.get("title"), MAX_TEXT),
            _clip(meta.get("record_time"), MAX_TEXT),
            _clip(meta.get("pipeline_version"), 40),
            _clip(meta.get("prompt_hash"), 40),
            _clip(meta.get("asr_model"), 40),
            _clip(meta.get("llm_model"), 60),
            meta.get("duration_sec"),
            str(meta.get("transcript_sha256") or "")[:64],
            1 if meta.get("has_frames") else 0,
            len(blob),
            str(rel),
        )
        try:
            with self.lock:
                row = self.db.execute(
                    "SELECT id, bundle_path FROM summaries WHERE lecture_key=?", (key,)
                ).fetchone()
                if row is not None:
                    self.db.execute(
                        """UPDATE summaries SET uploader_id=?, updated_at=?, course_name=?, title=?,
                             record_time=?, pipeline_version=?, prompt_hash=?, asr_model=?, llm_model=?,
                             duration_sec=?, transcript_sha256=?, has_frames=?, bytes=?, bundle_path=?,
                             hidden=0, report_count=0
                           WHERE id=?""",
                        values + (row["id"],),
                    )
                    self.db.execute("DELETE FROM reports WHERE summary_id=?", (row["id"],))
                    self.db.commit()
                    os.replace(tmp, dest)
                    old = row["bundle_path"]
                    if old != str(rel):
                        (self.data_dir / old).unlink(missing_ok=True)
                    return {"id": int(row["id"]), "updated": True}

                cur = self.db.execute(
                    """INSERT INTO summaries(
                         lecture_key, uploader_id, created_at, updated_at, course_name, title, record_time,
                         pipeline_version, prompt_hash, asr_model, llm_model, duration_sec,
                         transcript_sha256, has_frames, bytes, bundle_path)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, uploader_id, now, now, *values[2:]),  # created_at 与 updated_at 同为 now
                )
                self.db.commit()
                os.replace(tmp, dest)
                return {"id": int(cur.lastrowid), "updated": False}
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get_bundle(self, summary_id: int) -> tuple[Path, dict] | None:
        with self.lock:
            row = self.db.execute(
                """SELECT s.*, d.name AS uploader FROM summaries s
                   JOIN devices d ON d.id=s.uploader_id
                   WHERE s.id=? AND s.hidden=0""",
                (summary_id,),
            ).fetchone()
        if row is None:
            return None
        p = self.data_dir / row["bundle_path"]
        return (p, dict(row)) if p.is_file() else None

    def latest_for_key(self, key: str) -> tuple[Path, dict] | None:
        with self.lock:
            row = self.db.execute(
                """SELECT s.*, d.name AS uploader FROM summaries s
                   JOIN devices d ON d.id=s.uploader_id
                   WHERE s.lecture_key=? AND s.hidden=0
                   ORDER BY s.created_at DESC LIMIT 1""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        p = self.data_dir / row["bundle_path"]
        return (p, dict(row)) if p.is_file() else None

    def delete_summary(self, summary_id: int, device_id: int) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT bundle_path, uploader_id FROM summaries WHERE id=?", (summary_id,)
            ).fetchone()
            if row is None or row["uploader_id"] != device_id:
                return False
            self.db.execute("DELETE FROM summaries WHERE id=?", (summary_id,))
            self.db.execute("DELETE FROM reports WHERE summary_id=?", (summary_id,))
            self.db.commit()
        (self.data_dir / row["bundle_path"]).unlink(missing_ok=True)
        return True

    def report(self, summary_id: int, device_id: int) -> dict:
        with self.lock:
            exists = self.db.execute(
                "SELECT id FROM summaries WHERE id=?", (summary_id,)
            ).fetchone()
            if exists is None:
                raise ApiError(404, "没有这个共享")
            try:
                self.db.execute(
                    "INSERT INTO reports(summary_id, device_id, created_at) VALUES(?,?,?)",
                    (summary_id, device_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                pass
            self.db.execute(
                "UPDATE summaries SET report_count=(SELECT COUNT(*) FROM reports WHERE summary_id=?) WHERE id=?",
                (summary_id, summary_id),
            )
            row = self.db.execute(
                "SELECT report_count FROM summaries WHERE id=?", (summary_id,)
            ).fetchone()
            count = int(row["report_count"]) if row else 0
            if count >= 2:
                self.db.execute("UPDATE summaries SET hidden=1 WHERE id=?", (summary_id,))
            self.db.commit()
        return {"reports": count, "hidden": count >= 2}

    def stats(self) -> dict:
        with self.lock:
            s = self.db.execute("SELECT COUNT(*) c, COALESCE(SUM(bytes),0) b FROM summaries WHERE hidden=0").fetchone()
            d = self.db.execute("SELECT COUNT(*) c FROM devices WHERE revoked=0").fetchone()
        return {"summaries": int(s["c"]), "bytes": int(s["b"]), "devices": int(d["c"])}


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
@dataclass
class Config:
    data_dir: Path
    open_register: bool = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VideoLearnShare/0.1"
    sys_version = ""

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        return

    # ---- 基础 ----
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ApiError(413, f"请求体超过 {MAX_BODY // 1024 // 1024} MB")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        try:
            obj = json.loads(self._body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "请求体不是合法 JSON") from exc
        if not isinstance(obj, dict):
            raise ApiError(400, "请求体必须是对象")
        return obj

    def _device(self, required: bool = True) -> dict | None:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        dev = self.store.device_by_token(token)
        if dev is None and required:
            raise ApiError(401, "无效或已撤销的设备令牌")
        return dev

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/health":
                self._json({"ok": True, **self.store.stats()})
                return
            if path.startswith("/api/summaries/") and path.endswith("/bundle"):
                self._bundle(path)
                return
            self._json({"error": "not_found"}, 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/register":
                body = self._json_body()
                token, dev = self.store.register(
                    str(body.get("name") or ""), body.get("invite"), self.cfg.open_register
                )
                self._json({"ok": True, "token": token, "device": dev})
                return
            if path == "/api/lookup":
                self._device()
                body = self._json_body()
                keys = [k for k in (body.get("keys") or []) if isinstance(k, str) and KEY_RE.match(k)]
                self._json({"items": self.store.lookup(keys[:200])})
                return
            m = re.fullmatch(r"/api/summaries/([0-9A-Za-z_-]+:[0-9A-Za-z_-]+)/report", path)
            if m:
                dev = self._device()
                body = self._json_body()
                sid = int(body.get("id") or 0)
                self._json({"ok": True, **self.store.report(sid, int(dev["id"]))})
                return
            m = re.fullmatch(r"/api/summaries/([0-9A-Za-z_-]+:[0-9A-Za-z_-]+)", path)
            if m:
                self._put_summary(m.group(1))
                return
            self._json({"error": "not_found"}, 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            dev = self._device()
            m = re.fullmatch(r"/api/summaries/([0-9A-Za-z_-]+:[0-9A-Za-z_-]+)/(\d+)", urllib.parse.urlparse(self.path).path)
            if not m:
                raise ApiError(404, "not_found")
            if not self.store.delete_summary(int(m.group(2)), int(dev["id"])):
                raise ApiError(403, "只能删除自己上传的版本")
            self._json({"ok": True})
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---- 具体动作 ----
    def _put_summary(self, key: str) -> None:
        dev = self._device()
        body = self._json_body()
        meta = body.get("meta") or {}
        if not isinstance(meta, dict):
            raise ApiError(400, "meta 必须是对象")
        b64 = body.get("bundle_b64") or ""
        try:
            blob = base64.b64decode(b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "bundle_b64 不是合法 base64") from exc
        if not blob or len(blob) > MAX_BODY:
            raise ApiError(400, "包为空或过大")
        if not SHA_RE.match(str(meta.get("transcript_sha256") or "")):
            raise ApiError(400, "缺少 transcript_sha256")
        result = self.store.add_summary(key, int(dev["id"]), meta, blob)
        self._json({"ok": True, **result})

    def _bundle(self, path: str) -> None:
        self._device()
        m = re.fullmatch(r"/api/summaries/([0-9A-Za-z_-]+:[0-9A-Za-z_-]+)/bundle", path)
        if not m:
            raise ApiError(404, "not_found")
        got = self.store.latest_for_key(m.group(1))
        if got is None:
            raise ApiError(404, "没有这个共享")
        p, meta = got
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", f'"{sha256_bytes(data)[:32]}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    store: Store
    cfg: Config


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _default_data_dir() -> Path:
    return Path(os.environ.get("SHARE_DATA_DIR") or Path.home() / "videolearn-share" / "data")


def cmd_serve(args) -> int:
    data_dir = Path(args.data_dir or _default_data_dir())
    store = Store(data_dir)
    cfg = Config(data_dir=data_dir, open_register=args.open_register)
    server = Server((args.host, args.port), Handler)
    server.store = store
    server.cfg = cfg
    print(f"share server on http://{args.host}:{args.port}  data={data_dir}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_invite(args) -> int:
    store = Store(Path(args.data_dir or _default_data_dir()))
    for _ in range(max(1, args.count)):
        print(store.create_invite())
    return 0


def cmd_devices(args) -> int:
    store = Store(Path(args.data_dir or _default_data_dir()))
    for d in store.list_devices():
        flag = "revoked" if d["revoked"] else "active"
        print(f"{d['id']:>3}  {d['name']:<20} {flag:<8} created={d['created_at']} last={d['last_seen']}")
    return 0


def cmd_revoke(args) -> int:
    store = Store(Path(args.data_dir or _default_data_dir()))
    n = store.revoke(args.id)
    print("revoked" if n else "not found")
    return 0


def cmd_stats(args) -> int:
    store = Store(Path(args.data_dir or _default_data_dir()))
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="share_server", description="videolearn 共享总结服务")
    p.add_argument("--data-dir", help="数据目录（默认 ~/videolearn-share/data）")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--open-register", action="store_true", help="允许无邀请码注册（仅测试）")
    serve.set_defaults(func=cmd_serve)

    invite = sub.add_parser("invite", help="生成邀请码")
    invite.add_argument("--count", type=int, default=1)
    invite.set_defaults(func=cmd_invite)

    devices = sub.add_parser("devices", help="列出设备")
    devices.set_defaults(func=cmd_devices)

    revoke = sub.add_parser("revoke", help="撤销设备")
    revoke.add_argument("id", type=int)
    revoke.set_defaults(func=cmd_revoke)

    stats = sub.add_parser("stats", help="统计")
    stats.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
