"""T3（PR-2 改动 3）：map_openai_error 错误码映射（parametrize）。

BadRequestError 按 (status_code, error.code, body 关键词) 三级判断：
  413 / context_length → CONTEXT_OVERFLOW
  invalid_api_key / authentication → AUTH_ERROR
  model + not found → MODEL_UNAVAILABLE
  其余 → VALIDATION_ERROR
APITimeoutError → RATE_LIMITED（保持不变）
"""

import httpx
import openai
import pytest

from app.errors import ErrorCode
from app.llm.client import map_openai_error


def _status_error(cls, status: int, body: dict):
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    resp = httpx.Response(status_code=status, request=req)
    return cls("boom", response=resp, body=body)


def _timeout_error():
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    return openai.APITimeoutError(request=req)


CASES = [
    (
        "badreq-413",
        lambda: _status_error(
            openai.BadRequestError, 413, {"error": {"code": "x", "message": "too large"}}
        ),
        ErrorCode.CONTEXT_OVERFLOW,
    ),
    (
        "badreq-context-keyword",
        lambda: _status_error(
            openai.BadRequestError,
            400,
            {"error": {"code": "context_length_exceeded", "message": "max tokens"}},
        ),
        ErrorCode.CONTEXT_OVERFLOW,
    ),
    (
        "badreq-invalid-api-key",
        lambda: _status_error(
            openai.BadRequestError,
            400,
            {"error": {"code": "invalid_api_key", "message": "bad key"}},
        ),
        ErrorCode.AUTH_ERROR,
    ),
    (
        "badreq-model-not-found",
        lambda: _status_error(
            openai.BadRequestError,
            400,
            {"error": {"code": "model_not_found", "message": "The model 'x' not found"}},
        ),
        ErrorCode.MODEL_UNAVAILABLE,
    ),
    (
        "badreq-other",
        lambda: _status_error(
            openai.BadRequestError,
            400,
            {"error": {"code": "invalid_parameter", "message": "bad param"}},
        ),
        ErrorCode.VALIDATION_ERROR,
    ),
    ("api-timeout", _timeout_error, ErrorCode.RATE_LIMITED),
]


@pytest.mark.parametrize("name,exc_factory,expected", CASES, ids=[c[0] for c in CASES])
def test_error_code_mapping(name, exc_factory, expected):
    err = map_openai_error(exc_factory())
    assert err.code is expected
