"""媒体获取：HLS（明文）与直链 MP4。

安全边界：如果 playlist 声明 AES-128 / SAMPLE-AES / 其他加密方法，
本模块 **拒绝下载** 并抛出 EncryptedStreamError，由上层提示用户，
不做任何解密或 DRM 绕过。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urljoin

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .session import BBClient


class EncryptedStreamError(RuntimeError):
    """播放列表声明了不受支持的加密方式（SAMPLE-AES / DRM 等）。"""

    def __init__(self, method: str, info: str = "") -> None:
        self.method = method
        self.info = info
        super().__init__(
            f"回放使用了不受支持的加密方式（METHOD={method}）。"
            f"本工具只处理标准 HLS AES-128，不处理 SAMPLE-AES / DRM。{info}".strip()
        )


def _attr_list(line: str) -> dict[str, str]:
    """解析 m3u8 属性列表，保留带引号的值。"""
    out: dict[str, str] = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line):
        key = m.group(1)
        val = m.group(2).strip('"')
        out[key] = val
    return out


def _fetch_text(client: BBClient, url: str) -> str:
    r = client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"下载播放列表失败: HTTP {r.status_code}")
    return r.text


def pick_media_playlist(client: BBClient, url: str) -> tuple[str, str]:
    """若是 master playlist，选码率最高的一路；返回 (最终 URL, 内容)。"""
    text = _fetch_text(client, url)
    if "#EXTM3U" not in text:
        raise RuntimeError("返回内容不是 m3u8 播放列表")

    if "#EXT-X-STREAM-INF" in text:
        best_bw = -1
        best_uri = ""
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                attrs = _attr_list(line)
                try:
                    bw = int(attrs.get("BANDWIDTH", "0"))
                except ValueError:
                    bw = 0
                for nxt in lines[i + 1 :]:
                    if nxt.strip() and not nxt.startswith("#"):
                        if bw > best_bw:
                            best_bw, best_uri = bw, nxt.strip()
                        break
        if not best_uri:
            raise RuntimeError("master playlist 中没有可用码率")
        final = urljoin(url, best_uri)
        return final, _fetch_text(client, final)

    return url, text


def parse_media_playlist(text: str) -> dict:
    """解析媒体播放列表。

    - AES-128：记录每个分片的 key（URI/IV），由下载器按 RFC 8216 解密；
    - SAMPLE-AES / 其他 DRM：抛 EncryptedStreamError。
    """
    result: dict = {"segments": [], "init": None, "media_sequence": 0}
    current_key: dict | None = None
    pending_range: dict | None = None
    pending_duration: float | None = None
    end_offset = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                result["media_sequence"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-KEY"):
            attrs = _attr_list(line)
            method = attrs.get("METHOD", "").upper()
            if method in ("", "NONE"):
                current_key = None
            elif method == "AES-128":
                uri = attrs.get("URI")
                if not uri:
                    raise RuntimeError("AES-128 的 EXT-X-KEY 缺少 URI")
                current_key = {
                    "method": method,
                    "uri": uri,
                    "iv": _parse_iv(attrs.get("IV")),
                }
            else:
                raise EncryptedStreamError(
                    method, f"KEYFORMAT={attrs.get('KEYFORMAT', 'identity')}"
                )
            continue
        if line.startswith("#EXT-X-MAP"):
            attrs = _attr_list(line)
            uri = attrs.get("URI")
            if uri:
                result["init"] = {
                    "uri": uri,
                    "range": _parse_range(attrs.get("BYTERANGE")),
                    "key": current_key,
                }
            continue
        if line.startswith("#EXT-X-BYTERANGE"):
            pending_range = _parse_range(line.split(":", 1)[1].strip())
            continue
        if line.startswith("#EXTINF"):
            value = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_duration = float(value)
            except ValueError:
                pending_duration = None
            continue
        if line.startswith("#"):
            continue

        seg_range = pending_range
        pending_range = None
        if seg_range:
            if seg_range.get("offset") is None:
                seg_range["offset"] = end_offset
            end_offset = seg_range["offset"] + seg_range["length"]
        result["segments"].append(
            {
                "uri": line,
                "range": seg_range,
                "key": current_key,
                "duration": pending_duration or 0.0,
            }
        )
        pending_duration = None

    if not result["segments"]:
        raise RuntimeError("播放列表中没有分片")
    return result


def _parse_iv(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    try:
        return int(value, 16)
    except ValueError:
        return None


def _parse_range(value: str | None) -> dict | None:
    if not value:
        return None
    length, _, offset = value.partition("@")
    try:
        return {"length": int(length), "offset": int(offset) if offset else None}
    except ValueError:
        return None


def _get_bytes(client: BBClient, url: str, byte_range: dict | None = None) -> bytes:
    headers = {}
    if byte_range:
        start = byte_range.get("offset") or 0
        headers["Range"] = f"bytes={start}-{start + byte_range['length'] - 1}"
    r = client.get(url, headers=headers, stream=False)
    if r.status_code not in (200, 206):
        raise RuntimeError(f"分片下载失败: HTTP {r.status_code}")
    return r.content


def _decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    """HLS AES-128-CBC + PKCS7（RFC 8216 §4.3.2.4）。"""
    if len(data) % 16 != 0:
        raise RuntimeError(f"加密分片长度不是 16 的倍数: {len(data)}")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(data) + dec.finalize()
    pad = plain[-1] if plain else 0
    if pad < 1 or pad > 16 or plain[-pad:] != bytes([pad]) * pad:
        raise RuntimeError("AES-128 解密失败（PKCS7 padding 校验不通过）")
    return plain[:-pad]


def _get_key(client: BBClient, cache: dict[str, bytes], uri: str) -> bytes:
    if uri not in cache:
        key = _get_bytes(client, uri)
        if len(key) != 16:
            raise RuntimeError(f"AES-128 key 长度异常: {len(key)} 字节（应为 16）")
        cache[uri] = key
    return cache[uri]


def download_hls(
    client: BBClient,
    playlist_url: str,
    dest: Path,
    on_progress=None,
    max_seconds: float | None = None,
    start_seconds: float | None = None,
) -> Path:
    """下载 HLS 并拼接；返回本地媒体文件。

    - 支持明文与标准 AES-128（key 由同一会话取回，按分片解密）
    - 有 EXT-X-MAP：拼成 fMP4（.mp4）
    - 否则拼成 MPEG-TS（.ts），若本机有 ffmpeg 则无损转封装为 .mp4
    - start_seconds / max_seconds：只取某一段（用于小样验证或精听）
    """
    base_url, text = pick_media_playlist(client, playlist_url)
    pl = parse_media_playlist(text)
    indexed = list(enumerate(pl["segments"]))

    if start_seconds:
        acc = 0.0
        keep_from = len(indexed)
        for pos, (_idx, seg) in enumerate(indexed):
            if acc >= start_seconds:
                keep_from = pos
                break
            acc += seg["duration"]
        indexed = indexed[keep_from:]

    if max_seconds:
        acc = 0.0
        kept = []
        for item in indexed:
            kept.append(item)
            acc += item[1]["duration"]
            if acc >= max_seconds:
                break
        indexed = kept

    dest.parent.mkdir(parents=True, exist_ok=True)
    is_fmp4 = pl["init"] is not None
    tmp = dest.with_suffix(".part" + (".mp4" if is_fmp4 else ".ts"))

    key_cache: dict[str, bytes] = {}

    def materialize(raw: bytes, keyinfo: dict | None, seq: int) -> bytes:
        if not keyinfo:
            return raw
        key = _get_key(client, key_cache, urljoin(base_url, keyinfo["uri"]))
        iv_int = keyinfo["iv"]
        iv = (iv_int if iv_int is not None else seq).to_bytes(16, "big")
        return _decrypt_aes128(raw, key, iv)

    total = len(indexed)
    written = 0
    seq_base = pl["media_sequence"]
    with open(tmp, "wb") as f:
        if pl["init"]:
            init = pl["init"]
            if init["range"] and init["key"]:
                raise RuntimeError("不支持对加密的 EXT-X-MAP 做 ByteRange 请求")
            data = materialize(
                _get_bytes(client, urljoin(base_url, init["uri"]), init["range"]),
                init["key"],
                seq_base,
            )
            f.write(data)
        for done, (idx, seg) in enumerate(indexed):
            if seg["range"] and seg["key"]:
                raise RuntimeError("不支持对加密分片做 ByteRange 请求")
            raw = _get_bytes(client, urljoin(base_url, seg["uri"]), seg["range"])
            data = materialize(raw, seg["key"], seq_base + idx)
            f.write(data)
            written += len(data)
            if on_progress:
                on_progress(done + 1, total, written)

    if is_fmp4:
        tmp.replace(dest)
        return dest

    # TS：直接用 .part.ts 无损转封装，成功后删除临时文件
    mp4 = dest.with_suffix(".mp4")
    if _remux_ts_to_mp4(tmp, mp4):
        tmp.unlink(missing_ok=True)
        return mp4
    ts_out = dest.with_suffix(".ts")
    tmp.replace(ts_out)
    return ts_out


def download_direct_mp4(client: BBClient, url: str, dest: Path, on_progress=None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.get(url, stream=True) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"视频下载失败: HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written, total)
    return dest


# ---------------- ffmpeg ----------------
def find_ffmpeg() -> str | None:
    """定位 ffmpeg：优先 PATH，其次各平台常见安装位置。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    candidates: list[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.exists():
            candidates += sorted(packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
        candidates.append(local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
        candidates.append(Path(r"C:\ffmpeg\bin\ffmpeg.exe"))
    else:
        # macOS（Homebrew 两种前缀）与 Linux 常见位置；PATH 没有覆盖时兜底
        candidates += [
            Path("/opt/homebrew/bin/ffmpeg"),
            Path("/usr/local/bin/ffmpeg"),
            Path("/usr/bin/ffmpeg"),
            Path("/snap/bin/ffmpeg"),
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def find_ffprobe(ffmpeg: str | None = None) -> str | None:
    """定位 ffprobe：先看 ffmpeg 同目录（Windows 带 .exe），再退回 PATH。"""
    exe = ffmpeg or find_ffmpeg()
    if exe:
        name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        sibling = Path(exe).with_name(name)
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe")



def _remux_ts_to_mp4(src: Path, dst: Path) -> bool:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dst)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return dst.exists() and dst.stat().st_size > 0
    except (subprocess.SubprocessError, OSError):
        return False
