"""规范片段向量的数据结构和数据库表。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


@dataclass(frozen=True)
class ChunkEmbeddingRecord:
    id: str
    chunk_id: str
    document_id: str
    model_name: str
    dimension: int
    vector: list[float]
    created_at: datetime


class ChunkEmbeddingRow(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (UniqueConstraint("chunk_id", "model_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(200), index=True)
    dimension: Mapped[int] = mapped_column(Integer)
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
