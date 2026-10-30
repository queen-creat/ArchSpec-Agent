"""检索评测接口的输入和输出格式。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from archspec.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationCaseStatus,
    EvaluationResultRecord,
    EvaluationRunRecord,
    EvaluationRunStatus,
    RetrievalAlgorithm,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateEvaluationCaseRequest(BaseModel):
    document_id: ShortText
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    expected_clause_numbers: list[str] = Field(default_factory=list, max_length=20)
    source_note: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    status: EvaluationCaseStatus = EvaluationCaseStatus.DRAFT

    @field_validator("expected_clause_numbers")
    @classmethod
    def normalize_clause_numbers(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("正确条款号不能为空")
        return list(dict.fromkeys(cleaned))


class EvaluationCaseResponse(BaseModel):
    id: str
    document_id: str
    query: str
    expected_clause_numbers: list[str]
    source_note: str
    status: EvaluationCaseStatus
    created_at: datetime

    @classmethod
    def from_record(cls, record: EvaluationCaseRecord) -> "EvaluationCaseResponse":
        return cls(**record.__dict__)


class CreateEvaluationRunRequest(BaseModel):
    document_id: ShortText
    algorithm: RetrievalAlgorithm = RetrievalAlgorithm.BM25_V1


class EvaluationRunResponse(BaseModel):
    id: str
    document_id: str
    algorithm: str
    status: EvaluationRunStatus
    case_count: int
    recall_at_1: float | None
    recall_at_3: float | None
    recall_at_5: float | None
    mrr: float | None
    average_latency_ms: float | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_record(cls, record: EvaluationRunRecord) -> "EvaluationRunResponse":
        return cls(**record.__dict__)


class EvaluationResultResponse(BaseModel):
    id: str
    run_id: str
    case_id: str
    query: str
    expected_clause_numbers: list[str]
    first_relevant_rank: int | None
    retrieved_clause_numbers: list[str | None]
    retrieved_scores: list[float]
    latency_ms: float

    @classmethod
    def from_record(cls, record: EvaluationResultRecord) -> "EvaluationResultResponse":
        return cls(**record.__dict__)


class EvaluationRunDetailResponse(BaseModel):
    run: EvaluationRunResponse
    results: list[EvaluationResultResponse]

    @classmethod
    def from_records(
        cls,
        run: EvaluationRunRecord,
        results: list[EvaluationResultRecord],
    ) -> "EvaluationRunDetailResponse":
        return cls(
            run=EvaluationRunResponse.from_record(run),
            results=[EvaluationResultResponse.from_record(result) for result in results],
        )
