"""把提取、检索和人工确认编排成可追踪审查工作流。"""

from dataclasses import dataclass, replace

from archspec.assisted_labeling.models import DocumentRole
from archspec.assisted_labeling.service import AssistedLabelingService
from archspec.reviews.models import (
    EvidenceCandidate,
    ReviewJobRecord,
    ReviewJobStatus,
    ReviewStepDraft,
    ReviewStepRecord,
    ReviewStepStatus,
)
from archspec.reviews.repository import ReviewRepository
from archspec.standard_governance.compatibility import ContentUseDenied
from archspec.standard_governance.repository import GovernanceRepository
from archspec.standard_governance.service import GovernanceService
from archspec.standards.service import StandardMetadataService


class ReviewError(ValueError):
    pass


class ReviewNotFound(ReviewError):
    pass


class ReviewValidationError(ReviewError):
    pass


@dataclass(frozen=True)
class ReviewReportItem:
    requirement_text: str
    heading_path: str | None
    status: ReviewStepStatus
    conclusion: str
    confirmed_evidence: EvidenceCandidate | None


@dataclass(frozen=True)
class ReviewReport:
    job: ReviewJobRecord
    items: list[ReviewReportItem]


@dataclass(frozen=True)
class ReviewHistoryItem:
    """一条审查历史摘要，供工作台快速判断是否还要继续处理。"""

    job: ReviewJobRecord
    task_document_name: str
    total: int
    confirmed: int
    pending: int
    can_resume: bool


class ReviewService:
    def __init__(
        self,
        repository: ReviewRepository,
        assisted_service: AssistedLabelingService,
        standards_service: StandardMetadataService,
    ) -> None:
        self.repository = repository
        self.assisted_service = assisted_service
        self.standards_service = standards_service
        self.admission = standards_service.admission
        self.governance = GovernanceRepository(repository.session)

    def start(
        self, task_document_id: str, *, trusted_only: bool = False
    ) -> tuple[ReviewJobRecord, list[ReviewStepRecord]]:
        self._assert_read(task_document_id, "LOCAL_SEARCH")
        if trusted_only and not self.governance.available():
            raise ReviewValidationError("规范治理尚未迁移，不能创建正式准入审查")
        job = self.repository.create_job(task_document_id)
        try:
            if trusted_only:
                GovernanceService(self.repository.session).record_review_policy(job.id)
            self.assisted_service.analyze_document(
                task_document_id, DocumentRole.TASK_BRIEF
            )
            requirements = self.assisted_service.list_requirements(task_document_id)
            if not requirements:
                requirements = self.assisted_service.extract_requirements(
                    task_document_id
                )
            regulation_ids = [
                item.id
                for item in self.assisted_service.list_documents()
                if item.role == DocumentRole.REGULATION
            ]
            eligible_ids = (
                self.standards_service.verified_current_document_ids(regulation_ids)
                if trusted_only
                else self.standards_service.eligible_document_ids(regulation_ids)
            )
            drafts: list[ReviewStepDraft] = []
            for requirement in requirements:
                suggestions = self.assisted_service.suggest_across_regulations(
                    requirement.id,
                    top_k=3,
                    eligible_regulation_document_ids=eligible_ids,
                )
                candidates = [self._candidate(item, trusted_only=trusted_only) for item in suggestions]
                drafts.append(
                    ReviewStepDraft(
                        requirement_id=requirement.id,
                        sequence=requirement.sequence,
                        heading_path=requirement.heading_path,
                        requirement_text=requirement.text,
                        status=ReviewStepStatus.WAITING_CONFIRMATION,
                        candidates=candidates,
                        error_message=None,
                    )
                )
            steps = self.repository.replace_steps(job.id, drafts)
            updated_job = self.repository.update_job(
                job.id, ReviewJobStatus.WAITING_CONFIRMATION
            )
            return updated_job, steps
        except Exception as error:
            self.repository.update_job(job.id, ReviewJobStatus.FAILED, str(error))
            raise

    def get_detail(
        self, job_id: str
    ) -> tuple[ReviewJobRecord, list[ReviewStepRecord]]:
        detail = self.repository.get_detail(job_id)
        if detail is None:
            raise ReviewNotFound("审查任务不存在")
        job, steps = detail
        task_allowed = True
        try:
            self.admission.assert_document_read(job.task_document_id, "LOCAL_EXCERPT")
        except ContentUseDenied:
            task_allowed = False
        return job, [replace(
            step,
            requirement_text=step.requirement_text if task_allowed else "任务资料使用条件不允许显示正文",
            candidates=[self._visible_candidate(item, job_id) for item in step.candidates],
        ) for step in steps]

    def list_history(self, limit: int = 20) -> list[ReviewHistoryItem]:
        """返回最近审查记录，并计算每次审查的处理进度。"""
        history: list[ReviewHistoryItem] = []
        for job in self.repository.list_jobs(limit):
            steps = self.repository.list_steps(job.id)
            task_document = self.standards_service.document_repository.get(
                job.task_document_id
            )
            confirmed = sum(
                step.status == ReviewStepStatus.CONFIRMED for step in steps
            )
            history.append(
                ReviewHistoryItem(
                    job=job,
                    task_document_name=(
                        task_document.original_name
                        if task_document is not None
                        else "已删除或未知资料"
                    ),
                    total=len(steps),
                    confirmed=confirmed,
                    pending=len(steps) - confirmed,
                    can_resume=(
                        job.status == ReviewJobStatus.WAITING_CONFIRMATION
                    ),
                )
            )
        return history

    def confirm_step(
        self,
        job_id: str,
        step_id: str,
        regulation_document_id: str,
        clause_numbers: list[str],
    ) -> ReviewJobRecord:
        step = self._get_job_step(job_id, step_id)
        selected = set(clause_numbers)
        available = {
            item.clause_number
            for item in step.candidates
            if item.regulation_document_id == regulation_document_id
        }
        if not selected or not selected.issubset(available):
            raise ReviewValidationError("只能确认本次审查列出的候选条文")
        self._assert_read(regulation_document_id, "LOCAL_EXCERPT")
        if self._authoritative(job_id):
            decision = self.admission.for_document(regulation_document_id)
            chosen = [item for item in step.candidates if item.regulation_document_id == regulation_document_id and item.clause_number in selected]
            if not decision["is_citable"] or any(
                item.artifact_binding_id != decision["artifact_binding_id"]
                or item.manifest_sha256 != decision.get("manifest_sha256") for item in chosen
            ):
                raise ReviewValidationError("本次正式证据准入或固定正文已变化，须重新核验并审查")
        self.assisted_service.confirm(
            step.requirement_id, regulation_document_id, clause_numbers
        )
        self.repository.update_step(
            step_id,
            ReviewStepStatus.CONFIRMED,
            confirmed_regulation_document_id=regulation_document_id,
            confirmed_clause_numbers=clause_numbers,
        )
        return self._refresh_job(job_id)

    def mark_no_evidence(self, job_id: str, step_id: str) -> ReviewJobRecord:
        step = self._get_job_step(job_id, step_id)
        self.assisted_service.mark_no_evidence(step.requirement_id)
        self.repository.update_step(step_id, ReviewStepStatus.NO_EVIDENCE)
        return self._refresh_job(job_id)

    def get_report(self, job_id: str, *, operation: str = "LOCAL_EXCERPT") -> ReviewReport:
        job, steps = self.get_detail(job_id)
        task_allowed = True
        try:
            # 本机可读不等于允许导出；改成任务书角色也不能绕过已登记许可。
            self.admission.assert_document_read(job.task_document_id, operation)
        except ContentUseDenied:
            task_allowed = False
        if operation == "EXPORT":
            task_decision = self.admission.for_document(job.task_document_id)
            artifact_id = task_decision.get("artifact_binding_id")
            if artifact_id is not None:
                task_allowed = task_allowed and self.admission.admission.permission(artifact_id, "EXPORT") == "ALLOW"
        items: list[ReviewReportItem] = []
        for step in steps:
            evidence = self._confirmed_evidence(step)
            if evidence is not None:
                evidence = self._visible_candidate(evidence, job_id, operation=operation)
                conclusion = (
                    f"已人工确认关联：{evidence.standard_code or evidence.regulation_name} "
                    f"第 {evidence.clause_number} 条"
                )
                conclusion += (
                    "；来源准入已通过，项目适用性仍待专业核对" if evidence.is_citable
                    else "；仅是候选/历史条文关联，不代表规范核验通过"
                )
            elif step.status == ReviewStepStatus.NO_EVIDENCE:
                conclusion = "当前资料未确认到依据，待人工核查"
            else:
                conclusion = "候选证据尚未人工确认"
            items.append(
                ReviewReportItem(
                    requirement_text=step.requirement_text if task_allowed else "任务资料使用条件不允许本次导出正文",
                    heading_path=step.heading_path if task_allowed else None,
                    status=step.status,
                    conclusion=conclusion,
                    confirmed_evidence=evidence,
                )
            )
        return ReviewReport(job=job, items=items)

    def _candidate(self, suggestion, *, trusted_only: bool = False) -> EvidenceCandidate:
        source = self.standards_service.describe_source(
            suggestion.regulation_document_id
        )
        decision = self.admission.for_document(suggestion.regulation_document_id)
        return EvidenceCandidate(
            regulation_document_id=suggestion.regulation_document_id,
            regulation_name=suggestion.regulation_name,
            clause_number=suggestion.clause_number,
            heading_path=suggestion.heading_path,
            text=suggestion.text,
            page_start=suggestion.page_start,
            page_end=suggestion.page_end,
            score=suggestion.score,
            retrieval_mode=suggestion.retrieval_mode,
            standard_code=source.standard_code,
            standard_title=source.title,
            source_type=source.source_type,
            lifecycle_status=source.lifecycle_status,
            source_url=source.source_url,
            source_warning=source.warning,
            is_citable=trusted_only and decision["is_citable"],
            evidence_mode="AUTHORITATIVE" if trusted_only and decision["is_citable"] else "CANDIDATE_ONLY",
            admission_policy_version=decision["policy_version"],
            standard_version_id=decision["standard_version_id"],
            source_record_id=decision["source_record_id"],
            artifact_binding_id=decision["artifact_binding_id"],
            verification_record_ids=decision["verification_record_ids"],
            evaluated_at=decision["evaluated_at"],
            applicability_status=decision["applicability_status"],
            manifest_sha256=decision.get("manifest_sha256"),
        )

    def _authoritative(self, job_id: str) -> bool:
        policy = self.governance.get_review_policy(job_id)
        return bool(policy and policy["evidence_mode"] == "AUTHORITATIVE")

    def _assert_read(self, document_id: str, operation: str) -> None:
        # 使用限制保持独立异常，交给统一 403 响应，不误报成输入格式错误。
        self.admission.assert_document_read(document_id, operation)

    def _visible_candidate(self, item: EvidenceCandidate, job_id: str, *, operation: str = "LOCAL_EXCERPT") -> EvidenceCandidate:
        """只改变输出视图，不改历史 JSON；人工关联不产生来源核验事件。"""
        decision = self.admission.for_document(item.regulation_document_id)
        allowed = True
        try:
            self.admission.assert_document_read(item.regulation_document_id, operation)
        except ContentUseDenied:
            allowed = False
        if operation == "EXPORT":
            artifact_id = decision.get("artifact_binding_id")
            allowed = allowed and artifact_id is not None and self.admission.admission.permission(artifact_id, "EXPORT") == "ALLOW"
        citable = bool(
            allowed and item.is_citable and self._authoritative(job_id) and decision["is_citable"]
            and item.artifact_binding_id == decision["artifact_binding_id"]
            and item.manifest_sha256 == decision.get("manifest_sha256")
        )
        warning = item.source_warning
        if not allowed:
            warning = "正文使用条件不允许本次显示或导出，仅保留题录与状态"
        elif not citable:
            warning = warning or "候选/历史关联，来源与版本未通过本次正式引用核验"
        return replace(
            item, text=item.text if allowed else "正文已隐藏：本次使用许可未明确允许",
            is_citable=citable,
            evidence_mode="AUTHORITATIVE" if citable else "HISTORICAL" if item.admission_policy_version is None else "CANDIDATE_ONLY",
            source_warning=warning,
        )

    def _get_job_step(self, job_id: str, step_id: str) -> ReviewStepRecord:
        self.get_detail(job_id)
        step = self.repository.get_step(step_id)
        if step is None or step.job_id != job_id:
            raise ReviewNotFound("审查步骤不存在")
        if step.status != ReviewStepStatus.WAITING_CONFIRMATION:
            raise ReviewValidationError("这一步已经处理过，不能重复操作")
        return step

    def _refresh_job(self, job_id: str) -> ReviewJobRecord:
        _, steps = self.get_detail(job_id)
        terminal = {ReviewStepStatus.CONFIRMED, ReviewStepStatus.NO_EVIDENCE}
        status = (
            ReviewJobStatus.COMPLETED
            if steps and all(step.status in terminal for step in steps)
            else ReviewJobStatus.WAITING_CONFIRMATION
        )
        return self.repository.update_job(job_id, status)

    @staticmethod
    def _confirmed_evidence(step: ReviewStepRecord) -> EvidenceCandidate | None:
        if step.status != ReviewStepStatus.CONFIRMED:
            return None
        confirmed = set(step.confirmed_clause_numbers)
        return next(
            (
                candidate
                for candidate in step.candidates
                if candidate.regulation_document_id
                == step.confirmed_regulation_document_id
                and candidate.clause_number in confirmed
            ),
            None,
        )
