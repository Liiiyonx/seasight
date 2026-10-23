"""HTTP 下载响应头的安全构造。"""

from __future__ import annotations

import re
from urllib.parse import quote


def attachment_header(filename: str, *, ascii_fallback: str) -> str:
    """构造兼容 Latin-1 响应头、同时保留 UTF-8 中文文件名的 CD 头。

    Starlette 会按 Latin-1 编码响应头，直接在 ``filename`` 中放中文会抛
    ``UnicodeEncodeError``。RFC 6266 的标准做法是提供 ASCII 回退名，并用
    ``filename*`` 携带百分号编码后的 UTF-8 文件名。
    """
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", ascii_fallback).strip("._")
    fallback = fallback or "download"
    encoded = quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
