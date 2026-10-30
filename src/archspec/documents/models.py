"""文档的业务数据结构和数据库表。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


class DocumentStatus(StrEnum):
    """文档处理状态：已上传、解析成功或解析失败。"""

    UPLOADED = "UPLOADED"
    PARSED = "PARSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class DocumentRecord:
    """业务层使用的不可变文档记录，防止无意中修改数据库结果。"""

    id: str
    original_name: str
    stored_name: str
    media_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    extracted_text: str | None
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class DocumentChunkRecord:
    """已经保存的规范小卡片及其可追溯来源。"""

    id: str
    document_id: str
    chunk_index: int
    text: str
    page_start: int | None
    page_end: int | None
    clause_number: str | None
    heading_path: str | None
    character_count: int


class DocumentRow(Base):
    """documents 表在 Python 中的映射。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(255), unique=True)
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[DocumentStatus] = mapped_column(Enum(DocumentStatus))
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentChunkRow(Base):
    """document_chunks 表保存可独立检索的有序文档片段。"""

    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clause_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    heading_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    character_count: Mapped[int] = mapped_column(Integer)
