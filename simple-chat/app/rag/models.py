"""RAG 检索结果的数据模型。

这些模型是纯数据载体：检索到的文本片段和来源标识，不含任何权限语义。
注入上下文时由调用方标注为"数据段"并施加长度上限，不把它们变成 system 指令。
"""

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    """单个检索命中文本片段。"""

    text: str = Field(..., description="chunk 原文")
    document_id: str = Field(..., description="所属 document.id（来源可追踪）")
    score: float = Field(..., description="BM25 相关性得分，越大越相关")


class SearchResult(BaseModel):
    """一次检索的聚合结果。"""

    chunks: list[RetrievedChunk] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="是否因超过 RAG_MAX_CONTEXT_TOKENS 注入上限被按 score 截断",
    )
    total_tokens: int = Field(default=0, description="截断后实际注入的 token 估算")
