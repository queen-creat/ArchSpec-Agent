"""关键词检索接口的输入和输出格式。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from archspec.search.bm25 import SearchHit

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SearchRequest(BaseModel):
    """教师发起一次检索时需要填写的内容。"""

    document_id: NonBlankText
    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def reject_punctuation_only_query(cls, value: str) -> str:
        """空格由字段约束处理；纯标点也没有任何可检索意义。"""
        if not any(character.isalnum() for character in value):
            raise ValueError("检索问题必须包含文字或数字")
        return value


class SearchHitResponse(BaseModel):
    """一条命中的原文证据，以及它在原规范中的位置。"""

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    clause_number: str | None
    heading_path: str | None
    score: float

    @classmethod
    def from_hit(cls, hit: SearchHit) -> "SearchHitResponse":
        chunk = hit.chunk
        return cls(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            clause_number=chunk.clause_number,
            heading_path=chunk.heading_path,
            score=hit.score,
        )
