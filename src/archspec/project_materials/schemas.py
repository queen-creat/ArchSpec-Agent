"""项目资料接口格式。"""

from datetime import datetime

from pydantic import BaseModel

from archspec.documents.models import DocumentStatus
from archspec.project_materials.models import (
    MaterialClassificationStatus,
    MaterialRecommendationReport,
    MaterialStandardRecommendation,
    MaterialTopic,
    ProjectMaterialRecord,
)
from archspec.projects.models import ProjectDocumentType
from archspec.projects.schemas import DOCUMENT_TYPE_LABELS
from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogVerificationStatus,
)
from archspec.standards.models import StandardLifecycleStatus


class ProjectMaterialResponse(BaseModel):
    id: str
    project_id: str
    document_id: str
    document_name: str
    document_status: DocumentStatus
    document_type: ProjectDocumentType
    document_type_label: str
    classification_status: MaterialClassificationStatus
    classification_confidence: float
    classification_reasons: list[str]
    can_start_review: bool

    @classmethod
    def from_record(cls, record: ProjectMaterialRecord) -> "ProjectMaterialResponse":
        return cls(
            id=record.link.id,
            project_id=record.link.project_id,
            document_id=record.link.document_id,
            document_name=record.link.document_name,
            document_status=record.link.document_status,
            document_type=record.link.document_type,
            document_type_label=DOCUMENT_TYPE_LABELS[record.link.document_type],
            classification_status=record.classification.status,
            classification_confidence=record.classification.confidence,
            classification_reasons=record.classification.reasons,
            can_start_review=record.link.document_status == DocumentStatus.PARSED,
        )


class ProjectMaterialUpdateRequest(BaseModel):
    document_type: ProjectDocumentType


class MaterialTopicResponse(BaseModel):
    key: str
    label: str
    matched_terms: list[str]

    @classmethod
    def from_record(cls, record: MaterialTopic) -> "MaterialTopicResponse":
        return cls(**record.__dict__)


class MaterialStandardRecommendationResponse(BaseModel):
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

    @classmethod
    def from_record(
        cls, record: MaterialStandardRecommendation
    ) -> "MaterialStandardRecommendationResponse":
        return cls(**record.__dict__)


class MaterialRecommendationResponse(BaseModel):
    material_id: str
    document_id: str
    topics: list[MaterialTopicResponse]
    standards: list[MaterialStandardRecommendationResponse]
    notice: str

    @classmethod
    def from_record(
        cls, record: MaterialRecommendationReport
    ) -> "MaterialRecommendationResponse":
        return cls(
            material_id=record.material_id,
            document_id=record.document_id,
            topics=[MaterialTopicResponse.from_record(item) for item in record.topics],
            standards=[
                MaterialStandardRecommendationResponse.from_record(item)
                for item in record.standards
            ],
            notice=record.notice,
        )
