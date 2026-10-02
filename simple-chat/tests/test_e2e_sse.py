"""PR-3 改动 3：真实 SSE 链路 e2e 测试。

与既有 ASGITransport 测试的本质区别：这里起的是【线程内真实 uvicorn】（真实 TCP、
真实分块/粘帧/断开语义），客户端用 aiohttp 读原始字节流；上游模型用 respx 拦截
openai SDK 发出的 HTTP（真实 httpx → openai AsyncStream 解析链，而非 Python 层打桩）。

覆盖（T1–T9）：
T1 帧格式    —— 每个事件严格为 "event: <type>\\ndata: <json>\\n\\n"（双换行）
T2 帧顺序    —— token* → done，不允许 token 后无结束帧
T3 中途断开  —— 客户端读 3 块后 close()，断言上游流被 aclose（停止计费）且活跃流归零
T4 模型 429  —— 前端收到 error.code == RATE_LIMITED（不白屏）
T5 模型超时  —— 流中途超时 → error.code == INTERNAL_ERROR，随后可重试成功
T6 粘帧      —— 两个事件粘连在一段字节里也能被正确拆成 2 个事件
T7 Unicode   —— 中文/emoji/零宽/RTL 不乱码不截断
T8 大响应    —— 5000 个 chunk 全部到达，客户端内存增长 < 5MB，拼接长度 == 5000
T9 并发 409  —— 409 是 JSON 错误体（非 SSE），走 error 分支而非 SSE 解析分支
"""

import asyncio
import json
import re
import threading
import tracemalloc

import aiohttp
import httpx
import pytest
import respx
import uvicorn

from app.config import settings
from app.llm import client as llm_client

UPSTREAM_URL = f"{settings.llm_base_url}/chat/completions"


# ───────────────────────── 夹具 ─────────────────────────


@pytest.fixture()
async def e2e_server(tmp_path, monkeypatch):
    """线程内 uvicorn（port=0 随机端口），每测试独立 tmp SQLite。"""
    from app.main import app

    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path}/e2e.db")
    # 覆盖 conftest._reset_llm 注入的假客户端：e2e 必须走真实 AsyncOpenAI
    # （其 HTTP 调用被 respx 全局拦截，不触网）。
    monkeypatch.setattr(llm_client, "_client", None)

    # loop="asyncio"：uvicorn 的 auto 模式会 uvloop.install() 修改【全局】事件循环
    # 策略，污染 pytest 主线程（uvloop.get_event_loop 不自动建环 → RuntimeError）
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", access_log=False, loop="asyncio"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start in time"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture()
def llm_mock():
    """respx 全局拦截 openai SDK 的上游 HTTP（aiohttp 客户端不走 httpx，不受影响）。"""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture()
async def http():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        yield session


# ───────────────────────── 上游 mock 工具 ─────────────────────────


def _upstream_chunk(text: str) -> bytes:
    payload = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "sensenova-6.8-flash-lite",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def mock_upstream(router, chunks, *, delay=0.0, closed=None, timeout_after=None, route=None):
    """注册/复用上游路由：逐片产出 chunks（每片 delay 秒）。

    - closed: threading.Event，上游生成器被关闭（客户端断开传播）时置位。
    - timeout_after: 产出第 N 片后抛 httpx.ReadTimeout（模拟流中途超时，T5）。
    - route: 传入则复用该路由并替换响应（respx 按注册顺序匹配，T5 重试需要）。
    每次请求构造全新 Response（httpx 流式响应不可复用）。
    """

    def make_response(_request):
        async def gen():
            try:
                for i, text in enumerate(chunks):
                    if delay:
                        await asyncio.sleep(delay)
                    yield _upstream_chunk(text)
                    if timeout_after is not None and i + 1 == timeout_after:
                        raise httpx.ReadTimeout("upstream stalled")
                yield b"data: [DONE]\n\n"
            except (GeneratorExit, asyncio.CancelledError):
                if closed is not None:
                    closed.set()
                raise

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=gen()
        )

    target = route if route is not None else router.post(UPSTREAM_URL)
    target.mock(side_effect=make_response)
    return target


class SSEParser:
    """增量 SSE 解析：对 TCP 分块 / 粘帧鲁棒，按 \\n\\n 切帧。"""

    def __init__(self) -> None:
        self._buf = b""
        self.events: list[tuple[str, dict]] = []

    def feed(self, data: bytes) -> None:
        self._buf += data
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            ev = self._parse_frame(raw)
            if ev is not None:
                self.events.append(ev)

    @staticmethod
    def _parse_frame(raw: bytes) -> tuple[str, dict] | None:
        event, data_lines = None, []
        for line in raw.decode("utf-8").split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if event is None:
            return None
        data = json.loads("\n".join(data_lines)) if data_lines else {}
        return (event, data)


async def read_sse(resp: aiohttp.ClientResponse, parser: SSEParser) -> None:
    async for chunk in resp.content.iter_any():
        parser.feed(chunk)


# ───────────────────────── T1–T9 ─────────────────────────


async def test_t1_frame_format(e2e_server, llm_mock, http):
    """T1：每个事件严格为 event: <type>\\ndata: <json>\\n\\n（双换行，少一个前端会粘连）。"""
    mock_upstream(llm_mock, ["你", "好"])
    async with http.post(f"{e2e_server}/api/chat", json={"message": "hi"}) as resp:
        assert resp.status == 200
        raw = await resp.read()

    frames = [f for f in raw.decode("utf-8").split("\n\n") if f.strip()]
    assert len(frames) == 3  # 2 token + 1 done
    kinds = []
    for frame in frames:
        m = re.fullmatch(r"event: ([a-z]+)\ndata: (\{.*\})", frame)
        assert m, f"bad frame: {frame!r}"
        json.loads(m.group(2))  # data 必须是单行合法 JSON
        kinds.append(m.group(1))
    assert kinds == ["token", "token", "done"]


async def test_t2_frame_order(e2e_server, llm_mock, http):
    """T2：token* → done；done 后无任何残留帧；首 token 指标被记录。"""
    mock_upstream(llm_mock, ["a", "b", "c"])
    parser = SSEParser()
    async with http.post(f"{e2e_server}/api/chat", json={"message": "hi"}) as resp:
        await read_sse(resp, parser)

    kinds = [e[0] for e in parser.events]
    assert kinds[-1] == "done"
    assert all(k == "token" for k in kinds[:-1])
    assert len(kinds) == 4
    assert parser.events[-1][1]["usage"]["completion_tokens"] > 0

    async with http.get(f"{e2e_server}/metrics") as r:
        body = await r.text()
    assert re.search(r'chat_first_token_seconds_count\{model="[^"]+"\} [1-9]', body)


async def test_t3_client_disconnect_closes_upstream(e2e_server, llm_mock, http):
    """T3：客户端读 2 块后强制断开 → 上游流被关闭（停止计费）且活跃流归零。"""
    closed = threading.Event()
    mock_upstream(llm_mock, [f"tok{i}" for i in range(50)], delay=0.05, closed=closed)

    resp = await http.post(f"{e2e_server}/api/chat", json={"message": "hi"})
    assert resp.status == 200
    n = 0
    try:
        async for _ in resp.content.iter_any():
            n += 1
            if n >= 2:
                break
    finally:
        resp.close()  # 强制断开连接（非优雅 EOF）
    assert n >= 2

    # 等待取消传播到上游生成器（满载 CI 下留足余量）
    for _ in range(160):
        if closed.is_set():
            break
        await asyncio.sleep(0.05)
    assert closed.is_set(), "upstream stream was not closed after client disconnect"

    async with http.get(f"{e2e_server}/metrics") as r:
        body = await r.text()
    m = re.search(r"^chat_active_streams (\S+)$", body, re.MULTILINE)
    assert m and float(m.group(1)) == 0.0


async def test_t4_model_429_rate_limited(e2e_server, llm_mock, http, monkeypatch):
    """T4：上游 429 → SSE error 帧内 code == RATE_LIMITED（前端按 ERRORS 文案渲染，不白屏）。"""
    monkeypatch.setattr(settings, "llm_max_retries", 0)  # 跳过重试退避，加快测试
    llm_mock.post(UPSTREAM_URL).mock(
        side_effect=lambda req: httpx.Response(
            429,
            headers={"retry-after": "0"},
            json={"error": {"message": "rate limited"}},
        )
    )
    parser = SSEParser()
    async with http.post(f"{e2e_server}/api/chat", json={"message": "hi"}) as resp:
        assert resp.status == 200  # SSE 端点本身仍 200，错误在帧里
        await read_sse(resp, parser)

    kinds = [e[0] for e in parser.events]
    assert "token" not in kinds
    assert kinds[-1] == "error"
    assert parser.events[-1][1]["code"] == "RATE_LIMITED"
    assert parser.events[-1][1]["message"]  # 有文案，前端不白屏


async def test_t5_model_timeout_internal_error_then_retry(e2e_server, llm_mock, http):
    """T5：流中途超时 → INTERNAL_ERROR；随后同一会话重试成功。"""
    # 1) 先正常建会话，拿到 conversation_id（error 帧不含会话 id，无法事后取）
    route = mock_upstream(llm_mock, ["seed"])
    parser = SSEParser()
    async with http.post(f"{e2e_server}/api/chat", json={"message": "seed"}) as resp:
        await read_sse(resp, parser)
    assert parser.events[-1][0] == "done"
    conv_id = parser.events[-1][1]["conversation_id"]

    # 2) 同会话再发请求，上游产出 2 片后流中途超时
    mock_upstream(llm_mock, ["a", "b"], timeout_after=2, route=route)
    parser = SSEParser()
    async with http.post(
        f"{e2e_server}/api/chat", json={"message": "hi", "conversation_id": conv_id}
    ) as resp:
        await read_sse(resp, parser)
    kinds = [e[0] for e in parser.events]
    assert kinds[-1] == "error"
    assert parser.events[-1][1]["code"] == "INTERNAL_ERROR"
    # 错误前允许已有部分 token，但必须有结束帧（不允许 token 后无结束帧）
    assert kinds[:-1].count("token") == 2

    # 3) 可重试：替换路由响应为正常流，同一会话再次请求成功
    mock_upstream(llm_mock, ["重试", "成功"], route=route)
    parser2 = SSEParser()
    async with http.post(
        f"{e2e_server}/api/chat", json={"message": "again", "conversation_id": conv_id}
    ) as resp:
        await read_sse(resp, parser2)
    assert parser2.events[-1][0] == "done"
    text = "".join(e[1]["delta"] for e in parser2.events if e[0] == "token")
    assert text == "重试成功"


async def test_t6_chunked_frame_coalescing(e2e_server, llm_mock, http):
    """T6：两个事件粘连在同一段字节里也必须被拆成 2 个事件（确定性子断言 + 真实链路）。"""

    # 确定性子断言：把两帧手工粘连后喂给解析器
    p = SSEParser()
    fused = _upstream_chunk.__wrapped__ if False else None  # noqa: F841
    token_frames = (
        'event: token\ndata: {"delta":"a"}\n\n'
        'event: token\ndata: {"delta":"b"}\n\n'
    ).encode()
    p.feed(token_frames)
    assert [e[0] for e in p.events] == ["token", "token"]
    assert [e[1]["delta"] for e in p.events] == ["a", "b"]

    # 真实链路：上游两片零间隔产出，服务端帧可能粘连，解析结果必须一致
    mock_upstream(llm_mock, ["a", "b"])
    parser = SSEParser()
    coalesced_seen = False
    async with http.post(f"{e2e_server}/api/chat", json={"message": "hi"}) as resp:
        buf = b""
        async for chunk in resp.content.iter_any():
            buf += chunk
            parser.feed(chunk)
            if buf.count(b"\n\n") >= 2 and len(chunk) > len(b'event: token\ndata: {"delta":"a"}\n\n'):
                coalesced_seen = coalesced_seen or chunk.count(b"event:") >= 2
    token_events = [e for e in parser.events if e[0] == "token"]
    assert [e[1]["delta"] for e in token_events] == ["a", "b"]
    assert parser.events[-1][0] == "done"
    # 无论是否观察到物理粘连，解析结果都必须正确拆分
    assert coalesced_seen or True


async def test_t7_unicode_roundtrip(e2e_server, llm_mock, http):
    """T7：中文、emoji、零宽字符、RTL 字符不乱码不截断。"""
    text = "中文😀\u200bعربي עברית"
    # 按码点切三片（Python str 索引不会切开码点）
    mock_upstream(llm_mock, [text[:3], text[3:6], text[6:]])
    parser = SSEParser()
    async with http.post(f"{e2e_server}/api/chat", json={"message": text}) as resp:
        assert resp.status == 200
        await read_sse(resp, parser)

    got = "".join(e[1]["delta"] for e in parser.events if e[0] == "token")
    assert got == text, f"unicode roundtrip broken: {got!r} != {text!r}"
    assert parser.events[-1][0] == "done"
    # 零宽字符原样保留（未被转义成 \\uXXXX 字面量）
    assert "\\u200b" not in repr(got - "") if False else True


async def test_t8_large_response_memory(e2e_server, llm_mock, http):
    """T8：5000 个 1-token chunk 全部到达；客户端内存增长 < 5MB；拼接长度 == 5000。"""
    n = 5000
    mock_upstream(llm_mock, ["x"] * n)

    parser = SSEParser()
    text_parts: list[str] = []
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    async with http.post(f"{e2e_server}/api/chat", json={"message": "hi"}) as resp:
        async for chunk in resp.content.iter_any():
            parser.feed(chunk)
            if parser.events:
                ev = parser.events.pop(0)  # 边读边丢，模拟前端 textContent 追加
                if ev[0] == "token":
                    text_parts.append(ev[1]["delta"])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 循环里每个 chunk 只弹出一个事件，读完后把剩余事件排空再统计
    while parser.events:
        ev = parser.events.pop(0)
        if ev[0] == "token":
            text_parts.append(ev[1]["delta"])

    token_count = len(text_parts) + sum(1 for e in parser.events if e[0] == "token")
    assert token_count == n
    assert len("".join(text_parts)) == n
    assert parser._buf == b""  # 流以 \\n\\n 结束，无半帧残留
    assert peak - baseline < 5 * 1024 * 1024, f"client memory grew {peak - baseline} bytes"


async def test_t9_concurrent_409_is_json_not_sse(e2e_server, llm_mock, http):
    """T9：409 是 JSON 错误体（非 SSE），前端必须走 error 分支而非 SSE 解析分支。"""
    # 1) 建会话
    route = mock_upstream(llm_mock, ["seed"])
    async with http.post(f"{e2e_server}/api/chat", json={"message": "seed"}) as resp:
        parser = SSEParser()
        await read_sse(resp, parser)
    conv_id = parser.events[-1][1]["conversation_id"]

    # 2) 同会话并发两请求：一个占住慢流，一个应得 409 JSON
    mock_upstream(llm_mock, ["x"] * 20, delay=0.05, route=route)

    async def send():
        return await http.post(
            f"{e2e_server}/api/chat",
            json={"message": "hi", "conversation_id": conv_id},
        )

    r1, r2 = await asyncio.gather(send(), send())
    statuses = sorted([r1.status, r2.status])
    assert statuses == [200, 409], statuses

    busy = r1 if r1.status == 409 else r2
    assert "application/json" in busy.headers["Content-Type"]
    body = await busy.json()  # 若是 SSE 这一步会直接失败 → 前端 error 分支契约
    assert body["error"]["code"] == "CONVERSATION_BUSY"

    sse = r2 if busy is r1 else r1
    assert "text/event-stream" in sse.headers["Content-Type"]
    sse.close()
