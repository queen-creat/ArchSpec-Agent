"""设计项目的业务对象和数据库表。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base
from archspec.documents.models import DocumentStatus


class ProjectBuildingType(StrEnum):
    OFFICE = "OFFICE"
    RESIDENTIAL = "RESIDENTIAL"
    COMMERCIAL = "COMMERCIAL"
    EDUCATION = "EDUCATION"
    HEALTHCARE = "HEALTHCARE"
    CULTURAL = "CULTURAL"
    HOTEL = "HOTEL"
    INDUSTRIAL = "INDUSTRIAL"
    OTHER = "OTHER"


class ProjectDesignStage(StrEnum):
    CONCEPT = "CONCEPT"
    PRELIMINARY = "PRELIMINARY"
    CONSTRUCTION_DOCUMENTS = "CONSTRUCTION_DOCUMENTS"
    OTHER = "OTHER"


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectDocumentType(StrEnum):
    PROJECT_REQUIREMENT = "PROJECT_REQUIREMENT"
    SCHEME_DESCRIPTION = "SCHEME_DESCRIPTION"
    PRELIMINARY_DESCRIPTION = "PRELIMINARY_DESCRIPTION"
    DESIGN_DESCRIPTION = "DESIGN_DESCRIPTION"
    OTHER = "OTHER"


@dataclass(frozen=True)
class DesignProjectRecord:
    id: str
    name: str
    project_code: str | None
    province: str | None
    city: str | None
    building_type: ProjectBuildingType
    design_stage: ProjectDesignStage
    status: ProjectStatus
    height_m: float | None
    above_ground_floors: int | None
    underground_floors: int | None
    gross_floor_area_m2: float | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProjectDocumentRecord:
    id: str
    project_id: str
    document_id: str
    document_type: ProjectDocumentType
    document_name: str
    document_status: DocumentStatus
    attached_at: datetime


class DesignProjectRow(Base):
    __tablename__ = "design_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    project_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    province: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    building_type: Mapped[ProjectBuildingType] = mapped_column(
        Enum(ProjectBuildingType)
    )
    design_stage: Mapped[ProjectDesignStage] = mapped_column(Enum(ProjectDesignStage))
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), index=True)
    height_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    above_ground_floors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    underground_floors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gross_floor_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectDocumentRow(Base):
    __tablename__ = "project_documents"
    __table_args__ = (UniqueConstraint("project_id", "document_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("design_projects.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    document_type: Mapped[ProjectDocumentType] = mapped_column(Enum(ProjectDocumentType))
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
