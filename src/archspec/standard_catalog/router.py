"""中央规范目录查询接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from archspec.database import get_session
from archspec.standard_catalog.provider import UrllibOfficialPageFetcher
from archspec.standard_catalog.repository import (
    CatalogSyncRepository,
    StandardCatalogRepository,
)
from archspec.standard_catalog.schemas import (
    CatalogEntryResponse,
    CatalogSearchResultResponse,
    CatalogStatusResponse,
    CatalogSyncRequest,
    CatalogSyncRunResponse,
)
from archspec.standard_catalog.service import StandardCatalogService
from archspec.standard_catalog.sync_service import (
    CatalogEntryNotFound,
    CatalogSyncAlreadyRunning,
    CatalogSyncService,
    CatalogVerificationError,
    CatalogVerificationService,
    CatalogVerificationValidationError,
)

router = APIRouter(prefix="/api/standard-catalog", tags=["中央规范目录"])


def get_standard_catalog_service(
    session: Annotated[Session, Depends(get_session)],
) -> StandardCatalogService:
    repository = StandardCatalogRepository(session)
    service = StandardCatalogService(repository)
    return service


def get_catalog_verification_service(
    session: Annotated[Session, Depends(get_session)],
) -> CatalogVerificationService:
    allowed_hosts = {
        "mohurd.gov.cn",
        "www.mohurd.gov.cn",
        "openstd.samr.gov.cn",
        "std.samr.gov.cn",
    }
    return CatalogVerificationService(
        StandardCatalogRepository(session),
        UrllibOfficialPageFetcher(allowed_hosts),
    )


def get_catalog_sync_service(
    session: Annotated[Session, Depends(get_session)],
    verification: Annotated[
        CatalogVerificationService, Depends(get_catalog_verification_service)
    ],
) -> CatalogSyncService:
    return CatalogSyncService(
        StandardCatalogRepository(session),
        CatalogSyncRepository(session),
        verification,
    )


@router.get("/search", response_model=list[CatalogSearchResultResponse])
def search_catalog(
    query: Annotated[str, Query(min_length=1, max_length=500)],
    service: Annotated[StandardCatalogService, Depends(get_standard_catalog_service)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[CatalogSearchResultResponse]:
    return [
        CatalogSearchResultResponse.from_record(item)
        for item in service.search(query, limit)
    ]


@router.get("/status", response_model=CatalogStatusResponse)
def catalog_status(
    service: Annotated[StandardCatalogService, Depends(get_standard_catalog_service)],
) -> CatalogStatusResponse:
    return CatalogStatusResponse.from_record(service.status())


@router.post(
    "/entries/{entry_id}/verify",
    response_model=CatalogEntryResponse,
)
def verify_catalog_entry(
    entry_id: str,
    service: Annotated[
        CatalogVerificationService, Depends(get_catalog_verification_service)
    ],
) -> CatalogEntryResponse:
    try:
        return CatalogEntryResponse.from_record(service.verify_entry(entry_id))
    except CatalogEntryNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CatalogVerificationValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CatalogVerificationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/sync-runs", response_model=list[CatalogSyncRunResponse])
def list_catalog_sync_runs(
    service: Annotated[CatalogSyncService, Depends(get_catalog_sync_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CatalogSyncRunResponse]:
    return [
        CatalogSyncRunResponse.from_record(item)
        for item in service.syncs.list(limit)
    ]


@router.post("/sync-runs", response_model=CatalogSyncRunResponse)
def create_catalog_sync_run(
    request: CatalogSyncRequest,
    service: Annotated[CatalogSyncService, Depends(get_catalog_sync_service)],
) -> CatalogSyncRunResponse:
    if request.provider_name.strip().upper() != "MOHURD":
        raise HTTPException(status_code=422, detail="当前只支持住建部题录核验")
    if not 1 <= request.limit <= 20:
        raise HTTPException(status_code=422, detail="每次只能核验 1 至 20 条题录")
    try:
        return CatalogSyncRunResponse.from_record(
            service.run_existing_entries(request.provider_name, request.limit)
        )
    except CatalogSyncAlreadyRunning as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

