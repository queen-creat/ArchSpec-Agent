"""Agent 运行日志仓储。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.agent_runs.models import (
    AgentFindingDecisionRecord,
    AgentFindingDecisionRow,
    AgentRunDetail,
    AgentRunRecord,
    AgentRunRow,
    AgentRunStatus,
    AgentStepKind,
    AgentStepRecord,
    AgentStepRow,
    AgentStepStatus,
    FindingDecision,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None) -> dict[str, object] | None:
    return json.loads(value) if value is not None else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _run_record(row: AgentRunRow) -> AgentRunRecord:
    return AgentRunRecord(
        id=row.id,
        project_id=row.project_id,
        goal=row.goal,
        status=row.status,
        provider=row.provider,
        requested_model=row.requested_model,
        actual_model=row.actual_model,
        include_materials=row.include_materials,
        max_steps=row.max_steps,
        current_step=row.current_step,
        retry_of_run_id=row.retry_of_run_id,
        input_snapshot=_load(row.input_snapshot_json) or {},
        final_result=_load(row.final_result_json),
        error_code=row.error_code,
        error_message=row.error_message,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cached_tokens=row.cached_tokens,
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
        created_at=_utc(row.created_at),
    )


def _step_record(row: AgentStepRow) -> AgentStepRecord:
    return AgentStepRecord(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        kind=row.kind,
        name=row.name,
        status=row.status,
        input_summary=_load(row.input_summary_json) or {},
        output_summary=_load(row.output_summary_json) or {},
        duration_ms=row.duration_ms,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=_utc(row.created_at),
    )


def _decision_record(row: AgentFindingDecisionRow) -> AgentFindingDecisionRecord:
    return AgentFindingDecisionRecord(
        id=row.id,
        run_id=row.run_id,
        finding_index=row.finding_index,
        decision=row.decision,
        comment=row.comment,
        decided_at=_utc(row.decided_at),
    )


class AgentRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        *,
        project_id: str,
        goal: str,
        requested_model: str,
        include_materials: bool,
        max_steps: int,
        retry_of_run_id: str | None,
        input_snapshot: dict[str, object],
    ) -> AgentRunRecord:
        now = datetime.now(UTC)
        row = AgentRunRow(
            id=str(uuid4()),
            project_id=project_id,
            goal=goal,
            status=AgentRunStatus.CREATED,
            provider="DEEPSEEK",
            requested_model=requested_model,
            actual_model=None,
            include_materials=include_materials,
            max_steps=max_steps,
            current_step=0,
            retry_of_run_id=retry_of_run_id,
            input_snapshot_json=_json(input_snapshot),
            final_result_json=None,
            error_code=None,
            error_message=None,
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
            started_at=None,
            finished_at=None,
            created_at=now,
        )
        self.session.add(row)
        self.session.commit()
        return _run_record(row)

    def update_run(
        self,
        run_id: str,
        *,
        status: AgentRunStatus,
        current_step: int | None = None,
        actual_model: str | None = None,
        final_result: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cached_tokens: int | None = None,
    ) -> AgentRunRecord:
        row = self.session.get(AgentRunRow, run_id)
        if row is None:
            raise LookupError("Agent 运行不存在")
        row.status = status
        if current_step is not None:
            row.current_step = current_step
        if actual_model is not None:
            row.actual_model = actual_model
        if final_result is not None:
            row.final_result_json = _json(final_result)
        row.error_code = error_code
        row.error_message = error_message
        if prompt_tokens is not None:
            row.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            row.completion_tokens = completion_tokens
        if cached_tokens is not None:
            row.cached_tokens = cached_tokens
        now = datetime.now(UTC)
        if status == AgentRunStatus.RUNNING and row.started_at is None:
            row.started_at = now
        if status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}:
            row.finished_at = now
        self.session.commit()
        return _run_record(row)

    def append_step(
        self,
        run_id: str,
        *,
        sequence: int,
        kind: AgentStepKind,
        name: str,
        status: AgentStepStatus,
        input_summary: dict[str, object],
        output_summary: dict[str, object],
        duration_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AgentStepRecord:
        row = AgentStepRow(
            id=str(uuid4()),
            run_id=run_id,
            sequence=sequence,
            kind=kind,
            name=name,
            status=status,
            input_summary_json=_json(input_summary),
            output_summary_json=_json(output_summary),
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=error_code,
            error_message=error_message,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.commit()
        return _step_record(row)

    def get(self, run_id: str) -> AgentRunRecord | None:
        row = self.session.get(AgentRunRow, run_id)
        return _run_record(row) if row else None

    def get_detail(self, run_id: str) -> AgentRunDetail | None:
        run = self.session.get(AgentRunRow, run_id)
        if run is None:
            return None
        steps = self.session.scalars(
            select(AgentStepRow)
            .where(AgentStepRow.run_id == run_id)
            .order_by(AgentStepRow.sequence)
        ).all()
        decisions = self.session.scalars(
            select(AgentFindingDecisionRow)
            .where(AgentFindingDecisionRow.run_id == run_id)
            .order_by(AgentFindingDecisionRow.finding_index)
        ).all()
        return AgentRunDetail(
            run=_run_record(run),
            steps=[_step_record(row) for row in steps],
            decisions=[_decision_record(row) for row in decisions],
        )

    def list_for_project(self, project_id: str, limit: int) -> list[AgentRunRecord]:
        rows = self.session.scalars(
            select(AgentRunRow)
            .where(AgentRunRow.project_id == project_id)
            .order_by(AgentRunRow.created_at.desc())
            .limit(limit)
        ).all()
        return [_run_record(row) for row in rows]

    def save_decision(
        self,
        run_id: str,
        finding_index: int,
        decision: FindingDecision,
        comment: str | None,
    ) -> AgentFindingDecisionRecord:
        run = self.session.get(AgentRunRow, run_id)
        if run is None:
            raise LookupError("Agent 运行不存在")
        final_result = _load(run.final_result_json) or {}
        findings = final_result.get("findings", [])
        if not isinstance(findings, list) or not 0 <= finding_index < len(findings):
            raise ValueError("审查建议编号不存在")
        row = self.session.scalar(
            select(AgentFindingDecisionRow).where(
                AgentFindingDecisionRow.run_id == run_id,
                AgentFindingDecisionRow.finding_index == finding_index,
            )
        )
        now = datetime.now(UTC)
        if row is None:
            row = AgentFindingDecisionRow(
                id=str(uuid4()),
                run_id=run_id,
                finding_index=finding_index,
                decision=decision,
                comment=comment,
                decided_at=now,
            )
            self.session.add(row)
        else:
            row.decision = decision
            row.comment = comment
            row.decided_at = now
        self.session.commit()
        decision_count = len(
            self.session.scalars(
                select(AgentFindingDecisionRow).where(
                    AgentFindingDecisionRow.run_id == run_id
                )
            ).all()
        )
        if decision_count == len(findings):
            run.status = AgentRunStatus.COMPLETED
            run.finished_at = now
            self.session.commit()
        return _decision_record(row)
