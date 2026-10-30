"""项目资料上传和管理 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from archspec.documents.router import get_document_service
from archspec.documents.service import DocumentService, DocumentUploadError, UploadDocument
from archspec.project_materials.classifier import ProjectMaterialClassifier
from archspec.project_materials.schemas import (
    MaterialRecommendationResponse,
    ProjectMaterialResponse,
    ProjectMaterialUpdateRequest,
)
from archspec.project_materials.service import ProjectMaterialError, ProjectMaterialService
from archspec.projects.models import ProjectDocumentType
from archspec.projects.router import get_project_service
from archspec.projects.service import ProjectError, ProjectService
from archspec.reviews.router import get_review_service
from archspec.reviews.schemas import ReviewDetailResponse
from archspec.reviews.service import ReviewService
from archspec.standard_catalog.router import get_standard_catalog_service
from archspec.standard_catalog.service import StandardCatalogService

router = APIRouter(prefix="/api/projects", tags=["项目资料"])


def get_project_material_service(
    documents: Annotated[DocumentService, Depends(get_document_service)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
    reviews: Annotated[ReviewService, Depends(get_review_service)],
    catalog: Annotated[
        StandardCatalogService, Depends(get_standard_catalog_service)
    ],
) -> ProjectMaterialService:
    return ProjectMaterialService(
        documents,
        projects,
        ProjectMaterialClassifier(),
        reviews=reviews,
        catalog=catalog,
    )


def _raise_error(error: Exception) -> None:
    code = 404 if "不存在" in str(error) else 422
    raise HTTPException(status_code=code, detail=str(error)) from error


@router.post(
    "/{project_id}/materials",
    response_model=ProjectMaterialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_project_material(
    project_id: str,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
    file: Annotated[UploadFile, File(description="任务书、项目要求或设计说明")],
    document_type: Annotated[ProjectDocumentType | None, Form()] = None,
) -> ProjectMaterialResponse:
    try:
        record = service.upload(
            project_id,
            UploadDocument(
                file.filename or "未命名资料",
                file.content_type or "application/octet-stream",
                await file.read(),
            ),
            document_type,
        )
        return ProjectMaterialResponse.from_record(record)
    except (ProjectMaterialError, ProjectError, DocumentUploadError) as error:
        _raise_error(error)


@router.get(
    "/{project_id}/materials", response_model=list[ProjectMaterialResponse]
)
def list_project_materials(
    project_id: str,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
) -> list[ProjectMaterialResponse]:
    try:
        return [ProjectMaterialResponse.from_record(item) for item in service.list(project_id)]
    except (ProjectMaterialError, ProjectError) as error:
        _raise_error(error)


@router.patch(
    "/{project_id}/materials/{link_id}", response_model=ProjectMaterialResponse
)
def confirm_project_material_type(
    project_id: str,
    link_id: str,
    request: ProjectMaterialUpdateRequest,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
) -> ProjectMaterialResponse:
    try:
        return ProjectMaterialResponse.from_record(
            service.confirm_type(project_id, link_id, request.document_type)
        )
    except (ProjectMaterialError, ProjectError) as error:
        _raise_error(error)


@router.post(
    "/{project_id}/materials/{link_id}/reprocess",
    response_model=ProjectMaterialResponse,
)
def reprocess_project_material(
    project_id: str,
    link_id: str,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
) -> ProjectMaterialResponse:
    try:
        return ProjectMaterialResponse.from_record(
            service.reprocess(project_id, link_id)
        )
    except (ProjectMaterialError, ProjectError, DocumentUploadError) as error:
        _raise_error(error)


@router.post(
    "/{project_id}/materials/{link_id}/reviews",
    response_model=ReviewDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_project_material_review(
    project_id: str,
    link_id: str,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
) -> ReviewDetailResponse:
    try:
        return ReviewDetailResponse.from_records(
            *service.start_review(project_id, link_id)
        )
    except (ProjectMaterialError, ProjectError) as error:
        _raise_error(error)


@router.post(
    "/{project_id}/materials/{link_id}/recommendations",
    response_model=MaterialRecommendationResponse,
)
def recommend_standards_for_project_material(
    project_id: str,
    link_id: str,
    service: Annotated[ProjectMaterialService, Depends(get_project_material_service)],
) -> MaterialRecommendationResponse:
    """读取资料正文，自动列出建议核对的规范目录。"""

    try:
        return MaterialRecommendationResponse.from_record(
            service.recommendations(project_id, link_id)
        )
    except (ProjectMaterialError, ProjectError) as error:
        _raise_error(error)
