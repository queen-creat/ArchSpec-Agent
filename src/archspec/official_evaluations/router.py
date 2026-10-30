"""只运行服务端固定题库；客户端不得指定路径、选题或答案。"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from archspec.config import settings
from archspec.official_documents.router import get_service
from archspec.official_documents.service import OfficialDocumentService
from archspec.official_evaluations.service import EvaluationBusyError, OfficialEvaluationService

router = APIRouter(prefix="/api/official-retrieval-evaluations", tags=["官方正文搜索检查"])


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_evaluation_service(documents: Annotated[OfficialDocumentService, Depends(get_service)]):
    # 网页采用已逐页核对的独立绑定版本；不改历史调用默认，也不允许浏览器选题。
    dataset_path = Path(__file__).parent / "fixtures" / "official-cases-v2.json"
    return OfficialEvaluationService(settings.data_dir, documents, dataset_path)


@router.get("/status")
def status(service: Annotated[OfficialEvaluationService, Depends(get_evaluation_service)]):
    return service.status()


@router.post("/runs")
def run_compare(request: RunRequest,
                service: Annotated[OfficialEvaluationService, Depends(get_evaluation_service)]):
    try:
        return service.run_compare()
    except EvaluationBusyError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(503, "搜索检查结果无法保存，请检查本地存储；未标记通过") from error


@router.get("/runs/{run_id}")
def get_run(run_id: str, service: Annotated[OfficialEvaluationService, Depends(get_evaluation_service)]):
    try:
        return service.get(run_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
