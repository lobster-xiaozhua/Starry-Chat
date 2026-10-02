"""端到端重试验证：启动带 429 的假上游，确认客户端指数递增重试后成功。"""

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["APP_ENV"] = "development"
os.environ["LLM_API_KEY"] = "sk-test"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:4499/v1"
os.environ["LLM_MODEL"] = "test-model"
os.environ["LLM_MAX_RETRIES"] = "5"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{ROOT}/data/retry_e2e.db"
os.environ["LOG_LEVEL"] = "INFO"

PIECES = ["流式", "回答", "验证", "成功"]
FAIL_TIMES = 3
_counter = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        model = body.get("model", "m")
        _counter["n"] += 1
        calls.append(_counter["n"])

        if _counter["n"] <= FAIL_TIMES:
            # 前 3 次：429 + Retry-After: 1（注意 retry-after 设小，避免测试等太久）
            payload = json.dumps({"error": {"message": "rate limited", "type": "rate_limit_exceeded"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(payload)
            return

        # 第 4 次：正常流式
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(obj):
            data = ("data: " + json.dumps(obj) + "\n\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        for p in PIECES:
            frame({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
                   "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]})
        frame({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


calls: list[int] = []


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 4499), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    from app.llm.client import LLMClient
    from app.llm.tokenizer import count_tokens

    # 把退避延迟压到很小，避免测试等 0.5/1/2/4 秒
    import app.llm.client as llm_client_mod
    llm_client_mod.RETRY_DELAYS = [0.05 * (2 ** i) for i in range(5)]  # 0.05,0.1,0.2,0.4,0.8

    client = LLMClient()
    t0 = time.monotonic()
    parts: list[str] = []
    async for text, _tok in client.stream_chat([{"role": "user", "content": "你好"}]):
        parts.append(text)
    elapsed = time.monotonic() - t0

    server.shutdown()
    joined = "".join(parts)
    print(f"上游被调用次数: {len(calls)}（期望 {FAIL_TIMES + 1}）")
    print(f"收到的内容: {joined!r}（期望 {''.join(PIECES)!r}）")
    print(f"总耗时: {elapsed:.2f}s（重试退避累计应约 0.05+0.1+0.2=0.35s）")
    assert len(calls) == FAIL_TIMES + 1, f"重试次数不对: {len(calls)}"
    assert joined == "".join(PIECES), f"内容不对: {joined!r}"
    assert elapsed >= 0.3, f"似乎没有真正退避: {elapsed}"
    print("\n重试验证通过：指数递增重试后第 4 次成功，内容完整。")


if __name__ == "__main__":
    asyncio.run(main())
