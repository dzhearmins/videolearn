"""共享总结服务的单元/接口测试（本地临时目录，不联网）。"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from share_server.server import Config, Handler, Server, Store


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


OLD_SCHEMA = """
CREATE TABLE devices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lecture_key TEXT NOT NULL, uploader_id INTEGER NOT NULL,
  created_at TEXT NOT NULL, course_name TEXT, title TEXT, record_time TEXT,
  pipeline_version TEXT, prompt_hash TEXT, asr_model TEXT, llm_model TEXT, duration_sec REAL,
  transcript_sha256 TEXT NOT NULL, has_frames INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL,
  bundle_path TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, report_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(lecture_key, transcript_sha256));
INSERT INTO devices(token_hash, name, created_at) VALUES('h', '同学A', '2026-01-01');
INSERT INTO summaries(lecture_key, uploader_id, created_at, title, transcript_sha256, bytes, bundle_path)
  VALUES('1:2', 1, '2026-01-01 00:00:00', '旧版', 'aaa', 1, 'blobs/old.zip');
INSERT INTO summaries(lecture_key, uploader_id, created_at, title, transcript_sha256, bytes, bundle_path)
  VALUES('1:2', 1, '2026-01-02 00:00:00', '新版', 'bbb', 1, 'blobs/new.zip');
"""


def test_migrates_old_multi_version_schema(tmp_path):
    con = sqlite3.connect(tmp_path / "share.db")
    con.executescript(OLD_SCHEMA)
    con.commit()
    con.close()

    store = Store(tmp_path)  # 触发迁移

    items = store.lookup(["1:2"])
    assert set(items) == {"1:2"}
    assert items["1:2"]["title"] == "新版"
    assert items["1:2"]["uploader"] == "同学A"
    assert items["1:2"]["count"] == 1


def test_invite_register_and_revoke(tmp_path):
    store = Store(tmp_path)
    invite = store.create_invite()
    token, dev = store.register("同学A", invite, open_register=False)
    assert dev["name"] == "同学A"
    assert store.device_by_token(token) is not None

    with pytest.raises(Exception):
        store.register("同学B", invite, open_register=False)  # 邀请码已被使用

    assert store.revoke(dev["id"]) == 1
    assert store.device_by_token(token) is None


def test_add_summary_latest_wins_and_removes_old_blob(tmp_path):
    store = Store(tmp_path)
    _, dev = store.register("A", None, open_register=True)
    meta1 = {"transcript_sha256": _sha("t1"), "title": "旧", "asr_model": "base"}
    meta2 = {"transcript_sha256": _sha("t2"), "title": "新", "asr_model": "small"}
    r1 = store.add_summary("198750:4489574", dev["id"], meta1, b"PK\x03\x04old")
    r2 = store.add_summary("198750:4489574", dev["id"], meta2, b"PK\x03\x04new")
    assert r1["updated"] is False
    assert r2["updated"] is True
    assert r2["id"] == r1["id"]  # 同一节课原地覆盖

    items = store.lookup(["198750:4489574"])
    assert items["198750:4489574"]["title"] == "新"
    assert items["198750:4489574"]["count"] == 1

    got = store.latest_for_key("198750:4489574")
    assert got is not None
    path, row = got
    assert path.read_bytes() == b"PK\x03\x04new"
    assert len(list((tmp_path / "blobs").glob("*.zip"))) == 1  # 旧 blob 已删


def test_lookup_returns_one_row_per_lecture(tmp_path):
    store = Store(tmp_path)
    _, d1 = store.register("A", None, open_register=True)
    _, d2 = store.register("B", None, open_register=True)
    store.add_summary("1:2", d1["id"], {"transcript_sha256": _sha("a")}, b"a")
    store.add_summary("1:2", d2["id"], {"transcript_sha256": _sha("b")}, b"b")
    items = store.lookup(["1:2", "9:9"])
    assert set(items) == {"1:2"}
    assert items["1:2"]["uploader"] == "B"
    assert items["1:2"]["count"] == 1


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path)
    srv = Server(("127.0.0.1", 0), Handler)
    srv.store = store
    srv.cfg = Config(data_dir=tmp_path, open_register=False)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield srv, store
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def _call(srv, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{srv.server_port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=5)


def test_http_register_publish_lookup_bundle(server):
    srv, store = server
    invite = store.create_invite()
    with _call(srv, "POST", "/api/register", {"name": "同学A", "invite": invite}) as r:
        reg = json.loads(r.read())
    token = reg["token"]

    meta = {"transcript_sha256": _sha("hello"), "title": "第 3 次课", "asr_model": "small"}
    blob = b"PK\x03\x04fakezip"
    with _call(
        srv,
        "POST",
        "/api/summaries/198750:4489574",
        {"meta": meta, "bundle_b64": base64.b64encode(blob).decode()},
        token,
    ) as r:
        assert json.loads(r.read())["updated"] is False

    # 同一节课再传一次（模拟 LLM 升级后重跑）：只保留最新一份
    meta2 = dict(meta, transcript_sha256=_sha("hello-v2"), title="新版")
    blob2 = b"PK\x03\x04fakezip-v2"
    with _call(
        srv,
        "POST",
        "/api/summaries/198750:4489574",
        {"meta": meta2, "bundle_b64": base64.b64encode(blob2).decode()},
        token,
    ) as r:
        assert json.loads(r.read())["updated"] is True

    with _call(srv, "POST", "/api/lookup", {"keys": ["198750:4489574"]}, token) as r:
        items = json.loads(r.read())["items"]
    assert items["198750:4489574"]["uploader"] == "同学A"
    assert items["198750:4489574"]["count"] == 1
    assert items["198750:4489574"]["title"] == "新版"

    with _call(srv, "GET", "/api/summaries/198750:4489574/bundle", token=token) as r:
        assert r.read() == blob2


def test_http_requires_token_and_valid_key(server):
    srv, _ = server
    with pytest.raises(urllib.error.HTTPError) as e1:
        _call(srv, "POST", "/api/lookup", {"keys": ["1:2"]})
    assert e1.value.code == 401

    _, dev = srv.store.register("A", None, open_register=True)
    # open_register 在 store 层，HTTP 层仍走邀请码；直接拿一个有效设备令牌
    token = _token_for(srv.store, dev["id"])
    with pytest.raises(urllib.error.HTTPError) as e2:
        _call(srv, "POST", "/api/summaries/bad-key", {"meta": {}}, token)
    assert e2.value.code == 404  # 不符合 course:sub 的键不会进路由


def _token_for(store: Store, device_id: int) -> str:
    # 测试辅助：直接写一个新令牌，绕过邀请码流程
    import secrets

    from share_server.server import sha256_bytes

    token = secrets.token_urlsafe(16)
    store.db.execute(
        "UPDATE devices SET token_hash=? WHERE id=?", (sha256_bytes(token.encode()), device_id)
    )
    store.db.commit()
    return token


def test_report_hides_after_two(server):
    srv, store = server
    _, d1 = store.register("A", None, open_register=True)
    _, d2 = store.register("B", None, open_register=True)
    r = store.add_summary("1:2", d1["id"], {"transcript_sha256": _sha("x")}, b"zip")
    t1 = _token_for(store, d1["id"])
    t2 = _token_for(store, d2["id"])
    with _call(srv, "POST", "/api/summaries/1:2/report", {"id": r["id"]}, t1) as resp:
        assert json.loads(resp.read())["hidden"] is False
    with _call(srv, "POST", "/api/summaries/1:2/report", {"id": r["id"]}, t2) as resp:
        assert json.loads(resp.read())["hidden"] is True
    assert store.lookup(["1:2"]) == {}
