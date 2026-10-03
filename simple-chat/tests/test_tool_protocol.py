"""工具结果信封 JSON Schema 校验（ROADMAP v0.5 §4.3）。

校验：
- `is_error` 永远是 bool 且必出现；
- 成功必有 `result`、无 `error`；
- 失败只有 `error`、无 `result`；
- `meta` 必含 `duration_ms` 与 `truncated`；
- `to_payload` 输出紧凑载荷符合上述约束。
"""

from __future__ import annotations

import json

import pytest

from app.tools.protocol import (
    ToolErrorCode,
    ToolResult,
    error_result,
    ok_result,
    to_payload,
)


def test_ok_result_envelope():
    r = ok_result("calculate", {"value": 42}, duration_ms=5)
    assert isinstance(r["is_error"], bool)
    assert r["is_error"] is False
    assert r["tool"] == "calculate"
    assert r["result"] == {"value": 42}
    assert r["error"] is None
    assert r["meta"]["duration_ms"] == 5
    assert r["meta"]["truncated"] is False


def test_error_result_envelope():
    r = error_result(
        "read_file",
        ToolErrorCode.PATH_NOT_ALLOWED,
        "路径不在允许目录内",
        duration_ms=0,
    )
    assert isinstance(r["is_error"], bool)
    assert r["is_error"] is True
    assert r["tool"] == "read_file"
    assert r["result"] is None
    assert r["error"]["code"] == "PATH_NOT_ALLOWED"
    assert r["error"]["message"] == "路径不在允许目录内"
    assert isinstance(r["error"]["retryable"], bool)
    assert r["meta"]["duration_ms"] == 0
    assert isinstance(r["meta"]["truncated"], bool)


def test_to_payload_ok_compact():
    r = ok_result("calculate", {"value": 1.5}, duration_ms=2)
    p = to_payload(r)
    # 成功载荷必有 result 无 error
    assert "result" in p
    assert "error" not in p
    assert p["is_error"] is False
    assert p["meta"]["duration_ms"] == 2
    assert p["meta"]["truncated"] is False


def test_to_payload_error_compact():
    r = error_result(
        "web_search",
        ToolErrorCode.NOT_CONFIGURED,
        "未配置",
        duration_ms=0,
    )
    p = to_payload(r)
    assert "error" in p
    assert "result" not in p
    assert p["is_error"] is True


def test_meta_required_fields():
    r = ok_result("calculate", 1, duration_ms=10, truncated=True)
    meta = r["meta"]
    assert set(meta.keys()) >= {"duration_ms", "truncated"}
    assert isinstance(meta["duration_ms"], int)
    assert isinstance(meta["truncated"], bool)


def test_duration_ms_non_negative():
    r = error_result("calculate", "X", "msg", duration_ms=-5)
    assert r["meta"]["duration_ms"] >= 0


def test_error_body_required_fields():
    r = error_result("calculate", "CODE", "msg", duration_ms=0, retryable=True)
    err = r["error"]
    assert set(err.keys()) == {"code", "message", "retryable"}
    assert isinstance(err["code"], str)
    assert isinstance(err["message"], str)
    assert isinstance(err["retryable"], bool)


def test_payload_json_serializable():
    """信封必须可 JSON 序列化（写入消息历史）。"""
    r = ok_result("calculate", {"value": 218.79}, duration_ms=3)
    s = json.dumps(to_payload(r))
    assert json.loads(s) == {
        "is_error": False,
        "tool": "calculate",
        "result": {"value": 218.79},
        "meta": {"duration_ms": 3, "truncated": False},
    }


def test_error_payload_json_serializable():
    r = error_result(
        "read_file",
        ToolErrorCode.PATH_NOT_ALLOWED,
        "路径不在允许目录内",
        duration_ms=0,
    )
    s = json.dumps(to_payload(r))
    loaded = json.loads(s)
    assert loaded["is_error"] is True
    assert loaded["error"]["code"] == "PATH_NOT_ALLOWED"
    assert "result" not in loaded


def test_envelope_exclusive_result_error():
    """is_error=true 时不能有 result；false 时不能有 error。"""
    r_ok: ToolResult = ok_result("calculate", 1, duration_ms=0)
    r_err: ToolResult = error_result("calculate", "X", "m", duration_ms=0)

    p_ok = to_payload(r_ok)
    p_err = to_payload(r_err)

    assert not r_ok["is_error"]
    assert r_ok["error"] is None
    assert r_err["is_error"]
    assert r_err["result"] is None

    assert "result" in p_ok and "error" not in p_ok
    assert "error" in p_err and "result" not in p_err
