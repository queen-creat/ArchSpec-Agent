"""DeepSeek 证据型 Agent REST API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from archspec.agent_runs.guard import CitationGuard
from archspec.agent_runs.llm import DeepSeekChatGateway
from archspec.agent_runs.models import AgentRunDetail
from archspec.agent_runs.orchestrator import AgentOrchestrator
from archspec.agent_runs.repository import AgentRunRepository
from archspec.agent_runs.schemas import (
    AgentFindingDecisionRequest,
    AgentRunCreateRequest,
    AgentRunDetailResponse,
    AgentRunResponse,
    AgentStatusResponse,
)
from archspec.agent_runs.tools import AgentToolRegistry
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.config import settings
from archspec.database import get_session
from archspec.documents.repository import DocumentRepository
from archspec.projects.repository import ProjectRepository
from archspec.projects.service import ProjectNotFound, ProjectService
from archspec.regulation_search.router import get_regulation_search_service
from archspec.standard_governance.compatibility import LegacyAdmissionAdapter

router = APIRouter(tags=["DeepSeek 证据型 Agent"])


def get_agent_repository(
    session: Annotated[Session, Depends(get_session)],
) -> AgentRunRepository:
    return AgentRunRepository(session)


def get_agent_orchestrator(
    session: Annotated[Session, Depends(get_session)],
) -> AgentOrchestrator:
    api_key = settings.deepseek_api_key
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "LLM_NOT_CONFIGURED",
                "message": "服务端尚未配置 DeepSeek API Key",
            },
        )
    documents = DocumentRepository(session)
    projects = ProjectService(
        ProjectRepository(session),
        documents,
        AssistedLabelingRepository(session),
    )
    from archspec.persistence.runtime_bindings import current_runtime
    selected = current_runtime()
    options = ({"max_tokens": 1024, "thinking_disabled": True, "trust_env": False}
               if selected is not None else {})
    gateway = DeepSeekChatGateway(api_key, base_url=settings.deepseek_base_url,
        model=settings.deepseek_model, timeout_seconds=settings.deepseek_timeout_seconds, **options)
    if selected is not None:
        gateway = selected.wrap_gateway(gateway)
    return AgentOrchestrator(
        AgentRunRepository(session),
        gateway,
        AgentToolRegistry(
            projects,
            documents,
            get_regulation_search_service(session),
        ),
        CitationGuard(),
        model=settings.deepseek_model,
        max_steps=settings.agent_max_steps,
        max_tool_calls=settings.agent_max_tool_calls,
        max_material_characters=settings.agent_max_material_characters,
    )


@router.get("/api/agent/status", response_model=AgentStatusResponse)
def agent_status() -> AgentStatusResponse:
    return AgentStatusResponse(
        configured=settings.deepseek_api_key is not None,
        provider="DeepSeek",
        model=settings.deepseek_model,
        max_steps=settings.agent_max_steps,
        max_tool_calls=settings.agent_max_tool_calls,
        material_character_limit=settings.agent_max_material_characters,
        notice=(
            "项目资料只有在用户明确确认后才会以有限摘录发送给 DeepSeek；"
            "本系统不替代专业判断。"
        ),
    )


@router.post(
    "/api/projects/{project_id}/agent-runs",
    response_model=AgentRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_run(
    project_id: str,
    request: AgentRunCreateRequest,
    orchestrator: Annotated[AgentOrchestrator, Depends(get_agent_orchestrator)],
) -> AgentRunDetailResponse:
    if request.include_project_materials and not request.material_sharing_confirmed:
        raise HTTPException(status_code=422, detail="发送项目资料前必须明确确认")
    try:
        detail = orchestrator.run(
            project_id,
            request.goal,
            request.include_project_materials,
            request.material_sharing_confirmed,
        )
        return AgentRunDetailResponse.from_record(_required(detail), admission=LegacyAdmissionAdapter(orchestrator.repository.session), historical=False)
    except (ValueError, ProjectNotFound) as error:
        code = 404 if isinstance(error, ProjectNotFound) else 422
        raise HTTPException(status_code=code, detail=str(error)) from error


@router.get(
    "/api/projects/{project_id}/agent-runs",
    response_model=list[AgentRunResponse],
)
def list_agent_runs(
    project_id: str,
    repository: Annotated[AgentRunRepository, Depends(get_agent_repository)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[AgentRunResponse]:
    return [
        AgentRunResponse.from_record(item, admission=LegacyAdmissionAdapter(repository.session))
        for item in repository.list_for_project(project_id, limit)
    ]


@router.get("/api/agent-runs/{run_id}", response_model=AgentRunDetailResponse)
def get_agent_run(
    run_id: str,
    repository: Annotated[AgentRunRepository, Depends(get_agent_repository)],
) -> AgentRunDetailResponse:
    detail = repository.get_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")
    return AgentRunDetailResponse.from_record(detail, admission=LegacyAdmissionAdapter(repository.session))


@router.post(
    "/api/agent-runs/{run_id}/retry",
    response_model=AgentRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def retry_agent_run(
    run_id: str,
    orchestrator: Annotated[AgentOrchestrator, Depends(get_agent_orchestrator)],
) -> AgentRunDetailResponse:
    try:
        return AgentRunDetailResponse.from_record(orchestrator.retry(run_id), admission=LegacyAdmissionAdapter(orchestrator.repository.session), historical=False)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put(
    "/api/agent-runs/{run_id}/findings/{finding_index}/decision",
    response_model=AgentRunDetailResponse,
)
def decide_agent_finding(
    run_id: str,
    finding_index: int,
    request: AgentFindingDecisionRequest,
    repository: Annotated[AgentRunRepository, Depends(get_agent_repository)],
) -> AgentRunDetailResponse:
    try:
        repository.save_decision(
            run_id,
            finding_index,
            request.decision,
            request.comment.strip() if request.comment else None,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    detail = repository.get_detail(run_id)
    return AgentRunDetailResponse.from_record(_required(detail), admission=LegacyAdmissionAdapter(repository.session))


def _required(detail: AgentRunDetail | None) -> AgentRunDetail:
    if detail is None:
        raise LookupError("Agent 运行不存在")
    return detail
