"""快速规范检索接口格式。"""

from datetime import datetime

from pydantic import BaseModel, Field

from archspec.regulation_search.models import RegulationSearchResponse
from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogVerificationStatus,
)
from archspec.standards.models import StandardLifecycleStatus, StandardSourceType


class RegulationSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    project_id: str | None = None
    specialty: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class RegulationEvidenceResponse(BaseModel):
    regulation_document_id: str
    regulation_name: str
    standard_code: str | None
    standard_title: str | None
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
    score: float
    retrieval_mode: str
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


class SearchFactorResponse(BaseModel):
    key: str
    label: str
    value: str


class MaterialFactorResponse(BaseModel):
    document_id: str
    name: str
    type_label: str


class CatalogSuggestionResponse(BaseModel):
    standard_code: str
    title: str
    authority: str
    lifecycle_status: StandardLifecycleStatus
    official_url: str | None
    full_text_available: bool
    reason: str
    verification_status: CatalogVerificationStatus
    verified_at: datetime | None
    content_permission: CatalogContentPermission


class RegulationSearchApiResponse(BaseModel):
    query_context: str
    retrieval_mode: str
    results: list[RegulationEvidenceResponse]
    project_factors: list[SearchFactorResponse]
    material_factors: list[MaterialFactorResponse]
    catalog_suggestions: list[CatalogSuggestionResponse]
    evidence_mode: str = "AUTHORITATIVE"
    admission_notice: str | None = None
    excluded_documents: list[dict[str, object]] = Field(default_factory=list)

    @classmethod
    def from_record(
        cls, record: RegulationSearchResponse
    ) -> "RegulationSearchApiResponse":
        return cls(
            query_context=record.query_context,
            retrieval_mode=record.retrieval_mode,
            results=[RegulationEvidenceResponse(**item.__dict__) for item in record.results],
            project_factors=[
                SearchFactorResponse(**item.__dict__)
                for item in (record.project_factors or [])
            ],
            material_factors=[
                MaterialFactorResponse(**item.__dict__)
                for item in (record.material_factors or [])
            ],
            catalog_suggestions=[
                CatalogSuggestionResponse(**item.__dict__)
                for item in (record.catalog_suggestions or [])
            ],
            evidence_mode=record.evidence_mode,
            admission_notice=record.admission_notice,
            excluded_documents=record.excluded_documents,
        )
