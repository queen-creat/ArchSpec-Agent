"""可追踪审查工作流 REST API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from archspec.assisted_labeling.router import get_assisted_labeling_service
from archspec.database import get_session
from archspec.documents.repository import DocumentRepository
from archspec.reviews.report_html import render_review_report
from archspec.reviews.repository import ReviewRepository
from archspec.reviews.schemas import (
    ConfirmReviewStepRequest,
    ReviewDetailResponse,
    ReviewHistoryItemResponse,
    ReviewJobResponse,
    ReviewReportResponse,
    StartReviewRequest,
)
from archspec.reviews.service import (
    ReviewError,
    ReviewNotFound,
    ReviewService,
)
from archspec.standards.repository import StandardMetadataRepository
from archspec.standards.service import StandardMetadataService

router = APIRouter(prefix="/api/reviews", tags=["可追踪审查任务"])


def get_review_service(
    session: Annotated[Session, Depends(get_session)],
) -> ReviewService:
    return ReviewService(
        ReviewRepository(session),
        get_assisted_labeling_service(session),
        StandardMetadataService(
            DocumentRepository(session), StandardMetadataRepository(session)
        ),
    )


def _http_error(error: Exception) -> None:
    code = 404 if isinstance(error, ReviewNotFound) else 422
    raise HTTPException(status_code=code, detail=str(error)) from error


@router.post("", response_model=ReviewDetailResponse, status_code=status.HTTP_201_CREATED)
def start_review(
    request: StartReviewRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDetailResponse:
    try:
        return ReviewDetailResponse.from_records(*service.start(request.task_document_id))
    except (ReviewError, ValueError) as error:
        _http_error(error)


@router.get("", response_model=list[ReviewHistoryItemResponse])
def list_reviews(
    service: Annotated[ReviewService, Depends(get_review_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ReviewHistoryItemResponse]:
    """列出最近审查记录，供工作台显示进度和继续入口。"""
    return [
        ReviewHistoryItemResponse.from_record(item)
        for item in service.list_history(limit)
    ]


@router.get("/{job_id}", response_model=ReviewDetailResponse)
def get_review(
    job_id: str,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewDetailResponse:
    try:
        return ReviewDetailResponse.from_records(*service.get_detail(job_id))
    except ReviewError as error:
        _http_error(error)


@router.post(
    "/{job_id}/steps/{step_id}/confirm", response_model=ReviewJobResponse
)
def confirm_step(
    job_id: str,
    step_id: str,
    request: ConfirmReviewStepRequest,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewJobResponse:
    try:
        return ReviewJobResponse.from_record(
            service.confirm_step(
                job_id,
                step_id,
                request.regulation_document_id,
                request.clause_numbers,
            )
        )
    except ReviewError as error:
        _http_error(error)


@router.post(
    "/{job_id}/steps/{step_id}/no-evidence", response_model=ReviewJobResponse
)
def no_evidence(
    job_id: str,
    step_id: str,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewJobResponse:
    try:
        return ReviewJobResponse.from_record(service.mark_no_evidence(job_id, step_id))
    except ReviewError as error:
        _http_error(error)


@router.get("/{job_id}/report", response_model=ReviewReportResponse)
def get_report(
    job_id: str,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewReportResponse:
    try:
        return ReviewReportResponse.from_record(service.get_report(job_id))
    except ReviewError as error:
        _http_error(error)


@router.get("/{job_id}/report.html", response_class=HTMLResponse)
def get_printable_report(
    job_id: str,
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> HTMLResponse:
    try:
        return HTMLResponse(render_review_report(service.get_report(job_id, operation="EXPORT")))
    except ReviewError as error:
        _http_error(error)
