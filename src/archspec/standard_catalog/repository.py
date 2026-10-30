"""中央规范目录的数据库访问。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogSyncRun,
    CatalogSyncRunRow,
    CatalogSyncStatus,
    CatalogSyncTrigger,
    CatalogVerificationStatus,
    StandardCatalogEntry,
    StandardCatalogRow,
)
from archspec.standards.models import StandardMetadataRow


def _normalize_code(value: str) -> str:
    """合并多余空格，避免同一规范因为排版不同被重复保存。"""

    return " ".join(value.strip().upper().split())


def _to_entry(row: StandardCatalogRow) -> StandardCatalogEntry:
    checked_at = row.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    verified_at = row.verified_at
    if verified_at is not None and verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    return StandardCatalogEntry(
        id=row.id,
        standard_code=row.standard_code,
        title=row.title,
        authority=row.authority,
        lifecycle_status=row.lifecycle_status,
        published_at=row.published_at,
        effective_at=row.effective_at,
        official_url=row.official_url,
        source_provider=row.source_provider,
        last_checked_at=checked_at,
        linked_document_id=row.linked_document_id,
        verification_status=row.verification_status,
        verified_at=verified_at,
        verification_message=row.verification_message,
        announcement_number=row.announcement_number,
        source_updated_at=row.source_updated_at,
        replaced_by_code=row.replaced_by_code,
        content_permission=row.content_permission,
    )


class StandardCatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, entry: StandardCatalogEntry) -> StandardCatalogEntry:
        code = _normalize_code(entry.standard_code)
        provider = entry.source_provider.strip()
        row = self.session.scalar(
            select(StandardCatalogRow).where(
                StandardCatalogRow.source_provider == provider,
                StandardCatalogRow.standard_code == code,
            )
        )
        is_new = row is None
        source_changed = row is not None and (
            row.official_url != entry.official_url
            or row.linked_document_id != entry.linked_document_id
        )
        if is_new:
            row = StandardCatalogRow(id=entry.id or str(uuid4()))
            self.session.add(row)
        row.standard_code = code
        row.title = entry.title.strip()
        row.authority = entry.authority.strip()
        row.lifecycle_status = entry.lifecycle_status
        row.published_at = entry.published_at
        row.effective_at = entry.effective_at
        row.official_url = entry.official_url
        row.source_provider = provider
        row.last_checked_at = entry.last_checked_at
        row.linked_document_id = entry.linked_document_id
        if is_new:
            row.verification_status = entry.verification_status
            row.verified_at = entry.verified_at
            row.verification_message = entry.verification_message
            row.announcement_number = entry.announcement_number
            row.source_updated_at = entry.source_updated_at
            row.replaced_by_code = entry.replaced_by_code
            row.content_permission = entry.content_permission
        elif source_changed:
            # 旧核对和许可只属于旧页面/正文，不能随着一个可编辑 ID 漂移。
            row.verification_status = CatalogVerificationStatus.PENDING
            row.verified_at = None
            row.verification_message = "来源网址或正文关联已变化，须重新核对"
            row.announcement_number = None
            row.source_updated_at = None
            row.replaced_by_code = None
            row.content_permission = CatalogContentPermission.METADATA_ONLY
        self.session.commit()
        return _to_entry(row)

    def get(self, entry_id: str) -> StandardCatalogEntry | None:
        row = self.session.get(StandardCatalogRow, entry_id)
        return _to_entry(row) if row else None

    def update_verification(
        self,
        entry_id: str,
        *,
        verification_status: CatalogVerificationStatus,
        verified_at: datetime | None,
        verification_message: str,
        announcement_number: str | None,
        source_updated_at,
        replaced_by_code: str | None,
        content_permission: CatalogContentPermission,
    ) -> StandardCatalogEntry:
        row = self.session.get(StandardCatalogRow, entry_id)
        if row is None:
            raise LookupError(f"规范目录项不存在：{entry_id}")
        row.verification_status = verification_status
        row.verified_at = verified_at
        row.verification_message = verification_message
        row.announcement_number = announcement_number
        row.source_updated_at = source_updated_at
        row.replaced_by_code = replaced_by_code
        row.content_permission = content_permission
        self.session.commit()
        self.session.refresh(row)
        return _to_entry(row)

    def list(self) -> list[StandardCatalogEntry]:
        rows = self.session.scalars(
            select(StandardCatalogRow).order_by(StandardCatalogRow.standard_code)
        ).all()
        return [_to_entry(row) for row in rows]

    def backfill_registered_standards(self) -> int:
        """把旧版本中已经登记的规范补入中央目录，避免用户重复维护。"""

        rows = self.session.scalars(select(StandardMetadataRow)).all()
        for row in rows:
            checked_at = row.checked_at
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=UTC)
            self.upsert(
                StandardCatalogEntry(
                    id="",
                    standard_code=row.standard_code,
                    title=row.title,
                    authority=row.authority,
                    lifecycle_status=row.lifecycle_status,
                    published_at=row.published_at,
                    effective_at=row.effective_at,
                    official_url=row.source_url,
                    source_provider="STANDARD_METADATA",
                    last_checked_at=checked_at,
                    linked_document_id=row.document_id,
                )
            )
        return len(rows)


def _to_sync_run(row: CatalogSyncRunRow) -> CatalogSyncRun:
    started_at = row.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    finished_at = row.finished_at
    if finished_at is not None and finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return CatalogSyncRun(
        id=row.id,
        provider_name=row.provider_name,
        status=row.status,
        started_at=started_at,
        finished_at=finished_at,
        requested_count=row.requested_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        error_summary=row.error_summary,
        trigger_type=row.trigger_type,
    )


class CatalogSyncRepository:
    """保存每次官方题录刷新过程，便于解释成功和失败。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        provider_name: str,
        requested_count: int,
        trigger_type: CatalogSyncTrigger,
    ) -> CatalogSyncRun:
        row = CatalogSyncRunRow(
            id=str(uuid4()),
            provider_name=provider_name.strip().upper(),
            status=CatalogSyncStatus.RUNNING,
            started_at=datetime.now(UTC),
            finished_at=None,
            requested_count=requested_count,
            success_count=0,
            failure_count=0,
            error_summary=None,
            trigger_type=trigger_type,
        )
        self.session.add(row)
        self.session.commit()
        return _to_sync_run(row)

    def finish(
        self,
        run_id: str,
        *,
        success_count: int,
        failure_count: int,
        errors: list[str],
    ) -> CatalogSyncRun:
        row = self.session.get(CatalogSyncRunRow, run_id)
        if row is None:
            raise LookupError(f"同步记录不存在：{run_id}")
        if success_count and failure_count:
            row.status = CatalogSyncStatus.PARTIAL
        elif failure_count:
            row.status = CatalogSyncStatus.FAILED
        else:
            row.status = CatalogSyncStatus.COMPLETED
        row.finished_at = datetime.now(UTC)
        row.success_count = success_count
        row.failure_count = failure_count
        row.error_summary = json.dumps(errors, ensure_ascii=False) if errors else None
        self.session.commit()
        self.session.refresh(row)
        return _to_sync_run(row)

    def list(self, limit: int = 20) -> list[CatalogSyncRun]:
        rows = self.session.scalars(
            select(CatalogSyncRunRow)
            .order_by(CatalogSyncRunRow.started_at.desc())
            .limit(limit)
        ).all()
        return [_to_sync_run(row) for row in rows]

    def has_running(self, provider_name: str) -> bool:
        row = self.session.scalar(
            select(CatalogSyncRunRow).where(
                CatalogSyncRunRow.provider_name == provider_name.strip().upper(),
                CatalogSyncRunRow.status == CatalogSyncStatus.RUNNING,
            )
        )
        return row is not None

