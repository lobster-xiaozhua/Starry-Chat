"""工具执行器测试：read_file / web_search / 开关（ROADMAP v0.5 §4 验收）。

覆盖：
- read_file("../../etc/passwd") → is_error=true, code=PATH_NOT_ALLOWED；
- 读取白名单内正常文件成功；
- 读 .env 被拒（UNSUPPORTED）；
- web_search 未配置 endpoint → NOT_CONFIGURED；
- 超时路径（read_file 大文件慢读）；
- 工具开关禁用 → UNAUTHORIZED。
"""

from __future__ import annotations

import os

import pytest

from app.config import settings as _settings
from app.tools.executor import execute
from app.tools.protocol import ToolCallRequest, ToolErrorCode


def _req(name, args) -> ToolCallRequest:
    return {
        "call_id": "test-call",
        "name": name,
        "arguments": args,
        "timeout_ms": 0,  # 由 executor 强制取 TOOL_POLICIES
        "max_output_bytes": 0,
    }


def _exec(name, args, cfg=None):
    return execute(_req(name, args), settings=cfg)


# ── read_file：路径白名单 ──


@pytest.fixture
def read_root(tmp_path, monkeypatch):
    """配置一个白名单根目录并放置测试文件。"""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "hello.txt").write_text("hello world\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "note.md").write_text("# title\n内容\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_file_enabled", True)
    monkeypatch.setattr(_settings, "tool_calculate_enabled", True)
    monkeypatch.setattr(_settings, "tool_web_search_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_roots", [str(root)])
    return root


def test_read_file_traversal_rejected(read_root):
    res = _exec("read_file", {"path": "../../etc/passwd"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.PATH_NOT_ALLOWED
    assert res["error"]["retryable"] is False


def test_read_file_absolute_outside_rejected(read_root):
    res = _exec("read_file", {"path": "/etc/passwd"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.PATH_NOT_ALLOWED


def test_read_file_success(read_root):
    res = _exec("read_file", {"path": "hello.txt"})
    assert res["is_error"] is False
    assert res["tool"] == "read_file"
    assert "hello world" in res["result"]["content"]
    assert res["result"]["bytes"] == len("hello world\n".encode("utf-8"))
    assert res["meta"]["truncated"] is False


def test_read_file_subpath_success(read_root):
    res = _exec("read_file", {"path": "sub/note.md"})
    assert res["is_error"] is False
    assert "内容" in res["result"]["content"]


def test_read_file_env_rejected(read_root):
    res = _exec("read_file", {"path": ".env"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.UNSUPPORTED


def test_read_file_key_rejected(read_root, tmp_path):
    keyfile = read_root / "id_rsa"
    keyfile.write_text("PRIVATE KEY DATA\n")
    res = _exec("read_file", {"path": "id_rsa"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.UNSUPPORTED


def test_read_file_db_rejected(read_root):
    dbfile = read_root / "chat.db"
    dbfile.write_bytes(b"\x00binary")
    res = _exec("read_file", {"path": "chat.db"})
    assert res["is_error"] is True
    # 数据库文件按名称模式在预校验阶段拒绝
    assert res["error"]["code"] == ToolErrorCode.UNSUPPORTED


def test_read_file_binary_rejected(read_root):
    binfile = read_root / "data.bin"
    binfile.write_bytes(b"\x00\x01\x02\x00binary")
    res = _exec("read_file", {"path": "data.bin"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.UNSUPPORTED


def test_read_file_symlink_escape_rejected(read_root, tmp_path):
    """符号链接指向白名单外 → realpath 解析后前缀校验拒绝。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    link = read_root / "link.txt"
    os.symlink(outside, link)
    res = _exec("read_file", {"path": "link.txt"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.PATH_NOT_ALLOWED


def test_read_file_truncated(read_root):
    """读取超过 max_bytes 的文件 → truncated=True，内容被截断。"""
    big = read_root / "big.txt"
    big.write_text("A" * 10000, encoding="utf-8")
    res = _exec("read_file", {"path": "big.txt", "max_bytes": 100})
    assert res["is_error"] is False
    assert res["result"]["bytes"] == 100
    assert res["meta"]["truncated"] is True


def test_read_file_not_found(read_root):
    res = _exec("read_file", {"path": "missing.txt"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.PATH_NOT_ALLOWED


def test_read_file_no_roots_configured(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_file_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_roots", [])
    res = _exec("read_file", {"path": "any.txt"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.NOT_CONFIGURED


# ── web_search ──


def test_web_search_not_configured(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_web_search_enabled", True)
    monkeypatch.setattr(_settings, "tool_web_search_endpoint", "")
    res = _exec("web_search", {"query": "test", "max_results": 3})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.NOT_CONFIGURED


def test_web_search_configured_placeholder(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_web_search_enabled", True)
    monkeypatch.setattr(_settings, "tool_web_search_endpoint", "https://broker.example/search")
    res = _exec("web_search", {"query": "starrchat", "max_results": 2})
    assert res["is_error"] is False
    assert res["result"]["query"] == "starrchat"
    assert res["result"]["results"] == []


# ── 工具开关 ──


def test_tool_disabled(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_calculate_enabled", False)
    res = _exec("calculate", {"expression": "1+2"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.UNAUTHORIZED


def test_tools_master_switch_off(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", False)
    res = _exec("calculate", {"expression": "1+2"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.UNAUTHORIZED


def test_unknown_tool_rejected():
    res = _exec("not_a_tool", {})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR


def test_invalid_arguments(monkeypatch):
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_calculate_enabled", True)
    res = _exec("calculate", {"expression": 123})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.VALIDATION_ERROR


# ── read_file timeout（慢读内核模拟）──


def test_read_file_timeout(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "slow.txt").write_text("ok\n")
    monkeypatch.setattr(_settings, "tools_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_file_enabled", True)
    monkeypatch.setattr(_settings, "tool_read_roots", [str(root)])

    # 让内核 sleep 超过 timeout_ms
    import app.tools.executor as ex
    import time as _time

    orig = ex.read_file

    def slow_read(path, max_bytes=65536):
        _time.sleep(3)
        return orig(path, max_bytes)

    monkeypatch.setattr(ex, "read_file", slow_read)
    res = _exec("read_file", {"path": "slow.txt"})
    assert res["is_error"] is True
    assert res["error"]["code"] == ToolErrorCode.TIMEOUT
