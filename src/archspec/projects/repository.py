"""设计项目仓储。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.documents.models import DocumentRow
from archspec.projects.models import (
    DesignProjectRecord,
    DesignProjectRow,
    ProjectDocumentRecord,
    ProjectDocumentRow,
    ProjectDocumentType,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _record(row: DesignProjectRow) -> DesignProjectRecord:
    return DesignProjectRecord(
        id=row.id,
        name=row.name,
        project_code=row.project_code,
        province=row.province,
        city=row.city,
        building_type=row.building_type,
        design_stage=row.design_stage,
        status=row.status,
        height_m=row.height_m,
        above_ground_floors=row.above_ground_floors,
        underground_floors=row.underground_floors,
        gross_floor_area_m2=row.gross_floor_area_m2,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, record: DesignProjectRecord) -> DesignProjectRecord:
        self.session.add(DesignProjectRow(**record.__dict__))
        self.session.commit()
        return record

    def get(self, project_id: str) -> DesignProjectRecord | None:
        row = self.session.get(DesignProjectRow, project_id)
        return _record(row) if row else None

    def list_recent(self, limit: int = 20) -> list[DesignProjectRecord]:
        rows = self.session.scalars(
            select(DesignProjectRow)
            .order_by(DesignProjectRow.updated_at.desc())
            .limit(limit)
        ).all()
        return [_record(row) for row in rows]

    def update(
        self, project_id: str, changes: dict[str, object]
    ) -> DesignProjectRecord:
        row = self.session.get(DesignProjectRow, project_id)
        if row is None:
            raise LookupError("设计项目不存在")
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        return _record(row)

    def get_document_link(
        self, project_id: str, document_id: str
    ) -> ProjectDocumentRecord | None:
        statement = (
            select(ProjectDocumentRow, DocumentRow)
            .join(DocumentRow, DocumentRow.id == ProjectDocumentRow.document_id)
            .where(
                ProjectDocumentRow.project_id == project_id,
                ProjectDocumentRow.document_id == document_id,
            )
        )
        result = self.session.execute(statement).first()
        return self._document_record(*result) if result else None

    def attach_document(
        self,
        project_id: str,
        document_id: str,
        document_type: ProjectDocumentType,
    ) -> ProjectDocumentRecord:
        row = ProjectDocumentRow(
            id=str(uuid4()),
            project_id=project_id,
            document_id=document_id,
            document_type=document_type,
            attached_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.commit()
        document = self.session.get(DocumentRow, document_id)
        if document is None:
            raise LookupError("文档不存在")
        return self._document_record(row, document)

    def list_documents(self, project_id: str) -> list[ProjectDocumentRecord]:
        statement = (
            select(ProjectDocumentRow, DocumentRow)
            .join(DocumentRow, DocumentRow.id == ProjectDocumentRow.document_id)
            .where(ProjectDocumentRow.project_id == project_id)
            .order_by(ProjectDocumentRow.attached_at.desc())
        )
        return [
            self._document_record(link, document)
            for link, document in self.session.execute(statement).all()
        ]

    def get_document_link_by_id(
        self, link_id: str
    ) -> ProjectDocumentRecord | None:
        statement = (
            select(ProjectDocumentRow, DocumentRow)
            .join(DocumentRow, DocumentRow.id == ProjectDocumentRow.document_id)
            .where(ProjectDocumentRow.id == link_id)
        )
        result = self.session.execute(statement).first()
        return self._document_record(*result) if result else None

    def update_document_type(
        self, link_id: str, document_type: ProjectDocumentType
    ) -> ProjectDocumentRecord:
        link = self.session.get(ProjectDocumentRow, link_id)
        if link is None:
            raise LookupError("项目资料不存在")
        link.document_type = document_type
        self.session.commit()
        document = self.session.get(DocumentRow, link.document_id)
        if document is None:
            raise LookupError("项目资料文件不存在")
        return self._document_record(link, document)

    @staticmethod
    def _document_record(
        link: ProjectDocumentRow, document: DocumentRow
    ) -> ProjectDocumentRecord:
        return ProjectDocumentRecord(
            id=link.id,
            project_id=link.project_id,
            document_id=link.document_id,
            document_type=link.document_type,
            document_name=document.original_name,
            document_status=document.status,
            attached_at=_utc(link.attached_at),
        )
