"""FTS5 BM25 检索 + top-k + 注入上限截断。

设计要点（对齐 ROADMAP v0.6）：
- BM25 MATCH 查询，top-k（RAG_TOP_K，默认 3）。中文用 unicode61 默认按字符
  切分作为基线；后续评估项才考虑 embedding，本轮禁向量库。
- 返回 RetrievedChunk{text, document_id, score}；score 越大越相关。
- 注入上限：总 token 超 RAG_MAX_CONTEXT_TOKENS 时按 score 降序截断，标记
  truncated=True。token 估算复用 app.llm.tokenizer.count_tokens。
- 检索内容是数据：标注来源 document_id，作为受限长度的数据段；不做安全约束。
"""

import logging
import re

import aiosqlite

from app.llm.tokenizer import count_tokens
from app.rag.indexing import _indexable_text, _has_cjk
from app.rag.models import RetrievedChunk, SearchResult

logger = logging.getLogger(__name__)

# FTS5 查询语法对特殊字符敏感，做最小清洗：只保留字母/数字/中文/下划线/空格，
# 多个空白合并；空串直接返回空结果，避免 FTS5 syntax error。
_TOKEN_RE = re.compile(r"[\w一-鿿]+")


def _sanitize_query(raw: str) -> str:
    """将用户输入清洗为安全的 FTS5 MATCH 表达式。

    策略：拆成词项，每个词项用双引号包裹（FTS5 phrase），词项间空格（AND 语义）。
    中文词项原样保留（unicode61 按字符切）。空查询返回 ""，调用方据此短路。
    """
    if not raw:
        return ""
    tokens = _TOKEN_RE.findall(raw)
    if not tokens:
        return ""
    # 双引号包裹避免 FTS5 把运算符（AND/OR/NOT/^ 等）当语法解析。对含 CJK 的
    # 查询做预分词（每个汉字两侧加空格）使与索引侧 token 化一致；预分词后按
    # 空格拆成单字 token，再用 OR 连接：自然语言多字查询要求全部命中会严重
    # 漏召回，OR 语义下任一 token 命中即召回，BM25 把多 token 命中的排前面。
    char_tokens: list[str] = []
    for t in tokens:
        if _has_cjk(t):
            char_tokens.extend(_indexable_text(t).split())
        else:
            char_tokens.append(t)
    quoted = ['"' + ct.replace('"', " ") + '"' for ct in char_tokens]
    return " OR ".join(quoted)


async def search(
    conn: aiosqlite.Connection,
    query: str,
    *,
    top_k: int | None = None,
    max_context_tokens: int | None = None,
) -> SearchResult:
    """BM25 top-k 检索。

    Args:
        conn: 已开启外键/WAL 的 aiosqlite 连接。
        query: 用户原始查询文本。
        top_k: 覆盖 RAG_TOP_K；None 时读 settings。
        max_context_tokens: 覆盖 RAG_MAX_CONTEXT_TOKENS；None 时读 settings。

    RAG 关闭时直接返回空 SearchResult，不报错（调用方无需 try/except）。
    """
    from app.config import settings

    if not settings.effective_rag_enabled:
        return SearchResult()

    if top_k is None:
        top_k = settings.rag_top_k
    if max_context_tokens is None:
        max_context_tokens = settings.rag_max_context_tokens

    match_expr = _sanitize_query(query)
    if not match_expr:
        return SearchResult()

    # bm25() 在 FTS5 中返回负值（越小越相关），取负转成熟悉的"越大越相关"。
    # 用 RANK() 排序等价；这里显式取 -bm25 便于直接当 score 用。
    sql = (
        "SELECT chunks_fts.chunk_id AS chunk_id, chunks_fts.document_id AS document_id, "
        "chunk.text AS text, bm25(chunks_fts) AS bm5 "
        "FROM chunks_fts JOIN chunk ON chunk.id = chunks_fts.chunk_id "
        "WHERE chunks_fts MATCH ? "
        "ORDER BY bm5 ASC LIMIT ?"
    )
    cur = await conn.execute(sql, (match_expr, top_k))
    rows = await cur.fetchall()

    chunks: list[RetrievedChunk] = []
    for row in rows:
        chunks.append(
            RetrievedChunk(
                text=row["text"],
                document_id=row["document_id"],
                score=-float(row["bm5"]),
            )
        )
    # bm5 升序 = 相关性降序；保持已按 score 降序排列。
    chunks.sort(key=lambda c: c.score, reverse=True)

    # 注入上限：按 score 降序累加，超预算即截断。
    total_tokens = 0
    kept: list[RetrievedChunk] = []
    truncated = False
    for c in chunks:
        t = count_tokens(c.text) + 4  # 含分隔/角色开销近似
        if total_tokens + t > max_context_tokens and kept:
            truncated = True
            break
        kept.append(c)
        total_tokens += t

    return SearchResult(chunks=kept, truncated=truncated, total_tokens=total_tokens)
