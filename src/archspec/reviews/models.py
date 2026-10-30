"""审查任务、步骤和证据快照数据结构。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base
from archspec.standards.models import StandardLifecycleStatus, StandardSourceType


class ReviewJobStatus(StrEnum):
    PREPARING = "PREPARING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewStepStatus(StrEnum):
    SEARCHING = "SEARCHING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    NO_EVIDENCE = "NO_EVIDENCE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class EvidenceCandidate:
    regulation_document_id: str
    regulation_name: str
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
    score: float
    retrieval_mode: str
    standard_code: str | None
    standard_title: str | None
    source_type: StandardSourceType
    lifecycle_status: StandardLifecycleStatus
    source_url: str | None
    source_warning: str | None
    is_citable: bool = False
    evidence_mode: str = "CANDIDATE_ONLY"
    admission_policy_version: str | None = None
    standard_version_id: str | None = None
    source_record_id: str | None = None
    artifact_binding_id: str | None = None
    verification_record_ids: list[str] = field(default_factory=list)
    evaluated_at: str | None = None
    applicability_status: str = "NOT_REVIEWED"
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class ReviewJobRecord:
    id: str
    task_document_id: str
    status: ReviewJobStatus
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ReviewStepDraft:
    requirement_id: str
    sequence: int
    heading_path: str | None
    requirement_text: str
    status: ReviewStepStatus
    candidates: list[EvidenceCandidate]
    error_message: str | None


@dataclass(frozen=True)
class ReviewStepRecord:
    id: str
    job_id: str
    requirement_id: str
    sequence: int
    heading_path: str | None
    requirement_text: str
    status: ReviewStepStatus
    candidates: list[EvidenceCandidate]
    confirmed_regulation_document_id: str | None
    confirmed_clause_numbers: list[str]
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ReviewJobRow(Base):
    __tablename__ = "review_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ReviewJobStatus] = mapped_column(Enum(ReviewJobStatus), index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReviewStepRow(Base):
    __tablename__ = "review_steps"
    __table_args__ = (UniqueConstraint("job_id", "requirement_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("review_jobs.id", ondelete="CASCADE"), index=True
    )
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("requirement_items.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requirement_text: Mapped[str] = mapped_column(Text)
    status: Mapped[ReviewStepStatus] = mapped_column(Enum(ReviewStepStatus), index=True)
    candidates_json: Mapped[str] = mapped_column(Text)
    confirmed_regulation_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_clause_numbers_json: Mapped[str] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
