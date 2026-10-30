"""评测仓储：保存题目以及不可变的运行结果快照。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.evaluation.metrics import EvaluationMetrics
from archspec.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationCaseRow,
    EvaluationCaseStatus,
    EvaluationResultRecord,
    EvaluationResultRow,
    EvaluationRunRecord,
    EvaluationRunRow,
    EvaluationRunStatus,
    ResultDraft,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _case_record(row: EvaluationCaseRow) -> EvaluationCaseRecord:
    return EvaluationCaseRecord(
        id=row.id,
        document_id=row.document_id,
        query=row.query,
        expected_clause_numbers=json.loads(row.expected_clause_numbers_json),
        source_note=row.source_note,
        status=row.status,
        created_at=_utc(row.created_at),  # type: ignore[arg-type]
    )


def _run_record(row: EvaluationRunRow) -> EvaluationRunRecord:
    return EvaluationRunRecord(
        id=row.id,
        document_id=row.document_id,
        algorithm=row.algorithm,
        status=row.status,
        case_count=row.case_count,
        recall_at_1=row.recall_at_1,
        recall_at_3=row.recall_at_3,
        recall_at_5=row.recall_at_5,
        mrr=row.mrr,
        average_latency_ms=row.average_latency_ms,
        error_message=row.error_message,
        started_at=_utc(row.started_at),  # type: ignore[arg-type]
        finished_at=_utc(row.finished_at),
    )


def _result_record(row: EvaluationResultRow) -> EvaluationResultRecord:
    return EvaluationResultRecord(
        id=row.id,
        run_id=row.run_id,
        case_id=row.case_id,
        query=row.query,
        expected_clause_numbers=json.loads(row.expected_clause_numbers_json),
        first_relevant_rank=row.first_relevant_rank,
        retrieved_clause_numbers=json.loads(row.retrieved_clause_numbers_json),
        retrieved_scores=json.loads(row.retrieved_scores_json),
        latency_ms=row.latency_ms,
    )


class EvaluationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_case(
        self,
        document_id: str,
        query: str,
        expected_clause_numbers: list[str],
        source_note: str,
        status: EvaluationCaseStatus,
    ) -> EvaluationCaseRecord:
        row = EvaluationCaseRow(
            id=str(uuid4()),
            document_id=document_id,
            query=query,
            expected_clause_numbers_json=json.dumps(expected_clause_numbers, ensure_ascii=False),
            source_note=source_note,
            status=status,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.commit()
        return _case_record(row)

    def get_case(self, case_id: str) -> EvaluationCaseRecord | None:
        row = self.session.get(EvaluationCaseRow, case_id)
        return _case_record(row) if row else None

    def list_cases(
        self,
        document_id: str | None = None,
        status: EvaluationCaseStatus | None = None,
    ) -> list[EvaluationCaseRecord]:
        statement = select(EvaluationCaseRow).order_by(EvaluationCaseRow.created_at)
        if document_id is not None:
            statement = statement.where(EvaluationCaseRow.document_id == document_id)
        if status is not None:
            statement = statement.where(EvaluationCaseRow.status == status)
        return [_case_record(row) for row in self.session.scalars(statement).all()]

    def create_run(self, document_id: str, algorithm: str) -> EvaluationRunRecord:
        row = EvaluationRunRow(
            id=str(uuid4()), document_id=document_id, algorithm=algorithm,
            status=EvaluationRunStatus.RUNNING, case_count=0,
            recall_at_1=None, recall_at_3=None, recall_at_5=None, mrr=None,
            average_latency_ms=None, error_message=None,
            started_at=datetime.now(UTC), finished_at=None,
        )
        self.session.add(row)
        self.session.commit()
        return _run_record(row)

    def complete_run(
        self,
        run_id: str,
        metrics: EvaluationMetrics,
        average_latency_ms: float,
        results: list[ResultDraft],
    ) -> EvaluationRunRecord:
        row = self.session.get(EvaluationRunRow, run_id)
        if row is None:
            raise LookupError(f"评测运行不存在：{run_id}")
        for draft in results:
            self.session.add(EvaluationResultRow(
                id=str(uuid4()), run_id=run_id, case_id=draft.case_id,
                query=draft.query,
                expected_clause_numbers_json=json.dumps(draft.expected_clause_numbers, ensure_ascii=False),
                first_relevant_rank=draft.first_relevant_rank,
                retrieved_clause_numbers_json=json.dumps(draft.retrieved_clause_numbers, ensure_ascii=False),
                retrieved_scores_json=json.dumps(draft.retrieved_scores),
                latency_ms=draft.latency_ms,
            ))
        row.status = EvaluationRunStatus.COMPLETED
        row.case_count = metrics.case_count
        row.recall_at_1 = metrics.recall_at_1
        row.recall_at_3 = metrics.recall_at_3
        row.recall_at_5 = metrics.recall_at_5
        row.mrr = metrics.mrr
        row.average_latency_ms = average_latency_ms
        row.finished_at = datetime.now(UTC)
        self.session.commit()
        return _run_record(row)

    def fail_run(self, run_id: str, error_message: str) -> EvaluationRunRecord:
        row = self.session.get(EvaluationRunRow, run_id)
        if row is None:
            raise LookupError(f"评测运行不存在：{run_id}")
        row.status = EvaluationRunStatus.FAILED
        row.error_message = error_message
        row.finished_at = datetime.now(UTC)
        self.session.commit()
        return _run_record(row)

    def get_run(self, run_id: str) -> EvaluationRunRecord | None:
        row = self.session.get(EvaluationRunRow, run_id)
        return _run_record(row) if row else None

    def list_results(self, run_id: str) -> list[EvaluationResultRecord]:
        rows = self.session.scalars(
            select(EvaluationResultRow)
            .where(EvaluationResultRow.run_id == run_id)
            .order_by(EvaluationResultRow.id)
        ).all()
        return [_result_record(row) for row in rows]
