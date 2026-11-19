"""云端 ASR 适配层：OpenAI 兼容 /audio/transcriptions。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from videolearn import asr, media
from videolearn.config import Settings


def test_split_text_segments_distributes_time():
    segs = asr.split_text_segments("第一句话。第二句话更长一些！", 10.0)
    assert [s.text for s in segs] == ["第一句话。", "第二句话更长一些！"]
    assert segs[0].start == 0
    assert segs[-1].end == pytest.approx(10.0)
    assert segs[0].end <= segs[1].start + 1e-6


def test_make_backend_requires_cloud_key():
    with pytest.raises(RuntimeError):
        asr.make_backend(Settings(asr_backend="cloud", asr_api_key=""))


def test_make_backend_provider_defaults():
    b = asr.make_backend(Settings(asr_backend="siliconflow", asr_api_key="k"))
    assert isinstance(b, asr.CloudAsr)
    assert "siliconflow" in b.base_url
    assert b.model == asr.CLOUD_DEFAULT_MODEL


class _FakeResponse:
    def __init__(self, obj, status=200, text=""):
        self._obj = obj
        self.status_code = status
        self.text = text or json.dumps(obj)

    def json(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def test_cloud_parses_verbose_segments():
    resp = _FakeResponse(
        {
            "text": "你好世界",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "你好"},
                {"start": 1.5, "end": 3.0, "text": "世界"},
            ],
        }
    )
    segs = asr.CloudAsr._parse(resp, 3.0)
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 1.5, "你好"),
        (1.5, 3.0, "世界"),
    ]


def test_cloud_posts_wav_and_parses_text(monkeypatch):
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = body
            payload = json.dumps({"text": "第一句。第二句！"}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(asr, "_encode_wav", lambda audio: b"RIFF-fake")
        backend = asr.CloudAsr(
            f"http://127.0.0.1:{srv.server_port}/v1", "secret", "test-model"
        )
        segs = backend.transcribe([0.0] * 64000, language="zh")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    assert [s.text for s in segs] == ["第一句。", "第二句！"]
    assert seen["path"] == "/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer secret"
    assert b'test-model' in seen["body"]
    assert b'audio.wav' in seen["body"]


def test_transcribe_cloud_chunks_and_offsets(monkeypatch):
    calls: list[float] = []

    class FakeBackend:
        name = "cloud:fake"

        def transcribe(self, audio, **kwargs):
            calls.append(len(audio) / 16000)
            return [asr.Segment(0.0, 1.0, f"块{len(calls)}")]

    monkeypatch.setattr(asr, "make_backend", lambda settings, **kw: FakeBackend())
    monkeypatch.setattr(asr, "_probe_duration", lambda path: 700.0)
    monkeypatch.setattr(asr, "_decode_range", lambda path, start, seconds, ff: [0.0] * int(seconds * 16000))
    monkeypatch.setattr(media, "find_ffmpeg", lambda: "ffmpeg")

    settings = Settings(asr_backend="cloud", asr_api_key="k", asr_chunk_seconds=300)
    segs, duration = asr.transcribe("dummy.mp4", settings=settings, time_offset=600.0)

    assert duration == 700.0
    assert calls == [300.0, 300.0, 100.0]
    assert [s.start for s in segs] == [600.0, 900.0, 1200.0]
    assert [s.text for s in segs] == ["块1", "块2", "块3"]
