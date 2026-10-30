"""把模型给出的临时证据编号绑定到后端真实检索结果。"""

from archspec.agent_runs.schemas import (
    AgentFinalDraft,
    FindingStatus,
    GroundedAgentResult,
    GroundedCitation,
    GroundedFinding,
    OverallAgentStatus,
)
from archspec.agent_runs.tools import AgentEvidence


class CitationGuard:
    """模型不能自己填写条款；所有正式引用都由此处从证据表回填。"""

    def ground(
        self,
        draft: AgentFinalDraft,
        evidence_by_id: dict[str, AgentEvidence],
    ) -> GroundedAgentResult:
        findings: list[GroundedFinding] = []
        for item in draft.findings:
            citations = [
                self._citation(evidence_by_id[evidence_id])
                for evidence_id in item.evidence_ids
                if evidence_id in evidence_by_id
                and evidence_by_id[evidence_id].is_citable
            ]
            needs_evidence = item.status in {
                FindingStatus.SUPPORTED,
                FindingStatus.RISK,
            }
            invalid = [
                evidence_id
                for evidence_id in item.evidence_ids
                if evidence_id not in evidence_by_id
                or not evidence_by_id[evidence_id].is_citable
            ]
            status = item.status
            guard_message = None
            if needs_evidence and (not citations or invalid):
                status = FindingStatus.INSUFFICIENT_EVIDENCE
                rejected = "、".join(invalid) if invalid else "未提供证据编号"
                guard_message = f"引用核验未通过：{rejected}"
                citations = []
            findings.append(
                GroundedFinding(
                    title=item.title,
                    status=status,
                    statement=item.statement,
                    citations=citations,
                    suggested_action=item.suggested_action,
                    guard_message=guard_message,
                )
            )

        overall = draft.overall_status
        if all(
            item.status == FindingStatus.INSUFFICIENT_EVIDENCE for item in findings
        ):
            overall = OverallAgentStatus.INSUFFICIENT_EVIDENCE
        elif any(item.status == FindingStatus.NEEDS_DRAWING for item in findings):
            overall = OverallAgentStatus.NEEDS_DRAWING
        return GroundedAgentResult(
            summary=draft.summary,
            overall_status=overall,
            findings=findings,
            limitations=draft.limitations,
        )

    @staticmethod
    def _citation(evidence: AgentEvidence) -> GroundedCitation:
        return GroundedCitation(
            evidence_id=evidence.evidence_id,
            regulation_document_id=evidence.regulation_document_id,
            regulation_name=evidence.regulation_name,
            standard_code=evidence.standard_code,
            standard_title=evidence.standard_title,
            clause_number=evidence.clause_number,
            heading_path=evidence.heading_path,
            text=evidence.text,
            page_start=evidence.page_start,
            page_end=evidence.page_end,
            retrieval_mode=evidence.retrieval_mode,
            lifecycle_status=evidence.lifecycle_status,
            source_url=evidence.source_url,
            source_warning=evidence.source_warning,
            is_citable=evidence.is_citable,
            evidence_mode=evidence.evidence_mode,
            admission_policy_version=evidence.admission_policy_version,
            standard_version_id=evidence.standard_version_id,
            source_record_id=evidence.source_record_id,
            artifact_binding_id=evidence.artifact_binding_id,
            verification_record_ids=evidence.verification_record_ids,
            evaluated_at=evidence.evaluated_at,
            applicability_status=evidence.applicability_status,
            manifest_sha256=evidence.manifest_sha256,
        )
