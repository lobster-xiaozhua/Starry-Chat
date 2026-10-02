"""验证 5xx 重试：上游前 2 次返回 500，第 3 次流式成功。"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["LLM_API_KEY"] = "sk-test"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:4499/v1"
os.environ["LLM_MAX_RETRIES"] = "5"

import app.llm.client as m
m.RETRY_DELAYS = [0.05 * (2 ** i) for i in range(5)]

from app.llm.client import LLMClient


async def main():
    client = LLMClient()
    parts: list[str] = []
    t0 = time.monotonic()
    async for text, _tok in client.stream_chat([{"role": "user", "content": "x"}]):
        parts.append(text)
    joined = "".join(parts)
    dt = time.monotonic() - t0
    print(f"OK 内容={joined!r} 耗时={dt:.2f}s")
    assert joined == "流式回答验证成功", joined


if __name__ == "__main__":
    asyncio.run(main())
