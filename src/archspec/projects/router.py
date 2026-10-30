"""设计项目 REST API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.database import get_session
from archspec.documents.repository import DocumentRepository
from archspec.projects.repository import ProjectRepository
from archspec.projects.schemas import (
    ProjectCreateRequest,
    ProjectDocumentAttachRequest,
    ProjectDocumentResponse,
    ProjectResponse,
    ProjectUpdateRequest,
)
from archspec.projects.service import ProjectError, ProjectNotFound, ProjectService

router = APIRouter(prefix="/api/projects", tags=["设计项目"])


def get_project_service(
    session: Annotated[Session, Depends(get_session)],
) -> ProjectService:
    return ProjectService(
        ProjectRepository(session),
        DocumentRepository(session),
        AssistedLabelingRepository(session),
    )


def _http_error(error: ProjectError) -> None:
    code = 404 if isinstance(error, ProjectNotFound) else 422
    raise HTTPException(status_code=code, detail=str(error)) from error


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    request: ProjectCreateRequest,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectResponse:
    try:
        return ProjectResponse.from_record(service.create(**request.model_dump()))
    except ProjectError as error:
        _http_error(error)


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    service: Annotated[ProjectService, Depends(get_project_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ProjectResponse]:
    return [ProjectResponse.from_record(item) for item in service.list_recent(limit)]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectResponse:
    try:
        return ProjectResponse.from_record(service.get(project_id))
    except ProjectError as error:
        _http_error(error)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    request: ProjectUpdateRequest,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectResponse:
    try:
        changes = request.model_dump(exclude_unset=True)
        return ProjectResponse.from_record(service.update(project_id, changes))
    except ProjectError as error:
        _http_error(error)


@router.post(
    "/{project_id}/documents",
    response_model=ProjectDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def attach_project_document(
    project_id: str,
    request: ProjectDocumentAttachRequest,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectDocumentResponse:
    try:
        return ProjectDocumentResponse.from_record(
            service.attach_document(
                project_id, request.document_id, request.document_type
            )
        )
    except ProjectError as error:
        _http_error(error)


@router.get(
    "/{project_id}/documents", response_model=list[ProjectDocumentResponse]
)
def list_project_documents(
    project_id: str,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[ProjectDocumentResponse]:
    try:
        return [
            ProjectDocumentResponse.from_record(item)
            for item in service.list_documents(project_id)
        ]
    except ProjectError as error:
        _http_error(error)
