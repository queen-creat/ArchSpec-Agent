"""规范题录仓储。"""

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.standards.models import (
    StandardLifecycleStatus,
    StandardMetadataRecord,
    StandardMetadataRow,
    StandardSourceType,
)


def _to_record(row: StandardMetadataRow) -> StandardMetadataRecord:
    checked_at = row.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return StandardMetadataRecord(
        id=row.id,
        document_id=row.document_id,
        standard_code=row.standard_code,
        title=row.title,
        authority=row.authority,
        source_url=row.source_url,
        source_type=row.source_type,
        lifecycle_status=row.lifecycle_status,
        published_at=row.published_at,
        effective_at=row.effective_at,
        replaced_by=row.replaced_by,
        content_sha256=row.content_sha256,
        checked_at=checked_at,
    )


class StandardMetadataRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        document_id: str,
        standard_code: str,
        title: str,
        authority: str,
        source_url: str | None,
        source_type: StandardSourceType,
        lifecycle_status: StandardLifecycleStatus,
        published_at: date | None,
        effective_at: date | None,
        replaced_by: str | None,
        content_sha256: str,
    ) -> StandardMetadataRecord:
        row = self.session.scalar(
            select(StandardMetadataRow).where(
                StandardMetadataRow.document_id == document_id
            )
        )
        if row is None:
            row = StandardMetadataRow(id=str(uuid4()), document_id=document_id)
            self.session.add(row)
        row.standard_code = standard_code
        row.title = title
        row.authority = authority
        row.source_url = source_url
        row.source_type = source_type
        row.lifecycle_status = lifecycle_status
        row.published_at = published_at
        row.effective_at = effective_at
        row.replaced_by = replaced_by
        row.content_sha256 = content_sha256
        row.checked_at = datetime.now(UTC)
        self.session.commit()
        return _to_record(row)

    def get_for_document(self, document_id: str) -> StandardMetadataRecord | None:
        row = self.session.scalar(
            select(StandardMetadataRow).where(
                StandardMetadataRow.document_id == document_id
            )
        )
        return _to_record(row) if row else None

    def list_for_documents(
        self, document_ids: list[str]
    ) -> dict[str, StandardMetadataRecord]:
        if not document_ids:
            return {}
        rows = self.session.scalars(
            select(StandardMetadataRow).where(
                StandardMetadataRow.document_id.in_(document_ids)
            )
        ).all()
        return {row.document_id: _to_record(row) for row in rows}
