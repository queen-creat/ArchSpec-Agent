"""保存和读取规范片段的本地向量索引。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from archspec.embeddings.models import ChunkEmbeddingRecord, ChunkEmbeddingRow
from archspec.embeddings.vector_codec import decode_vector, encode_vector


def _to_record(row: ChunkEmbeddingRow) -> ChunkEmbeddingRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ChunkEmbeddingRecord(
        id=row.id,
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        model_name=row.model_name,
        dimension=row.dimension,
        vector=decode_vector(row.vector_blob, row.dimension),
        created_at=created_at,
    )


class EmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_document(
        self,
        document_id: str,
        model_name: str,
        chunk_vectors: list[tuple[str, list[float]]],
    ) -> list[ChunkEmbeddingRecord]:
        """模型或片段变化时，整份文档的旧向量一次性替换。"""
        encoded = [
            (chunk_id, vector, encode_vector(vector))
            for chunk_id, vector in chunk_vectors
        ]
        self.session.execute(
            delete(ChunkEmbeddingRow).where(
                ChunkEmbeddingRow.document_id == document_id
            )
        )
        now = datetime.now(UTC)
        rows = [
            ChunkEmbeddingRow(
                id=str(uuid4()),
                chunk_id=chunk_id,
                document_id=document_id,
                model_name=model_name,
                dimension=len(vector),
                vector_blob=blob,
                created_at=now,
            )
            for chunk_id, vector, blob in encoded
        ]
        self.session.add_all(rows)
        self.session.commit()
        return [_to_record(row) for row in rows]

    def list_for_document(
        self, document_id: str, model_name: str
    ) -> list[ChunkEmbeddingRecord]:
        rows = self.session.scalars(
            select(ChunkEmbeddingRow)
            .where(
                ChunkEmbeddingRow.document_id == document_id,
                ChunkEmbeddingRow.model_name == model_name,
            )
            .order_by(ChunkEmbeddingRow.chunk_id)
        ).all()
        return [_to_record(row) for row in rows]
