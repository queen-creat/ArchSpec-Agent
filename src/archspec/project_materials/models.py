"""项目资料分类结果和持久化结构。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base
from archspec.projects.models import ProjectDocumentRecord, ProjectDocumentType
from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogVerificationStatus,
)
from archspec.standards.models import StandardLifecycleStatus


class MaterialClassificationStatus(StrEnum):
    SUGGESTED = "SUGGESTED"
    CONFIRMED = "CONFIRMED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class MaterialClassification:
    document_type: ProjectDocumentType
    confidence: float
    status: MaterialClassificationStatus
    reasons: list[str]


@dataclass(frozen=True)
class ProjectMaterialRecord:
    link: ProjectDocumentRecord
    classification: MaterialClassification

    @property
    def id(self) -> str:
        return self.link.id

    @property
    def project_id(self) -> str:
        return self.link.project_id

    @property
    def document_id(self) -> str:
        return self.link.document_id

    @property
    def document_name(self) -> str:
        return self.link.document_name


@dataclass(frozen=True)
class MaterialTopic:
    """从任务书或设计说明中识别出的一个专业主题。"""

    key: str
    label: str
    matched_terms: list[str]


@dataclass(frozen=True)
class MaterialStandardRecommendation:
    """建议核对的规范目录卡，不代表已经得出合规结论。"""

    standard_code: str
    title: str
    lifecycle_status: StandardLifecycleStatus
    official_url: str | None
    full_text_available: bool
    matched_topics: list[str]
    reason: str
    verification_status: CatalogVerificationStatus
    verified_at: datetime | None
    content_permission: CatalogContentPermission


@dataclass(frozen=True)
class MaterialRecommendationReport:
    material_id: str
    document_id: str
    topics: list[MaterialTopic]
    standards: list[MaterialStandardRecommendation]
    notice: str


class ProjectMaterialMetadataRow(Base):
    __tablename__ = "project_material_metadata"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    link_id: Mapped[str] = mapped_column(
        ForeignKey("project_documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[MaterialClassificationStatus] = mapped_column(
        Enum(MaterialClassificationStatus)
    )
    confidence: Mapped[float] = mapped_column(Float)
    reasons_json: Mapped[str] = mapped_column(Text)
