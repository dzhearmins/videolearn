"""终端输出兼容。

POSIX 上如果 locale 是 C/POSIX（常见于 cron、容器、SSH 默认环境），
`sys.stdout` 可能是 ASCII，打印中文会直接抛 UnicodeEncodeError，
把整个程序打断在一句提示上。这里把编码错误降级成替换字符。
"""

from __future__ import annotations

import sys


def soften_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            # 被重定向成 StringIO / 管道包装器时没有 reconfigure，忽略即可
            pass
