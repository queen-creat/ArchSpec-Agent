"""快速规范检索结果。"""

from dataclasses import dataclass, field
from datetime import datetime

from archspec.standard_catalog.models import (
    CatalogContentPermission,
    CatalogVerificationStatus,
)
from archspec.standards.models import StandardLifecycleStatus, StandardSourceType


@dataclass(frozen=True)
class SearchFactor:
    key: str
    label: str
    value: str


@dataclass(frozen=True)
class MaterialFactor:
    document_id: str
    name: str
    type_label: str


@dataclass(frozen=True)
class RegulationEvidence:
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
    verification_record_ids: list[str] = field(default_factory=list)
    evaluated_at: str | None = None
    applicability_status: str = "NOT_REVIEWED"
    manifest_sha256: str | None = None


@dataclass(frozen=True)
class CatalogSuggestion:
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


@dataclass(frozen=True)
class RegulationSearchResponse:
    query_context: str
    retrieval_mode: str
    results: list[RegulationEvidence]
    project_factors: list[SearchFactor] | None = None
    material_factors: list[MaterialFactor] | None = None
    catalog_suggestions: list[CatalogSuggestion] | None = None
    evidence_mode: str = "AUTHORITATIVE"
    admission_notice: str | None = None
    excluded_documents: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.project_factors is None:
            object.__setattr__(self, "project_factors", [])
        if self.material_factors is None:
            object.__setattr__(self, "material_factors", [])
        if self.catalog_suggestions is None:
            object.__setattr__(self, "catalog_suggestions", [])
