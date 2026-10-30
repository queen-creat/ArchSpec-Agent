"""设计项目业务规则。"""

from datetime import UTC, datetime
from uuid import uuid4

from archspec.assisted_labeling.models import DocumentRole
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.documents.repository import DocumentRepository
from archspec.projects.models import (
    DesignProjectRecord,
    ProjectBuildingType,
    ProjectDesignStage,
    ProjectDocumentRecord,
    ProjectDocumentType,
    ProjectStatus,
)
from archspec.projects.repository import ProjectRepository


class ProjectError(ValueError):
    pass


class ProjectNotFound(ProjectError):
    pass


class ProjectValidationError(ProjectError):
    pass


class ProjectService:
    def __init__(
        self,
        repository: ProjectRepository,
        document_repository: DocumentRepository | None = None,
        labeling_repository: AssistedLabelingRepository | None = None,
    ) -> None:
        self.repository = repository
        self.document_repository = document_repository
        self.labeling_repository = labeling_repository

    def create(
        self,
        *,
        name: str,
        project_code: str | None = None,
        province: str | None = None,
        city: str | None = None,
        building_type: ProjectBuildingType = ProjectBuildingType.OTHER,
        design_stage: ProjectDesignStage = ProjectDesignStage.OTHER,
        height_m: float | None = None,
        above_ground_floors: int | None = None,
        underground_floors: int | None = None,
        gross_floor_area_m2: float | None = None,
    ) -> DesignProjectRecord:
        values = {
            "name": name,
            "height_m": height_m,
            "above_ground_floors": above_ground_floors,
            "underground_floors": underground_floors,
            "gross_floor_area_m2": gross_floor_area_m2,
        }
        self._validate(values)
        now = datetime.now(UTC)
        return self.repository.create(
            DesignProjectRecord(
                id=str(uuid4()),
                name=name.strip(),
                project_code=self._optional_text(project_code),
                province=self._optional_text(province),
                city=self._optional_text(city),
                building_type=building_type,
                design_stage=design_stage,
                status=ProjectStatus.ACTIVE,
                height_m=height_m,
                above_ground_floors=above_ground_floors,
                underground_floors=underground_floors,
                gross_floor_area_m2=gross_floor_area_m2,
                created_at=now,
                updated_at=now,
            )
        )

    def get(self, project_id: str) -> DesignProjectRecord:
        record = self.repository.get(project_id)
        if record is None:
            raise ProjectNotFound("设计项目不存在")
        return record

    def list_recent(self, limit: int = 20) -> list[DesignProjectRecord]:
        return self.repository.list_recent(limit)

    def update(
        self, project_id: str, changes: dict[str, object]
    ) -> DesignProjectRecord:
        self.get(project_id)
        self._validate(changes)
        normalized = dict(changes)
        for field in ("name", "project_code", "province", "city"):
            if field not in normalized:
                continue
            value = normalized[field]
            if field == "name":
                normalized[field] = str(value).strip()
            else:
                normalized[field] = self._optional_text(
                    str(value) if value is not None else None
                )
        return self.repository.update(project_id, normalized)

    def attach_document(
        self,
        project_id: str,
        document_id: str,
        document_type: ProjectDocumentType,
    ) -> ProjectDocumentRecord:
        self.get(project_id)
        if self.document_repository is None or self.labeling_repository is None:
            raise ProjectValidationError("项目资料服务尚未配置")
        if self.document_repository.get(document_id) is None:
            raise ProjectNotFound("项目资料不存在")
        profile = self.labeling_repository.get_profile(document_id)
        if profile is not None and profile.role == DocumentRole.REGULATION:
            raise ProjectValidationError("建筑规范属于全局规范库，不能作为项目资料重复关联")
        if self.repository.get_document_link(project_id, document_id) is not None:
            raise ProjectValidationError("这份资料已经关联到当前项目")
        return self.repository.attach_document(project_id, document_id, document_type)

    def list_documents(self, project_id: str) -> list[ProjectDocumentRecord]:
        self.get(project_id)
        return self.repository.list_documents(project_id)

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        cleaned = value.strip() if value else ""
        return cleaned or None

    @staticmethod
    def _validate(values: dict[str, object]) -> None:
        if "name" in values and not str(values["name"] or "").strip():
            raise ProjectValidationError("项目名称不能为空")
        numeric_rules = {
            "height_m": "建筑高度不能小于 0",
            "above_ground_floors": "地上层数不能小于 0",
            "underground_floors": "地下层数不能小于 0",
            "gross_floor_area_m2": "建筑面积不能小于 0",
        }
        for field, message in numeric_rules.items():
            value = values.get(field)
            if value is not None and float(value) < 0:
                raise ProjectValidationError(message)
