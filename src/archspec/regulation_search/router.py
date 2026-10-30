"""快速查规范 REST API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.assisted_labeling.router import get_assisted_labeling_service
from archspec.database import get_session
from archspec.documents.repository import DocumentRepository
from archspec.projects.repository import ProjectRepository
from archspec.projects.service import ProjectError, ProjectService
from archspec.regulation_search.schemas import (
    RegulationSearchApiResponse,
    RegulationSearchRequest,
)
from archspec.regulation_search.service import (
    RegulationSearchService,
    RegulationSearchValidationError,
)
from archspec.standard_catalog.repository import StandardCatalogRepository
from archspec.standard_catalog.service import StandardCatalogService
from archspec.standards.repository import StandardMetadataRepository
from archspec.standards.service import StandardMetadataService

router = APIRouter(prefix="/api/regulations", tags=["快速查规范"])


def get_regulation_search_service(
    session: Annotated[Session, Depends(get_session)],
) -> RegulationSearchService:
    documents = DocumentRepository(session)
    assisted = get_assisted_labeling_service(session)
    catalog_repository = StandardCatalogRepository(session)
    catalog_service = StandardCatalogService(catalog_repository)
    return RegulationSearchService(
        documents,
        AssistedLabelingRepository(session),
        StandardMetadataService(documents, StandardMetadataRepository(session)),
        ProjectService(ProjectRepository(session)),
        assisted.search_service,
        catalog_service,
    )


@router.post("/search", response_model=RegulationSearchApiResponse)
def quick_search(
    request: RegulationSearchRequest,
    service: Annotated[
        RegulationSearchService, Depends(get_regulation_search_service)
    ],
) -> RegulationSearchApiResponse:
    try:
        return RegulationSearchApiResponse.from_record(
            service.search(
                request.query,
                request.project_id,
                request.specialty,
                request.top_k,
            )
        )
    except (RegulationSearchValidationError, ProjectError) as error:
        code = 404 if isinstance(error, ProjectError) else 422
        raise HTTPException(status_code=code, detail=str(error)) from error
