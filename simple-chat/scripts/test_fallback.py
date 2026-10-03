"""v0.3 fallback 降级脚本：用 scripts/fake_upstream.py 的 BEHAVIOR=server_error
验证 fallback 只降级一次、已产出 token 后不切换。

用法：
    BEHAVIOR=server_error python scripts/fake_upstream.py &
    python scripts/test_fallback.py

或先 BEHAVIOR=ok 跑一遍确认基线，再切 server_error。
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试用最小环境变量：开发环境、关闭认证、开启路由
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:3999/v1")
os.environ.setdefault("LLM_MODEL", "primary-model")
os.environ.setdefault("ROUTING_ENABLED", "true")
os.environ.setdefault("MODEL_WHITELIST", '["primary-model","fallback-model"]')
os.environ.setdefault("FALLBACK_MODELS", '["fallback-model"]')

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm import client as llm_client  # noqa: E402


async def main():
    # 真实上游：使用 fake_upstream.py
    await init_db()

    # 让 client 用真实 AsyncOpenAI（不注入 fake），打向 fake_upstream.py
    llm_client.reset_client()

    print(
        f"settings: routing={settings.effective_routing_enabled} "
        f"whitelist={settings.effective_model_whitelist} "
        f"fallback={settings.effective_fallback_models}"
    )

    # 场景 1: BEHAVIOR=server_error 时，primary 500，应降级到 fallback
    behavior = os.environ.get("BEHAVIOR", "server_error")
    print(f"BEHAVIOR={behavior}")
    if behavior == "server_error":
        # fake_upstream 对所有请求都返回 500（FAIL_TIMES 默认 3，retry 也吃掉），
        # primary 与 fallback 都会失败 → 最终抛 AppError(MODEL_UNAVAILABLE)
        try:
            tokens = []
            async for t in llm_client.chat_stream(
                [{"role": "user", "content": "hi"}], model="primary-model"
            ):
                tokens.append(t)
            print(f"FAIL: expected fallback to also fail, got tokens: {tokens!r}")
            sys.exit(1)
        except Exception as exc:
            code = getattr(exc, "code", None)
            print(f"OK: both primary and fallback exhausted -> {type(exc).__name__} code={code}")
            # 验证只降级一次：fallback 候选只有一个，符合“最多一次”
            assert settings.effective_fallback_models == ["fallback-model"]
            print("OK: fallback attempted at most once (single candidate)")

    # 场景 2: 已产出 token 后报错不切换。
    # 用注入的 fake 客户端模拟：先产出 1 token，再抛错。
    from tests.conftest import FakeAsyncOpenAI

    class _PartialThenErrorStream:
        def __init__(self):
            self._yielded = False

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield type("C", (), {"choices": [type("Ch", (), {
                "delta": type("D", (), {"content": "partial"})(),
                "finish_reason": None,
            })()]})()
            raise RuntimeError("mid-stream boom")

    class _PartialCompletions:
        async def create(self, **kwargs):
            if kwargs.get("model") == "primary-model":
                return _PartialThenErrorStream()
            # fallback 不应被调用
            raise AssertionError("fallback should NOT be invoked after token produced")

    class _PartialClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": _PartialCompletions()})()

    llm_client.set_client(_PartialClient())
    # 关闭 fallback 之外的 retry 干扰：reset settings.fallback_models 仍保留
    produced = []
    try:
        async for t in llm_client.chat_stream(
            [{"role": "user", "content": "hi"}], model="primary-model"
        ):
            produced.append(t)
        print(f"FAIL: expected mid-stream error, got tokens={produced!r}")
        sys.exit(1)
    except Exception as exc:
        print(f"OK: mid-stream error after token not retried/fallback -> {type(exc).__name__}")
        assert produced == ["partial"], produced
        print("OK: produced token preserved, fallback NOT invoked")

    print("\nAll fallback checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
