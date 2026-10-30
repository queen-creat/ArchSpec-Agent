"""审查任务仓储，保存每一步当时看到的候选证据快照。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from archspec.reviews.models import (
    EvidenceCandidate,
    ReviewJobRecord,
    ReviewJobRow,
    ReviewJobStatus,
    ReviewStepDraft,
    ReviewStepRecord,
    ReviewStepRow,
    ReviewStepStatus,
)
from archspec.standards.models import StandardLifecycleStatus, StandardSourceType


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _job_record(row: ReviewJobRow) -> ReviewJobRecord:
    return ReviewJobRecord(
        id=row.id,
        task_document_id=row.task_document_id,
        status=row.status,
        error_message=row.error_message,
        started_at=_utc(row.started_at),  # type: ignore[arg-type]
        finished_at=_utc(row.finished_at),
    )


def _candidate_from_dict(value: dict) -> EvidenceCandidate:
    fields = dict(value)
    fields["source_type"] = StandardSourceType(fields["source_type"])
    fields["lifecycle_status"] = StandardLifecycleStatus(
        fields["lifecycle_status"]
    )
    return EvidenceCandidate(
        **fields,
    )


def _step_record(row: ReviewStepRow) -> ReviewStepRecord:
    return ReviewStepRecord(
        id=row.id,
        job_id=row.job_id,
        requirement_id=row.requirement_id,
        sequence=row.sequence,
        heading_path=row.heading_path,
        requirement_text=row.requirement_text,
        status=row.status,
        candidates=[
            _candidate_from_dict(value) for value in json.loads(row.candidates_json)
        ],
        confirmed_regulation_document_id=row.confirmed_regulation_document_id,
        confirmed_clause_numbers=json.loads(row.confirmed_clause_numbers_json),
        error_message=row.error_message,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_utc(row.updated_at),  # type: ignore[arg-type]
    )


class ReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(self, task_document_id: str) -> ReviewJobRecord:
        row = ReviewJobRow(
            id=str(uuid4()),
            task_document_id=task_document_id,
            status=ReviewJobStatus.PREPARING,
            error_message=None,
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        self.session.add(row)
        self.session.commit()
        return _job_record(row)

    def get_job(self, job_id: str) -> ReviewJobRecord | None:
        row = self.session.get(ReviewJobRow, job_id)
        return _job_record(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[ReviewJobRecord]:
        """按最近开始优先返回审查任务，供历史记录页面使用。"""
        rows = self.session.scalars(
            select(ReviewJobRow)
            .order_by(ReviewJobRow.started_at.desc())
            .limit(limit)
        ).all()
        return [_job_record(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        status: ReviewJobStatus,
        error_message: str | None = None,
    ) -> ReviewJobRecord:
        row = self.session.get(ReviewJobRow, job_id)
        if row is None:
            raise LookupError("审查任务不存在")
        row.status = status
        row.error_message = error_message
        row.finished_at = (
            datetime.now(UTC)
            if status in {ReviewJobStatus.COMPLETED, ReviewJobStatus.FAILED}
            else None
        )
        self.session.commit()
        return _job_record(row)

    def replace_steps(
        self, job_id: str, drafts: list[ReviewStepDraft]
    ) -> list[ReviewStepRecord]:
        self.session.execute(
            delete(ReviewStepRow).where(ReviewStepRow.job_id == job_id)
        )
        now = datetime.now(UTC)
        rows = [
            ReviewStepRow(
                id=str(uuid4()),
                job_id=job_id,
                requirement_id=draft.requirement_id,
                sequence=draft.sequence,
                heading_path=draft.heading_path,
                requirement_text=draft.requirement_text,
                status=draft.status,
                candidates_json=json.dumps(
                    [asdict(item) for item in draft.candidates], ensure_ascii=False
                ),
                confirmed_regulation_document_id=None,
                confirmed_clause_numbers_json="[]",
                error_message=draft.error_message,
                created_at=now,
                updated_at=now,
            )
            for draft in drafts
        ]
        self.session.add_all(rows)
        self.session.commit()
        return [_step_record(row) for row in rows]

    def list_steps(self, job_id: str) -> list[ReviewStepRecord]:
        rows = self.session.scalars(
            select(ReviewStepRow)
            .where(ReviewStepRow.job_id == job_id)
            .order_by(ReviewStepRow.sequence)
        ).all()
        return [_step_record(row) for row in rows]

    def get_step(self, step_id: str) -> ReviewStepRecord | None:
        row = self.session.get(ReviewStepRow, step_id)
        return _step_record(row) if row else None

    def update_step(
        self,
        step_id: str,
        status: ReviewStepStatus,
        *,
        confirmed_regulation_document_id: str | None = None,
        confirmed_clause_numbers: list[str] | None = None,
        error_message: str | None = None,
    ) -> ReviewStepRecord:
        row = self.session.get(ReviewStepRow, step_id)
        if row is None:
            raise LookupError("审查步骤不存在")
        row.status = status
        row.confirmed_regulation_document_id = confirmed_regulation_document_id
        row.confirmed_clause_numbers_json = json.dumps(
            confirmed_clause_numbers or [], ensure_ascii=False
        )
        row.error_message = error_message
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        return _step_record(row)

    def get_detail(
        self, job_id: str
    ) -> tuple[ReviewJobRecord, list[ReviewStepRecord]] | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        return job, self.list_steps(job_id)
