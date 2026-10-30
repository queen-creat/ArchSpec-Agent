"""设计项目接口输入输出格式。"""

from datetime import datetime

from pydantic import BaseModel, Field

from archspec.projects.models import (
    DesignProjectRecord,
    ProjectBuildingType,
    ProjectDesignStage,
    ProjectDocumentRecord,
    ProjectDocumentType,
    ProjectStatus,
)

BUILDING_TYPE_LABELS = {
    ProjectBuildingType.OFFICE: "办公建筑",
    ProjectBuildingType.RESIDENTIAL: "居住建筑",
    ProjectBuildingType.COMMERCIAL: "商业建筑",
    ProjectBuildingType.EDUCATION: "教育建筑",
    ProjectBuildingType.HEALTHCARE: "医疗建筑",
    ProjectBuildingType.CULTURAL: "文化建筑",
    ProjectBuildingType.HOTEL: "旅馆建筑",
    ProjectBuildingType.INDUSTRIAL: "工业建筑",
    ProjectBuildingType.OTHER: "其他",
}
DESIGN_STAGE_LABELS = {
    ProjectDesignStage.CONCEPT: "方案设计",
    ProjectDesignStage.PRELIMINARY: "初步设计",
    ProjectDesignStage.CONSTRUCTION_DOCUMENTS: "施工图设计",
    ProjectDesignStage.OTHER: "其他",
}
STATUS_LABELS = {
    ProjectStatus.ACTIVE: "进行中",
    ProjectStatus.ARCHIVED: "已归档",
}
DOCUMENT_TYPE_LABELS = {
    ProjectDocumentType.PROJECT_REQUIREMENT: "项目要求",
    ProjectDocumentType.SCHEME_DESCRIPTION: "方案说明",
    ProjectDocumentType.PRELIMINARY_DESCRIPTION: "初步设计说明",
    ProjectDocumentType.DESIGN_DESCRIPTION: "施工图设计说明",
    ProjectDocumentType.OTHER: "其他资料",
}


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_code: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    building_type: ProjectBuildingType = ProjectBuildingType.OTHER
    design_stage: ProjectDesignStage = ProjectDesignStage.OTHER
    height_m: float | None = None
    above_ground_floors: int | None = None
    underground_floors: int | None = None
    gross_floor_area_m2: float | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    project_code: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    building_type: ProjectBuildingType | None = None
    design_stage: ProjectDesignStage | None = None
    status: ProjectStatus | None = None
    height_m: float | None = None
    above_ground_floors: int | None = None
    underground_floors: int | None = None
    gross_floor_area_m2: float | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    project_code: str | None
    province: str | None
    city: str | None
    building_type: ProjectBuildingType
    building_type_label: str
    design_stage: ProjectDesignStage
    design_stage_label: str
    status: ProjectStatus
    status_label: str
    height_m: float | None
    above_ground_floors: int | None
    underground_floors: int | None
    gross_floor_area_m2: float | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: DesignProjectRecord) -> "ProjectResponse":
        return cls(
            **record.__dict__,
            building_type_label=BUILDING_TYPE_LABELS[record.building_type],
            design_stage_label=DESIGN_STAGE_LABELS[record.design_stage],
            status_label=STATUS_LABELS[record.status],
        )


class ProjectDocumentAttachRequest(BaseModel):
    document_id: str = Field(min_length=1)
    document_type: ProjectDocumentType


class ProjectDocumentResponse(BaseModel):
    id: str
    project_id: str
    document_id: str
    document_type: ProjectDocumentType
    document_type_label: str
    document_name: str
    document_status: str
    attached_at: datetime

    @classmethod
    def from_record(cls, record: ProjectDocumentRecord) -> "ProjectDocumentResponse":
        return cls(
            **record.__dict__,
            document_type_label=DOCUMENT_TYPE_LABELS[record.document_type],
        )
