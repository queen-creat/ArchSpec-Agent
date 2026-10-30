"""辅助标注页面使用的简化接口格式。"""

from datetime import datetime

from pydantic import BaseModel, Field

from archspec.assisted_labeling.models import (
    DocumentProfileRecord,
    DocumentRole,
    RequirementConfirmationRecord,
    RequirementItemRecord,
    RequirementStatus,
)
from archspec.assisted_labeling.service import (
    ClauseSuggestion,
    LabeledDocument,
    RetrievalStatus,
)

ROLE_LABELS = {
    DocumentRole.REGULATION: "建筑规范",
    DocumentRole.TASK_BRIEF: "任务书",
    DocumentRole.OTHER: "其他文档",
}
STATUS_LABELS = {
    RequirementStatus.PENDING: "待确认",
    RequirementStatus.CONFIRMED: "已确认",
    RequirementStatus.NO_EVIDENCE: "暂未找到依据",
}


class AnalyzeDocumentRequest(BaseModel):
    role: DocumentRole | None = None


class DocumentProfileResponse(BaseModel):
    document_id: str
    role: DocumentRole
    role_label: str
    confidence: float
    reasons: list[str]
    confirmed_by_user: bool

    @classmethod
    def from_record(cls, record: DocumentProfileRecord) -> "DocumentProfileResponse":
        return cls(
            document_id=record.document_id,
            role=record.role,
            role_label=ROLE_LABELS[record.role],
            confidence=record.confidence,
            reasons=record.reasons,
            confirmed_by_user=record.confirmed_by_user,
        )


class LabeledDocumentResponse(BaseModel):
    id: str
    original_name: str
    role: DocumentRole
    role_label: str

    @classmethod
    def from_record(cls, record: LabeledDocument) -> "LabeledDocumentResponse":
        return cls(
            id=record.id,
            original_name=record.original_name,
            role=record.role,
            role_label=ROLE_LABELS[record.role],
        )


class RequirementResponse(BaseModel):
    id: str
    document_id: str
    sequence: int
    heading_path: str | None
    text: str
    query: str
    status: RequirementStatus
    status_label: str

    @classmethod
    def from_record(cls, record: RequirementItemRecord) -> "RequirementResponse":
        return cls(
            **record.__dict__,
            status_label=STATUS_LABELS[record.status],
        )


class SuggestRequest(BaseModel):
    regulation_document_id: str
    top_k: int = Field(default=3, ge=1, le=10)


class ClauseSuggestionResponse(BaseModel):
    regulation_document_id: str
    regulation_name: str
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
    score: float
    retrieval_mode: str

    @classmethod
    def from_record(cls, record: ClauseSuggestion) -> "ClauseSuggestionResponse":
        return cls(**record.__dict__)


class ConfirmRequirementRequest(BaseModel):
    regulation_document_id: str
    clause_numbers: list[str]


class SuggestAllRequest(BaseModel):
    top_k: int = Field(default=3, ge=1, le=10)


class ConfirmationResponse(BaseModel):
    id: str
    requirement_id: str
    regulation_document_id: str
    clause_numbers: list[str]
    evaluation_case_id: str
    created_at: datetime
    message: str

    @classmethod
    def from_record(
        cls, record: RequirementConfirmationRecord
    ) -> "ConfirmationResponse":
        return cls(**record.__dict__, message="已确认并加入检索评测")


class MessageResponse(BaseModel):
    message: str


class RetrievalStatusResponse(BaseModel):
    mode: str
    label: str
    available: bool
    message: str

    @classmethod
    def from_record(cls, record: RetrievalStatus) -> "RetrievalStatusResponse":
        return cls(**record.__dict__)
