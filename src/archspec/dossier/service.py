"""从关联资料整理项目总览，不覆盖使用者已经确认的参数。"""

import re
from copy import deepcopy
from dataclasses import replace

from archspec.assisted_labeling.extractor import RequirementExtractor
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.project_materials.recommendations import MaterialTopicDetector
from archspec.projects.schemas import ProjectResponse
from archspec.projects.service import ProjectService
from archspec.standard_governance.compatibility import ContentUseDenied, LegacyAdmissionAdapter

CONTENT_NOTICE = "正文及派生要求已隐藏：当前使用条件未允许本次操作，仅保留题录和状态"


def content_allowed(admission: LegacyAdmissionAdapter, document_id: str, operation: str) -> bool:
    """资料角色不是使用许可；本次操作沿全部既有来源检查，不改历史记录。"""
    try:
        admission.assert_document_read(document_id, "LOCAL_EXCERPT")
        admission.assert_document_read(document_id, operation)
    except ContentUseDenied:
        return False
    if operation == "EXPORT":
        bindings = admission.repository.bindings_for_legacy("DOCUMENT", document_id)
        artifacts = [item["artifact_binding_id"] for item in bindings
                     if item.get("artifact_binding_id")]
        # 普通未登记任务书维持兼容；已治理资料导出不能把 UNKNOWN 当作允许。
        if bindings and (not artifacts or any(
                admission.admission.permission(item, "EXPORT") != "ALLOW" for item in artifacts)):
            return False
    return True

# 必须有字段名称和单位才提取，避免把任意数字当成建筑参数。
CONDITION_RULES = (
    ("height_m", "建筑高度", "米", r"建筑高度\s*(?:为|约|[:：])?\s*(\d+(?:\.\d+)?)\s*(?:米|m)"),
    ("above_ground_floors", "地上层数", "层", r"地上(?:层数)?\s*(?:为|[:：])?\s*(\d+)\s*层"),
    (
        "gross_floor_area_m2",
        "建筑面积",
        "平方米",
        r"(?:总建筑面积|建筑面积)\s*(?:为|约|[:：])?\s*(\d+(?:\.\d+)?)\s*(?:平方米|㎡|m²|m2)",
    ),
)


class ProjectDossierService:
    def __init__(
        self,
        projects: ProjectService,
        documents: DocumentRepository,
        chunks: DocumentChunkRepository,
    ) -> None:
        self.projects = projects
        self.documents = documents
        self.chunks = chunks
        self.admission = LegacyAdmissionAdapter(documents.session)

    def build(self, project_id: str, *, operation: str = "LOCAL_EXCERPT") -> dict:
        project = self.projects.get(project_id)
        conditions, requirements, materials = [], [], []
        notices = []
        topics = {}
        for link in self.projects.list_documents(project_id):
            document = self.documents.get(link.document_id)
            if document is None:
                continue
            materials.append(
                {"id": document.id, "name": document.original_name, "status": document.status.value}
            )
            # 提取需 LOCAL_SEARCH，输出需摘录/导出许可；不能先提取再只添加警告。
            if not (content_allowed(self.admission, document.id, "LOCAL_SEARCH")
                    and content_allowed(self.admission, document.id, operation)):
                materials[-1].update(content_restricted=True, notice=CONTENT_NOTICE)
                notices.append({"document_id": document.id, "operation": operation,
                                "notice": CONTENT_NOTICE})
                continue
            text = document.extracted_text or ""
            for field, label, unit, pattern in CONDITION_RULES:
                for match in re.finditer(pattern, text):
                    value = float(match.group(1))
                    if value <= 0:
                        continue
                    conditions.append(
                        {
                            "field": field,
                            "label": label,
                            "value": value,
                            "unit": unit,
                            "source_name": document.original_name,
                            "document_id": document.id,
                            "source_text": match.group(0),
                            "status": "NEEDS_CONFIRMATION",
                        }
                    )
            for draft in RequirementExtractor().extract(self.chunks.list_for_document(document.id)):
                requirements.append(
                    {
                        "text": draft.text,
                        "query": draft.query,
                        "source_name": document.original_name,
                        "document_id": document.id,
                        "heading_path": draft.heading_path,
                    }
                )
            for topic in MaterialTopicDetector().detect(text):
                entry = topics.setdefault(
                    topic.key,
                    {
                        "key": topic.key,
                        "label": topic.label,
                        "query": topic.search_query,
                        "reason": "资料出现相关关键词，建议核对适用条件和正式原文。",
                        "source_names": [],
                        "status": "PENDING_REVIEW",
                    },
                )
                if document.original_name not in entry["source_names"]:
                    entry["source_names"].append(document.original_name)
        missing = [
            label for field, label, _, _ in CONDITION_RULES if getattr(project, field) is None
        ]
        return {
            "project": ProjectResponse.from_record(project).model_dump(mode="json"),
            "conditions": conditions,
            "requirements": requirements,
            "topics": list(topics.values()),
            "materials": materials,
            "missing_fields": missing,
            "content_notices": notices,
        }

    def report_agent_detail(self, detail):
        """报告是导出入口；AI 复述也可能含受限正文，不能仅隐藏引用的 text。"""
        if detail is None or not detail.run.final_result:
            return detail
        result = deepcopy(detail.run.final_result)
        findings = result.get("findings", [])
        citations = [citation for finding in findings for citation in finding.get("citations", [])]
        manifests = result.get("outbound_document_manifests", {})
        document_ids = set(manifests) if isinstance(manifests, dict) else set()
        document_ids.update(item["regulation_document_id"] for item in citations
                            if isinstance(item.get("regulation_document_id"), str))
        restricted = {identifier for identifier in document_ids
                      if not content_allowed(self.admission, identifier, "EXPORT")}
        for citation in citations:
            citation.update(is_citable=False, evidence_mode="HISTORICAL")
            if citation.get("regulation_document_id") in restricted:
                citation.update(text=CONTENT_NOTICE, heading_path=None, source_warning=CONTENT_NOTICE)
        if restricted:
            result.update(summary=CONTENT_NOTICE, limitations=[CONTENT_NOTICE], content_redacted=True)
            for finding in findings:
                finding.update(title="历史分析（受限内容已隐藏）", statement=CONTENT_NOTICE,
                               suggested_action="先核对资料使用条件", guard_message=CONTENT_NOTICE)
        # 创建新的响应对象，原来的运行日志和 JSON 不覆盖、不删除。
        run = replace(detail.run, final_result=result,
                      error_message=CONTENT_NOTICE if restricted and detail.run.error_message
                      else detail.run.error_message)
        return replace(detail, run=run)
