"""ArchSpec Agent 的程序入口。"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from archspec.agent_runs.router import router as agent_runs_router
from archspec.assisted_labeling.router import router as assisted_labeling_router
from archspec.config import settings
from archspec.database import (
    apply_sqlite_compatibility_migrations,
    create_legacy_tables,
    create_qa_task_tables,
    engine,
)
from archspec.documents.router import router as documents_router
from archspec.dossier.router import router as dossier_router
from archspec.evaluation.router import router as evaluation_router
from archspec.health.router import router as health_router
from archspec.official_discovery.router import router as official_discovery_router
from archspec.official_documents.acquisition_router import (
    get_runtime,
)
from archspec.official_documents.acquisition_router import (
    router as official_acquisitions_router,
)
from archspec.official_documents.router import router as official_documents_router
from archspec.official_documents.source_context import SourceRegistryContextMiddleware
from archspec.official_evaluations.router import router as official_evaluations_router
from archspec.official_qa.router import router as official_qa_router
from archspec.persistence.acquisition_state import AcquisitionStateUnavailable
from archspec.persistence.business_files import BusinessFilesUnavailable
from archspec.persistence.gate_middleware import SourceWriteGateMiddleware
from archspec.persistence.router import router as persistence_router
from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable, runtime_scope
from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
from archspec.persistence.source_registry import SourceRegistryUnavailable
from archspec.persistence.source_write_gate import SourceWriteGate
from archspec.project_materials.router import router as project_materials_router
from archspec.projects.router import router as projects_router
from archspec.regulation_search.router import router as regulation_search_router
from archspec.reviews.router import router as reviews_router
from archspec.search.router import router as search_router
from archspec.standard_catalog.router import router as standard_catalog_router
from archspec.standard_governance.compatibility import ContentUseDenied
from archspec.standard_governance.router import router as standard_governance_router
from archspec.standards.router import router as standards_router


def application_runtime(application):
    """已选云端必须保留实际绑定；丢失时拒绝请求和worker，不回退本地。"""
    selected = getattr(application.state, "runtime_bindings", None)
    if getattr(application.state, "persistence_backend", "LOCAL") == "REMOTE":
        generation = getattr(application.state, "cloud_generation", None)
        if selected is None or generation is None or generation.closed or generation.closing:
            raise RuntimeBindingsUnavailable("云端运行绑定缺失或代次已关闭，未回退本地")
    return selected


@asynccontextmanager
async def lifespan(application):
    # 工人构造/启动失败也必须走同一个退出路径；先退出运行Session再释放连接。
    acquisition = None
    generation = getattr(application.state, "cloud_generation", None)
    try:
        with runtime_scope(application_runtime(application)):
            acquisition = get_runtime(application)
            if generation is not None:
                generation.attach_acquisition(acquisition)
            acquisition.start()
            yield
    finally:
        original_error = sys.exception()
        def stop_and_close():
            try:
                if acquisition is not None:
                    acquisition.stop()
            finally:
                if generation is not None:
                    generation.close()
        try:
            await asyncio.to_thread(stop_and_close)
        except Exception:  # 清理失败保留原启动/请求异常及安全说明。
            if original_error is None:
                raise
            original_error.add_note("服务退出清理未完整确认，恢复缓存保留供核验")


app = FastAPI(
    title="ArchSpec Agent",
    description="面向建筑设计工作的规范检索与审查辅助平台",
    version="0.1.0",
    lifespan=lifespan,
)

# 单进程所有API与正文worker共用一个门；在lifespan启动worker前建立，不隐式恢复。
app.state.source_write_gate = SourceWriteGate()
app.add_middleware(SourceWriteGateMiddleware, gate=app.state.source_write_gate)
# 同一请求内包括QA/model/治理适配构造；没有配置时保持原本地路径。
app.add_middleware(SourceRegistryContextMiddleware,
    registry_provider=lambda: getattr(app.state, "official_source_registry", None),
    cache_dir_provider=lambda: settings.data_dir / "official-cache",
    runtime_provider=lambda: application_runtime(app))

# Router（路由器）负责把网址分发给对应的 Python 函数。
app.include_router(health_router)
app.include_router(persistence_router)
app.include_router(official_discovery_router)
from archspec.official_documents.official_fetch import check_configuration
from archspec.official_documents.official_fetch import router as official_fetch_router

check_configuration()
app.include_router(official_fetch_router)
app.include_router(official_documents_router)
app.include_router(official_acquisitions_router)
app.include_router(official_evaluations_router)
app.include_router(official_qa_router)
app.include_router(agent_runs_router)
app.include_router(projects_router)
app.include_router(dossier_router)
app.include_router(project_materials_router)
app.include_router(regulation_search_router)
app.include_router(documents_router)
app.include_router(search_router)
app.include_router(standard_catalog_router)
app.include_router(evaluation_router)
app.include_router(assisted_labeling_router)
app.include_router(standards_router)
app.include_router(reviews_router)
app.include_router(standard_governance_router)


@app.exception_handler(BusinessFilesUnavailable)
async def business_files_unavailable(request, error):
    """业务文件持久化失败单独显示503，不冒充本地保存成功或吞为查询冲突。"""
    return JSONResponse(status_code=503, content={"detail": {
        "code": "BUSINESS_FILES_UNAVAILABLE", "message": "业务文件无法完整保存或恢复，请查看存储状态。",
    }})


@app.exception_handler(AcquisitionStateUnavailable)
async def acquisition_state_unavailable(request, error):
    """损坏任务/驱动故障明确503，不跳过记录或回退本地队列。"""
    return JSONResponse(status_code=503, content={"detail": {
        "code": "ACQUISITION_STATE_UNAVAILABLE", "message": "正文准备状态暂不可用，请检查存储状态。",
    }})


@app.exception_handler(RuntimeBindingsUnavailable)
async def runtime_bindings_unavailable(request, error):
    """组合入口拒绝不同库或不完整状态；不回显提供方异常或启用本地。"""
    return JSONResponse(status_code=503, content={"detail": {
        "code": "SERVICE_RUNTIME_UNAVAILABLE", "message": "服务运行状态无法核对，请查看存储状态。",
    }})


@app.exception_handler(SnapshotCatalogUnavailable)
async def snapshot_catalog_unavailable(request, error):
    """当前快照库/固定字节异常明确503，不回退旧latest或回显路径。"""
    return JSONResponse(status_code=503, content={"detail": {
        "code": "SNAPSHOT_CATALOG_UNAVAILABLE", "message": "当前正文快照无法核对，请检查存储状态。",
    }})


@app.exception_handler(SourceRegistryUnavailable)
async def source_registry_unavailable(request, error):
    """来源库不可核验时显示具体状态，不回退旧文件或回显SQL细节。"""
    return JSONResponse(status_code=503, content={"detail": {
        "code": "SOURCE_REGISTRY_UNAVAILABLE",
        "message": "来源登记暂不可用，请检查存储状态；本次没有回退本地。",
    }})


@app.exception_handler(ContentUseDenied)
async def content_use_denied(request, error):
    """旧接口也返回可解释的拒绝，不把限制误显示为服务故障或泄漏正文。"""
    return JSONResponse(status_code=403, content={"detail": {
        "code": error.code, "message": str(error),
    }})

web_dir = Path(__file__).parent / "web"
app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")


@app.get("/evaluation-center.html", include_in_schema=False)
def evaluation_center() -> FileResponse:
    """给建筑学教师使用的简化中文页面。"""
    return FileResponse(web_dir / "evaluation-center.html")


@app.get("/project-workbench.html", include_in_schema=False)
def project_workbench() -> FileResponse:
    """给建筑设计人员使用的中文项目工作台。"""
    return FileResponse(web_dir / "project-workbench.html")


@app.get("/standard-catalog-admin.html", include_in_schema=False)
def standard_catalog_admin() -> FileResponse:
    """给知识库管理员使用的规范题录状态页面。"""
    return FileResponse(web_dir / "standard-catalog-admin.html")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """打开系统时直接进入项目工作台，避免用户记网址。"""
    return FileResponse(web_dir / "project-workbench.html")

# LOCAL保留原升级路径；REMOTE已核迁入的控制库不得被启动隐式补空。
from archspec.persistence.remote_startup import current_startup

if current_startup() is None:
    create_legacy_tables(engine)
    apply_sqlite_compatibility_migrations(engine)
    # LOCAL问答已是默认流程，启动时补齐任务表；不在请求中临时DDL。
    # 已选REMOTE仍由上面的已核迁移分支恢复，不经本地建表入口。
    create_qa_task_tables(engine)
