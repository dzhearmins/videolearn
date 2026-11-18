"""本机 Web UI 的单元测试（不联网、不启动真实任务）。"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from dotenv import dotenv_values

from videolearn import web
from videolearn.config import Settings
from videolearn.web import LineSink, _mask_id, _mask_key, artifact_path, update_env_file


# ---------------------------------------------------------------- .env
def test_update_env_roundtrips_and_preserves_others(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# 注释\nPKU_STUDENT_ID=old\nOTHER=keep=1\n", encoding="utf-8")

    update_env_file(path, {"PKU_STUDENT_ID": "1106180264", "PKU_PASSWORD": 'p@ss "x"\\y\nz'})

    values = dotenv_values(path)
    assert values["PKU_STUDENT_ID"] == "1106180264"
    assert values["PKU_PASSWORD"] == 'p@ss "x"\\y\nz'
    assert values["OTHER"] == "keep=1"
    text = path.read_text(encoding="utf-8")
    assert "# 注释" in text
    assert text.count("PKU_STUDENT_ID=") == 1


def test_update_env_appends_missing_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    update_env_file(path, {"NEW_KEY": "with space", "EMPTY": ""})
    values = dotenv_values(path)
    assert values["A"] == "1"
    assert values["NEW_KEY"] == "with space"
    assert values["EMPTY"] == ""


# ---------------------------------------------------------------- 产物路径
def test_artifact_path_whitelist(tmp_path):
    (tmp_path / "retell.md").write_text("x", encoding="utf-8")
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "12-00-00.jpg").write_bytes(b"jpeg")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")

    assert artifact_path(tmp_path, "retell.md") == (tmp_path / "retell.md").resolve()
    assert artifact_path(tmp_path, "frames/12-00-00.jpg") is not None

    assert artifact_path(tmp_path, "secret.txt") is None
    assert artifact_path(tmp_path, "../outside.txt") is None
    assert artifact_path(tmp_path, "frames/../../outside.jpg") is None
    assert artifact_path(tmp_path, "/etc/passwd") is None
    assert artifact_path(tmp_path, "frames/not-a-frame.txt") is None


# ---------------------------------------------------------------- 输出捕获
def test_line_sink_splits_and_finishes():
    sink = LineSink()
    sink.write("[1/5] 解析播放地址 …\n[asr] #0")
    lines, cursor, state = sink.wait(0, 0.01)
    assert lines == ["[1/5] 解析播放地址 …"]
    assert cursor == 1
    assert state == "running"

    sink.finish("done")
    lines, cursor, state = sink.wait(cursor, 0.01)
    assert lines == ["[asr] #0"]
    assert state == "done"


def test_line_sink_wait_times_out_without_output():
    sink = LineSink()
    sink.write("a\n")
    lines, cursor, state = sink.wait(1, 0.05)
    assert lines == []
    assert state == "running"


# ---------------------------------------------------------------- 打码
def test_masks():
    assert _mask_id("1106180264") == "1106***264"
    assert _mask_id("") == ""
    assert _mask_key("sk-1234567890abcdef") == "sk-1…cdef"
    assert _mask_key("") == ""


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web.App, "authenticated", lambda self, check=False: False)
    srv = web.Server(("127.0.0.1", 0), web.Handler)
    srv.app = web.App("testkey", Settings())
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.server_port}{path}"


def _get(srv, path: str, key: str | None = "testkey"):
    headers = {"X-VL-Key": key} if key else {}
    return urllib.request.urlopen(urllib.request.Request(_url(srv, path), headers=headers), timeout=5)


def _post(srv, path: str, body: dict, key: str | None = "testkey"):
    headers = {"X-VL-Key": key} if key else {}
    headers["Content-Type"] = "application/json"
    return urllib.request.urlopen(
        urllib.request.Request(
            _url(srv, path), data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        ),
        timeout=5,
    )


def test_api_rejects_missing_token(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(_url(server, "/api/status"), timeout=5)
    assert exc.value.code == 403


def test_api_status_with_token(server):
    with _get(server, "/api/status") as resp:
        assert resp.status == 200
        data = json.loads(resp.read())
    assert data["authenticated"] is False
    assert "ffmpeg" in data
    assert "jobs" in data


def test_index_requires_key(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(_url(server, "/"), timeout=5)
    assert exc.value.code == 403

    with _get(server, "/?k=testkey", key=None) as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
    assert "回放课程 helper" in html
    # 令牌必须作为完整的 JS 字面量注入，不能污染标识符
    assert 'const KEY = "testkey";' in html
    assert "__VL_KEY_JSON__" not in html


def test_wrong_key_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/status", key="wrong")
    assert exc.value.code == 403


def test_index_debug_sample_is_600_seconds(server):
    with _get(server, "/?k=testkey", key=None) as resp:
        html = resp.read().decode("utf-8")
    assert "const SAMPLE_SECONDS = 600;" in html
    assert "只跑前 90 秒" not in html


def test_job_accepts_600s_sample_and_rejects_out_of_range(server, monkeypatch):
    captured: dict = {}

    def fake_start_job(self, params):
        captured.update(params)
        return web.Job(id="sample1", params=params)

    monkeypatch.setattr(web.App, "start_job", fake_start_job)
    body = {
        "play_url": "https://course.pku.edu.cn/web/learning/study/?vc=1",
        "title": "小样",
        "limit_seconds": 600,
    }
    with _post(server, "/api/jobs", body) as resp:
        assert resp.status == 202
        data = json.loads(resp.read())
    assert data["job"]["params"]["limit_seconds"] == 600
    assert captured["limit_seconds"] == 600

    for bad in (9, 3601):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(server, "/api/jobs", {**body, "limit_seconds": bad})
        assert exc.value.code == 400


def test_job_rejects_foreign_play_url(server):
    body = json.dumps({"play_url": "https://evil.example/x"}).encode("utf-8")
    req = urllib.request.Request(
        _url(server, "/api/jobs"),
        data=body,
        headers={"X-VL-Key": "testkey", "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


def test_job_file_endpoint_serves_whitelisted_only(server, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "retell.md").write_text("# 重述", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")

    job = web.Job(id="abc123", params={})
    job.out_dir = out
    job.state = "done"
    server.app.jobs["abc123"] = job

    with _get(server, "/api/jobs/abc123/file?name=retell.md") as resp:
        assert resp.status == 200
        assert resp.read().decode("utf-8") == "# 重述"

    for bad in ("../secret.txt", "secret.txt", "/etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(server, f"/api/jobs/abc123/file?name={urllib.parse.quote(bad)}")
        assert exc.value.code == 404


def test_summary_includes_course_key():
    job = web.Job(id="x", params={"course_key": "199138", "title": "第 3 次课"})
    summary = job.summary()
    assert summary["course_key"] == "199138"
    assert summary["params"]["title"] == "第 3 次课"
    assert "play_url" not in summary["params"]  # 含短期 JWT，不能回传


def test_jobs_list_filters_by_course(server):
    for jid, course in (("j1", "_1_1"), ("j2", "_2_2")):
        job = web.Job(id=jid, params={"course_key": course, "title": "t", "time": "2026-01-01"})
        job.state = "done"
        server.app.jobs[jid] = job

    with _get(server, "/api/jobs?course=_1_1") as resp:
        data = json.loads(resp.read())
    assert [j["id"] for j in data["jobs"]] == ["j1"]

    with _get(server, "/api/jobs") as resp:
        data = json.loads(resp.read())
    assert {j["id"] for j in data["jobs"]} == {"j1", "j2"}


def test_live_returns_streaming_and_final_files(server, tmp_path):
    out = tmp_path / "live"
    out.mkdir()
    (out / "stream_restated.md").write_text("# 初稿\n第一段", encoding="utf-8")
    (out / "transcript.partial.md").write_text("[00:00:01] 你好", encoding="utf-8")
    job = web.Job(id="live1", params={"course_key": "_1_1"})
    job.out_dir = out
    job.state = "running"
    server.app.jobs["live1"] = job

    with _get(server, "/api/jobs/live1/live") as resp:
        data = json.loads(resp.read())
    keys = {f["key"]: f["text"] for f in data["files"]}
    assert keys["draft"].startswith("# 初稿")
    assert keys["partial"].startswith("[00:00:01]")
    assert data["state"] == "running"

    (out / "retell.md").write_text("# 重述", encoding="utf-8")
    job.state = "done"
    with _get(server, "/api/jobs/live1/live") as resp:
        data = json.loads(resp.read())
    assert "retell" in {f["key"] for f in data["files"]}


def test_live_tolerates_half_written_utf8(server, tmp_path):
    out = tmp_path / "live2"
    out.mkdir()
    (out / "transcript.partial.md").write_bytes("你好".encode("utf-8")[:-1])  # 截断半个汉字
    job = web.Job(id="live2", params={})
    job.out_dir = out
    server.app.jobs["live2"] = job

    with _get(server, "/api/jobs/live2/live") as resp:
        data = json.loads(resp.read())
    text = {f["key"]: f["text"] for f in data["files"]}["partial"]
    assert "你" in text  # 不抛 UnicodeDecodeError，坏字节变成替换符


def test_live_strips_superseded_notice(tmp_path):
    out = tmp_path / "live3"
    out.mkdir()
    (out / "stream_draft.superseded.md").write_text(
        "> 本文件是流式初稿，已被 retell.md 的全量总结取代，仅作审计。\n\n# 草稿\n正文\n",
        encoding="utf-8",
    )
    job = web.Job(id="live3", params={})
    job.out_dir = out
    data = job.live()
    text = {f["key"]: f["text"] for f in data["files"]}["draft_final"]
    assert text.startswith("# 草稿")
    assert "取代" not in text


def test_live_reports_eyes_and_frames(tmp_path):
    out = tmp_path / "eyes"
    out.mkdir()
    (out / "frames").mkdir()
    (out / "frames" / "00-00-08.jpg").write_bytes(b"jpeg")
    (out / "meta.json").write_text(
        json.dumps(
            {
                "eyes": True,
                "eyes_status": "finished",
                "vision_model": "deepseek-flash",
                "shots": [{"time": "00:00:08"}],
            }
        ),
        encoding="utf-8",
    )
    job = web.Job(id="eyes1", params={"eyes": True})
    job.out_dir = out
    job.started = time.time()

    data = job.live()
    assert data["eyes"]["enabled"] is True
    assert data["eyes"]["done"] is True
    assert data["eyes"]["status"] == "finished"
    assert data["eyes"]["shots"] == 1
    assert data["eyes"]["vision_model"] == "deepseek-flash"
    assert [f["name"] for f in data["frames"]] == ["frames/00-00-08.jpg"]


def test_artifacts_ignore_previous_run(tmp_path):
    out = tmp_path / "rerun"
    out.mkdir()
    old = out / "retell.md"
    old.write_text("上一次的结果", encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))

    job = web.Job(id="rerun", params={})
    job.out_dir = out
    job.started = time.time()

    # 同一个输出目录重跑时，旧产物不能冒充本次结果
    assert job.artifacts() == []
    assert job.live()["files"] == []

    (out / "transcript.md").write_text("本次的文字稿", encoding="utf-8")
    assert {a["name"] for a in job.artifacts()} == {"transcript.md"}


# ---------------------------------------------------------------- 共享
def test_job_from_shared_requires_share_config(server):
    body = {
        "play_url": "https://course.pku.edu.cn/x",
        "title": "t",
        "time": "2026-11-18 10:00:00",
        "course_name": "c",
        "lecture_key": "1:2",
        "from_shared": True,
    }
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/jobs", body)
    assert exc.value.code == 400


def _share_job(tmp_path) -> web.Job:
    out = tmp_path / "job"
    out.mkdir(parents=True)
    (out / "retell.md").write_text("# 重述", encoding="utf-8")
    (out / "transcript.md").write_text("[00:00:00] 你好", encoding="utf-8")
    (out / "meta.json").write_text(
        json.dumps({"course": "高等代数", "title": "第 3 次课", "whisper_model": "small"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    job = web.Job(id="jshare", params={"lecture_key": "1:2"})
    job.state = "done"
    job.out_dir = out
    return job


def _app_with_fake_client(monkeypatch, client):
    from videolearn import share as sharelib

    app = web.App("k", Settings())
    app.share = sharelib.ShareConfig(server="http://example", token="t", name="n")
    monkeypatch.setattr(app, "share_client", lambda: client)
    return app


def test_auto_share_publishes_completed_job(tmp_path, monkeypatch):
    captured: dict = {}

    class FakeClient:
        def publish(self, key, meta, blob):
            captured.update(key=key, meta=meta, size=len(blob))
            return {"id": 7, "updated": False}

    app = _app_with_fake_client(monkeypatch, FakeClient())
    job = _share_job(tmp_path)

    app._auto_share(job)

    assert job.share["state"] == "done"
    assert captured["key"] == "1:2"
    assert captured["meta"]["title"] == "第 3 次课"
    assert captured["meta"]["transcript_sha256"]
    assert captured["size"] > 0


def test_auto_share_failure_is_non_fatal(tmp_path, monkeypatch):
    from videolearn import share as sharelib

    class Boom:
        def publish(self, key, meta, blob):
            raise sharelib.ShareError("服务挂了")

    app = _app_with_fake_client(monkeypatch, Boom())
    job = _share_job(tmp_path)

    app._auto_share(job)  # 不抛异常

    assert job.share["state"] == "failed"
    assert "服务挂了" in job.share["message"]


def test_auto_share_skips_sample(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeClient:
        def publish(self, key, meta, blob):
            calls.append(key)
            return {"id": 1}

    app = _app_with_fake_client(monkeypatch, FakeClient())
    sample = _share_job(tmp_path)
    sample.params["limit_seconds"] = 90

    app._auto_share(sample)

    assert sample.share["state"] == "skipped"
    assert calls == []


def test_auto_share_off_when_not_configured(tmp_path):
    app = web.App("k", Settings())  # 默认没有 SHARE_*
    job = _share_job(tmp_path)

    app._auto_share(job)

    assert job.share["state"] == "off"


def test_auto_share_is_idempotent_per_job(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeClient:
        def publish(self, key, meta, blob):
            calls.append(key)
            return {"id": 1}

    app = _app_with_fake_client(monkeypatch, FakeClient())
    job = _share_job(tmp_path)

    app._auto_share(job)
    app._auto_share(job)  # 重入（例如重复回调）不能重复上传

    assert calls == ["1:2"]


def test_run_triggers_auto_upload(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from videolearn import share as sharelib

    calls: list[str] = []

    class FakeClient:
        def publish(self, key, meta, blob):
            calls.append(key)
            return {"id": 1, "updated": False}

    app = web.App("k", Settings())
    app.share = sharelib.ShareConfig(server="http://x", token="t", name="n")
    monkeypatch.setattr(app, "share_client", lambda: FakeClient())
    monkeypatch.setattr(app, "ensured_client", lambda force=False: object())

    out = _share_job(tmp_path).out_dir
    monkeypatch.setattr(web.pipeline, "run_for_video", lambda *a, **k: SimpleNamespace(out_dir=out))

    job = web.Job(
        id="jrun",
        params={
            "title": "t",
            "time": "2026-11-18 10:00:00",
            "play_url": "https://course.pku.edu.cn/x",
            "course_name": "c",
            "lecture_key": "1:2",
        },
    )
    app._run(job)

    deadline = time.time() + 3
    while time.time() < deadline and (job.share or {}).get("state") == "uploading":
        time.sleep(0.02)
    assert job.state == "done"
    assert job.share["state"] == "done"
    assert calls == ["1:2"]


# ---------------------------------------------------------------- 视频存储
def _video(tmp_path, name="video.mp4", size=1234):
    d = tmp_path / "高等代数" / "第3次课"
    d.mkdir(parents=True)
    p = d / name
    p.write_bytes(b"x" * size)
    return d, p


def test_storage_lists_video_files_with_sizes(tmp_path):
    app = web.App("k", Settings(output_dir=tmp_path))
    _, p = _video(tmp_path)
    (p.parent / "transcript.md").write_bytes(b"y" * 10)

    data = app.storage()

    assert [i["rel"] for i in data["items"]] == ["高等代数/第3次课/video.mp4"]
    assert data["items"][0]["bytes"] == 1234
    assert data["total_bytes"] == 1234
    assert data["free_bytes"] > 0


def test_delete_video_validates_path_and_removes(tmp_path):
    app = web.App("k", Settings(output_dir=tmp_path))
    _, p = _video(tmp_path)
    rel = "高等代数/第3次课/video.mp4"

    for bad in ("../outside/video.mp4", "/etc/passwd", "C:\\Windows\\win.ini",
                "高等代数/第3次课/transcript.md", ""):
        with pytest.raises(web.ApiError):
            app.delete_video(bad)

    result = app.delete_video(rel)
    assert result["freed_bytes"] == 1234
    assert not p.exists()
    assert result["storage"]["items"] == []

    with pytest.raises(web.ApiError) as exc:  # 第二次删除
        app.delete_video(rel)
    assert exc.value.status == 404


def test_delete_video_refuses_active_job(tmp_path):
    app = web.App("k", Settings(output_dir=tmp_path))
    d, p = _video(tmp_path)
    job = web.Job(id="j1", params={})
    job.out_dir = d
    app.jobs["j1"] = job
    app.current = "j1"

    with pytest.raises(web.ApiError) as exc:
        app.delete_video("高等代数/第3次课/video.mp4")
    assert exc.value.status == 409
    assert p.exists()


def test_auto_clean_deletes_only_job_video(tmp_path):
    app = web.App("k", Settings(output_dir=tmp_path))
    d, p = _video(tmp_path)
    keep = d / "transcript.md"
    keep.write_bytes(b"text")

    job = web.Job(id="j1", params={})
    job.out_dir = d
    app._auto_clean(job)

    assert not p.exists()
    assert keep.exists()  # 文案产物必须保留
    assert job.clean["state"] == "done"
    assert job.clean["freed_bytes"] == 1234


def test_storage_api_list_and_delete(server, tmp_path):
    server.app.settings.output_dir = tmp_path
    d, p = _video(tmp_path)
    rel = "高等代数/第3次课/video.mp4"

    with _get(server, "/api/storage") as resp:
        data = json.loads(resp.read())
    assert [i["rel"] for i in data["items"]] == [rel]

    with _post(server, "/api/storage/delete", {"rel": rel}) as resp:
        result = json.loads(resp.read())
    assert result["ok"] is True
    assert result["freed_bytes"] == 1234
    assert not p.exists()

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/api/storage/delete", {"rel": "../windows/win.ini"})
    assert exc.value.code == 400


def test_settings_toggle_auto_cleanup(server, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setenv("VIDEOLRN_AUTO_CLEAN_VIDEO", "0")

    with _post(server, "/api/settings", {"auto_cleanup": True}) as resp:
        assert resp.status == 200

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VIDEOLRN_AUTO_CLEAN_VIDEO=1" in env_text
