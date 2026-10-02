"""最小 OpenAI 兼容上游：可注入 429/5xx/超时/正常四种行为，用于验证重试。

用法：BEHAVIOR=rate_limit|server_error|slow|ok python fake_upstream.py
- rate_limit: 前 N 次返回 429 + Retry-After: 1，第 N+1 次正常流式
- server_error: 前 N 次返回 500，第 N+1 次正常
- slow: 首 token 延迟 20s（用于触发客户端 read timeout，本脚本不主动断开）
- ok: 直接流式
N 由 FAIL_TIMES 控制（默认 3）。
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIECES = ["流式", "回答", "验证", "成功"]
BEHAVIOR = os.environ.get("BEHAVIOR", "ok")
FAIL_TIMES = int(os.environ.get("FAIL_TIMES", "3"))

# 进程级调用计数：不同测试用例共用同一进程时按行为隔离
_COUNTERS: dict[str, int] = {}


def _bump(key: str) -> int:
    _COUNTERS[key] = _COUNTERS.get(key, 0) + 1
    return _COUNTERS[key]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, status: int, payload: dict, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _stream_ok(self, model: str):
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
            frame({
                "id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
                "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}],
            })
            time.sleep(0.25)
        frame({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        model = body.get("model", "m")

        if BEHAVIOR == "ok":
            if body.get("stream"):
                self._stream_ok(model)
            else:
                self._send_json(200, {
                    "id": "t", "object": "chat.completion", "created": 0, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "测试标题"}, "finish_reason": "stop"}],
                })
            return

        if BEHAVIOR == "rate_limit":
            k = _bump("rl")
            if k <= FAIL_TIMES:
                self._send_json(429, {"error": {"message": "rate limited", "type": "rate_limit_exceeded"}},
                                extra_headers={"Retry-After": "1"})
            else:
                self._stream_ok(model)
            return

        if BEHAVIOR == "server_error":
            k = _bump("se")
            if k <= FAIL_TIMES:
                self._send_json(500, {"error": {"message": "internal", "type": "server_error"}})
            else:
                self._stream_ok(model)
            return

        if BEHAVIOR == "slow":
            # 接受请求后长时间不写响应体，触发客户端 read timeout
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            time.sleep(30)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        self._send_json(400, {"error": {"message": f"unknown BEHAVIOR={BEHAVIOR}"}})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3999"))
    print(f"fake upstream on :{port}, BEHAVIOR={BEHAVIOR}, FAIL_TIMES={FAIL_TIMES}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
