"""对话路由：/api/chat 及会话相关端点。

路由层只做 HTTP 适配：解析请求、调用 service、设置 SSE 响应头、格式化响应。
所有业务逻辑下沉到 app.chat.service，本文件不实现业务规则。
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.chat import service
from app.db import utcnow_iso
from app.deps import DbDep, UserIdDep
from app.errors import AppError, ErrorCode
from app.schema import (
    ChatRequest,
    ChatResponse,
    ConversationListOut,
    MessageDTO,
    MessageListOut,
    Usage,
)

logger = logging.getLogger(__name__)

# 路由自带前缀；main.py 用 include_router(chat_router.router) 挂载，不要再重复加前缀。
router = APIRouter(prefix="/api/chat", tags=["chat"])

# 流式响应必须设置的头：禁用缓存与代理缓冲，保证逐字推送。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 防止 Nginx 缓冲 SSE
}


def _parse_sse_frame(frame: str) -> tuple[str | None, dict]:
    """解析 service 产出的单个 SSE 帧，返回 (event, data)。

    service 产出的格式为 "event: <name>\ndata: <json>\n\n"。
    """
    event: str | None = None
    data_parts: list[str] = []
    for line in frame.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].strip())
    if not data_parts:
        return event, {}
    return event, json.loads("\n".join(data_parts))


async def _sse_stream(gen, conversation_id, request_id, user_id):
    """逐帧转发 service 已产出的 SSE 事件流。

    gen 是 service.send_message(...) 的返回值（_stream_response 异步生成器），
    其 preflight（建会话/落用户消息/裁剪上下文/取会话锁）已在端点中 await 完成，
    这里只负责把帧推给客户端；流式落 assistant 消息用的是 _stream_response 自己
    打开的 DB 连接（见 CODEBUDDY.md “流式连接 gotcha”）。

    客户端断开（关闭标签页 / AbortController）时 Starlette 取消本协程并抛
    CancelledError：连接已不可用，记录 "client_disconnected"（结构化字段），
    并确保内层生成器被 aclose，从而触发其 finally（落库/释放锁/上游 aclose）。
    上游 LLM 流的取消与计费释放由 app.llm.client.chat_stream 内部处理。
    """
    try:
        async for frame in gen:
            yield frame
    except asyncio.CancelledError:
        logger.info(
            "client_disconnected",
            extra={
                "event": "client_disconnected",
                "conversation_id": conversation_id,
                "request_id": request_id,
                "user_id": user_id,
            },
        )
        return
    finally:
        await gen.aclose()


@router.post("", summary="发送消息（流式 SSE 或 JSON）")
async def chat(body: ChatRequest, conn: DbDep, user_id: UserIdDep, request: Request):
    # 401 AUTH_ERROR：若未来配置要求登录（settings.require_login），此处对
    # anonymous 用户抛 AppError(ErrorCode.AUTH_ERROR)。当前无该配置，默认放行。
    #
    # 预检（建会话/落用户消息/裁剪上下文/取会话锁）必须在端点内 await 完成，
    # 因为依赖注入的请求级 DB 连接（conn）只在端点执行期间有效，FastAPI 在响应头
    # 发出后即关闭它；StreamingResponse 的 body_iterator 是在响应头之后才跑的，
    # 那时 conn 已不可用。预检通过后，剩下的流式落库由 _stream_response 自管的
    # 连接负责（见 CODEBUDDY.md “流式连接 gotcha”）。
    gen = await service.send_message(body, conn, user_id)
    request_id = getattr(request.state, "request_id", None)
    if body.stream:
        return StreamingResponse(
            _sse_stream(gen, body.conversation_id, request_id, user_id),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    return await _non_stream_response_from_gen(gen)


async def _non_stream_response_from_gen(gen) -> ChatResponse:
    """非流式响应：复用同一套流式逻辑逐块拼接，最后组装 ChatResponse。

    不另写一套非流式逻辑，避免两条实现分歧；流式过程通过 error 事件传达异常，
    这里把它还原成 AppError 交给全局处理器统一成信封。
    """
    parts: list[str] = []
    done: dict | None = None
    try:
        async for frame in gen:
            event, data = _parse_sse_frame(frame)
            if event == "token":
                parts.append(data.get("delta", ""))
            elif event == "done":
                done = data
            elif event == "error":
                raise AppError(ErrorCode(data["code"]), data.get("message"))
    finally:
        await gen.aclose()
    if done is None:
        # 正常流应以 done 结束；走到这里说明异常路径未抛出 error 事件，兜底为内部错误。
        raise AppError(ErrorCode.INTERNAL_ERROR, "流式未返回 done 事件")
    content = "".join(parts)
    message = MessageDTO(
        id=done["message_id"],
        role="assistant",
        content=content,
        tokens=done["usage"]["completion_tokens"],
        created_at=utcnow_iso(),
    )
    return ChatResponse(
        conversation_id=done["conversation_id"],
        message=message,
        usage=Usage(**done["usage"]),
    )


@router.get("/conversations", response_model=ConversationListOut, summary="会话列表")
async def list_conversations(
    conn: DbDep,
    user_id: UserIdDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    items = await service.list_conversations(conn, user_id, limit=limit, offset=offset)
    return {"conversations": items}


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessageListOut,
    summary="会话历史消息",
)
async def get_messages(
    conversation_id: str,
    conn: DbDep,
    user_id: UserIdDep,
    limit: int = Query(100, ge=1, le=500),
):
    messages = await service.get_messages(conversation_id, conn, user_id, limit=limit)
    return {"conversation_id": conversation_id, "messages": messages}


@router.delete("/conversations/{conversation_id}", status_code=204, summary="删除会话")
async def delete_conversation(conversation_id: str, conn: DbDep, user_id: UserIdDep):
    await service.delete_conversation(conversation_id, conn, user_id)
    return None
