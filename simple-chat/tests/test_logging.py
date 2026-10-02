"""结构化日志与脱敏测试。"""

import json
import logging

from app.log import JsonFormatter, redact


def test_redact_masks_api_key():
    s = redact("config LLM_API_KEY=sk-abcdefghij1234567890xyz")
    assert "sk-abcdefghij1234567890xyz" not in s
    assert "sk-***" in s
    # 仅保留原 key 的最后 4 位（此处为 0xyz）
    assert s.endswith("0xyz")


def test_redact_masks_authorization_header():
    s = redact("Authorization: Bearer sk-secret-token-1234")
    assert "sk-secret-token-1234" not in s
    assert "Authorization: ***" in s


def test_redact_idempotent_on_plain_text():
    assert redact("hello world") == "hello world"


def _make_record(msg, **extra):
    # 构造一条携带 extra 字段的 LogRecord，模拟 logger.info(msg, extra={...})
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_includes_required_fields():
    fmt = JsonFormatter()
    record = _make_record("llm_call", model="m1", tokens_out=12, user_id="u1", latency_ms=3.5)
    out = json.loads(fmt.format(record))
    for f in (
        "timestamp",
        "level",
        "logger",
        "message",
        "request_id",
        "latency_ms",
        "model",
        "tokens_in",
        "tokens_out",
        "user_id",
        "error_code",
    ):
        assert f in out, f"missing field {f}"
    assert out["level"] == "INFO"
    assert out["message"] == "llm_call"
    assert out["model"] == "m1"
    assert out["tokens_out"] == 12
    assert out["user_id"] == "u1"
    # 缺失的结构化字段以 null 占位
    assert out["request_id"] is None
    assert out["error_code"] is None


def test_json_formatter_redacts_keys_in_message():
    fmt = JsonFormatter()
    record = _make_record("key=sk-abcdefghij9999 plaintext")
    out = json.loads(fmt.format(record))
    assert "sk-abcdefghij9999" not in out["message"]
    assert "sk-***" in out["message"]
