"""中央规范目录的数据结构。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base
from archspec.standards.models import StandardLifecycleStatus


class CatalogVerificationStatus(StrEnum):
    """题录是否已经和官方公告核对。"""

    VERIFIED = "VERIFIED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class CatalogContentPermission(StrEnum):
    """系统目前获得的正文使用权限，不等于网页是否存在附件。"""

    METADATA_ONLY = "METADATA_ONLY"
    ONLINE_READ_ONLY = "ONLINE_READ_ONLY"
    AUTHORIZED_FULL_TEXT = "AUTHORIZED_FULL_TEXT"


class CatalogSyncStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class CatalogSyncTrigger(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


@dataclass(frozen=True)
class StandardCatalogEntry:
    """一张规范目录卡片；linked_document_id 有值才代表正文已入库。"""

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
    verification_status: CatalogVerificationStatus = CatalogVerificationStatus.PENDING
    verified_at: datetime | None = None
    verification_message: str = "尚未连接具体官方公告页面"
    announcement_number: str | None = None
    source_updated_at: date | None = None
    replaced_by_code: str | None = None
    content_permission: CatalogContentPermission = CatalogContentPermission.METADATA_ONLY


@dataclass(frozen=True)
class CatalogSearchResult:
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


@dataclass(frozen=True)
class CatalogStatus:
    total: int
    with_full_text: int
    last_sync_at: datetime | None
    verified: int
    pending_or_failed: int
    mode: str = "CACHED_OFFICIAL_METADATA"


@dataclass(frozen=True)
class CatalogSyncRun:
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


class StandardCatalogRow(Base):
    __tablename__ = "standard_catalog"
    __table_args__ = (
        UniqueConstraint(
            "source_provider",
            "standard_code",
            name="uq_standard_catalog_provider_code",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    standard_code: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    authority: Mapped[str] = mapped_column(String(200))
    lifecycle_status: Mapped[StandardLifecycleStatus] = mapped_column(
        Enum(StandardLifecycleStatus), index=True
    )
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    official_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_provider: Mapped[str] = mapped_column(String(100), index=True)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    linked_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    verification_status: Mapped[CatalogVerificationStatus] = mapped_column(
        Enum(CatalogVerificationStatus),
        default=CatalogVerificationStatus.PENDING,
        index=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_message: Mapped[str] = mapped_column(
        Text, default="尚未连接具体官方公告页面"
    )
    announcement_number: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_updated_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    replaced_by_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_permission: Mapped[CatalogContentPermission] = mapped_column(
        Enum(CatalogContentPermission),
        default=CatalogContentPermission.METADATA_ONLY,
    )


class CatalogSyncRunRow(Base):
    __tablename__ = "catalog_sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[CatalogSyncStatus] = mapped_column(Enum(CatalogSyncStatus), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_count: Mapped[int] = mapped_column(Integer)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[CatalogSyncTrigger] = mapped_column(Enum(CatalogSyncTrigger))

