"""RAG 子系统：SQLite FTS5 全文检索（MVI 阶段，禁向量数据库）。

设计要点（对齐 ROADMAP v0.6）：
- indexing：只扫描管理员配置的白名单目录（RAG_DOC_ROOTS），按文件内容 sha256
  增量去重；删除的源文件级联清理 chunk/FTS。
- retrieval：BM25 top-k 检索，结果作为受限长度的数据段注入上下文，标注来源
  document_id；不做安全约束（不靠提示词）。
- 中文分词：FTS5 默认 unicode61 按字符切分，作为基线可接受；后续评估项才考虑
  embedding，本轮绝不引入向量库。
"""
