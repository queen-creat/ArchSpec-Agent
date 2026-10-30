"""文档仓储：把业务对象的读写和 SQLAlchemy 细节隔离开。"""

from datetime import UTC
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from archspec.documents.chunker import ChunkDraft
from archspec.documents.models import (
    DocumentChunkRecord,
    DocumentChunkRow,
    DocumentRecord,
    DocumentRow,
    DocumentStatus,
)


def _to_record(row: DocumentRow) -> DocumentRecord:
    # SQLite 不保存时区标记，所以取出时明确恢复为 UTC，避免部署后的时间歧义。
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return DocumentRecord(
        id=row.id,
        original_name=row.original_name,
        stored_name=row.stored_name,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        status=row.status,
        extracted_text=row.extracted_text,
        error_message=row.error_message,
        created_at=created_at,
    )


class DocumentRepository:
    """Repository（仓储）像文档档案管理员，负责保存和查找记录。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, record: DocumentRecord) -> DocumentRecord:
        self.session.add(DocumentRow(**record.__dict__))
        self.session.commit()
        return record

    def get(self, document_id: str) -> DocumentRecord | None:
        row = self.session.get(DocumentRow, document_id)
        return _to_record(row) if row else None

    def list(self) -> list[DocumentRecord]:
        """按最近上传优先返回文档，供普通用户页面选择。"""
        rows = self.session.scalars(
            select(DocumentRow).order_by(DocumentRow.created_at.desc())
        ).all()
        return [_to_record(row) for row in rows]

    def update_parse_result(
        self,
        document_id: str,
        text: str | None,
        status: DocumentStatus,
        error: str | None,
        commit: bool = True,
    ) -> DocumentRecord:
        row = self.session.get(DocumentRow, document_id)
        if row is None:
            raise LookupError(f"文档不存在：{document_id}")
        row.extracted_text = text
        row.status = status
        row.error_message = error
        if commit:
            self.session.commit()
            self.session.refresh(row)
        else:
            self.session.flush()
        return _to_record(row)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


def _to_chunk_record(row: DocumentChunkRow) -> DocumentChunkRecord:
    return DocumentChunkRecord(
        id=row.id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        text=row.text,
        page_start=row.page_start,
        page_end=row.page_end,
        clause_number=row.clause_number,
        heading_path=row.heading_path,
        character_count=row.character_count,
    )


class DocumentChunkRepository:
    """成批替换和按原文顺序读取规范小卡片。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_document(
        self,
        document_id: str,
        drafts: list[ChunkDraft],
        commit: bool = True,
    ) -> list[DocumentChunkRecord]:
        self.session.execute(
            delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
        )
        rows = [
            DocumentChunkRow(
                id=str(uuid4()),
                document_id=document_id,
                chunk_index=index,
                text=draft.text,
                page_start=draft.page_start,
                page_end=draft.page_end,
                clause_number=draft.clause_number,
                heading_path=draft.heading_path,
                character_count=len(draft.text),
            )
            for index, draft in enumerate(drafts)
        ]
        self.session.add_all(rows)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return [_to_chunk_record(row) for row in rows]

    def list_for_document(self, document_id: str) -> list[DocumentChunkRecord]:
        rows = self.session.scalars(
            select(DocumentChunkRow)
            .where(DocumentChunkRow.document_id == document_id)
            .order_by(DocumentChunkRow.chunk_index)
        ).all()
        return [_to_chunk_record(row) for row in rows]
