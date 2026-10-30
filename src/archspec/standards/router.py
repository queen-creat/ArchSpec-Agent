"""规范来源和版本题录接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from archspec.database import get_session
from archspec.documents.repository import DocumentRepository
from archspec.standard_catalog.repository import StandardCatalogRepository
from archspec.standards.repository import StandardMetadataRepository
from archspec.standards.schemas import (
    SaveStandardMetadataRequest,
    StandardMetadataResponse,
)
from archspec.standards.service import (
    StandardMetadataService,
    StandardMetadataValidationError,
)

router = APIRouter(prefix="/api/standards", tags=["规范来源与版本"])


def get_standard_metadata_service(
    session: Annotated[Session, Depends(get_session)],
) -> StandardMetadataService:
    return StandardMetadataService(
        DocumentRepository(session),
        StandardMetadataRepository(session),
        StandardCatalogRepository(session),
    )


@router.put(
    "/documents/{document_id}/metadata", response_model=StandardMetadataResponse
)
def save_metadata(
    document_id: str,
    request: SaveStandardMetadataRequest,
    service: Annotated[StandardMetadataService, Depends(get_standard_metadata_service)],
) -> StandardMetadataResponse:
    try:
        record = service.register(document_id=document_id, **request.model_dump())
    except StandardMetadataValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return StandardMetadataResponse.from_record(record)


@router.get(
    "/documents/{document_id}/metadata", response_model=StandardMetadataResponse
)
def get_metadata(
    document_id: str,
    service: Annotated[StandardMetadataService, Depends(get_standard_metadata_service)],
) -> StandardMetadataResponse:
    record = service.get_metadata(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="这份文档还没有规范来源信息")
    return StandardMetadataResponse.from_record(record)
