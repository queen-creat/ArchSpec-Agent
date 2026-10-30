"""Agent 的严格结构化输入、草稿和落地结果。"""

from copy import deepcopy
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from archspec.agent_runs.models import (
    AgentFindingDecisionRecord,
    AgentRunDetail,
    AgentRunRecord,
    AgentRunStatus,
    AgentStepKind,
    AgentStepRecord,
    AgentStepStatus,
    FindingDecision,
)
from archspec.standard_governance.compatibility import ContentUseDenied


def _result_view(result, admission, historical):
    """只投影历史响应；原 JSON 保留，当前 DENY 不能通过旧引用和日志绕过。"""
    if result is None:
        return None, False
    view = deepcopy(result)
    findings = view.get("findings", [])
    findings = findings if isinstance(findings, list) else []
    citations = [citation for finding in findings if isinstance(finding, dict)
                 for citation in finding.get("citations", []) if isinstance(citation, dict)]
    manifests = view.get("outbound_document_manifests", {})
    document_ids = set(manifests) if isinstance(manifests, dict) else set()
    document_ids.update(item["regulation_document_id"] for item in citations if isinstance(item.get("regulation_document_id"), str))
    denied = set()
    for document_id in document_ids:
        if admission is not None:
            try:
                admission.assert_document_read(document_id, "LOCAL_EXCERPT")
            except ContentUseDenied:
                denied.add(document_id)
    for citation in citations:
        document_id = citation.get("regulation_document_id")
        if historical:
            citation["is_citable"] = False
            citation["evidence_mode"] = "HISTORICAL"
            citation["admission_notice"] = "历史运行记录，不代表当前来源准入或项目适用性"
        elif admission is not None and isinstance(document_id, str):
            decision = admission.for_document(document_id)
            current = bool(decision["is_citable"] and citation.get("artifact_binding_id") == decision["artifact_binding_id"] and citation.get("manifest_sha256") == decision.get("manifest_sha256"))
            citation["is_citable"] = current
            citation["evidence_mode"] = "AUTHORITATIVE" if current else "CANDIDATE_ONLY"
        if document_id in denied:
            citation["text"] = "正文已隐藏：当前使用条件不允许展示"
            citation["is_citable"] = False
            citation["source_warning"] = "正文使用条件明确拒绝当前摘录操作"
    if not historical:
        # 新结果要以本次输出前的核验为准；不能留下“引用失效但仍受支持”。
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("status") not in {"SUPPORTED", "RISK"}:
                continue
            current_citations = finding.get("citations", [])
            if not current_citations or any(
                not isinstance(item, dict) or not item.get("is_citable", False)
                for item in current_citations
            ):
                finding["status"] = "INSUFFICIENT_EVIDENCE"
                finding["guard_message"] = "本次输出前来源准入已变化，须重新核验并运行"
        if any(isinstance(item, dict) and item.get("status") == "INSUFFICIENT_EVIDENCE" for item in findings):
            view["overall_status"] = "INSUFFICIENT_EVIDENCE"
    if denied:
        # 生成文字可能复述工具正文，不能只遮引用而放出复述内容。
        view["summary"] = "关联资料的当前使用条件不允许展示正文，历史分析内容已隐藏"
        view["limitations"] = ["原始审计记录保留，仅返回题录、状态和隐藏原因"]
        for finding in findings:
            if isinstance(finding, dict):
                finding["title"] = "历史分析（受限正文已隐藏）"
                finding["statement"] = "分析正文已隐藏"
                finding["suggested_action"] = "核对资料使用许可后再查看"
                finding["guard_message"] = "当前资料使用条件不允许显示分析正文"
        view["content_redacted"] = True
    return view, bool(denied)


class FindingStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    RISK = "RISK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_DRAWING = "NEEDS_DRAWING"


class OverallAgentStatus(StrEnum):
    DRAFT = "DRAFT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_DRAWING = "NEEDS_DRAWING"


class AgentFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    status: FindingStatus
    statement: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)
    suggested_action: str = Field(min_length=1, max_length=1000)


class AgentFinalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    overall_status: OverallAgentStatus
    findings: list[AgentFindingDraft] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class GroundedCitation(BaseModel):
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
    verification_record_ids: list[str] = Field(default_factory=list)
    evaluated_at: str | None = None
    applicability_status: str = "NOT_REVIEWED"
    manifest_sha256: str | None = None


class GroundedFinding(BaseModel):
    title: str
    status: FindingStatus
    statement: str
    citations: list[GroundedCitation]
    suggested_action: str
    guard_message: str | None


class GroundedAgentResult(BaseModel):
    summary: str
    overall_status: OverallAgentStatus
    findings: list[GroundedFinding]
    limitations: list[str]


class AgentStatusResponse(BaseModel):
    configured: bool
    provider: str
    model: str
    max_steps: int
    max_tool_calls: int
    material_character_limit: int
    notice: str


class AgentRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=500)
    include_project_materials: bool = False
    material_sharing_confirmed: bool = False


class AgentFindingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: FindingDecision
    comment: str | None = Field(default=None, max_length=500)


class AgentRunResponse(BaseModel):
    id: str
    project_id: str
    goal: str
    status: AgentRunStatus
    provider: str
    requested_model: str
    actual_model: str | None
    include_materials: bool
    max_steps: int
    current_step: int
    retry_of_run_id: str | None
    final_result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    evidence_mode: str = "HISTORICAL"
    admission_notice: str = "历史运行状态不等于当前可引用；请在新运行中重新核验"
    content_redacted: bool = False

    @classmethod
    def from_record(cls, record: AgentRunRecord, *, admission=None, historical=True) -> "AgentRunResponse":
        values = record.__dict__.copy()
        values.pop("input_snapshot")
        values["final_result"], values["content_redacted"] = _result_view(record.final_result, admission, historical)
        values["evidence_mode"] = "HISTORICAL" if historical else "CURRENT_RUN"
        if values["content_redacted"]:
            values["error_message"] = "相关资料使用条件不允许显示正文" if record.error_message else None
        return cls(**values)


class AgentStepResponse(BaseModel):
    id: str
    sequence: int
    kind: AgentStepKind
    name: str
    status: AgentStepStatus
    input_summary: dict[str, object]
    output_summary: dict[str, object]
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    error_code: str | None
    error_message: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: AgentStepRecord) -> "AgentStepResponse":
        values = record.__dict__.copy()
        values.pop("run_id")
        return cls(**values)


class AgentFindingDecisionResponse(BaseModel):
    id: str
    finding_index: int
    decision: FindingDecision
    comment: str | None
    decided_at: datetime

    @classmethod
    def from_record(
        cls, record: AgentFindingDecisionRecord
    ) -> "AgentFindingDecisionResponse":
        values = record.__dict__.copy()
        values.pop("run_id")
        return cls(**values)


class AgentRunDetailResponse(BaseModel):
    run: AgentRunResponse
    steps: list[AgentStepResponse]
    decisions: list[AgentFindingDecisionResponse]

    @classmethod
    def from_record(cls, record: AgentRunDetail, *, admission=None, historical=True) -> "AgentRunDetailResponse":
        run = AgentRunResponse.from_record(record.run, admission=admission, historical=historical)
        steps = [AgentStepResponse.from_record(item) for item in record.steps]
        if run.content_redacted:
            for step in steps:
                step.input_summary = {"notice": "相关正文使用条件限制，历史输入摘要已隐藏"}
                step.output_summary = {"notice": "相关正文使用条件限制，历史输出摘要已隐藏"}
                step.error_message = "历史错误正文已隐藏" if step.error_message else None
        return cls(
            run=run,
            steps=steps,
            decisions=[
                AgentFindingDecisionResponse.from_record(item)
                for item in record.decisions
            ],
        )
