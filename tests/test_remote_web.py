"""远端内测模式：会话隔离、内存设置、单任务锁、子路径 Cookie。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from videolearn import web
from videolearn.config import Settings


def _base(tmp_path) -> Settings:
    return Settings(
        output_dir=tmp_path / "base-output",
        state_dir=tmp_path / "base-state",
        deepseek_api_key="sk-shared",
        llm_model="deepseek-chat",
    )


def test_sessions_are_isolated_and_do_not_write_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEOLRN_HOME", str(tmp_path / "home"))
    store = web.SessionStore("k", _base(tmp_path), tmp_path / "sessions", cookie_path="/secret/")

    a, sid_a, new_a = store.get(None)
    b, sid_b, new_b = store.get(None)
    assert new_a and new_b and sid_a != sid_b
    assert a.settings.output_dir != b.settings.output_dir
    assert a.settings.output_dir.parent == tmp_path / "sessions" / sid_a
    assert a.settings.deepseek_api_key == "sk-shared"  # 服务器默认 key 可继承
    assert a.settings.student_id == "" and a.settings.password == ""

    a.apply_settings(
        {
            "PKU_STUDENT_ID": "1106180264",
            "PKU_PASSWORD": "secret",
            "LLM_MODEL": "deepseek-chat",
            "VIDEOLRN_AUTO_CLEAN_VIDEO": "1",
        }
    )

    assert a.settings.student_id == "1106180264"
    assert a.settings.password == "secret"
    assert a.settings.auto_clean_video is True
    # 另一个会话看不到，也没有落盘
    assert b.settings.student_id == ""
    assert b.settings.password == ""
    assert not (tmp_path / "home" / ".env").exists()

    # 同一个 sid 再取还是同一个会话
    again, sid_again, new_again = store.get(sid_a)
    assert again is a and sid_again == sid_a and new_again is False


def test_invalid_sid_is_replaced_not_used_as_path(tmp_path):
    store = web.SessionStore("k", _base(tmp_path), tmp_path / "sessions")
    app, sid, new = store.get("../../escape")
    assert new is True
    assert sid != "../../escape"
    assert app.settings.output_dir.parent.parent == (tmp_path / "sessions").resolve()


def test_job_lock_serializes(tmp_path):
    lock1 = web.JobLock(tmp_path / "job.lock")
    lock2 = web.JobLock(tmp_path / "job.lock")
    assert lock1.acquire() is True
    assert lock2.acquire() is False
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()
    assert lock1.acquire() is True
    lock1.release()


def test_session_mode_sets_cookie_and_isolates_jobs(tmp_path):
    base = Settings()

    srv = web.Server(("127.0.0.1", 0), web.Handler)
    srv.sessions = web.SessionStore("testkey", base, tmp_path / "sessions", cookie_path="/secret/")
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port = srv.server_port

        def get(path: str, cookie: str = "", key: str = "testkey"):
            headers = {"X-VL-Key": key}
            if cookie:
                headers["Cookie"] = cookie
            return urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers),
                timeout=5,
            )

        with get("/") as resp:
            assert resp.status == 200
            set_cookie = resp.headers.get("Set-Cookie", "")
            html = resp.read().decode("utf-8")
        assert set_cookie.startswith("vl_sid=")
        assert "Path=/secret/" in set_cookie
        assert 'const KEY = "testkey";' in html
        cookie = set_cookie.split(";", 1)[0]

        with get("/api/status", cookie=cookie) as resp:
            data = json.loads(resp.read())
        assert data["api_key"] == ""
        job = web.Job(id="only-a", params={"title": "t"})
        app, sid, _ = srv.sessions.get(cookie.split("=", 1)[1])
        app.jobs["only-a"] = job

        # 没有 Cookie 的另一个浏览器看不到 A 的任务
        with get("/api/status") as resp:
            other = json.loads(resp.read())
        assert [j["id"] for j in other["jobs"]] == []
        assert other["student_id"] == ""

        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/api/status", key="wrong")
        assert exc.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_index_injects_notice_and_sample_seconds():
    html = web._index_html(
        "k", notice="内测：请用小样", sample_seconds=90, sample_start_seconds=600
    ).decode("utf-8")
    assert 'const SAMPLE_SECONDS = 90;' in html
    assert 'const SAMPLE_START_SECONDS = 600;' in html
    assert 'const NOTICE = "内测：请用小样";' in html
    assert "__VL_NOTICE_JSON__" not in html
    assert "__VL_SAMPLE_SECONDS__" not in html
    assert "__VL_SAMPLE_START_SECONDS__" not in html


def test_session_settings_do_not_touch_other_session(tmp_path):
    store = web.SessionStore("k", _base(tmp_path), tmp_path / "sessions")
    a, _, _ = store.get(None)
    b, _, _ = store.get(None)
    a.apply_settings({"DEEPSEEK_API_KEY": "sk-a", "PKU_STUDENT_ID": "1"})
    b.apply_settings({"DEEPSEEK_API_KEY": "sk-b", "PKU_STUDENT_ID": "2"})
    assert a.settings.llm_key == "sk-a" and a.settings.student_id == "1"
    assert b.settings.llm_key == "sk-b" and b.settings.student_id == "2"

    a.apply_settings(
        {
            "VIDEOLRN_ASR_BACKEND": "cloud",
            "ASR_BASE_URL": "http://example/v1/",
            "ASR_MODEL": "m",
            "ASR_API_KEY": "k",
        }
    )
    assert a.settings.asr_backend == "cloud"
    assert a.settings.asr_base_url == "http://example/v1"
    assert a.settings.asr_model == "m"
    assert a.settings.asr_api_key == "k"
    assert b.settings.asr_backend == "local"
