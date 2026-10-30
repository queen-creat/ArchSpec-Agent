"""检索评测题、运行和逐题快照的数据结构。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


class EvaluationCaseStatus(StrEnum):
    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"


class EvaluationRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RetrievalAlgorithm(StrEnum):
    """评测时可选择的检索算法版本。"""

    BM25_V1 = "BM25_V1"
    SEMANTIC_V1 = "SEMANTIC_V1"
    HYBRID_RRF_V1 = "HYBRID_RRF_V1"


@dataclass(frozen=True)
class EvaluationCaseRecord:
    id: str
    document_id: str
    query: str
    expected_clause_numbers: list[str]
    source_note: str
    status: EvaluationCaseStatus
    created_at: datetime


@dataclass(frozen=True)
class EvaluationRunRecord:
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


@dataclass(frozen=True)
class EvaluationResultRecord:
    id: str
    run_id: str
    case_id: str
    query: str
    expected_clause_numbers: list[str]
    first_relevant_rank: int | None
    retrieved_clause_numbers: list[str | None]
    retrieved_scores: list[float]
    latency_ms: float


@dataclass(frozen=True)
class ResultDraft:
    case_id: str
    query: str
    expected_clause_numbers: list[str]
    first_relevant_rank: int | None
    retrieved_clause_numbers: list[str | None]
    retrieved_scores: list[float]
    latency_ms: float


class EvaluationCaseRow(Base):
    __tablename__ = "evaluation_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(200))
    expected_clause_numbers_json: Mapped[str] = mapped_column(Text)
    source_note: Mapped[str] = mapped_column(String(500))
    status: Mapped[EvaluationCaseStatus] = mapped_column(Enum(EvaluationCaseStatus), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationRunRow(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    algorithm: Mapped[str] = mapped_column(String(50))
    status: Mapped[EvaluationRunStatus] = mapped_column(Enum(EvaluationRunStatus))
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    recall_at_1: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall_at_3: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall_at_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    mrr: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationResultRow(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (UniqueConstraint("run_id", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String(200))
    expected_clause_numbers_json: Mapped[str] = mapped_column(Text)
    first_relevant_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_clause_numbers_json: Mapped[str] = mapped_column(Text)
    retrieved_scores_json: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float] = mapped_column(Float)
