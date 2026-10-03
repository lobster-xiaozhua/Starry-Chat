"""测试 RAG 子系统（FTS5 索引 + BM25 检索 + 增量去重 + 注入上限）。

对齐 ROADMAP v0.6 验收：
- 建索引 → 检索命中
- 二次入库不重复
- 删除源文件后索引可更新
- top-k 限制
- 注入上限触发截断
- RAG_ENABLED=false 时检索返回空列表（不报错）
- 召回硬门槛：20 个已知答案 fixture 问题，top-3 命中率 ≥ 80%（中文 FTS5
  不达标时放宽到 ≥ 60% 并说明，但不放弃 FTS5 改用向量）
"""

import hashlib
import os
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import settings
from app.rag import indexing, retrieval
from app.rag.models import RetrievedChunk, SearchResult

pytestmark = pytest.mark.asyncio


# ───────────────────────── fixture 文档库 ─────────────────────────

DOCS = {
    "api.md": (
        "# API 概述\n\n"
        "本服务提供聊天与流式 SSE 接口。\n\n"
        "## 流式响应\n\n"
        "POST /api/chat 返回 text/event-stream。\n"
    ),
    "auth.md": (
        "# 用户认证\n\n"
        "系统使用 scrypt 慢哈希存储密码，不存明文。\n"
        "会话通过签名 Cookie 维持，HttpOnly 且 SameSite=Strict。\n"
    ),
    "config.md": (
        "# 配置说明\n\n"
        "全部配置集中在 app/config.py，由 pydantic-settings 读取 .env。\n"
        "APP_ENV=production 时强制 LLM_API_KEY 非空。\n"
    ),
    "db.md": (
        "# 数据库\n\n"
        "使用 SQLite WAL 模式，单 worker 默认。\n"
        "conversation 与 message 表通过外键级联删除。\n"
    ),
    "ratelimit.md": (
        "# 限流\n\n"
        "单实例内存滑动窗口实现，按 IP 与用户标识分桶。\n"
        "多实例部署需要替换为 Redis。\n"
    ),
    "metrics.md": (
        "# 指标\n\n"
        "Prometheus 文本格式，/metrics 端点。\n"
        "包含 chat_tokens_total、context_truncated_total 等。\n"
    ),
    "errors.md": (
        "# 错误处理\n\n"
        "统一错误信封 {error: {code, message}}。\n"
        "ErrorCode 枚举是唯一错误码字典，映射 http_status。\n"
    ),
    "sse.md": (
        "# SSE 协议\n\n"
        "事件序列 start -> token* -> done。\n"
        "流开启后错误以 in-band error event 返回。\n"
    ),
    "concurrency.md": (
        "# 并发模型\n\n"
        "每个会话用 asyncio.Lock 串行化，避免乱序写。\n"
        "重复请求返回 409 ConversationBusy。\n"
    ),
    "context.md": (
        "# 上下文裁剪\n\n"
        "build_context 保留最近 40 条消息并按 token 裁剪。\n"
        "从最旧端丢弃，保留当前用户消息。\n"
    ),
    "memory.md": (
        "# 工作记忆\n\n"
        "每 20 轮生成一次摘要，存入 conversation_memory 表。\n"
        "摘要作为数据注入，不是 system 指令。\n"
    ),
    "tools.md": (
        "# 工具调用\n\n"
        "三个工具：web_search、calculate、read_file。\n"
        "沙盒限制 CPU 1 秒、内存 128MB。\n"
    ),
    "rag.md": (
        "# RAG 检索\n\n"
        "MVI 阶段使用 SQLite FTS5 全文检索，禁向量数据库。\n"
        "文档按 hash 去重，BM25 取 top-k。\n"
    ),
    "routing.md": (
        "# 多模型路由\n\n"
        "确定性规则路由，不调用模型判断模型。\n"
        "FALLBACK_MODELS 在首 token 前失败时降级一次。\n"
    ),
    "sandbox.md": (
        "# 沙盒\n\n"
        "calculate 用 AST 白名单解释器，禁止 eval exec。\n"
        "read_file 做 realpath 校验，拒绝符号链接逃逸。\n"
    ),
    "deploy.md": (
        "# 部署\n\n"
        "uvicorn 启动，生产开 Secure Cookie。\n"
        "反向代理设置 client_max_body_size。\n"
    ),
    "roadmap.md": (
        "# 路线图\n\n"
        "v0.2 认证，v0.3 路由，v0.4 记忆，v0.5 工具。\n"
        "v0.6 RAG 使用 FTS5 建立基线。\n"
    ),
    "cost.md": (
        "# 成本\n\n"
        "model_price 按模型名配置每百万 token 价格。\n"
        "/api/admin/cost 需 X-Admin-Key 头。\n"
    ),
    "logging.md": (
        "# 日志\n\n"
        "LLM 日志不含消息内容，只记模型 token 耗时错误码。\n"
        "结构化日志便于聚合查询。\n"
    ),
}


@pytest_asyncio.fixture
async def rag_docs(tmp_path, monkeypatch):
    """在 tmp_path/docs 下生成 DOCS 全部文件，返回根目录路径。"""
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    for name, body in DOCS.items():
        (docs_root / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(settings, "rag_doc_roots", [str(docs_root)])
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_top_k", 3)
    monkeypatch.setattr(settings, "rag_max_context_tokens", 2048)
    return docs_root


@pytest_asyncio.fixture
async def rag_indexed(db, rag_docs):
    """建好索引的数据库连接。"""
    stats = await indexing.reindex(db, [str(rag_docs)])
    assert stats["added"] == len(DOCS)
    return rag_docs


# ───────────────────────── 基础用例 ─────────────────────────


async def test_index_then_search_hits(db, rag_indexed):
    """建索引后检索能命中相关文档。"""
    res = await retrieval.search(db, "认证 密码 scrypt")
    assert isinstance(res, SearchResult)
    assert len(res.chunks) > 0
    # 应命中 auth.md（document_id 包含根名 hash 派生，但 text 必含 scrypt）
    joined = " ".join(c.text for c in res.chunks)
    assert "scrypt" in joined
    for c in res.chunks:
        assert c.document_id
        assert isinstance(c.score, float)


async def test_reindex_no_duplicates(db, rag_indexed):
    """二次入库同一批文件不产生重复 chunk（按 hash 跳过）。"""
    before = await indexing.chunk_count(db)
    stats = await indexing.reindex(db, [str(rag_indexed)])
    after = await indexing.chunk_count(db)
    assert stats["added"] == 0
    assert stats["skipped"] == len(DOCS)
    assert before == after


async def test_deleted_file_removed_from_index(db, rag_indexed):
    """删除源文件后重索引，对应 document 与 chunk 被清理。"""
    target = rag_indexed / "auth.md"
    target.unlink()
    stats = await indexing.reindex(db, [str(rag_indexed)])
    assert stats["removed"] == 1
    # 检索 scrypt 不再命中 auth（其它文档不含 scrypt）
    res = await retrieval.search(db, "scrypt 密码")
    joined = " ".join(c.text for c in res.chunks)
    assert "scrypt" not in joined


async def test_modified_file_updates_index(db, rag_indexed):
    """文件内容变化（hash 变化）触发更新而非新增重复。"""
    target = rag_indexed / "auth.md"
    new_body = "# 认证\n\n改用 argon2 哈希算法存储密码。\n"
    target.write_text(new_body, encoding="utf-8")
    stats = await indexing.reindex(db, [str(rag_indexed)])
    assert stats["updated"] == 1
    res = await retrieval.search(db, "argon2")
    assert any("argon2" in c.text for c in res.chunks)


async def test_top_k_limit(db, rag_indexed, monkeypatch):
    """top-k 限制返回条数。"""
    monkeypatch.setattr(settings, "rag_top_k", 1)
    res = await retrieval.search(db, "token 模型 配置")
    assert len(res.chunks) <= 1


async def test_injection_cap_truncates(db, rag_indexed, monkeypatch):
    """注入上限触发截断：把上限设到极小值，结果应被截断且 kept 少于全部。"""
    # 先用宽松上限取全集：用单字查询"的"在多文档命中（多个文档含"的"字）
    monkeypatch.setattr(settings, "rag_max_context_tokens", 1_000_000)
    monkeypatch.setattr(settings, "rag_top_k", 10)
    full = await retrieval.search(db, "模型")
    # "模型"出现在 config.md / routing.md / metrics.md 等多个文档
    assert len(full.chunks) >= 2, f"expected >=2 chunks for 模型, got {len(full.chunks)}"
    # 再收紧到只能容下一条
    monkeypatch.setattr(settings, "rag_max_context_tokens", 5)
    res = await retrieval.search(db, "模型")
    assert res.truncated is True
    assert len(res.chunks) < len(full.chunks)


async def test_rag_disabled_returns_empty(db, rag_indexed, monkeypatch):
    """RAG_ENABLED=false 时检索返回空列表，不报错。"""
    monkeypatch.setattr(settings, "rag_enabled", False)
    res = await retrieval.search(db, "认证 scrypt")
    assert res.chunks == []
    assert res.truncated is False


async def test_empty_query_returns_empty(db, rag_indexed):
    """空查询不报 FTS5 syntax error。"""
    res = await retrieval.search(db, "")
    assert res.chunks == []
    res2 = await retrieval.search(db, "!!! @@ ###")
    assert res2.chunks == []


async def test_only_whitelisted_roots_scanned(tmp_path, db, monkeypatch):
    """不存在的根目录被跳过，不抛错。"""
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_doc_roots", [str(tmp_path / "nope")])
    stats = await indexing.reindex(db, [str(tmp_path / "nope")])
    assert stats["scanned"] == 0
    assert stats["added"] == 0


# ───────────────────────── 召回硬门槛 ─────────────────────────

# 20 个已知答案的查询：每个 (query, 期望命中文本中的关键词)。
# 关键词用于校验 top-3 是否召回了正确文档。
RECALL_QUERIES = [
    ("密码如何存储", "scrypt"),
    ("会话用什么维持", "Cookie"),
    ("如何配置环境变量", ".env"),
    ("数据库是什么模式", "WAL"),
    ("限流怎么实现", "滑动窗口"),
    ("指标端点", "/metrics"),
    ("错误返回格式", "code"),
    ("流式事件序列", "start"),
    ("会话并发怎么处理", "Lock"),
    ("上下文保留多少条", "40"),
    ("摘要每隔多少轮", "20"),
    ("有哪些工具", "read_file"),
    ("RAG 用什么检索", "FTS5"),
    ("路由怎么做", "确定性规则"),
    ("calculate 怎么实现", "AST"),
    ("生产部署注意", "Secure"),
    ("v0.6 做什么", "RAG"),
    ("成本接口", "Admin-Key"),
    ("日志包含什么", "token"),
    ("错误码字典", "ErrorCode"),
]


async def test_recall_threshold(db, rag_indexed):
    """top-3 命中率 ≥ 80%（中文 FTS5 baseline）。

    评估口径：检索 top-3，检查结果文本中是否含期望关键词。FTS5 unicode61
    对中文按字符切，BM25 在中文上仍可工作；若实测低于 80% 会在此报告。
    """
    hits = 0
    misses: list[str] = []
    for query, expected in RECALL_QUERIES:
        res = await retrieval.search(db, query)
        joined = " ".join(c.text for c in res.chunks)
        if expected in joined:
            hits += 1
        else:
            misses.append(f"{query} (期望含 '{expected}')")
    rate = hits / len(RECALL_QUERIES)
    # 硬门槛 80%；若不达标打印详情便于诊断（不在 CI 上 fail，但应在报告说明）。
    if rate < 0.80:
        pytest.fail(
            f"recall {rate:.0%} < 80%; misses: {'; '.join(misses)}"
        )
    print(f"\nrecall rate: {rate:.0%} ({hits}/{len(RECALL_QUERIES)})")
