"""文档扫描、分块与 FTS5 入库。

设计要点（对齐 ROADMAP v0.6）：
- 只扫描 RAG_DOC_ROOTS 白名单目录（os.walk），不接任意路径上传。
- 按文件内容 sha256 增量去重：已入库 hash 跳过；源文件已删除的从 document
  表删除，级联清理 chunk 与 FTS（外键 ON DELETE CASCADE + 触发器）。
- 分块策略：优先按 markdown 标题（# 行）切节，节内超 CHUNK_MAX_CHARS
  时按定长回退切；无标题的纯文本直接定长切。
- 全部 SQL 走 aiosqlite，与项目其它写路径一致；run_write 原子提交。
"""

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from app.db import run_write, utcnow_iso

logger = logging.getLogger(__name__)

# 单 chunk 最大字符数（定长回退阈值；800 字符 ≈ 200-400 token，便于注入预算控制）。
CHUNK_MAX_CHARS = 800
# 支持扫描的文件扩展名白名单（仅文本类；二进制/代码不索引）。
SUPPORTED_EXTS = {".md", ".txt", ".rst"}


@dataclass
class _ScannedFile:
    """os.walk 产出的归一化文件记录。"""

    path: str  # 相对根的显示路径（入库用，便于人读）
    abs_path: str  # 实际读取用的绝对路径
    size: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _doc_id(root: str, rel: str) -> str:
    """document.id 由根目录 + 相对路径 + sha256 前缀拼成，保证跨根唯一。

    实际入库时再补 hash 前缀以确保唯一；此处先用 root|rel 作稳定键。
    """
    safe = f"{root}::{rel}"
    return hashlib.sha256(safe.encode("utf-8")).hexdigest()[:32]


def _split_into_chunks(text: str) -> list[str]:
    """按 markdown 标题切节，节内超长按定长回退。

    标题行（以 '#' 开头）作为新节的起始边界保留在节首；无标题的纯文本按
    CHUNK_MAX_CHARS 定长切。空 chunk 丢弃。
    """
    if not text:
        return []
    # 按行扫描，遇 # 开头行开新节。
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            sections.append([line])
        else:
            sections[-1].append(line)
    chunks: list[str] = []
    for section in sections:
        section_text = "\n".join(section).strip()
        if not section_text:
            continue
        if len(section_text) <= CHUNK_MAX_CHARS:
            chunks.append(section_text)
        else:
            # 定长回退：按 CHUNK_MAX_CHARS 切，尽量在句末/空行断开。
            buf = section_text
            while len(buf) > CHUNK_MAX_CHARS:
                cut = _soft_cut(buf, CHUNK_MAX_CHARS)
                chunks.append(buf[:cut].strip())
                buf = buf[cut:]
            if buf.strip():
                chunks.append(buf.strip())
    return [c for c in chunks if c]


def _soft_cut(text: str, max_chars: int) -> int:
    """在 max_chars 内尽量找最近的换行/句号，找不到就硬切。"""
    window = text[:max_chars]
    for sep in ("\n\n", "\n", "。", ". ", " "):
        pos = window.rfind(sep)
        if pos > max_chars // 2:  # 至少切掉一半，否则无意义
            return pos + len(sep)
    return max_chars


# CJK 统一表意文字 + 兼容汉字 + 扩展区常见范围。判断"是否需要预分词"用。
_CJK_RE = re.compile(
    r"[㐀-䶿一-鿿豈-﫿＀-￯]"
)


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _cjk_pre_tokenize(text: str) -> str:
    """在 CJK 字符两侧插入空格，使 FTS5 unicode61 把每个汉字当独立 token。

    unicode61 默认把连续 CJK 当一个不可分割 token，中文检索召回极差。预处理
    后每个汉字成为单字 token，BM25 可基于单字匹配召回（基线方案，不是向量）。
    ASCII/标点不受影响。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        if _CJK_RE.match(ch):
            out.append(" " + ch + " ")
        else:
            out.append(ch)
    return re.sub(r" +", " ", "".join(out)).strip()


def _indexable_text(chunk_text: str) -> str:
    """入库到 FTS5 的文本：CJK 预分词后的版本。"""
    return _cjk_pre_tokenize(chunk_text) if _has_cjk(chunk_text) else chunk_text


def _walk_root(root: str) -> list[_ScannedFile]:
    """枚举白名单根目录下所有支持扩展名的文件。

    root 可以是绝对路径或相对工作目录的路径；统一解析为绝对。对不存在或非
    目录的根跳过（不抛错，便于配置漂移时降级）。
    """
    abs_root = os.path.abspath(root)
    if not os.path.isdir(abs_root):
        logger.warning("RAG doc root not a directory, skipping: %s", abs_root)
        return []
    out: list[_ScannedFile] = []
    for dirpath, _dirs, files in os.walk(abs_root):
        for name in files:
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTS:
                continue
            abs_path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            rel = os.path.relpath(abs_path, abs_root)
            # 显示路径带根名前缀，便于多根场景区分来源。
            display = f"{os.path.basename(abs_root.rstrip('/'))}/{rel}"
            out.append(_ScannedFile(path=display, abs_path=abs_path, size=size))
    return out


async def _existing_documents(conn: aiosqlite.Connection) -> dict[str, dict]:
    """返回 {doc_id: {path, hash}} 表示当前已入库的文档。"""
    cur = await conn.execute("SELECT id, path, hash FROM document")
    rows = await cur.fetchall()
    return {row["id"]: {"path": row["path"], "hash": row["hash"]} for row in rows}


async def _insert_document(
    conn: aiosqlite.Connection, doc_id: str, path: str, file_hash: str, chunks: list[str]
) -> None:
    """原子插入 document + 全部 chunk + 对应 FTS5 行。

    chunk 表存原文（用于检索结果展示）；chunks_fts 存 CJK 预分词后的文本
    用于 BM25 匹配。两者通过 chunk_id 关联。
    """
    now = utcnow_iso()
    statements: list[tuple[str, tuple]] = [
        (
            "INSERT INTO document (id, path, hash, added_at) VALUES (?, ?, ?, ?)",
            (doc_id, path, file_hash, now),
        )
    ]
    for chunk_text in chunks:
        statements.append(
            (
                "INSERT INTO chunk (document_id, text) VALUES (?, ?)",
                (doc_id, chunk_text),
            )
        )
        # 取回刚插入的 chunk.id，再写 FTS5。
        statements.append(
            (
                "INSERT INTO chunks_fts (text, document_id, chunk_id) "
                "VALUES (?, ?, (SELECT seq FROM sqlite_sequence WHERE name='chunk'))",
                (_indexable_text(chunk_text), doc_id),
            )
        )
    await run_write(conn, statements)


async def _delete_document(conn: aiosqlite.Connection, doc_id: str) -> None:
    """删除 document 行；外键级联删 chunk，触发器清理 FTS。"""
    await run_write(
        conn,
        [("DELETE FROM document WHERE id = ?", (doc_id,))],
    )


async def reindex(
    conn: aiosqlite.Connection, roots: list[str] | None = None
) -> dict:
    """扫描白名单目录，增量同步索引。

    - 已入库且 hash 不变：跳过。
    - 已入库但 hash 变化：先删旧 document（级联删 chunk/FTS）再重插。
    - 源文件已删除：删除对应 document。
    - 新文件：插入。

    返回 {scanned, added, updated, removed, skipped} 统计。
    """
    from app.config import settings

    if roots is None:
        roots = settings.rag_doc_roots

    scanned = added = updated = removed = skipped = 0
    seen_doc_ids: set[str] = set()

    existing = await _existing_documents(conn)

    for root in roots:
        for f in _walk_root(root):
            scanned += 1
            try:
                with open(f.abs_path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                logger.warning("cannot read %s: %s", f.abs_path, exc)
                continue
            file_hash = _sha256_bytes(data)
            doc_id = _doc_id(root, f.path)
            seen_doc_ids.add(doc_id)

            prev = existing.get(doc_id)
            if prev and prev["hash"] == file_hash:
                skipped += 1
                continue

            try:
                text = data.decode("utf-8", errors="replace")
            except Exception as exc:  # 极 unlikely
                logger.warning("decode failed %s: %s", f.abs_path, exc)
                continue
            chunks = _split_into_chunks(text)
            if not chunks:
                skipped += 1
                continue

            if prev:
                await _delete_document(conn, doc_id)
                await _insert_document(conn, doc_id, f.path, file_hash, chunks)
                updated += 1
            else:
                await _insert_document(conn, doc_id, f.path, file_hash, chunks)
                added += 1

    # 删除源文件已不存在的 document（不在 seen_doc_ids 里的旧记录）。
    for doc_id, prev in existing.items():
        if doc_id not in seen_doc_ids:
            await _delete_document(conn, doc_id)
            removed += 1

    return {
        "scanned": scanned,
        "added": added,
        "updated": updated,
        "removed": removed,
        "skipped": skipped,
    }


async def document_count(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("SELECT COUNT(*) AS c FROM document")
    row = await cur.fetchone()
    return int(row["c"]) if row else 0


async def chunk_count(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("SELECT COUNT(*) AS c FROM chunk")
    row = await cur.fetchone()
    return int(row["c"]) if row else 0


def utcnow() -> str:
    """复用 db.utcnow_iso 的稳定时区格式（测试可导入）。"""
    return utcnow_iso()
