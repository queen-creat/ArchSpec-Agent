"""跨全部可用规范查找可追溯条文。"""

from archspec.assisted_labeling.models import DocumentRole
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.documents.repository import DocumentRepository
from archspec.projects.models import ProjectBuildingType, ProjectDesignStage
from archspec.projects.schemas import DOCUMENT_TYPE_LABELS
from archspec.projects.service import ProjectService
from archspec.regulation_search.models import (
    CatalogSuggestion,
    MaterialFactor,
    RegulationEvidence,
    RegulationSearchResponse,
    SearchFactor,
)
from archspec.search.service import SearchService
from archspec.standard_catalog.service import StandardCatalogService
from archspec.standard_governance.compatibility import ContentUseDenied
from archspec.standards.models import StandardLifecycleStatus
from archspec.standards.service import StandardMetadataService

BUILDING_LABELS = {
    ProjectBuildingType.OFFICE: "办公",
    ProjectBuildingType.RESIDENTIAL: "居住",
    ProjectBuildingType.COMMERCIAL: "商业",
    ProjectBuildingType.EDUCATION: "教育",
    ProjectBuildingType.HEALTHCARE: "医疗",
    ProjectBuildingType.CULTURAL: "文化",
    ProjectBuildingType.HOTEL: "旅馆",
    ProjectBuildingType.INDUSTRIAL: "工业",
    ProjectBuildingType.OTHER: "其他",
}
STAGE_LABELS = {
    ProjectDesignStage.CONCEPT: "方案设计",
    ProjectDesignStage.PRELIMINARY: "初步设计",
    ProjectDesignStage.CONSTRUCTION_DOCUMENTS: "施工图设计",
    ProjectDesignStage.OTHER: "其他",
}
SPECIALTY_LABELS = {
    "GENERAL": "总体与通用",
    "FIRE_SAFETY": "防火与疏散",
    "ACCESSIBILITY": "无障碍",
    "FUNCTION": "建筑功能与空间",
    "LOCAL": "地方要求",
    "OTHER": "其他",
}


class RegulationSearchValidationError(ValueError):
    pass


class RegulationSearchService:
    def __init__(
        self,
        documents: DocumentRepository,
        labels: AssistedLabelingRepository,
        standards: StandardMetadataService,
        projects: ProjectService,
        keyword_search: SearchService,
        catalog: StandardCatalogService | None = None,
    ) -> None:
        self.documents = documents
        self.labels = labels
        self.standards = standards
        self.projects = projects
        self.keyword_search = keyword_search
        self.catalog = catalog

    def search(
        self,
        query: str,
        project_id: str | None = None,
        specialty: str | None = None,
        top_k: int = 5,
    ) -> RegulationSearchResponse:
        if not query.strip():
            raise RegulationSearchValidationError("请输入需要查找的规范问题")
        context, public_context, project_factors, material_factors = self._query_context(
            query.strip(), project_id, specialty
        )
        regulations = [
            item
            for item in self.documents.list()
            if (profile := self.labels.get_profile(item.id)) is not None
            and profile.role == DocumentRole.REGULATION
        ]
        eligible_ids = self.standards.verified_current_document_ids(
            [item.id for item in regulations]
        )
        excluded = [
            {
                "document_id": item.id,
                "reasons": self.standards.admission.for_document(item.id)["reasons"],
            }
            for item in regulations if item.id not in eligible_ids
        ]
        names = {item.id: item.original_name for item in regulations}
        hits = self.keyword_search.search_many(eligible_ids, context, max(top_k * 4, 10))
        results: list[RegulationEvidence] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            clause = hit.chunk.clause_number
            key = (hit.chunk.document_id, clause or "")
            if clause is None or key in seen:
                continue
            seen.add(key)
            # 检索后再复核，防止核验/许可在查询过程中已经改变。
            decision = self.standards.admission.for_document(hit.chunk.document_id)
            if not decision["is_citable"]:
                continue
            source = self.standards.describe_source(hit.chunk.document_id)
            results.append(
                RegulationEvidence(
                    regulation_document_id=hit.chunk.document_id,
                    regulation_name=names[hit.chunk.document_id],
                    standard_code=source.standard_code,
                    standard_title=source.title,
                    clause_number=clause,
                    heading_path=hit.chunk.heading_path,
                    text=hit.chunk.text,
                    page_start=hit.chunk.page_start,
                    page_end=hit.chunk.page_end,
                    score=hit.score,
                    retrieval_mode="BM25_V1",
                    source_type=source.source_type,
                    lifecycle_status=source.lifecycle_status,
                    source_url=source.source_url,
                    source_warning=source.warning,
                    is_citable=True,
                    evidence_mode=decision["evidence_mode"],
                    admission_policy_version=decision["policy_version"],
                    standard_version_id=decision["standard_version_id"],
                    source_record_id=decision["source_record_id"],
                    artifact_binding_id=decision["artifact_binding_id"],
                    verification_record_ids=decision["verification_record_ids"],
                    evaluated_at=decision["evaluated_at"],
                    applicability_status=decision["applicability_status"],
                    manifest_sha256=decision.get("manifest_sha256"),
                )
            )
            if len(results) == top_k:
                break
        catalog_suggestions = self._catalog_suggestions(
            query.strip(), top_k, results
        )
        return RegulationSearchResponse(
            public_context,
            "BM25_V1",
            results,
            project_factors,
            material_factors,
            catalog_suggestions,
            admission_notice=(
                "本次没有已通过显式核验的匹配正文；核验不足不代表没有规范要求。"
                if not results else "条文通过来源准入，但项目适用性仍须专业人员核对。"
            ),
            excluded_documents=excluded,
        )

    def _catalog_suggestions(
        self,
        query: str,
        top_k: int,
        evidence: list[RegulationEvidence],
    ) -> list[CatalogSuggestion]:
        if self.catalog is None:
            return []
        used_codes = {item.standard_code for item in evidence if item.standard_code}
        suggestions: list[CatalogSuggestion] = []
        for item in self.catalog.search(query, top_k * 2):
            if item.standard_code in used_codes:
                continue
            if item.lifecycle_status == StandardLifecycleStatus.UNKNOWN:
                reason = "目录已收录但状态尚未核验"
                if not item.full_text_available:
                    reason += "，且正文尚未入库；请通过官方链接核对"
            elif item.lifecycle_status != StandardLifecycleStatus.CURRENT:
                reason = "该规范状态不是现行，仅作版本提醒，请先核对官方状态"
            elif item.full_text_available:
                reason = "目录已关联正文，但本次未找到可直接引用的匹配条文"
            else:
                reason = "可能相关，但正文尚未入库；请通过官方链接核对原文"
            suggestions.append(
                CatalogSuggestion(
                    standard_code=item.standard_code,
                    title=item.title,
                    authority=item.authority,
                    lifecycle_status=item.lifecycle_status,
                    official_url=item.official_url,
                    full_text_available=item.full_text_available,
                    reason=reason,
                    verification_status=item.verification_status,
                    verified_at=item.verified_at,
                    content_permission=item.content_permission,
                )
            )
            if len(suggestions) == top_k:
                break
        return suggestions

    def _query_context(
        self, query: str, project_id: str | None, specialty: str | None
    ) -> tuple[str, str, list[SearchFactor], list[MaterialFactor]]:
        lines = [f"用户问题：{query}"]
        internal_materials: list[str] = []
        project_factors: list[SearchFactor] = []
        material_factors: list[MaterialFactor] = []
        if project_id:
            project = self.projects.get(project_id)
            values = []
            if project.city:
                values.append(f"地点={project.city}")
                project_factors.append(SearchFactor("city", "城市", project.city))
            values.append(f"建筑类型={BUILDING_LABELS[project.building_type]}")
            project_factors.append(
                SearchFactor(
                    "building_type",
                    "建筑类型",
                    BUILDING_LABELS[project.building_type],
                )
            )
            values.append(f"设计阶段={STAGE_LABELS[project.design_stage]}")
            project_factors.append(
                SearchFactor(
                    "design_stage",
                    "设计阶段",
                    STAGE_LABELS[project.design_stage],
                )
            )
            if project.height_m is not None:
                values.append(f"建筑高度={project.height_m:g}m")
                project_factors.append(
                    SearchFactor("height_m", "建筑高度", f"{project.height_m:g}m")
                )
            if project.above_ground_floors is not None:
                values.append(f"地上{project.above_ground_floors}层")
                project_factors.append(
                    SearchFactor(
                        "above_ground_floors",
                        "地上层数",
                        f"{project.above_ground_floors}层",
                    )
                )
            lines.append(f"项目条件：{'；'.join(values)}")
            for material in self.projects.list_documents(project_id):
                material_factors.append(
                    MaterialFactor(
                        material.document_id,
                        material.document_name,
                        DOCUMENT_TYPE_LABELS[material.document_type],
                    )
                )
                document = self.documents.get(material.document_id)
                try:
                    self.standards.admission.assert_document_read(material.document_id, "LOCAL_SEARCH")
                except ContentUseDenied:
                    continue
                if document and document.extracted_text:
                    # 最多带入 800 个字符，既利用项目语境，也避免一份长任务书
                    # 淹没用户真正的问题。原文不放进公开的 query_context。
                    remaining = 800 - sum(len(item) for item in internal_materials)
                    if remaining > 0:
                        clean_text = " ".join(document.extracted_text.split())
                        internal_materials.append(clean_text[:remaining])
        if specialty:
            lines.append(f"专业方向：{SPECIALTY_LABELS.get(specialty, specialty)}")
        public_context = "\n".join(lines)
        internal_context = public_context
        if internal_materials:
            internal_context += "\n项目资料摘录：" + "\n".join(internal_materials)
        return internal_context, public_context, project_factors, material_factors
