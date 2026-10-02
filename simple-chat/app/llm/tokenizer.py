"""token 估算：tiktoken 优先，离线时退化为字符近似。

所有数值均为估算，非精确 token 数；用于上下文裁剪与用量日志，不用于计费。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_encoder: Any = None
_tried: bool = False


def _get_encoder() -> Any:
    """惰性加载 cl100k_base 编码（兼容 gpt-4o / gpt-4o-mini）；失败回退 None。"""
    global _encoder, _tried
    if _tried:
        return _encoder
    _tried = True
    try:
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # 离线/无网络时拿不到编码文件
        logger.warning("tiktoken unavailable, falling back to char estimate: %s", exc)
        _encoder = None
    return _encoder


def count_tokens(text: str) -> int:
    """估算单段文本 token 数；tiktoken 不可用时回退 len/3。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return max(1, len(text) // 3)
    return len(enc.encode(text, disallowed_special=()))


def estimate_tokens(messages: list[dict]) -> int:
    """估算消息列表 token 数。

    估算公式（非精确值）：
        每条消息 ≈ len(role) + len(content) / 3 + 4
        另加 2（对话格式开销）
    tiktoken 可用时改用精确编码累加，公式仅作离线回退。结果用于上下文裁剪与
    用量日志，不用于计费。
    """
    if not messages:
        return 2
    enc = _get_encoder()
    total = 2  # 对话格式开销
    for m in messages:
        role = str(m.get("role", ""))
        content = str(m.get("content", ""))
        if enc is not None:
            total += len(enc.encode(role, disallowed_special=()))
            total += len(enc.encode(content, disallowed_special=()))
            total += 4
        else:
            total += len(role) + len(content) // 3 + 4
    return total


def count_message_tokens(role: str, content: str) -> int:
    """估算单条消息 token 数；等价于 estimate_tokens 对一条消息的贡献（不含格式开销 2）。"""
    return count_tokens(content) + count_tokens(role) + 4
