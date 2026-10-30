"""审查工作流接口格式。"""

from datetime import datetime

from pydantic import BaseModel, Field

from archspec.reviews.models import (
    EvidenceCandidate,
    ReviewJobRecord,
    ReviewJobStatus,
    ReviewStepRecord,
    ReviewStepStatus,
)
from archspec.reviews.service import (
    ReviewHistoryItem,
    ReviewReport,
    ReviewReportItem,
)
from archspec.standards.models import StandardLifecycleStatus, StandardSourceType

JOB_LABELS = {
    ReviewJobStatus.PREPARING: "正在准备审查",
    ReviewJobStatus.WAITING_CONFIRMATION: "等待人工确认",
    ReviewJobStatus.COMPLETED: "审查已完成",
    ReviewJobStatus.FAILED: "审查失败",
}
STEP_LABELS = {
    ReviewStepStatus.SEARCHING: "正在查找规范",
    ReviewStepStatus.WAITING_CONFIRMATION: "等待确认",
    ReviewStepStatus.CONFIRMED: "已确认",
    ReviewStepStatus.NO_EVIDENCE: "待人工核查",
    ReviewStepStatus.FAILED: "处理失败",
}


class StartReviewRequest(BaseModel):
    task_document_id: str = Field(min_length=1)


class ConfirmReviewStepRequest(BaseModel):
    regulation_document_id: str = Field(min_length=1)
    clause_numbers: list[str] = Field(min_length=1, max_length=20)


class EvidenceCandidateResponse(BaseModel):
    regulation_document_id: str
    regulation_name: str
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
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
    verification_record_ids: list[str] = Field(default_factory=list)
    evaluated_at: str | None = None
    applicability_status: str = "NOT_REVIEWED"
    manifest_sha256: str | None = None

    @classmethod
    def from_record(cls, record: EvidenceCandidate) -> "EvidenceCandidateResponse":
        return cls(**{key: value for key, value in record.__dict__.items() if key != "score"})


class ReviewJobResponse(BaseModel):
    id: str
    task_document_id: str
    status: ReviewJobStatus
    status_label: str
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_record(cls, record: ReviewJobRecord) -> "ReviewJobResponse":
        return cls(**record.__dict__, status_label=JOB_LABELS[record.status])


class ReviewHistoryItemResponse(BaseModel):
    id: str
    task_document_id: str
    task_document_name: str
    status: ReviewJobStatus
    status_label: str
    total: int
    confirmed: int
    pending: int
    can_resume: bool
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_record(cls, item: ReviewHistoryItem) -> "ReviewHistoryItemResponse":
        return cls(
            **item.job.__dict__,
            task_document_name=item.task_document_name,
            status_label=JOB_LABELS[item.job.status],
            total=item.total,
            confirmed=item.confirmed,
            pending=item.pending,
            can_resume=item.can_resume,
        )


class ReviewStepResponse(BaseModel):
    id: str
    requirement_id: str
    sequence: int
    heading_path: str | None
    requirement_text: str
    status: ReviewStepStatus
    status_label: str
    candidates: list[EvidenceCandidateResponse]
    confirmed_regulation_document_id: str | None
    confirmed_clause_numbers: list[str]
    error_message: str | None

    @classmethod
    def from_record(cls, record: ReviewStepRecord) -> "ReviewStepResponse":
        return cls(
            id=record.id,
            requirement_id=record.requirement_id,
            sequence=record.sequence,
            heading_path=record.heading_path,
            requirement_text=record.requirement_text,
            status=record.status,
            status_label=STEP_LABELS[record.status],
            candidates=[
                EvidenceCandidateResponse.from_record(item)
                for item in record.candidates
            ],
            confirmed_regulation_document_id=record.confirmed_regulation_document_id,
            confirmed_clause_numbers=record.confirmed_clause_numbers,
            error_message=record.error_message,
        )


class ReviewDetailResponse(BaseModel):
    job: ReviewJobResponse
    steps: list[ReviewStepResponse]

    @classmethod
    def from_records(
        cls, job: ReviewJobRecord, steps: list[ReviewStepRecord]
    ) -> "ReviewDetailResponse":
        return cls(
            job=ReviewJobResponse.from_record(job),
            steps=[ReviewStepResponse.from_record(step) for step in steps],
        )


class ReviewReportItemResponse(BaseModel):
    requirement_text: str
    heading_path: str | None
    status: ReviewStepStatus
    status_label: str
    conclusion: str
    confirmed_evidence: EvidenceCandidateResponse | None

    @classmethod
    def from_record(cls, item: ReviewReportItem) -> "ReviewReportItemResponse":
        return cls(
            requirement_text=item.requirement_text,
            heading_path=item.heading_path,
            status=item.status,
            status_label=STEP_LABELS[item.status],
            conclusion=item.conclusion,
            confirmed_evidence=(
                EvidenceCandidateResponse.from_record(item.confirmed_evidence)
                if item.confirmed_evidence
                else None
            ),
        )


class ReviewReportResponse(BaseModel):
    job: ReviewJobResponse
    items: list[ReviewReportItemResponse]

    @classmethod
    def from_record(cls, report: ReviewReport) -> "ReviewReportResponse":
        return cls(
            job=ReviewJobResponse.from_record(report.job),
            items=[ReviewReportItemResponse.from_record(item) for item in report.items],
        )
