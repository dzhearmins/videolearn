"""跨平台兼容性的单元测试：不联网、不依赖已安装的 ffmpeg。"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from videolearn import media, pipeline
from videolearn.term import soften_console

EXE = ".exe" if os.name == "nt" else ""


def _touch(path: Path) -> Path:
    path.write_bytes(b"not-a-real-binary")
    return path


def test_find_ffprobe_prefers_sibling(tmp_path, monkeypatch):
    ffmpeg = _touch(tmp_path / f"ffmpeg{EXE}")
    ffprobe = _touch(tmp_path / f"ffprobe{EXE}")

    def _no_path_lookup(name):
        pytest.fail("同目录已找到 ffprobe，不应该再查 PATH")

    monkeypatch.setattr(media.shutil, "which", _no_path_lookup)
    assert media.find_ffprobe(str(ffmpeg)) == str(ffprobe)


def test_find_ffprobe_falls_back_to_path(tmp_path, monkeypatch):
    ffmpeg = _touch(tmp_path / f"ffmpeg{EXE}")
    monkeypatch.setattr(
        media.shutil, "which", lambda name: "/fake/bin/ffprobe" if name == "ffprobe" else None
    )
    assert media.find_ffprobe(str(ffmpeg)) == "/fake/bin/ffprobe"


def test_find_ffprobe_missing(tmp_path, monkeypatch):
    ffmpeg = _touch(tmp_path / f"ffmpeg{EXE}")
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    assert media.find_ffprobe(str(ffmpeg)) is None


def test_find_ffmpeg_never_raises(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    found = media.find_ffmpeg()
    assert found is None or Path(found).exists()


def test_clip_utf8_never_splits_multibyte():
    text = "高等代数" * 50  # 600 字节
    clipped = pipeline._clip_utf8(text, 160)
    assert len(clipped.encode("utf-8")) <= 160
    assert "\ufffd" not in clipped
    assert text.startswith(clipped)


def test_clip_utf8_keeps_short_text():
    assert pipeline._clip_utf8("高等代数", 160) == "高等代数"


def test_safe_name_basics():
    assert pipeline.safe_name("a/b:c") == "a_b_c"
    assert pipeline.safe_name("   ") == "未命名"
    assert len(pipeline.safe_name("x" * 200)) <= 160


def test_soften_console_tolerates_plain_streams():
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        soften_console()  # StringIO 没有 reconfigure，应该被静默忽略
    finally:
        sys.stdout, sys.stderr = old_out, old_err
