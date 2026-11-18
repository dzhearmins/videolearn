"""教学网（Blackboard）课程 / 回放列表 / 播放地址解析。

端点与参数已对照真实站点与开源实现 pku3b 核对：
- 课程：tabAction?tab_tab_group_id=_1_1 → div.portlet ul.courseListing li a[href*=key=_xxx_1]
- 回放列表：bb-streammedia-hqy-BBLEARN/videoList.action?course_id=_xxx_1&...
- 播放：playVideo.action?token=... → iframe → 302 Location(course_id/sub_id/app_id/auth_data)
- 媒体地址：yjapise.pku.edu.cn/courseapi/v2/schedule/get-sub-info-by-auth-data
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import bs4

from .session import AuthError, BBClient

BB_HOME = "https://course.pku.edu.cn/webapps/portal/execute/tabs/tabAction"
BB_LOGIN = "https://course.pku.edu.cn/webapps/login/"
VIDEO_LIST = "https://course.pku.edu.cn/webapps/bb-streammedia-hqy-BBLEARN/videoList.action"
VIDEO_SUB_INFO = "https://yjapise.pku.edu.cn/courseapi/v2/schedule/get-sub-info-by-auth-data"

_COURSE_KEY_RE = re.compile(r"key=([\d_]+)")


@dataclass
class Course:
    key: str          # 形如 _80052_1
    name: str
    term: str = ""    # portlet 标题，如“2026 秋季学期”


@dataclass
class Video:
    title: str
    time: str
    teacher: str
    play_url: str


@dataclass
class Playback:
    course_id: str
    sub_id: str
    app_id: str
    auth_data: str = field(repr=False)  # 敏感，禁止进日志
    is_m3u8: bool = False
    contents: str = ""


def _check_html(resp, what: str) -> str:
    if "charset" not in (resp.headers.get("Content-Type") or "").lower():
        resp.encoding = "utf-8"
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if "login" in loc.lower():
            raise AuthError(f"{what}: 会话已失效，需要重新登录")
    if resp.status_code != 200:
        raise RuntimeError(f"{what}: HTTP {resp.status_code}")
    text = resp.text
    if "JWT signature" in text or "JWT validity" in text:
        raise RuntimeError(f"{what}: 回放 token 已失效或被篡改，请从回放列表重新获取链接")
    if 'id="loginPageContainer"' in text or "<title>登录教学网" in text or "登录教学网</title>" in text:
        raise AuthError(f"{what}: 被重定向到登录页，需要重新登录")
    return text


def _clean_course_name(name: str) -> str:
    """去掉教学网课程名前的教务代码与学期后缀，只保留可读名称。"""
    name = re.sub(r"^[0-9A-Za-z*][0-9A-Za-z*\-]*:\s*", "", name)
    name = re.sub(r"[（(]\d{2}-\d{2}学年第\d学期[）)]\s*$", "", name)
    return name.strip() or "未命名课程"


def list_courses(client: BBClient) -> list[Course]:
    r = client.get(BB_HOME, params={"tab_tab_group_id": "_1_1"}, allow_redirects=False)
    html = _check_html(r, "获取课程列表")
    soup = bs4.BeautifulSoup(html, "html.parser")

    courses: list[Course] = []
    seen: set[str] = set()
    for portlet in soup.select("div.portlet"):
        title_el = portlet.select_one("span.moduleTitle")
        term = title_el.get_text(" ", strip=True) if title_el else ""
        ul = portlet.select_one("ul.courseListing")
        if not ul:
            continue
        for a in ul.select("li a"):
            href = a.get("href", "")
            m = _COURSE_KEY_RE.search(href)
            if not m:
                continue
            key = m.group(1)
            name = _clean_course_name(a.get_text(" ", strip=True))
            if not name or key in seen:
                continue
            seen.add(key)
            courses.append(Course(key=key, name=name, term=term))
    return courses


def list_videos(client: BBClient, course_key: str) -> list[Video]:
    r = client.get(
        VIDEO_LIST,
        params={
            "sortDir": "ASCENDING",
            "numResults": "200",
            "editPaging": "false",
            "course_id": course_key,
            "mode": "view",
            "startIndex": "0",
        },
        allow_redirects=False,
    )
    html = _check_html(r, f"获取回放列表({course_key})")
    soup = bs4.BeautifulSoup(html, "html.parser")
    tbody = soup.select_one("tbody#listContainer_databody")
    if not tbody:
        return []

    videos: list[Video] = []
    for tr in tbody.select("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        title = tds[0].get_text(" ", strip=True)
        values = [s.get_text(" ", strip=True) for s in tr.select("span.table-data-cell-value")]
        a = tr.select_one("span.table-data-cell-value a[href]") or tr.select_one("a[href]")
        if not a:
            continue
        play_url = urljoin(VIDEO_LIST, a["href"])
        time = values[0] if len(values) > 0 else ""
        teacher = values[1] if len(values) > 1 else ""
        videos.append(Video(title=title, time=time, teacher=teacher, play_url=play_url))
    return videos


def _parse_query_preserve_plus(query: str) -> dict[str, str]:
    """Location 里的 auth_data 可能含未编码的 '+'，不能按 form 语义解码成空格。"""
    out: dict[str, str] = {}
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[unquote(k)] = unquote(v)
    return out


def resolve_playback(client: BBClient, play_url: str) -> Playback:
    r = client.get(play_url, allow_redirects=True)
    html = _check_html(r, "打开回放页面")
    soup = bs4.BeautifulSoup(html, "html.parser")

    iframe = soup.select_one("#content iframe") or soup.select_one("iframe[src]")
    if not iframe:
        raise RuntimeError("回放页面中没有找到播放器 iframe（token 可能已失效）")
    iframe_src = urljoin(play_url, iframe["src"])

    # 播放器地址会 302 到带 course_id/sub_id/app_id/auth_data 的地址
    r2 = client.get(iframe_src, allow_redirects=False)
    if r2.status_code != 302:
        raise RuntimeError(
            f"播放器未返回 302（HTTP {r2.status_code}），可能是 token 失效或页面结构变化"
        )
    loc = r2.headers.get("Location", "")
    if not loc:
        raise RuntimeError("播放器 302 没有 Location")

    qs = _parse_query_preserve_plus(urlparse(loc).query)
    missing = [k for k in ("course_id", "sub_id", "app_id", "auth_data") if not qs.get(k)]
    if missing:
        raise RuntimeError(f"播放地址缺少参数: {', '.join(missing)}")

    sub = _fetch_sub_info(client, qs["course_id"], qs["sub_id"], qs["app_id"], qs["auth_data"])
    item = (sub.get("list") or [{}])[0]
    raw = item.get("sub_content")
    if isinstance(raw, str):
        content = json.loads(raw)
    elif isinstance(raw, dict):
        content = raw
    else:
        raise RuntimeError("sub_info 中没有 sub_content")
    sp = content.get("save_playback") or {}
    contents = sp.get("contents") or ""
    if not contents:
        raise RuntimeError("save_playback.contents 为空")
    return Playback(
        course_id=qs["course_id"],
        sub_id=qs["sub_id"],
        app_id=qs["app_id"],
        auth_data=qs["auth_data"],
        is_m3u8=str(sp.get("is_m3u8", "")).lower() == "yes",
        contents=contents,
    )


def _fetch_sub_info(
    client: BBClient, course_id: str, sub_id: str, app_id: str, auth_data: str
) -> dict:
    r = client.get(
        VIDEO_SUB_INFO,
        params={
            "all": "1",
            "course_id": course_id,
            "sub_id": sub_id,
            "with_sub_data": "1",
            "app_id": app_id,
            "auth_data": auth_data,
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"获取回放媒体信息失败: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError("回放媒体信息不是合法 JSON") from e
