"""接口响应格式；只公开教师需要看到的信息。"""

from datetime import datetime

from pydantic import BaseModel

from archspec.documents.models import (
    DocumentChunkRecord,
    DocumentRecord,
    DocumentStatus,
)


class DocumentResponse(BaseModel):
    id: str
    original_name: str
    media_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    extracted_text: str | None
    error_message: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: DocumentRecord) -> "DocumentResponse":
        """主动挑选公开字段，绝不把服务器保存文件名或路径传给网页。"""
        return cls(
            id=record.id,
            original_name=record.original_name,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            status=record.status,
            extracted_text=record.extracted_text,
            error_message=record.error_message,
            created_at=record.created_at,
        )


class DocumentChunkResponse(BaseModel):
    """网页可查看的规范小卡片和来源信息。"""

    id: str
    document_id: str
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    clause_number: str | None
    heading_path: str | None
    character_count: int

    @classmethod
    def from_record(cls, record: DocumentChunkRecord) -> "DocumentChunkResponse":
        return cls(**record.__dict__)
