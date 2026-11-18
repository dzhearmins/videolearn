"""带 Cookie 持久化的 HTTP 会话。

安全约定：
- Cookie 只写入 .state/（已被 .gitignore 排除）；
- 任何日志、异常信息都不输出密码或 Cookie 内容。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class AuthError(RuntimeError):
    """认证失败（凭据错误、会话失效、SSO 回调未建立会话）。"""


class BBClient:
    def __init__(self, state_dir: Path, timeout: int = 45) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )

    # ---------------- cookie persistence ----------------
    @property
    def cookie_path(self) -> Path:
        return self.state_dir / "cookies.json"

    def save_cookies(self) -> None:
        data = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
                "expires": c.expires,
            }
            for c in self.s.cookies
        ]
        self.cookie_path.write_text(json.dumps(data), encoding="utf-8")
        try:  # 仅 POSIX 生效；Windows 上忽略
            self.cookie_path.chmod(0o600)
        except OSError:
            pass

    def load_cookies(self) -> bool:
        if not self.cookie_path.exists():
            return False
        try:
            data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        for c in data:
            self.s.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or None,
                path=c.get("path") or "/",
                secure=bool(c.get("secure")),
            )
        return True

    def clear_cookies(self) -> None:
        self.s.cookies.clear()
        try:
            self.cookie_path.unlink()
        except FileNotFoundError:
            pass

    # ---------------- requests wrappers ----------------
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self.s.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return self.s.post(url, **kwargs)
