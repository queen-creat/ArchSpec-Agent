"""只通过已有任务编号查进度或重试，不提供任意网址下载接口。"""

import inspect
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from archspec.config import settings
from archspec.official_documents.jobs import AcquisitionService
from archspec.official_documents.registry import OfficialSourceRegistry
from archspec.official_documents.service import OfficialDocumentService

router = APIRouter(prefix="/api/official-acquisitions", tags=["官方正文准备"])
_runtime_lock = RLock()


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_acquisition_service(data_dir, *, gate=None, registry=None):
    from archspec.official_documents.source_context import resolve_registry
    registry = resolve_registry(data_dir / "official-cache", registry)
    documents = OfficialDocumentService(data_dir / "official-cache", registry=registry)
    return AcquisitionService(data_dir / "official-acquisitions", registry, documents, gate=gate)


def get_runtime(app):
    """把工人挂在应用上，启动、请求、退出都使用同一个实例。"""
    with _runtime_lock:
        runtime = getattr(app.state, "acquisition_service", None)
        gate = getattr(app.state, "source_write_gate", None)
        registry = getattr(app.state, "official_source_registry", None)
        from archspec.persistence.runtime_bindings import (
            RuntimeBindingsUnavailable,
            current_runtime,
        )
        selected = current_runtime()
        data_dir = settings.data_dir
        if selected is not None:
            if registry is not None and registry is not selected.registry:
                raise RuntimeBindingsUnavailable("正文工人来源与选定运行配置不同")
            registry, data_dir = selected.registry, selected.cache_dir.parent
        if registry is not None and not isinstance(registry, OfficialSourceRegistry):
            raise RuntimeError("服务器来源登记实例无效，未回退本地")
        created = runtime is None
        if created:
            # 旧测试/集成工厂只有 data_dir 参数；先看签名，不能吞掉工厂自身的 TypeError。
            parameters = inspect.signature(create_acquisition_service).parameters
            accepts_gate = "gate" in parameters or any(
                value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters.values()
            )
            accepts_registry = "registry" in parameters or any(
                value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters.values())
            if registry is not None and not accepts_registry:
                raise RuntimeError("正文工厂不能忽略服务器已配置的来源登记")
            options = {}
            if gate is not None and accepts_gate:
                options["gate"] = gate
            if registry is not None:
                options["registry"] = registry
            runtime = create_acquisition_service(data_dir, **options)
        if registry is not None and (getattr(runtime, "registry", None) is not registry
                or getattr(getattr(runtime, "documents", None), "registry", None) is not registry):
            raise RuntimeError("正文工人来源登记与服务器配置不同，未回退本地")
        if selected is not None and getattr(runtime, "state_repository", None) is not selected.acquisition_state:
            raise RuntimeBindingsUnavailable("正文工人准备状态与选定服务配置不同")
        if gate is not None and isinstance(runtime, AcquisitionService):
            runtime.bind_gate(gate)
        if created:
            # 登记和停写门均核齐后再发布实例，失败不缓存错后端工人。
            app.state.acquisition_service = runtime
        return runtime


def get_acquisition_service(request: Request):
    return get_runtime(request.app)


@router.get("/{batch_id}")
def get_batch(
    batch_id: str,
    service: Annotated[AcquisitionService, Depends(get_acquisition_service)],
):
    try:
        return service.get(batch_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    service: Annotated[AcquisitionService, Depends(get_acquisition_service)],
    request: RetryRequest | None = None,
):
    try:
        return service.retry(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
