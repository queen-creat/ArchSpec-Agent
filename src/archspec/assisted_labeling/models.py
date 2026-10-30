"""辅助标注文档画像的数据结构。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base


class DocumentRole(StrEnum):
    REGULATION = "REGULATION"
    TASK_BRIEF = "TASK_BRIEF"
    OTHER = "OTHER"


class RequirementStatus(StrEnum):
    """设计要求的确认状态。"""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class DocumentProfileRecord:
    id: str
    document_id: str
    role: DocumentRole
    confidence: float
    reasons: list[str]
    confirmed_by_user: bool
    analyzed_at: datetime


class DocumentProfileRow(Base):
    __tablename__ = "document_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    role: Mapped[DocumentRole] = mapped_column(Enum(DocumentRole))
    confidence: Mapped[float] = mapped_column(Float)
    reasons_json: Mapped[str] = mapped_column(Text)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class RequirementItemRecord:
    """从任务书原文中提取的一条设计要求。"""

    id: str
    document_id: str
    sequence: int
    heading_path: str | None
    text: str
    query: str
    status: RequirementStatus
    created_at: datetime


class RequirementItemRow(Base):
    """任务书要求表；原文和检索词分开保存，便于追溯。"""

    __tablename__ = "requirement_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[RequirementStatus] = mapped_column(Enum(RequirementStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class RequirementConfirmationRecord:
    """教师确认的任务书要求与规范条文对应关系。"""

    id: str
    requirement_id: str
    regulation_document_id: str
    clause_numbers: list[str]
    evaluation_case_id: str
    created_at: datetime


class RequirementConfirmationRow(Base):
    __tablename__ = "requirement_confirmations"
    __table_args__ = (UniqueConstraint("requirement_id", "regulation_document_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("requirement_items.id", ondelete="CASCADE"), index=True
    )
    regulation_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    clause_numbers_json: Mapped[str] = mapped_column(Text)
    evaluation_case_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_cases.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
