"""逐条要求检索与证据快照；检索命中只代表候选，仍需专业复核。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from archspec.database_schema import Base
from archspec.dossier.service import CONTENT_NOTICE, content_allowed
from archspec.regulation_search.schemas import RegulationSearchApiResponse
from archspec.standard_governance.compatibility import LegacyAdmissionAdapter


class RequirementAssessmentRow(Base):
    __tablename__ = "requirement_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("design_projects.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    snapshot: Mapped[str] = mapped_column(Text)


class RequirementAssessmentService:
    def __init__(self, session: Session, dossier, search) -> None:
        self.session = session
        self.dossier = dossier
        self.search = search
        self.admission = LegacyAdmissionAdapter(session)

    def run(self, project_id: str) -> dict:
        overview = self.dossier.build(project_id)
        requirements = overview["requirements"]
        if not requirements:
            raise ValueError("当前项目没有可检索的设计要求，请先上传可解析的任务书或设计说明")
        if len(requirements) > 100:
            raise ValueError("要求超过100条，请先拆分任务书后再执行逐条检索")
        items = []
        for index, requirement in enumerate(requirements):
            # 原文、项目条件和检索结果共同保存在快照里，之后资料变化也能复盘。
            response = self.search.search(requirement["query"][:500], project_id, None, 3)
            payload = RegulationSearchApiResponse.from_record(response).model_dump(mode="json")
            state = (
                "CANDIDATE_EVIDENCE"
                if payload["results"]
                else ("CATALOG_ONLY" if payload["catalog_suggestions"] else "NO_EVIDENCE")
            )
            items.append(
                {"index": index, "requirement": requirement, "status": state, "search": payload}
            )
        result = {
            "id": str(uuid4()),
            "project_id": project_id,
            "created_at": datetime.now(UTC).isoformat(),
            "project_snapshot": overview.get("project", {}),
            "items": items,
            "summary": {
                "total": len(items),
                "candidate_evidence": sum(item["status"] == "CANDIDATE_EVIDENCE" for item in items),
                "catalog_only": sum(item["status"] == "CATALOG_ONLY" for item in items),
                "no_evidence": sum(item["status"] == "NO_EVIDENCE" for item in items),
            },
            "notice": "逐条检索结果为候选依据，命中率不代表合规率；须核对条文适用范围、版本和图纸。",
        }
        self.session.add(
            RequirementAssessmentRow(
                id=result["id"],
                project_id=project_id,
                created_at=datetime.now(UTC),
                snapshot=json.dumps(result, ensure_ascii=False),
            )
        )
        self.session.commit()
        return self.get(project_id, result["id"])

    def latest(self, project_id: str, *, operation: str = "LOCAL_EXCERPT") -> dict | None:
        row = self.session.scalar(
            select(RequirementAssessmentRow)
            .where(RequirementAssessmentRow.project_id == project_id)
            .order_by(RequirementAssessmentRow.created_at.desc())
            .limit(1)
        )
        return self.get(project_id, row.id, operation=operation) if row else None

    def get(self, project_id: str, assessment_id: str, *, operation: str = "LOCAL_EXCERPT") -> dict:
        row = self.session.get(RequirementAssessmentRow, assessment_id)
        if row is None or row.project_id != project_id:
            raise LookupError("当前项目不存在这次对照记录")
        data = json.loads(row.snapshot)
        decisions = self.session.scalars(
            select(RequirementDecisionRow)
            .where(RequirementDecisionRow.assessment_id == assessment_id)
            .order_by(RequirementDecisionRow.item_index)
        ).all()
        data["decisions"] = [json.loads(item.record) for item in decisions]
        data["review_summary"] = {
            "total": len(data["items"]),
            "processed": len(decisions),
            "pending": len(data["items"]) - len(decisions),
            "confirmed": sum(item["action"] == "CONFIRMED" for item in data["decisions"]),
            "needs_check": sum(item["action"] == "NEEDS_CHECK" for item in data["decisions"]),
        }
        self._project_content(data, operation)
        return data

    def _project_content(self, data: dict, operation: str) -> None:
        """只投影本次响应，包含派生检索词、人工摘录和已选证据；不改旧 JSON。"""
        restricted_items = set()
        redacted = False
        for index, item in enumerate(data["items"]):
            requirement = item.get("requirement", {})
            identifier = requirement.get("document_id")
            if identifier and not content_allowed(self.admission, identifier, operation):
                requirement.update(text=CONTENT_NOTICE, query="", heading_path=None,
                                   content_redacted=True, content_notice=CONTENT_NOTICE)
                restricted_items.add(item.get("index", index))
                redacted = True
            search = item.get("search", {})
            for evidence in search.get("results", []):
                if self._project_evidence(evidence, operation):
                    restricted_items.add(item.get("index", index))
                    redacted = True
            if item.get("index", index) in restricted_items:
                # query_context 可能拼入被拒绝的原文，不能只遮 requirement.text。
                search["query_context"] = ""
                item["content_notice"] = CONTENT_NOTICE
        for decision in data.get("decisions", []):
            selected = decision.get("selected_evidence")
            if selected and self._project_evidence(selected, operation):
                restricted_items.add(decision["item_index"])
                redacted = True
            if decision["item_index"] in restricted_items:
                # 复核意见可能引用同一原文；保留动作与时间，不输出其复述。
                decision.update(comment=CONTENT_NOTICE, content_redacted=True,
                                content_notice=CONTENT_NOTICE)
        data["content_redacted"] = redacted
        data["content_notices"] = [CONTENT_NOTICE] if redacted else []

    def _project_evidence(self, evidence: dict, operation: str) -> bool:
        evidence.update(is_citable=False, evidence_mode="HISTORICAL",
                        admission_notice="历史候选关联不代表当前来源准入或项目适用性")
        identifier = evidence.get("regulation_document_id")
        if identifier and not content_allowed(self.admission, identifier, operation):
            evidence.update(text=CONTENT_NOTICE, heading_path=None, source_warning=CONTENT_NOTICE,
                            content_redacted=True)
            return True
        return False

    def history(self, project_id: str) -> list[dict]:
        rows = self.session.scalars(
            select(RequirementAssessmentRow)
            .where(RequirementAssessmentRow.project_id == project_id)
            .order_by(RequirementAssessmentRow.created_at.desc())
            .limit(20)
        ).all()
        return [
            {
                "id": row.id,
                "created_at": json.loads(row.snapshot)["created_at"],
                "total": len(json.loads(row.snapshot)["items"]),
            }
            for row in rows
        ]

    def decide(
        self,
        project_id: str,
        assessment_id: str,
        item_index: int,
        action: str,
        evidence_index: int | None,
        comment: str,
    ) -> dict:
        data = self.get(project_id, assessment_id)
        if action not in {"CONFIRMED", "NEEDS_CHECK", "NOT_APPLICABLE"}:
            raise ValueError("不支持的复核动作")
        if not 0 <= item_index < len(data["items"]):
            raise ValueError("设计要求序号不存在")
        comment = comment.strip()
        if len(comment) > 1000:
            raise ValueError("复核意见不能超过1000字")
        if action != "CONFIRMED" and not comment:
            raise ValueError("请填写复核意见，说明待核查或不适用的原因")
        evidence = None
        if action == "CONFIRMED":
            candidates = data["items"][item_index]["search"]["results"]
            if evidence_index is None or not 0 <= evidence_index < len(candidates):
                raise ValueError("确认依据必须选择本次记录中的候选正文")
            evidence = candidates[evidence_index]
            if evidence.get("content_redacted") or data["items"][item_index].get(
                    "requirement", {}).get("content_redacted"):
                raise ValueError("当前使用许可不允许显示或确认受限历史正文")
        record = {
            "item_index": item_index,
            "action": action,
            "comment": comment,
            "selected_evidence": evidence,
            "evidence_index": evidence_index if evidence else None,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        row = self.session.scalar(
            select(RequirementDecisionRow).where(
                RequirementDecisionRow.assessment_id == assessment_id,
                RequirementDecisionRow.item_index == item_index,
            )
        )
        if row is None:
            row = RequirementDecisionRow(
                id=str(uuid4()), assessment_id=assessment_id, item_index=item_index, record=""
            )
            self.session.add(row)
        row.record = json.dumps(record, ensure_ascii=False)
        self.session.commit()
        return self.get(project_id, assessment_id)


class RequirementDecisionRow(Base):
    """复核结果独立保存，保留原始检索快照。"""

    __tablename__ = "requirement_decisions"
    __table_args__ = (UniqueConstraint("assessment_id", "item_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("requirement_assessments.id"), index=True)
    item_index: Mapped[int] = mapped_column(Integer)
    record: Mapped[str] = mapped_column(Text)
