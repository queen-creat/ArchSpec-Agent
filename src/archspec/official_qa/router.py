"""本地问答与明确同意的模型问答分开；不隐式触发官方下载。"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from archspec.config import settings
from archspec.database import get_session
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.official_documents.service import OfficialDocumentService
from archspec.official_qa.model_config import (
    ModelConfigurationError,
    create_budget_gateway,
    load_model_configuration,
)
from archspec.official_qa.model_layer import ModelQuestionRequest, OfficialModelQAService
from archspec.official_qa.project_conditions import (
    ProjectConditionChanged,
    ProjectConditionProvider,
)
from archspec.official_qa.question_plan import thaw_value
from archspec.official_qa.schemas import QuestionRequest
from archspec.official_qa.service import OfficialQAService
from archspec.official_qa.task_flow import (
    QATaskFlow,
    legacy_condition_mode,
    operation_metadata,
    public_task,
)
from archspec.official_qa.task_repository import TaskConflict
from archspec.projects.repository import ProjectRepository
from archspec.projects.service import ProjectNotFound, ProjectService
from archspec.standard_governance.compatibility import ContentUseDenied, LegacyAdmissionAdapter

router = APIRouter(prefix="/api/official-qa", tags=["官方出处本地问答"])


def get_service(session: Annotated[Session, Depends(get_session)]):
    projects = ProjectService(ProjectRepository(session))
    return OfficialQAService(OfficialDocumentService(settings.data_dir / "official-cache"),
                             validate_project=projects.get,
                             project_conditions=ProjectConditionProvider(projects, DocumentRepository(session),
                                 DocumentChunkRepository(session), settings.data_dir / "uploads"))


def get_model_service(session):
    """仅在请求同意校验后创建；状态查询和旧本地入口不会走到这里。"""
    try:
        configuration = load_model_configuration()
    except ModelConfigurationError as error:
        raise HTTPException(503, {"code": "LLM_CONFIGURATION_INVALID",
                                  "message": "本机模型配置不符合本轮开发范围，请检查配置"}) from error
    if configuration is None:
        raise HTTPException(503, {"code": "LLM_NOT_CONFIGURED",
                                  "message": "服务端尚未配置官方问答模型密钥"})
    try:
        gateway = create_budget_gateway(configuration, settings.data_dir)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise HTTPException(503, {"code": "MODEL_RUNTIME_UNAVAILABLE",
                                  "message": "模型开发费用记录暂不可用，请检查本机状态"}) from error
    projects = ProjectService(ProjectRepository(session))
    documents = OfficialDocumentService(settings.data_dir / "official-cache",
        governance=LegacyAdmissionAdapter(session, settings.data_dir))
    return OfficialModelQAService(documents, gateway, validate_project=projects.get,
        project_conditions=ProjectConditionProvider(projects, DocumentRepository(session),
            DocumentChunkRepository(session), settings.data_dir / "uploads"))


@router.get("/model-status")
def model_status(response: Response):
    """只说明配置可用性，不测试连接，不把密钥或预算文件交给浏览器。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        configured = load_model_configuration() is not None
    except ModelConfigurationError:
        configured = False
    return {"configured": configured}


@router.get("/project-conditions")
def project_condition_preview(
    response: Response, service: Annotated[OfficialQAService, Depends(get_service)],
    project_id: Annotated[str, Query(min_length=1, max_length=100)],
):
    """问答前只读既有候选；仍需主动勾选，并在原问答提交时重新resolve核对。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        if service.project_conditions is None:
            raise HTTPException(503, "当前项目条件暂不可读取，请手动补充")
        return {"version": "PROJECT_CONDITION_PREVIEW_V1", **service.project_conditions.inspect(project_id)}
    except ProjectNotFound as error:
        raise HTTPException(404, "当前项目不存在，请重新选择") from error
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "项目资料条件暂不可读取，请稍后重试或手动补充") from error


@router.post("/model")
def model_answer(request: ModelQuestionRequest, response: Response, http_request: Request,
                 session: Annotated[Session, Depends(get_session)]):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        # 请求同意和操作认领均在模型工厂之前；重复收据不再创建网关或收费。
        metadata = operation_metadata(http_request.headers)
        if metadata is not None:
            if request.project_id:
                ProjectService(ProjectRepository(session)).get(request.project_id)
            return QATaskFlow(session).run(request, "MODEL", metadata,
                                          lambda: get_model_service(session))
        return get_model_service(session).answer(request)
    except TaskConflict as error:
        raise HTTPException(409, {"code": error.code, "message": error.reason}) from error
    except ProjectNotFound as error:
        raise HTTPException(404, "当前项目不存在，请重新选择") from error
    except ProjectConditionChanged as error:
        raise HTTPException(409, "项目条件已变化，请重新查询并确认，或手动补充条件") from error
    except ContentUseDenied as error:
        raise HTTPException(403, {"code": "CONTENT_USE_NOT_AUTHORIZED",
                                  "message": "资料使用条件已变化，本轮模型问答已停止"}) from error
    except LookupError as error:
        raise HTTPException(404, "该官方来源尚未登记或取得") from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(422, "模型开发预算、问题参数或固定资料无法安全核对，请检查本机状态") from error
    except (RuntimeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "模型问答暂不可用，本轮没有自动重试") from error


@router.post("")
def answer(request: QuestionRequest, response: Response, http_request: Request,
           service: Annotated[OfficialQAService, Depends(get_service)],
           session: Annotated[Session, Depends(get_session)]):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        metadata = operation_metadata(http_request.headers)
        if metadata is not None:
            if request.project_id:
                ProjectService(ProjectRepository(session)).get(request.project_id)
            return QATaskFlow(session).run(request, "LOCAL", metadata, lambda: service)
        return service.answer(request)
    except TaskConflict as error:
        raise HTTPException(409, {"code": error.code, "message": error.reason}) from error
    except ProjectNotFound as error:
        raise HTTPException(404, "当前项目不存在，请重新选择") from error
    except ProjectConditionChanged as error:
        raise HTTPException(409, "项目条件已变化，请重新查询并确认，或手动补充条件") from error
    except LookupError as error:
        raise HTTPException(404, "该官方来源尚未登记或取得") from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(422, "官方缓存或问题参数无法安全核对，请检查资料状态") from error

    except (RuntimeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "问答任务记录暂不可用，请检查本机升级状态") from error


@router.get("/tasks")
def task_history(response: Response, session: Annotated[Session, Depends(get_session)],
                 project_id: str | None = None,
                 limit: Annotated[int, Query(ge=1, le=50)] = 20,
                 offset: Annotated[int, Query(ge=0, le=10000)] = 0):
    """历史列表只回当前项目摘要，不携带条件、回答、提案、原文或模型授权。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return QATaskFlow(session).repository.list_summaries(project_id, limit=limit, offset=offset)
    except LookupError as error:
        raise HTTPException(404, "当前项目不存在，请重新选择") from error
    except (ValueError, RuntimeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "历史问题暂不可读取，请稍后刷新列表") from error


@router.get("/tasks/{task_id}/reading-positions")
def task_reading_positions(task_id: str, response: Response,
                          session: Annotated[Session, Depends(get_session)],
                          service: Annotated[OfficialQAService, Depends(get_service)],
                          project_id: str | None = None):
    """只读已提交操作的固定位置；原文仍需显式点击后走当前固定读取门。"""
    from archspec.official_qa.reading_receipts import load_positions
    from archspec.persistence.business_files import BusinessFilesUnavailable

    response.headers["Cache-Control"] = "private, no-store"
    try:
        task, operation = QATaskFlow(session).repository.reading_operation(task_id, project_id)
        if task is None:
            raise HTTPException(404, "当前项目下未找到该问答任务")
        return load_positions(service.documents.cache_dir.parent, task, operation)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(422, "本题出处位置记录无法核对，未重放问答") from error
    except (BusinessFilesUnavailable, RuntimeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "本题出处位置暂不可读取，未自动重试") from error


@router.get("/tasks/{task_id}")
def task_status(task_id: str, response: Response,
                session: Annotated[Session, Depends(get_session)],
                project_id: str | None = None):
    """只恢复当前归属的任务状态，不执行问答/下载/模型，不重放旧规范原文。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        flow = QATaskFlow(session)
        task = flow.repository.get(task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(404, "当前项目下未找到该问答任务")
        if project_id:
            ProjectService(ProjectRepository(session)).get(project_id)
        task = flow.get(task_id)
        view = public_task(task)
        if view["condition_mode"] == "UNVERIFIED":
            from archspec.persistence.runtime_bindings import current_runtime

            selected = current_runtime()
            root = selected.cache_dir.parent if selected is not None else settings.data_dir
            view["condition_mode"] = legacy_condition_mode(task, flow.repository, root)
        return {"qa_task": view,
                "request": QuestionRequest.model_validate(
                    thaw_value(task.request)).model_dump(exclude_none=True)}
    except ProjectNotFound as error:
        raise HTTPException(404, "当前项目下未找到该问答任务") from error
    except ValueError as error:
        raise HTTPException(422, "问答任务编号不合法") from error
    except (RuntimeError, sqlite3.Error, SQLAlchemyError) as error:
        raise HTTPException(503, "问答任务记录暂不可用，请检查本机升级状态") from error
