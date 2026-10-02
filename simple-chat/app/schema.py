"""请求 / 响应模型；纯 Pydantic v2，不依赖 FastAPI 路由签名。

模型契约对齐 README 与 SSE 契约：
- 请求：ChatRequest（流式默认 True）、CreateConversationRequest。
- 响应：MessageDTO / ConversationDTO（含 last_message 预览）/ ChatResponse（非流式）/ ErrorResponse。
- SSE 事件载荷：TokenEvent / DoneEvent / ErrorEvent（用于校验帧内容，不参与响应模型）。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["user", "assistant", "system"]


# ───────────────────────── 请求 ─────────────────────────


class ChatRequest(BaseModel):
    """对话请求；conversation_id 缺省即新建会话。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(
        default=None, description="为空则新建会话", max_length=64
    )
    message: str = Field(
        ..., min_length=1, max_length=4000, description="用户输入，非空，最大 4000 字符"
    )
    model: str | None = Field(
        default=None, description="覆盖默认模型", max_length=128
    )
    stream: bool = Field(default=True, description="是否流式")

    @field_validator("message")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        """去空白后为空 → ValueError 触发 422 VALIDATION_ERROR。"""
        stripped = v.strip()
        if not stripped:
            raise ValueError("message 不能为空")
        return stripped


class CreateConversationRequest(BaseModel):
    """显式建会话；title 缺省则服务端用首条消息截断生成。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)


# ───────────────────────── 响应 ─────────────────────────


class MessageDTO(BaseModel):
    """单条消息的对外表示。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: Role
    content: str
    tokens: int
    created_at: str


class ConversationDTO(BaseModel):
    """会话对外表示；last_message 为最近一条消息内容预览（≤80 字）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    created_at: str
    updated_at: str
    last_message: str | None = None


class Usage(BaseModel):
    """token 用量；对齐 OpenAI usage 结构。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """非流式对话响应。"""

    conversation_id: str
    message: MessageDTO
    usage: Usage


class ErrorBody(BaseModel):
    """错误体；code 取 ErrorCode 枚举字符串。"""

    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """统一错误响应：{"error": {...}}。"""

    error: ErrorBody


# ───────────────────────── SSE 事件载荷 ─────────────────────────
# 不作为 HTTP 响应模型，仅用于校验每个 event: 的 data 内容与契约一致。


class TokenEvent(BaseModel):
    """event: "token" 的 data。"""

    delta: str


class DoneEvent(BaseModel):
    """event: "done" 的 data。"""

    conversation_id: str
    message_id: int
    usage: Usage


class ErrorEvent(BaseModel):
    """event: "error" 的 data；扁平结构，不包 error 外壳。"""

    code: str
    message: str


# ───────────────────────── 兼容旧名 ─────────────────────────
# 早期 schema 用 ConversationListOut / MessageListOut 作为列表响应模型，保留以避免
# 大范围改动 router/service。它们不在新规格里，但 router 仍引用。


class ConversationListOut(BaseModel):
    """会话列表响应。"""

    conversations: list[ConversationDTO]


class MessageListOut(BaseModel):
    """消息列表响应。"""

    conversation_id: str
    messages: list[MessageDTO]


__all__ = [
    "Role",
    "ChatRequest",
    "CreateConversationRequest",
    "MessageDTO",
    "ConversationDTO",
    "Usage",
    "ChatResponse",
    "ErrorBody",
    "ErrorResponse",
    "TokenEvent",
    "DoneEvent",
    "ErrorEvent",
    "ConversationListOut",
    "MessageListOut",
]
