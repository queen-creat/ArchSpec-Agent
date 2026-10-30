"""中央规范目录 API 的输入输出格式。"""

from datetime import date, datetime

from pydantic import BaseModel

from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogSearchResult,
    CatalogStatus,
    CatalogSyncRun,
    CatalogSyncStatus,
    CatalogSyncTrigger,
    CatalogVerificationStatus,
    StandardCatalogEntry,
)
from archspec.standards.models import StandardLifecycleStatus


class CatalogSearchResultResponse(BaseModel):
    id: str
    standard_code: str
    title: str
    authority: str
    lifecycle_status: StandardLifecycleStatus
    official_url: str | None
    source_provider: str
    full_text_available: bool
    score: float
    verification_status: CatalogVerificationStatus
    verified_at: datetime | None
    content_permission: CatalogContentPermission

    @classmethod
    def from_record(cls, record: CatalogSearchResult) -> "CatalogSearchResultResponse":
        return cls(**record.__dict__)


class CatalogStatusResponse(BaseModel):
    total: int
    with_full_text: int
    last_sync_at: datetime | None
    verified: int
    pending_or_failed: int
    mode: str

    @classmethod
    def from_record(cls, record: CatalogStatus) -> "CatalogStatusResponse":
        return cls(**record.__dict__)


class CatalogEntryResponse(BaseModel):
    id: str
    standard_code: str
    title: str
    authority: str
    lifecycle_status: StandardLifecycleStatus
    published_at: date | None
    effective_at: date | None
    official_url: str | None
    source_provider: str
    last_checked_at: datetime
    linked_document_id: str | None
    verification_status: CatalogVerificationStatus
    verified_at: datetime | None
    verification_message: str
    announcement_number: str | None
    source_updated_at: date | None
    replaced_by_code: str | None
    content_permission: CatalogContentPermission

    @classmethod
    def from_record(cls, record: StandardCatalogEntry) -> "CatalogEntryResponse":
        return cls(**record.__dict__)


class CatalogSyncRequest(BaseModel):
    provider_name: str
    limit: int = 20


class CatalogSyncRunResponse(BaseModel):
    id: str
    provider_name: str
    status: CatalogSyncStatus
    started_at: datetime
    finished_at: datetime | None
    requested_count: int
    success_count: int
    failure_count: int
    error_summary: str | None
    trigger_type: CatalogSyncTrigger

    @classmethod
    def from_record(cls, record: CatalogSyncRun) -> "CatalogSyncRunResponse":
        return cls(**record.__dict__)

