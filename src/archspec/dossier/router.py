"""当前项目的分析总览与报告入口。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from archspec.agent_runs.repository import AgentRunRepository
from archspec.database import get_session
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.dossier.assessment import RequirementAssessmentService
from archspec.dossier.report import render_dossier_report
from archspec.dossier.service import ProjectDossierService
from archspec.projects.router import get_project_service
from archspec.projects.service import ProjectNotFound, ProjectService
from archspec.regulation_search.router import get_regulation_search_service
from archspec.regulation_search.service import RegulationSearchService

router = APIRouter(tags=["项目分析总览"])


class RequirementDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["CONFIRMED", "NEEDS_CHECK", "NOT_APPLICABLE"]
    evidence_index: int | None = Field(default=None, ge=0)
    comment: str = Field(default="", max_length=1000)


def get_dossier_service(
    session: Annotated[Session, Depends(get_session)],
    projects: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectDossierService:
    return ProjectDossierService(
        projects, DocumentRepository(session), DocumentChunkRepository(session)
    )


def get_assessment_service(
    session: Annotated[Session, Depends(get_session)],
    dossier: Annotated[ProjectDossierService, Depends(get_dossier_service)],
    search: Annotated[RegulationSearchService, Depends(get_regulation_search_service)],
) -> RequirementAssessmentService:
    return RequirementAssessmentService(session, dossier, search)


@router.post("/api/projects/{project_id}/requirement-assessments", status_code=201)
def assess_requirements(
    project_id: str,
    service: Annotated[RequirementAssessmentService, Depends(get_assessment_service)],
):
    try:
        return service.run(project_id)
    except ProjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/projects/{project_id}/requirement-assessments/latest")
def latest_assessment(
    project_id: str,
    service: Annotated[RequirementAssessmentService, Depends(get_assessment_service)],
):
    try:
        service.dossier.projects.get(project_id)
    except ProjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return service.latest(project_id)


@router.get("/api/projects/{project_id}/dossier")
def project_dossier(
    project_id: str, service: Annotated[ProjectDossierService, Depends(get_dossier_service)]
):
    try:
        return service.build(project_id)
    except ProjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/projects/{project_id}/requirement-assessments")
def assessment_history(
    project_id: str,
    service: Annotated[RequirementAssessmentService, Depends(get_assessment_service)],
):
    try:
        service.dossier.projects.get(project_id)
        return service.history(project_id)
    except ProjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/projects/{project_id}/requirement-assessments/{assessment_id}")
def assessment_detail(
    project_id: str,
    assessment_id: str,
    service: Annotated[RequirementAssessmentService, Depends(get_assessment_service)],
):
    try:
        return service.get(project_id, assessment_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/api/projects/{project_id}/requirement-assessments/{assessment_id}/items/{item_index}/decision"
)
def decide_requirement(
    project_id: str,
    assessment_id: str,
    item_index: int,
    request: RequirementDecisionRequest,
    service: Annotated[RequirementAssessmentService, Depends(get_assessment_service)],
):
    try:
        return service.decide(
            project_id,
            assessment_id,
            item_index,
            request.action,
            request.evidence_index,
            request.comment,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/projects/{project_id}/dossier/report.html", response_class=HTMLResponse)
def project_report(
    project_id: str,
    service: Annotated[ProjectDossierService, Depends(get_dossier_service)],
    session: Annotated[Session, Depends(get_session)],
    assessment_id: str | None = None,
):
    try:
        dossier = service.build(project_id, operation="EXPORT")
    except ProjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    runs = AgentRunRepository(session)
    latest = runs.list_for_project(project_id, 1)
    detail = service.report_agent_detail(runs.get_detail(latest[0].id) if latest else None)
    assessments = RequirementAssessmentService(session, service, None)
    try:
        dossier["assessment"] = (
            assessments.get(project_id, assessment_id, operation="EXPORT")
            if assessment_id
            else assessments.latest(project_id, operation="EXPORT")
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return HTMLResponse(render_dossier_report(dossier, detail))
