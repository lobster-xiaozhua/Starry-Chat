"""FastAPI 入口：应用装配、生命周期、静态页、全局异常处理、健康检查与中间件。"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__ as _APP_VERSION, metrics
from app.admin import router as admin_router
from app.auth.router import router as auth_router
from app.auth.session import SESSION_COOKIE, verify_session_value
from app.chat import router as chat_router
from app.chat.service import shutdown_tasks, _reap_locks
from app.config import settings
from app.db import get_db, init_db
from app.errors import AppError, ErrorCode
from app.llm import client as llm_client
from app.log import configure_logging
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.middleware.rate_limit import RateLimitMiddleware

configure_logging()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"


def error_response(
    status_code: int, code: ErrorCode, message: str, request_id: str | None = None
) -> JSONResponse:
    err: dict = {"code": code.value, "message": message}
    if request_id:
        err["request_id"] = request_id
    return JSONResponse(status_code=status_code, content={"error": err})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("database ready at %s", settings.db_path)
    # 后台回收会话锁残留条目（防 dict 无限增长 / 异常崩溃泄漏）
    reaper = asyncio.create_task(_reap_locks())
    try:
        yield
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
        await shutdown_tasks()


app = FastAPI(title="Simple Chat API", version=_APP_VERSION, lifespan=lifespan)


# ───────────────────────── 请求日志中间件（结构化 JSON） ─────────────────────────
def _log_identity(request: Request) -> str:
    """日志用身份：认证开启时优先已验证 Cookie，否则回退 header 兼容路径。

    仅用于日志与 request.state，不承担权限判断；拒绝匿名请求由 UserIdDep 负责。
    """
    if settings.effective_auth_enabled:
        uid = verify_session_value(request.cookies.get(SESSION_COOKIE))
        if uid:
            return uid
    return request.headers.get("X-User-Id") or "anonymous"


class RequestLogMiddleware(BaseHTTPMiddleware):
    """记录每个请求：request_id / method / path / status / latency_ms / user_id。

    request_id 回写 X-Request-Id 响应头；user_id 优先取已验证的会话 Cookie，
    认证关闭时回退 X-User-Id（缺失为 anonymous），并写入 request.state 供流式
    断开日志复用。请求体不记录（隐私），认证 Cookie 值绝不进日志。
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = request_id
        user_id = _log_identity(request)
        request.state.user_id = user_id
        # PR-3 改动 1：trace_id 经 contextvars 透传，供慢查询等内部日志关联请求
        metrics.trace_id.set(request_id)

        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            latency_ms = (time.monotonic() - start) * 1000
            logger.error(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "user_id": user_id,
                    "method": request.method,
                    "path": request.url.path,
                    "latency_ms": round(latency_ms, 1),
                    "event": "request",
                    "error_code": "UNHANDLED",
                },
                exc_info=True,
            )
            raise

        latency_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "user_id": user_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
                "event": "request",
            },
        )
        return response


app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ───────────────────────── Prometheus 指标 ─────────────────────────
@app.get("/metrics", include_in_schema=False, summary="Prometheus 指标（PR-3 改动 1）")
async def prometheus_metrics():
    """暴露 Prometheus text format 指标。

    - 不参与限流（rate_limit 豁免名单）、不参与鉴权（无用户态）。
    - 本端点自身不产生任何 chat_* 指标更新（chat_requests_total 只在 chat
      路径递增，避免自增风暴）。
    - 生产建议：仅监听 127.0.0.1 或由反向代理（docs/nginx.conf）做 IP 白名单。
    """
    metrics.refresh_process_memory()
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


# ───────────────────────── 健康检查 ─────────────────────────
@app.get("/healthz", include_in_schema=True, summary="存活探针（不查 DB）")
async def healthz() -> dict:
    """轻量存活检查，供负载均衡/容器探针使用；不触碰数据库或模型。"""
    return {
        "status": "ok",
        "version": app.version,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/readyz", include_in_schema=True, summary="就绪探针（DB + Key + 模型可达）")
async def readyz() -> JSONResponse:
    """就绪检查：DB 可达 + LLM_API_KEY 非空 + 模型轻量可达（超时 5s）。

    任一失败返回 503，并在 checks / error 字段中说明原因。
    """

    def _body(status: str, checks: dict, reason: str | None, error: str | None):
        payload: dict = {
            "status": status,
            "version": app.version,
            "ts": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        }
        if reason:
            payload["reason"] = reason
        if error:
            payload["error"] = error
        return JSONResponse(status_code=200 if status == "ok" else 503, content=payload)

    checks: dict = {}

    # 1) DB 可达
    try:
        async with get_db() as conn:
            await conn.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        logger.error(
            "readyz check failed",
            extra={"event": "readyz", "error_code": "DB_UNREACHABLE"},
        )
        return _body("unavailable", checks, "db_unreachable", str(exc))

    # 2) LLM API Key 非空
    if not settings.llm_api_key.strip():
        checks["llm_key"] = "missing"
        return _body("unavailable", checks, "llm_key_missing", "LLM_API_KEY 未配置")
    checks["llm_key"] = "ok"

    # 3) 模型轻量可达（5s 超时）
    try:
        await asyncio.wait_for(llm_client.ping(), timeout=5.0)
        checks["model"] = "ok"
    except Exception as exc:
        logger.error(
            "readyz check failed",
            extra={
                "event": "readyz",
                "error_code": getattr(exc, "code", type(exc).__name__),
            },
        )
        return _body("unavailable", checks, "model_unreachable", str(exc))

    return _body("ok", checks, None, None)


# ───────────────────────── 首页（注入模型名） ─────────────────────────
@app.get("/", include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    # 注入 model 名给前端 <meta name="llm-model">（app.js 读取展示于侧边栏底部），
    # 以及认证开关 <meta name="auth-enabled">（app.js 决定是否显示登录门）。
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'name="llm-model" content=""',
        f'name="llm-model" content="{settings.llm_model}"',
    )
    html = html.replace(
        'name="auth-enabled" content=""',
        f'name="auth-enabled" content="{"true" if settings.effective_auth_enabled else "false"}"',
    )
    return HTMLResponse(html)


# ───────────────────────── 全局异常处理器 ─────────────────────────
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return error_response(exc.status_code, exc.code, exc.message, rid)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query"))
    detail = f"{loc}: {first.get('msg', '参数不合法')}" if loc else "请求参数不合法"
    rid = getattr(request.state, "request_id", None)
    return error_response(422, ErrorCode.VALIDATION_ERROR, detail, rid)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        code = ErrorCode.NOT_FOUND
    elif exc.status_code == 405:
        code = ErrorCode.METHOD_NOT_ALLOWED
    else:
        code = ErrorCode.INTERNAL_ERROR
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    rid = getattr(request.state, "request_id", None)
    return error_response(exc.status_code, code, message, rid)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    message = "服务内部错误" if settings.is_production else f"服务内部错误: {exc}"
    rid = getattr(request.state, "request_id", None)
    return error_response(500, ErrorCode.INTERNAL_ERROR, message, rid)


app.include_router(auth_router)  # /api/auth/*（v0.2 注册/登录/登出/me）
app.include_router(chat_router.router)  # router 自身已带 prefix="/api/chat"
app.include_router(admin_router)  # /api/admin/cost（PR-3 改动 2）
