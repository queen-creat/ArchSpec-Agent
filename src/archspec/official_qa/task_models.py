"""官方QA任务只持久化恢复元数据，不保存回答或读取阶段对象。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base
from archspec.official_qa.question_plan import freeze_value


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str | None
    mode: str
    state: str
    revision: int
    request: Mapping
    proposal: Mapping | None
    question_keys: tuple[str, ...]
    trace: tuple
    current_step: str
    retry_count: int
    owner_token: str | None
    active_operation_id: str | None
    created_at: datetime
    updated_at: datetime
    contract_version: str = "QA_TASK_V1"

    def __post_init__(self):
        for name in ("request", "proposal", "question_keys", "trace"):
            object.__setattr__(self, name, freeze_value(getattr(self, name)))


@dataclass(frozen=True)
class OperationRecord:
    task_id: str
    operation_id: str
    expected_revision: int
    request_digest: str
    owner_token: str
    state: str
    result_revision: int | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class TaskClaim:
    task: TaskRecord
    operation: OperationRecord
    replay: bool


class QATaskRow(Base):
    __tablename__ = "qa_tasks"
    __table_args__ = (
        CheckConstraint("revision >= 0"),
        CheckConstraint("retry_count >= 0 AND retry_count <= 2"),
        CheckConstraint("mode IN ('LOCAL', 'MODEL')"),
        CheckConstraint("state IN ('CREATED', 'RUNNING', 'WAITING_CONDITIONS', "
                        "'WAITING_SOURCE', 'COMPLETED', 'INTERRUPTED', 'FAILED')"),
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("design_projects.id"), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(24), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    request_json: Mapped[str] = mapped_column(Text)
    proposal_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_keys_json: Mapped[str] = mapped_column(Text)
    trace_json: Mapped[str] = mapped_column(Text)
    current_step: Mapped[str] = mapped_column(String(64))
    owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    active_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    contract_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QAOperationRow(Base):
    __tablename__ = "qa_operations"
    __table_args__ = (CheckConstraint("expected_revision >= 0"),)

    # 复合主键在数据库层保护同任务、同操作的唯一收据。
    task_id: Mapped[str] = mapped_column(
        ForeignKey("qa_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    expected_revision: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    owner_token: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24))
    result_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)