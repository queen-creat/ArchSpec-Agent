"""辅助标注仓储：保存可解释的文档角色判断。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from archspec.assisted_labeling.extractor import RequirementDraft
from archspec.assisted_labeling.models import (
    DocumentProfileRecord,
    DocumentProfileRow,
    DocumentRole,
    RequirementConfirmationRecord,
    RequirementConfirmationRow,
    RequirementItemRecord,
    RequirementItemRow,
    RequirementStatus,
)


def _to_profile(row: DocumentProfileRow) -> DocumentProfileRecord:
    analyzed_at = row.analyzed_at
    if analyzed_at.tzinfo is None:
        analyzed_at = analyzed_at.replace(tzinfo=UTC)
    return DocumentProfileRecord(
        id=row.id,
        document_id=row.document_id,
        role=row.role,
        confidence=row.confidence,
        reasons=json.loads(row.reasons_json),
        confirmed_by_user=row.confirmed_by_user,
        analyzed_at=analyzed_at,
    )


def _to_requirement(row: RequirementItemRow) -> RequirementItemRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return RequirementItemRecord(
        id=row.id,
        document_id=row.document_id,
        sequence=row.sequence,
        heading_path=row.heading_path,
        text=row.text,
        query=row.query,
        status=row.status,
        created_at=created_at,
    )


def _normalize_requirement(text: str) -> str:
    """忽略空白差异，防止相同原文被重复保存。"""
    return "".join(text.split())


def _to_confirmation(
    row: RequirementConfirmationRow,
) -> RequirementConfirmationRecord:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return RequirementConfirmationRecord(
        id=row.id,
        requirement_id=row.requirement_id,
        regulation_document_id=row.regulation_document_id,
        clause_numbers=json.loads(row.clause_numbers_json),
        evaluation_case_id=row.evaluation_case_id,
        created_at=created_at,
    )


class AssistedLabelingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_profile(
        self,
        document_id: str,
        role: DocumentRole,
        confidence: float,
        reasons: list[str],
        confirmed_by_user: bool,
    ) -> DocumentProfileRecord:
        row = self.session.scalar(
            select(DocumentProfileRow).where(
                DocumentProfileRow.document_id == document_id
            )
        )
        if row is None:
            row = DocumentProfileRow(id=str(uuid4()), document_id=document_id)
            self.session.add(row)
        row.role = role
        row.confidence = confidence
        row.reasons_json = json.dumps(reasons, ensure_ascii=False)
        row.confirmed_by_user = confirmed_by_user
        row.analyzed_at = datetime.now(UTC)
        self.session.commit()
        return _to_profile(row)

    def get_profile(self, document_id: str) -> DocumentProfileRecord | None:
        row = self.session.scalar(
            select(DocumentProfileRow).where(
                DocumentProfileRow.document_id == document_id
            )
        )
        return _to_profile(row) if row else None

    def replace_requirements(
        self, document_id: str, drafts: list[RequirementDraft]
    ) -> list[RequirementItemRecord]:
        """替换自动识别项，同时保留教师已经确认过的要求。"""
        confirmed_rows = list(
            self.session.scalars(
                select(RequirementItemRow).where(
                    RequirementItemRow.document_id == document_id,
                    RequirementItemRow.status == RequirementStatus.CONFIRMED,
                )
            )
        )
        confirmed_texts = {_normalize_requirement(row.text) for row in confirmed_rows}
        self.session.execute(
            delete(RequirementItemRow).where(
                RequirementItemRow.document_id == document_id,
                RequirementItemRow.status != RequirementStatus.CONFIRMED,
            )
        )

        next_sequence = max((row.sequence for row in confirmed_rows), default=-1) + 1
        for draft in drafts:
            if _normalize_requirement(draft.text) in confirmed_texts:
                continue
            self.session.add(
                RequirementItemRow(
                    id=str(uuid4()),
                    document_id=document_id,
                    sequence=next_sequence,
                    heading_path=draft.heading_path,
                    text=draft.text,
                    query=draft.query,
                    status=RequirementStatus.PENDING,
                    created_at=datetime.now(UTC),
                )
            )
            next_sequence += 1
        self.session.commit()
        return self.list_requirements(document_id)

    def list_requirements(self, document_id: str) -> list[RequirementItemRecord]:
        rows = self.session.scalars(
            select(RequirementItemRow)
            .where(RequirementItemRow.document_id == document_id)
            .order_by(RequirementItemRow.sequence, RequirementItemRow.created_at)
        )
        return [_to_requirement(row) for row in rows]

    def get_requirement(self, requirement_id: str) -> RequirementItemRecord | None:
        row = self.session.get(RequirementItemRow, requirement_id)
        return _to_requirement(row) if row else None

    def mark_requirement_status(
        self, requirement_id: str, status: RequirementStatus
    ) -> RequirementItemRecord:
        row = self.session.get(RequirementItemRow, requirement_id)
        if row is None:
            raise LookupError("设计要求不存在")
        row.status = status
        self.session.commit()
        return _to_requirement(row)

    def get_confirmation(
        self, requirement_id: str, regulation_document_id: str
    ) -> RequirementConfirmationRecord | None:
        row = self.session.scalar(
            select(RequirementConfirmationRow).where(
                RequirementConfirmationRow.requirement_id == requirement_id,
                RequirementConfirmationRow.regulation_document_id
                == regulation_document_id,
            )
        )
        return _to_confirmation(row) if row else None

    def create_confirmation(
        self,
        requirement_id: str,
        regulation_document_id: str,
        clause_numbers: list[str],
        evaluation_case_id: str,
    ) -> RequirementConfirmationRecord:
        row = RequirementConfirmationRow(
            id=str(uuid4()),
            requirement_id=requirement_id,
            regulation_document_id=regulation_document_id,
            clause_numbers_json=json.dumps(clause_numbers, ensure_ascii=False),
            evaluation_case_id=evaluation_case_id,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.commit()
        return _to_confirmation(row)
