"""建筑规范题录、来源信任和生命周期数据结构。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


class StandardSourceType(StrEnum):
    OFFICIAL = "OFFICIAL"
    LOCAL_UNVERIFIED = "LOCAL_UNVERIFIED"


class StandardLifecycleStatus(StrEnum):
    CURRENT = "CURRENT"
    UPCOMING = "UPCOMING"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StandardMetadataRecord:
    id: str
    document_id: str
    standard_code: str
    title: str
    authority: str
    source_url: str | None
    source_type: StandardSourceType
    lifecycle_status: StandardLifecycleStatus
    published_at: date | None
    effective_at: date | None
    replaced_by: str | None
    content_sha256: str
    checked_at: datetime


class StandardMetadataRow(Base):
    __tablename__ = "standard_metadata"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    standard_code: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    authority: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[StandardSourceType] = mapped_column(Enum(StandardSourceType))
    lifecycle_status: Mapped[StandardLifecycleStatus] = mapped_column(
        Enum(StandardLifecycleStatus), index=True
    )
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
