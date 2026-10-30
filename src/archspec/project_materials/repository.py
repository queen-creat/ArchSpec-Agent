"""项目资料分类信息仓储。"""

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from archspec.project_materials.models import (
    MaterialClassification,
    ProjectMaterialMetadataRow,
)
from archspec.projects.models import ProjectDocumentType


class ProjectMaterialRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self, link_id: str, classification: MaterialClassification
    ) -> MaterialClassification:
        row = self.session.scalar(
            select(ProjectMaterialMetadataRow).where(
                ProjectMaterialMetadataRow.link_id == link_id
            )
        )
        if row is None:
            row = ProjectMaterialMetadataRow(id=str(uuid4()), link_id=link_id)
            self.session.add(row)
        row.status = classification.status
        row.confidence = classification.confidence
        row.reasons_json = json.dumps(classification.reasons, ensure_ascii=False)
        self.session.commit()
        return classification

    def get(
        self, link_id: str, document_type: ProjectDocumentType
    ) -> MaterialClassification | None:
        row = self.session.scalar(
            select(ProjectMaterialMetadataRow).where(
                ProjectMaterialMetadataRow.link_id == link_id
            )
        )
        if row is None:
            return None
        return MaterialClassification(
            document_type=document_type,
            confidence=row.confidence,
            status=row.status,
            reasons=json.loads(row.reasons_json),
        )
