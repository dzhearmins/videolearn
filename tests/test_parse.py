"""不依赖网络的解析测试（HTML/JSON/m3u8/JWT）。"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from videolearn.blackboard import _clean_course_name, _parse_query_preserve_plus
from videolearn.llm import (
    CHUNK_MAX_CHARS,
    CONTEXT_TRACE,
    _build_frame_messages,
    _extract_json,
    chunk_transcript,
    clamp_max_shots,
    estimate_tokens,
    fit_text,
    format_timecode,
    frame_key,
    group_notes,
    max_input_chars,
    parse_shot_time,
    render_shot_images,
    reset_trace,
)
from videolearn.media import EncryptedStreamError, _attr_list, find_ffmpeg, parse_media_playlist
from videolearn.pipeline import token_hint
from videolearn.vision import (
    _split_observations,
    extract_frames,
    load_eyes_analysis,
    normalize_frame_requests,
    save_eyes_analysis,
)
from videolearn.stream_bench import dedupe_first_sentence, norm, split_sentences

COURSE_HTML = """
<div class="portlet">
  <span class="moduleTitle">2026 秋季学期</span>
  <ul class="courseListing">
    <li><a href="/webapps/blackboard/execute/launcher?type=Course&id=_80052_1&key=_80052_1">高等代数 (上)</a></li>
    <li><a href="/webapps/blackboard/execute/launcher?type=Course&id=_81000_1&key=_81000_1">数学分析</a></li>
  </ul>
</div>
<div class="portlet">
  <span class="moduleTitle">2026 春季学期</span>
  <ul class="courseListing">
    <li><a href="/webapps/blackboard/execute/launcher?type=Course&id=_70001_1&key=_70001_1">解析几何</a></li>
  </ul>
</div>
"""

VIDEO_HTML = """
<table><tbody id="listContainer_databody">
<tr>
  <td>第 3 次课</td>
  <td><span class="table-data-cell-value">2026-11-03 10:10:00</span></td>
  <td><span class="table-data-cell-value">张三</span></td>
  <td><span class="table-data-cell-value"><a href="/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=abc.def.ghi">播放</a></span></td>
</tr>
<tr>
  <td>第 4 次课</td>
  <td><span class="table-data-cell-value">2026-11-05 08:00:00</span></td>
  <td><span class="table-data-cell-value">张三</span></td>
  <td><span class="table-data-cell-value"><a href="https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=x.y.z">播放</a></span></td>
</tr>
</tbody></table>
"""

SAMPLE_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJyZWNvcmRUaW1lIjoiMjAyNi0xMS0wMyAxMDoxMDowMCIsImtjd3libSI6IjI2MjcxLTAwMDEtMDAxMzIzMjEtMDAwNjE3MjM4OS0wMC0xIiwiaHF5U3ViSWQiOiI0NDk2MjA2IiwiaHF5Q291cnNlSWQiOiIxOTkxMzgiLCJpYXQiOjE3OTQ5NzIzMzF9."
    "wXh2re2wRTLx6pGDwqozXH01gASWd1xKiAnlbVj8w6E"
)


def _parse_courses(html):
    import bs4
    from videolearn.blackboard import _COURSE_KEY_RE

    soup = bs4.BeautifulSoup(html, "html.parser")
    out = []
    for portlet in soup.select("div.portlet"):
        title = portlet.select_one("span.moduleTitle")
        term = title.get_text(" ", strip=True) if title else ""
        ul = portlet.select_one("ul.courseListing")
        if not ul:
            continue
        for a in ul.select("li a"):
            m = _COURSE_KEY_RE.search(a.get("href", ""))
            if m:
                out.append((m.group(1), a.get_text(" ", strip=True), term))
    return out


def _parse_videos(html):
    import bs4
    from urllib.parse import urljoin
    from videolearn.blackboard import VIDEO_LIST

    soup = bs4.BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tbody#listContainer_databody tr"):
        tds = tr.find_all("td")
        a = tr.select_one("span.table-data-cell-value a[href]")
        if not tds or not a:
            continue
        values = [s.get_text(" ", strip=True) for s in tr.select("span.table-data-cell-value")]
        out.append((tds[0].get_text(" ", strip=True), values[0], urljoin(VIDEO_LIST, a["href"])))
    return out


def test_course_keys_and_terms():
    courses = _parse_courses(COURSE_HTML)
    assert [c[0] for c in courses] == ["_80052_1", "_81000_1", "_70001_1"]
    assert courses[0][1] == "高等代数 (上)"
    assert courses[0][2] == "2026 秋季学期"


def test_clean_course_name_strips_code_and_term():
    raw = "26271-00001-00132321-0006172389-00-1: 高等代数（I）(26-27学年第1学期)"
    assert _clean_course_name(raw) == "高等代数（I）"
    assert _clean_course_name("数学分析（I）(26-27学年第1学期)") == "数学分析（I）"
    masked = "26271-00048-04830010-110****264-00-1: 信息科学技术概论"
    assert _clean_course_name(masked) == "信息科学技术概论"


def test_video_rows_parse_time_and_url():
    videos = _parse_videos(VIDEO_HTML)
    assert len(videos) == 2
    assert videos[0][0] == "第 3 次课"
    assert videos[0][1] == "2026-11-03 10:10:00"
    assert videos[0][2].startswith("https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/playVideo.action?token=")


def test_query_keeps_plus():
    q = "course_id=_1_1&auth_data=abc+def/ghi==&sub_id=42"
    parsed = _parse_query_preserve_plus(q)
    assert parsed["auth_data"] == "abc+def/ghi=="
    assert parsed["sub_id"] == "42"


def test_token_hint_decodes_record_time():
    hint = token_hint(f"https://course.pku.edu.cn/x?token={SAMPLE_TOKEN}")
    assert hint["recordTime"] == "2026-11-03 10:10:00"
    assert hint["hqySubId"] == "4496206"


def test_plain_playlist_segments():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
    pl = parse_media_playlist(text)
    assert [s["uri"] for s in pl["segments"]] == ["seg0.ts", "seg1.ts"]
    assert pl["init"] is None


def test_aes128_key_recorded_on_segments():
    text = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://example.invalid/key.bin",IV=0x0F\n'
        "#EXTINF:4.0,\nseg0.ts\n"
        "#EXTINF:4.0,\nseg1.ts\n"
    )
    pl = parse_media_playlist(text)
    key = pl["segments"][0]["key"]
    assert key["method"] == "AES-128"
    assert key["uri"] == "https://example.invalid/key.bin"
    assert key["iv"] == 0x0F
    assert pl["segments"][1]["key"] is key


def test_sample_aes_refused():
    text = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x"\n'
        "#EXTINF:4.0,\nseg0.ts\n"
    )
    with pytest.raises(EncryptedStreamError) as e:
        parse_media_playlist(text)
    assert e.value.method == "SAMPLE-AES"


def test_aes128_decrypt_roundtrip():
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from videolearn.media import _decrypt_aes128

    key = bytes(range(16))
    iv = bytes(range(16, 32))
    plain = "线性空间 与 线性变换 ".encode("utf-8") * 4
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()

    assert _decrypt_aes128(ciphertext, key, iv) == plain


def test_fmp4_init_map_parsed():
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2.0,\nseg1.m4s\n'
    pl = parse_media_playlist(text)
    assert pl["init"]["uri"] == "init.mp4"
    assert pl["segments"][0]["uri"] == "seg1.m4s"


def test_chunking_preserves_boundaries():
    lines = [f"[00:0{i}:00] 第{i}段内容" for i in range(5)]
    text = "\n".join(lines)
    chunks = chunk_transcript(text, max_chars=40)
    assert len(chunks) >= 2
    assert chunks[0].start_hint == "00:00:00"
    assert all(c.text.startswith("[") for c in chunks)


def test_attr_list_quoted_values():
    attrs = _attr_list('#EXT-X-KEY:METHOD=AES-128,URI="https://a/b?c=1,d=2",IV=0x1')
    assert attrs["METHOD"] == "AES-128"
    assert attrs["URI"] == "https://a/b?c=1,d=2"


def test_shot_timecode_parsing():
    assert parse_shot_time("01:02:03") == 3723
    assert parse_shot_time("10:30") == 630
    assert parse_shot_time("45") == 45
    assert parse_shot_time("bad") is None
    assert format_timecode(3723) == "01:02:03"
    assert frame_key(3723) == "01-02-03"


def test_extract_json_handles_fences():
    raw = '```json\n{"shots":[{"time":"00:10:30","reason":"板书"}]}\n```'
    assert _extract_json(raw)["shots"][0]["time"] == "00:10:30"


def test_render_shot_images_replaces_marker_and_appends_rest():
    shots = [
        {"key": "00-10-30", "time": "00:10:30", "reason": "矩阵定义"},
        {"key": "00-12-00", "time": "00:12:00", "reason": "例题"},
    ]
    text = "讲解见下。\n\n[[SHOT:00-10-30]]\n\n继续。"
    out = render_shot_images(text, shots)
    assert "[[SHOT:" not in out
    assert "frames/00-10-30.jpg" in out
    assert "## 黑板与 PPT 截图" in out
    assert "frames/00-12-00.jpg" in out
    assert out.count("frames/00-10-30.jpg") == 1


def test_render_shot_images_tolerates_colons_and_fullwidth():
    shots = [{"key": "00-10-30", "time": "00:10:30", "reason": ""}]
    for marker in ("[[SHOT:00:10:30]]", "[[SHOT:00：10：30]]", "[[SHOT: 00-10-30 ]]"):
        out = render_shot_images(marker, shots)
        assert "[[SHOT:" not in out
        assert "frames/00-10-30.jpg" in out
        assert "## 黑板与 PPT 截图" not in out


def _long_transcript(n: int = 600) -> str:
    lines = []
    for i in range(n):
        h, rem = divmod(i * 4, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] 线性空间与矩阵的定义及推导。" * 2)
    return "\n".join(lines)


def test_long_transcript_chunks_are_bounded_with_overlap():
    reset_trace()
    text = _long_transcript(600)
    chunks = chunk_transcript(text)
    assert len(chunks) > 1
    assert all(len(c.text) <= CHUNK_MAX_CHARS for c in chunks)
    assert all(estimate_tokens(c.text) <= CHUNK_MAX_CHARS for c in chunks)
    # 相邻块有重叠：第二块的首行来自第一块
    assert chunks[1].text.splitlines()[0] in chunks[0].text
    assert any(e.get("kind") == "chunking" for e in CONTEXT_TRACE)


def test_fit_text_truncates_middle_and_traces():
    reset_trace()
    out = fit_text("线" * 1000, 200, "unit")
    assert len(out) <= 200
    assert "截断" in out
    assert any(e.get("kind") == "truncate" and e.get("label") == "unit" for e in CONTEXT_TRACE)


def test_group_notes_respects_budget():
    notes = ["a" * 100 for _ in range(5)]
    groups = group_notes(notes, 250)
    assert sum(len(g) for g in groups) == 5
    for g in groups:
        assert sum(len(n) + 1 for n in g) <= 250


def test_clamp_max_shots_hard_cap():
    assert clamp_max_shots(100) == 20
    assert clamp_max_shots(8) == 8
    assert clamp_max_shots(-3) == 0


def test_estimate_tokens_is_conservative():
    assert estimate_tokens("矩阵") == 2
    assert estimate_tokens("") == 1


def test_max_input_chars_env_override(monkeypatch):
    monkeypatch.setenv("VIDEOLRN_MAX_INPUT_TOKENS", "12345")
    assert max_input_chars() == 12345
    monkeypatch.setenv("VIDEOLRN_MAX_INPUT_TOKENS", "bad")
    assert max_input_chars() == 60000


def test_compress_notes_hierarchical_and_truncates_single(monkeypatch):
    import videolearn.llm as llm_mod

    calls: list[int] = []

    def fake_chat(settings, system, user, max_tokens=8192):
        calls.append(len(user))
        return "压缩摘要" * 20

    monkeypatch.setattr(llm_mod, "_chat", fake_chat)
    notes = ["A" * 4000 for _ in range(10)]
    out = llm_mod._compress_notes(object(), notes, 5000)
    assert len(out) <= 5000
    assert calls, "应当触发分层摘要调用"

    single = llm_mod._compress_notes(object(), ["B" * 20000], 5000)
    assert len(single) <= 5000
    assert "截断" in single


def test_build_frame_messages_keeps_timestamp_image_order(tmp_path):
    p1, p2 = tmp_path / "a.jpg", tmp_path / "b.jpg"
    p1.write_bytes(b"fake-a")
    p2.write_bytes(b"fake-b")
    frames = [
        {"time": "00:10:41", "focus": "上三角阵定义", "path": p1},
        {"time": "00:11:09", "focus": "Borel 子群", "path": p2},
    ]
    messages = _build_frame_messages("高等代数", frames)
    content = messages[1]["content"]
    images = [i for i, part in enumerate(content) if part.get("type") == "image_url"]
    assert len(images) == 2
    t1 = next(i for i, p in enumerate(content) if p.get("type") == "text" and "00:10:41" in p["text"])
    t2 = next(i for i, p in enumerate(content) if p.get("type") == "text" and "00:11:09" in p["text"])
    assert t1 < images[0] < t2 < images[1]


def test_normalize_frame_requests_dedup_and_range():
    reqs = normalize_frame_requests(
        [
            {"time": "00:10:30", "focus": "a"},
            {"time": "00:10:30", "focus": "dup"},
            {"time": "00:10:35", "focus": "too close"},
            {"time": "00:09:00", "focus": "out of range"},
            {"time": "00:12:00", "focus": "b"},
        ],
        start=600.0,
        end=1200.0,
        observed=[],
        remaining=5,
        max_batch=4,
    )
    assert [r["time"] for r in reqs] == ["00:10:30", "00:12:00"]
    assert reqs[0]["focus"] == "a"


def test_split_observations_by_timestamp():
    text = "### 00:10:41\nA 定义\n### 00:11:09\nB 定义\n### 综合分析\n总结"
    frames = [{"time": "00:10:41"}, {"time": "00:11:09"}, {"time": "00:12:00"}]
    out = _split_observations(text, frames)
    assert out["00:10:41"] == "A 定义"
    assert out["00:11:09"] == "B 定义"
    assert out["00:12:00"] == ""


def test_eyes_analysis_roundtrip(tmp_path):
    payload = {
        "title": "高等代数",
        "status": "finished",
        "summary": "黑板确认 B 是上三角阵",
        "time_offset": 600,
        "duration_sec": 120,
        "steps": [
            {
                "step": 1,
                "analysis": "需要确认 B 的定义",
                "action": {"type": "get_frames", "times": [{"time": "00:10:41"}]},
                "frames": [{"time": "00:10:41", "file": "frames/00-10-41.jpg", "focus": ""}],
                "observation": "### 00:10:41\nB={A|a_ij=0}",
            }
        ],
        "shots": [{"time": "00:10:41"}],
    }
    json_path, md_path = save_eyes_analysis(tmp_path, payload)
    assert json_path.exists() and md_path.exists()
    loaded = load_eyes_analysis(tmp_path)
    assert loaded["summary"] == "黑板确认 B 是上三角阵"
    text = md_path.read_text(encoding="utf-8")
    assert "思考/分析" in text and "B={A|a_ij=0}" in text


@pytest.mark.skipif(find_ffmpeg() is None, reason="需要 ffmpeg")
def test_extract_frames_preserves_request_order(tmp_path):
    ffmpeg = find_ffmpeg()
    video = tmp_path / "v.mp4"
    subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-t", "3", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        timeout=60,
    )
    reqs = [
        {"seconds": 1.0, "time": "00:00:01", "focus": "b"},
        {"seconds": 0.2, "time": "00:00:00", "focus": "a"},
        {"seconds": 2.0, "time": "00:00:02", "focus": "c"},
    ]
    frames = extract_frames(video, reqs, tmp_path / "frames")
    assert [f["time"] for f in frames] == ["00:00:01", "00:00:00", "00:00:02"]
    assert all((tmp_path / f["file"]).exists() for f in frames)


def test_stream_dedupe_first_sentence():
    prev = "上一段讲了上三角矩阵的定义与例子。"
    text = "上一段讲了上三角矩阵的定义与例子。这一段继续讲 Borel 子群。"
    deduped, flag = dedupe_first_sentence(text, prev)
    assert flag is True
    assert "上一段讲了" not in deduped
    assert "Borel" in deduped

    # 唯一一句重复时不能删空
    only, flag2 = dedupe_first_sentence("上一段讲了上三角矩阵的定义与例子。", prev)
    assert flag2 is False
    assert only


def test_stream_sentence_split_and_norm():
    sents = split_sentences("这是第一句话。这是第二句话！这是第三句话？")
    assert sents == ["这是第一句话。", "这是第二句话！", "这是第三句话？"]
    assert norm("A， B。") == "AB"


def test_chat_retries_empty_content_without_thinking(monkeypatch):
    import videolearn.llm as llm_mod

    calls: list[bool] = []

    def fake_chat(settings, messages, max_tokens=8192, model=None, disable_thinking=False):
        calls.append(disable_thinking)
        if not disable_thinking:
            raise llm_mod.LLMError("LLM 返回了空正文（finish_reason=length）。")
        return "ok"

    monkeypatch.setattr(llm_mod, "_chat_messages", fake_chat)

    class _Settings:
        llm_model = "test-model"

    assert llm_mod._chat(_Settings(), "system", "user") == "ok"
    assert calls == [False, True]
