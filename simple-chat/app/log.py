"""结构化日志：标准库 logging + JSON Formatter，字段固定、敏感信息脱敏。

设计要点：
- 仅挂在 "app" logger 上（propagate=False），不打扰 pytest / 第三方库（uvicorn、openai…）的日志。
- JSON 字段固定为：timestamp / level / logger / message / request_id / latency_ms /
  model / tokens_in / tokens_out / user_id / error_code。
  其中 request_id/latency_ms/user_id 由请求日志中间件写入；model/tokens_in/out 由 LLM
  调用写入；error_code 由异常路径写入。缺失字段以 null 占位，便于日志系统做 schema 解析。
- 脱敏：LLM_API_KEY 显示为 "sk-***" + 后 4 位；Authorization 头值置为 "***"。
- 绝不记录 message.content 全文（见 app.chat / app.llm 的注释约束）；此处脱敏器仅作为
  兜底，防止任何意外把密钥/密钥类字符串写进日志。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

# ───────────────────────── 脱敏 ─────────────────────────

# LLM API Key：形如 sk-xxxx...，保留 "sk-***" + 最后 4 位
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
# Authorization 头：Authorization: Bearer xxx -> Authorization: ***
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)\S+")


def redact(text: str) -> str:
    """对日志文本做敏感字段脱敏，返回脱敏后文本。"""
    if not text:
        return text
    # 1) Authorization 头值
    text = _AUTH_RE.sub(lambda m: m.group(1) + "***", text)
    # 2) LLM API Key：sk-***<后4位>
    text = _API_KEY_RE.sub(lambda m: "sk-***" + m.group(0)[-4:], text)
    return text


# ───────────────────────── Formatter ─────────────────────────

# 始终输出的结构化字段（缺失为 null）
_BASE_FIELDS = (
    "request_id",
    "latency_ms",
    "model",
    "tokens_in",
    "tokens_out",
    "user_id",
    "error_code",
)
# 额外可选字段（存在才输出）
_EXTRA_FIELDS = ("method", "path", "status", "conversation_id", "event", "stream", "upstream_status")


class JsonFormatter(logging.Formatter):
    """将日志记录渲染为单行 JSON，符合上述字段约定。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S."
            )
            + f"{int(record.msecs):03d}Z"
        )
        payload = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            # message 永远经过脱敏
            "message": redact(record.getMessage()),
        }
        for key in _BASE_FIELDS:
            payload[key] = getattr(record, key, None)
        for key in _EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ───────────────────────── 配置入口 ─────────────────────────

# 延迟导入，避免与 app.config 形成循环依赖（main 在 import app.log 前已 import app.config）
from app.config import settings  # noqa: E402


def configure_logging(level: str | None = None) -> None:
    """为 "app" logger 安装 JSON handler；幂等（重复调用不会叠加 handler）。

    其他 logger（uvicorn / openai / aiosqlite）保持默认行为，避免干扰测试捕获与依赖库日志。
    """
    level = (level or settings.log_level).upper()
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    for h in list(app_logger.handlers):
        app_logger.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    app_logger.addHandler(handler)
    # 不向上传播到 root，避免与 root 的默认 handler 重复输出
    app_logger.propagate = False


__all__ = ["redact", "JsonFormatter", "configure_logging"]
