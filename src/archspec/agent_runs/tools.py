"""Agent 可以调用的只读工具；未登记的能力一律拒绝。"""

from dataclasses import dataclass, field
from typing import Protocol

from archspec.documents.repository import DocumentRepository
from archspec.projects.service import ProjectService
from archspec.regulation_search.models import RegulationEvidence
from archspec.regulation_search.service import RegulationSearchService
from archspec.standard_governance.compatibility import ContentUseDenied, LegacyAdmissionAdapter


class AgentToolRejected(ValueError):
    pass


@dataclass(frozen=True)
class AgentEvidence:
    evidence_id: str
    regulation_document_id: str
    regulation_name: str
    standard_code: str | None
    standard_title: str | None
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
    retrieval_mode: str
    lifecycle_status: str
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


@dataclass
class AgentToolContext:
    project_id: str
    include_materials: bool
    max_material_characters: int
    evidence_by_id: dict[str, AgentEvidence] = field(default_factory=dict)
    outbound_documents: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentToolResult:
    name: str
    content: dict[str, object]
    evidence: list[AgentEvidence]


class _Projects(Protocol):
    def get(self, project_id: str): ...

    def list_documents(self, project_id: str): ...


class _Documents(Protocol):
    def get(self, document_id: str): ...


class _RegulationSearch(Protocol):
    def search(
        self,
        query: str,
        project_id: str | None = None,
        specialty: str | None = None,
        top_k: int = 5,
    ): ...


class AgentToolRegistry:
    """把模型可见的 JSON Schema 映射到后端真实只读服务。"""

    def __init__(
        self,
        projects: ProjectService | _Projects,
        documents: DocumentRepository | _Documents,
        regulation_search: RegulationSearchService | _RegulationSearch,
        *,
        admission: LegacyAdmissionAdapter | None = None,
    ) -> None:
        self.projects = projects
        self.documents = documents
        self.regulation_search = regulation_search
        session = getattr(documents, "session", None)
        self.admission = admission or (LegacyAdmissionAdapter(session) if session is not None else None)

    def _model_read_allowed(self, document_id: str) -> bool:
        if self.admission is None or not self.admission.model_document_allowed(document_id):
            return False
        try:
            self.admission.assert_document_read(document_id, "LOCAL_EXCERPT")
        except ContentUseDenied:
            return False
        return True

    def _remember_outbound(self, document_id: str, context: AgentToolContext) -> None:
        fingerprint = self.admission.document_fingerprint(document_id) if self.admission else None
        if fingerprint is None:
            raise AgentToolRejected("无法绑定本次出站资料的固定指纹")
        previous = context.outbound_documents.get(document_id)
        if previous is not None and previous != fingerprint:
            raise AgentToolRejected("本轮资料已经变化，不能混用不同正文")
        context.outbound_documents[document_id] = fingerprint

    def validate_outbound(self, context: AgentToolContext) -> None:
        """每轮重新发送完整对话前，重查已进入对话的正文，不靠角色或文本猜测。"""
        for document_id, fingerprint in context.outbound_documents.items():
            if not self._model_read_allowed(document_id):
                raise AgentToolRejected("资料 MODEL_SEND 使用许可已撤销、到期或尚未明确")
            if self.admission.document_fingerprint(document_id) != fingerprint:
                raise AgentToolRejected("资料原件、登记或正文指纹已变化，停止重新发送")
        for evidence in context.evidence_by_id.values():
            if evidence.is_citable and not self.admission.for_document(evidence.regulation_document_id)["is_citable"]:
                raise AgentToolRejected("正式证据准入已变化，请重新运行并核验")

    def schemas(self, include_materials: bool) -> list[dict[str, object]]:
        schemas = [self._project_schema(), self._search_schema()]
        if include_materials:
            schemas.insert(1, self._materials_schema())
        return schemas

    def execute(
        self,
        name: str,
        arguments: dict[str, object],
        context: AgentToolContext,
    ) -> AgentToolResult:
        if name == "get_project_profile":
            return self._project_profile(arguments, context)
        if name == "read_project_materials":
            if not context.include_materials:
                raise AgentToolRejected("用户没有授权发送项目资料摘录")
            return self._project_materials(arguments, context)
        if name == "search_regulations":
            return self._search_regulations(arguments, context)
        raise AgentToolRejected(f"不允许调用工具：{name}")

    def _project_profile(
        self, arguments: dict[str, object], context: AgentToolContext
    ) -> AgentToolResult:
        self._reject_extra(arguments, set())
        project = self.projects.get(context.project_id)
        return AgentToolResult(
            "get_project_profile",
            {
                "name": project.name,
                "province": project.province,
                "city": project.city,
                "building_type": project.building_type.value,
                "design_stage": project.design_stage.value,
                "height_m": project.height_m,
                "above_ground_floors": project.above_ground_floors,
                "underground_floors": project.underground_floors,
                "gross_floor_area_m2": project.gross_floor_area_m2,
            },
            [],
        )

    def _project_materials(
        self, arguments: dict[str, object], context: AgentToolContext
    ) -> AgentToolResult:
        self._reject_extra(arguments, {"keyword"})
        keyword = str(arguments.get("keyword", "")).strip()
        if len(keyword) > 100:
            raise AgentToolRejected("资料关键词不能超过 100 个字符")
        remaining = context.max_material_characters
        items: list[dict[str, object]] = []
        for link in self.projects.list_documents(context.project_id):
            # 已登记规范改为任务书/项目材料，也不能绕过规范自身的发送许可。
            if not self._model_read_allowed(link.document_id):
                continue
            self._remember_outbound(link.document_id, context)
            document = self.documents.get(link.document_id)
            if document is None or not document.extracted_text:
                continue
            text = " ".join(document.extracted_text.split())
            if keyword and keyword not in text:
                continue
            excerpt = text[:remaining]
            items.append(
                {
                    "document_id": link.document_id,
                    "name": link.document_name,
                    "type": link.document_type.value,
                    "excerpt": excerpt,
                }
            )
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        return AgentToolResult("read_project_materials", {"materials": items}, [])

    def _search_regulations(
        self, arguments: dict[str, object], context: AgentToolContext
    ) -> AgentToolResult:
        self._reject_extra(arguments, {"query", "specialty", "top_k"})
        query = str(arguments.get("query", "")).strip()
        if not query or len(query) > 500:
            raise AgentToolRejected("规范检索问题长度必须为 1—500 个字符")
        specialty_value = arguments.get("specialty")
        specialty = str(specialty_value) if specialty_value else None
        allowed_specialties = {
            "GENERAL",
            "FIRE_SAFETY",
            "ACCESSIBILITY",
            "FUNCTION",
            "LOCAL",
            "OTHER",
        }
        if specialty is not None and specialty not in allowed_specialties:
            raise AgentToolRejected("专业方向不在允许范围内")
        top_k = int(arguments.get("top_k", 5))
        if not 1 <= top_k <= 5:
            raise AgentToolRejected("每次最多返回 5 条规范证据")
        response = self.regulation_search.search(
            query, context.project_id, specialty, top_k
        )
        evidence = []
        blocked = []
        for item in response.results:
            if not self._model_read_allowed(item.regulation_document_id):
                blocked.append({"standard_code": item.standard_code, "reason": "正文未取得明确模型发送许可，不发送原文", "is_citable": False})
                continue
            self._remember_outbound(item.regulation_document_id, context)
            evidence.append(self._evidence(item, context))
        catalog = [
            {
                "catalog_id": f"catalog_{index:02d}",
                "standard_code": item.standard_code,
                "title": item.title,
                "status": item.lifecycle_status.value,
                "official_url": item.official_url,
                "reason": item.reason,
                "is_citable": False,
            }
            for index, item in enumerate(response.catalog_suggestions or [], start=1)
        ]
        content = {
            "query_context": response.query_context,
            "retrieval_mode": response.retrieval_mode,
            "evidence": [self._evidence_payload(item) for item in evidence],
            "catalog_suggestions": catalog,
            "blocked_sources": blocked,
        }
        return AgentToolResult("search_regulations", content, evidence)

    @staticmethod
    def _evidence_payload(item: AgentEvidence) -> dict[str, object]:
        return {
            "evidence_id": item.evidence_id,
            "standard_code": item.standard_code,
            "standard_title": item.standard_title,
            "clause_number": item.clause_number,
            "heading_path": item.heading_path,
            "text": item.text,
            "page_start": item.page_start,
            "page_end": item.page_end,
            "lifecycle_status": item.lifecycle_status,
            "source_warning": item.source_warning,
            "is_citable": item.is_citable,
            "evidence_mode": item.evidence_mode,
            "standard_version_id": item.standard_version_id,
            "source_record_id": item.source_record_id,
            "artifact_binding_id": item.artifact_binding_id,
            "verification_record_ids": item.verification_record_ids,
            "applicability_status": item.applicability_status,
        }

    @staticmethod
    def _reject_extra(arguments: dict[str, object], allowed: set[str]) -> None:
        extra = set(arguments) - allowed
        if extra:
            raise AgentToolRejected(f"工具参数不允许：{', '.join(sorted(extra))}")

    @staticmethod
    def _project_schema() -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": "get_project_profile",
                "description": "读取当前设计项目的已知基本条件，缺失字段不会推断。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _materials_schema() -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": "read_project_materials",
                "description": "读取用户明确同意分享的项目资料有限摘录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "可选关键词"}
                    },
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _search_schema() -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": "search_regulations",
                "description": "结合当前项目条件检索可追溯规范正文和目录建议。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "规范问题"},
                        "specialty": {
                            "type": "string",
                            "enum": [
                                "GENERAL",
                                "FIRE_SAFETY",
                                "ACCESSIBILITY",
                                "FUNCTION",
                                "LOCAL",
                                "OTHER",
                            ],
                        },
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _next_evidence_id(context: AgentToolContext) -> str:
        return f"ev_{len(context.evidence_by_id) + 1:02d}"

    def _evidence(
        self, item: RegulationEvidence, context: AgentToolContext
    ) -> AgentEvidence:
        # 不信任检索 DTO 里的 is_citable 字段；独立评估当前固定正文。
        decision = self.admission.for_document(item.regulation_document_id)
        matches_binding = bool(
            item.artifact_binding_id is not None
            and item.artifact_binding_id == decision["artifact_binding_id"]
            and item.manifest_sha256 == decision.get("manifest_sha256")
        )
        citable = decision["is_citable"] and matches_binding
        evidence = AgentEvidence(
            evidence_id=self._next_evidence_id(context),
            regulation_document_id=item.regulation_document_id,
            regulation_name=item.regulation_name,
            standard_code=item.standard_code,
            standard_title=item.standard_title,
            clause_number=item.clause_number,
            heading_path=item.heading_path,
            text=item.text,
            page_start=item.page_start,
            page_end=item.page_end,
            retrieval_mode=item.retrieval_mode,
            lifecycle_status=item.lifecycle_status.value,
            source_url=item.source_url,
            source_warning=item.source_warning,
            is_citable=citable,
            evidence_mode="AUTHORITATIVE" if citable else "CANDIDATE_ONLY",
            admission_policy_version=decision["policy_version"],
            standard_version_id=decision["standard_version_id"],
            source_record_id=decision["source_record_id"],
            artifact_binding_id=decision["artifact_binding_id"],
            verification_record_ids=decision["verification_record_ids"],
            evaluated_at=decision["evaluated_at"],
            applicability_status=decision["applicability_status"],
            manifest_sha256=decision.get("manifest_sha256"),
        )
        context.evidence_by_id[evidence.evidence_id] = evidence
        return evidence
