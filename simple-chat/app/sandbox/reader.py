"""read_file 工具内核（ROADMAP v0.5 §4）。

安全模型：
- 路径必须在 `TOOL_READ_ROOTS` 白名单根目录内；
- 用 `os.path.realpath` 解析后做前缀校验，防 `..` 与 symlink 逃逸；
- 拒绝 `.env` / 数据库 / 密钥文件（按名称模式，不读内容）；
- 只读文本，二进制（含 NUL 字节）→ UNSUPPORTED；
- 服务端强制 max_bytes 与 max_output_bytes 截断。
"""

from __future__ import annotations

import os
import re
from typing import Any

from app.config import settings
from app.tools.protocol import ToolErrorCode


class ReadFileError(Exception):
    """read_file 受控失败。code 字段供 executor 映射到信封。"""

    def __init__(self, message: str, code: str = "PATH_NOT_ALLOWED") -> None:
        super().__init__(message)
        self.code = code


# 受限文件名模式（按 basename 与末尾后缀匹配，不读内容）
_DENY_PATTERNS = [
    re.compile(r"(^|/)\.env$", re.IGNORECASE),
    re.compile(r"\.env$|\.env\.[A-Za-z0-9_-]+$", re.IGNORECASE),  # .env / .env.local
    re.compile(r"chat\.db$|chat\.db\-(wal|shm|journal)$", re.IGNORECASE),
    re.compile(r"\.sqlite3?$", re.IGNORECASE),
    re.compile(r"\.key$|\.pem$|\.crt$|\.p12$|\.pfx$|id_rsa$|id_ecdsa$|id_ed25519$", re.IGNORECASE),
    re.compile(r"secrets?\.ya?ml$|credentials?\.ya?ml$|credentials?\.json$", re.IGNORECASE),
]

# 二进制检测：前 N 字节中若出现 NUL 即判为二进制
_BIN_DETECT_BYTES = 4096


def _is_denied(name: str) -> bool:
    n = name.replace("\\", "/")
    return any(p.search(n) for p in _DENY_PATTERNS)


def _resolve_path(path: str) -> str:
    """把用户路径相对每个白名单根解析为绝对路径，返回首个落在根内的。

    用户路径可以是绝对路径，也可以是相对某个根的相对路径。
    """
    if not isinstance(path, str) or not path.strip():
        raise ReadFileError("路径不能为空", code="VALIDATION_ERROR")
    if len(path) > 512:
        raise ReadFileError("路径过长（>512 字符）", code="VALIDATION_ERROR")

    roots = settings.effective_tool_read_roots
    if not roots:
        raise ReadFileError("未配置 TOOL_READ_ROOTS", code="NOT_CONFIGURED")

    p = path.strip()
    # 规范化用户输入再尝试
    candidates: list[str] = []
    if os.path.isabs(p):
        candidates.append(p)
    else:
        for root in roots:
            candidates.append(os.path.join(root, p))

    for cand in candidates:
        rp = os.path.realpath(cand)
        # 必须落在某个根内（前缀匹配 + 路径分隔符边界）
        for root in roots:
            rroot = os.path.realpath(root)
            if rp == rroot or rp.startswith(rroot + os.sep):
                return rp
    raise ReadFileError("路径不在允许目录内", code="PATH_NOT_ALLOWED")


def read_file(path: str, max_bytes: int = 65536) -> dict[str, Any]:
    """读取白名单内的文本文件。

    返回 {"path","content","bytes"}。失败抛 ReadFileError。
    """
    resolved = _resolve_path(path)

    base = os.path.basename(resolved)
    if _is_denied(base) or _is_denied(resolved):
        raise ReadFileError("该文件类型被拒绝读取", code="UNSUPPORTED")

    if not os.path.isfile(resolved):
        raise ReadFileError("文件不存在或不是普通文件", code="PATH_NOT_ALLOWED")
    if not os.access(resolved, os.R_OK):
        raise ReadFileError("文件不可读", code="PATH_NOT_ALLOWED")

    size = os.path.getsize(resolved)
    # 服务端强制上限（min(用户请求, 262144, max_bytes 配置)）
    cap = max(1, min(int(max_bytes), 262144))

    with open(resolved, "rb") as f:
        head = f.read(_BIN_DETECT_BYTES)
        if b"\x00" in head:
            raise ReadFileError("文件为二进制，不支持读取", code="UNSUPPORTED")
        # 继续读取剩余至 cap
        rest = f.read(cap - len(head)) if len(head) < cap else b""
        data = head[:cap] if len(head) >= cap else head + rest

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReadFileError("文件非 UTF-8 文本", code="UNSUPPORTED") from exc

    truncated = size > len(data)
    return {
        "path": path.strip(),
        "content": text,
        "bytes": len(data),
        "truncated": truncated,
    }
