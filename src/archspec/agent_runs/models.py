"""Agent 运行、步骤和人工决定的数据结构。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


class AgentRunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentStepKind(StrEnum):
    MODEL = "MODEL"
    TOOL = "TOOL"
    GUARD = "GUARD"
    SYSTEM = "SYSTEM"


class AgentStepStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FindingDecision(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class AgentRunRecord:
    id: str
    project_id: str
    goal: str
    status: AgentRunStatus
    provider: str
    requested_model: str
    actual_model: str | None
    include_materials: bool
    max_steps: int
    current_step: int
    retry_of_run_id: str | None
    input_snapshot: dict[str, object]
    final_result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class AgentStepRecord:
    id: str
    run_id: str
    sequence: int
    kind: AgentStepKind
    name: str
    status: AgentStepStatus
    input_summary: dict[str, object]
    output_summary: dict[str, object]
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    error_code: str | None
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class AgentFindingDecisionRecord:
    id: str
    run_id: str
    finding_index: int
    decision: FindingDecision
    comment: str | None
    decided_at: datetime


@dataclass(frozen=True)
class AgentRunDetail:
    run: AgentRunRecord
    steps: list[AgentStepRecord]
    decisions: list[AgentFindingDecisionRecord]


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("design_projects.id", ondelete="CASCADE"), index=True
    )
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[AgentRunStatus] = mapped_column(Enum(AgentRunStatus), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    requested_model: Mapped[str] = mapped_column(String(100))
    actual_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    include_materials: Mapped[bool]
    max_steps: Mapped[int] = mapped_column(Integer)
    current_step: Mapped[int] = mapped_column(Integer)
    retry_of_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    input_snapshot_json: Mapped[str] = mapped_column(Text)
    final_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AgentStepRow(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[AgentStepKind] = mapped_column(Enum(AgentStepKind))
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[AgentStepStatus] = mapped_column(Enum(AgentStepStatus))
    input_summary_json: Mapped[str] = mapped_column(Text)
    output_summary_json: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentFindingDecisionRow(Base):
    __tablename__ = "agent_finding_decisions"
    __table_args__ = (UniqueConstraint("run_id", "finding_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    finding_index: Mapped[int] = mapped_column(Integer)
    decision: Mapped[FindingDecision] = mapped_column(Enum(FindingDecision))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
