"""只读项目条件候选：提取明确字段，不从规范要求推导项目实际情况。"""

import hashlib
import json
import re
from pathlib import Path

from archspec.assisted_labeling.models import DocumentRole
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.documents.models import DocumentStatus
from archspec.dossier.service import content_allowed
from archspec.official_qa.condition_fields import CONDITION_FIELDS
from archspec.official_qa.height_conditions import height_condition_known
from archspec.projects.models import ProjectDocumentType
from archspec.standard_governance.compatibility import LegacyAdmissionAdapter

USE_LABELS = {"OFFICE": "办公", "RESIDENTIAL": "住宅", "COMMERCIAL": "商业",
              "EDUCATION": "学校", "HEALTHCARE": "医疗", "CULTURAL": "文化",
              "HOTEL": "旅馆", "INDUSTRIAL": "工业建筑"}
MATERIAL_TYPES = {ProjectDocumentType.PROJECT_REQUIREMENT, ProjectDocumentType.SCHEME_DESCRIPTION,
                  ProjectDocumentType.PRELIMINARY_DESCRIPTION, ProjectDocumentType.DESIGN_DESCRIPTION}
FIELD_LABELS = {key: field.project_label or field.label for key, field in CONDITION_FIELDS.items()}
ASSIGNMENTS = tuple((key, "(?:" + "|".join(map(re.escape, field.project_assignment_labels)) + ")",
                     field.project_value_pattern)
                    for key, field in CONDITION_FIELDS.items() if field.project_value_pattern)



class ProjectConditionChanged(ValueError):
    """旧候选不再对应当前项目资料，应重新查询，不能静默改用新值。"""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _assignments(text):
    # 必须是本项目明确字段赋值。示例、下限、未知值和规范里的“应为”均不作实际条件。
    for line in re.split(r"[\n；;。]", text):
        excerpt = line.strip()
        if any(word in excerpt for word in ("示例", "例如", "暂定", "未知", "待定", "不确定",
                                           "不低于", "不少于", "不高于", "可能", "建议", "是否",
                                           "应", "须", "需", "？", "?")):
            continue
        for field, label, value_pattern in ASSIGNMENTS:
            match = re.fullmatch(rf"(?:本项目)?\s*{label}\s*(?:[:：]|为|是)\s*({value_pattern})", excerpt)
            if not match:
                continue
            value = match.group(1).strip()
            if field == "fire_context" and len({word for word in ("地上", "地下") if word in value}) != 1:
                continue
            if CONDITION_FIELDS[field].group == "height" and not height_condition_known(field, value):
                continue
            yield field, value, excerpt


class ProjectConditionProvider:
    """候选编号绑定归属、原件和解析结果；只重新读取，不解析、下载或写数据库。"""

    def __init__(self, projects, documents, chunks, upload_dir):
        self.projects, self.documents, self.chunks = projects, documents, chunks
        self.upload_dir = Path(upload_dir)
        self.labeling = AssistedLabelingRepository(documents.session)
        self.admission = LegacyAdmissionAdapter(documents.session)

    def inspect(self, project_id):
        # 同一请求可能在整理器期间等了一段时间，复核时不能沿用ORM的旧对象缓存。
        self.documents.session.expire_all()
        project = self.projects.get(project_id)
        packet = {"project_id": project_id, "region": {"province": project.province, "city": project.city},
                  "candidates": [], "conflicts": [], "gaps": []}
        rows, gaps = packet["candidates"], packet["gaps"]
        use = USE_LABELS.get(project.building_type.value)
        if use:
            rows.append(self._candidate(project_id, "building_use", use, {
                "source_kind": "PROJECT_RECORD", "source_name": project.name,
                "document_id": None, "chunk_id": None, "page_start": None, "page_end": None,
                "file_sha256": None, "text_sha256": _fingerprint([use, project.updated_at]),
                "source_text": f"建筑用途：{use}", "project_updated_at": project.updated_at.isoformat(),
            }))
        links = self.projects.list_documents(project_id)
        if len(links) > 20:
            gaps.append("本次最多检查20份关联资料，其余资料尚未检查。")
        for link in links[:20]:
            document = self.documents.get(link.document_id)
            profile = self.labeling.get_profile(link.document_id)
            if link.document_type not in MATERIAL_TYPES or (profile and profile.role == DocumentRole.REGULATION):
                gaps.append("关联资料中有未分类资料或规范，不用于推导项目条件。")
                continue
            if not document or document.status != DocumentStatus.PARSED:
                gaps.append("部分项目资料尚未成功解析，未提取条件。")
                continue
            if not content_allowed(self.admission, document.id, "LOCAL_SEARCH"):
                gaps.append("部分资料的使用条件不允许检索或摘录，未提取条件。")
                continue
            chunks = self.chunks.list_for_document(document.id)
            if (len(document.extracted_text or "") > 50000 or sum(len(row.text) for row in chunks) > 50000):
                gaps.append("部分资料超过本次50000字符检查上限，未把部分内容称为完整条件。")
                continue
            if not self._original_matches(document):
                gaps.append("部分资料原件缺失或指纹变化，请重新检查资料状态。")
                continue
            text_sha = _fingerprint([document.extracted_text, [row.__dict__ for row in chunks]])
            for chunk in chunks:
                for field, value, excerpt in _assignments(chunk.text):
                    candidate = self._candidate(project_id, field, value, {
                        "source_kind": "PROJECT_MATERIAL", "source_name": document.original_name,
                        "document_id": document.id, "chunk_id": chunk.id,
                        "page_start": chunk.page_start, "page_end": chunk.page_end,
                        "file_sha256": document.sha256, "text_sha256": text_sha,
                        "source_text": excerpt, "link_id": link.id,
                        "document_type": link.document_type.value,
                    })
                    # 同片段同字段的重复行只有一个身份；不同资料/页码的出处仍分别保留。
                    if any(row["id"] == candidate["id"] for row in rows):
                        continue
                    if len(rows) >= 60:
                        gaps.append("本次最多列出60个条件候选，其余候选尚未列出。")
                        break
                    rows.append(candidate)
        packet["gaps"] = list(dict.fromkeys(gaps))
        packet["conflicts"] = [field for field in FIELD_LABELS
                               if len({row["value"] for row in rows if row["field"] == field}) > 1]
        return packet

    def resolve(self, project_id, identifiers):
        if not project_id:
            raise ProjectConditionChanged("项目条件编号失效，请重新选择项目并查询")
        packet = self.inspect(project_id)
        ledger = {row["id"]: row for row in packet["candidates"]}
        if any(identifier not in ledger for identifier in identifiers):
            raise ProjectConditionChanged("项目资料或条件已变化，旧编号失效，请重新查询并确认")
        selected = [ledger[identifier] for identifier in dict.fromkeys(identifiers)]
        conditions = {}
        for row in selected:
            if row["field"] in conditions and conditions[row["field"]] != row["value"]:
                raise ValueError("所选项目条件冲突，请每项只选择一个明确值")
            conditions[row["field"]] = row["value"]
        return conditions, selected

    def _original_matches(self, document):
        # 既有上传文件名只能是根目录内单个文件；链接越界或文件被替换都不读作原件。
        if Path(document.stored_name).name != document.stored_name:
            return False
        try:
            root = self.upload_dir.resolve()
            path = (root / document.stored_name).resolve()
            if path.parent != root or path.stat().st_size != document.size_bytes:
                return False
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == document.sha256
        except OSError:
            return False

    @staticmethod
    def _candidate(project_id, field, value, source):
        row = {"kind": "PROJECT_CONDITION", "status": "NEEDS_CONFIRMATION", "project_id": project_id,
               "field": field, "label": FIELD_LABELS[field], "value": value, **source}
        return {"id": "pc_" + _fingerprint(row), **row}
