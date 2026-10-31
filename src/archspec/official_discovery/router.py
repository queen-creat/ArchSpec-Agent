"""自动查询官方公告；项目资料仅转为可解释的主题关键词。"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from archspec.config import settings
from archspec.dossier.router import get_dossier_service
from archspec.dossier.service import ProjectDossierService
from archspec.official_discovery.free_search import load_free_search_provider
from archspec.official_discovery.service import DiscoveryService
from archspec.official_discovery.tavily import TavilyConfigurationError, load_tavily_provider
from archspec.official_documents.acquisition_router import get_acquisition_service
from archspec.official_documents.jobs import AcquisitionService
from archspec.projects.service import ProjectNotFound

router = APIRouter(prefix="/api/official-discovery", tags=["官方规范发现"])


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(default="", max_length=500)
    project_id: str | None = Field(default=None, min_length=1, max_length=100)
    refresh: bool = False
    # 默认关闭以兼容旧接口；工作台显式开启自动正文准备。
    auto_acquire: bool = False
    public_titles: list[str] = Field(default_factory=list, max_length=3)
    # 新工作台显式采用后端流程；所有旧请求和响应保持原合同。
    query_flow: StrictBool = False

    @model_validator(mode="after")
    def flow_requires_body_preparation(self):
        from archspec.official_discovery.public_queries import validated_public_titles

        validated_public_titles(self.public_titles)
        if self.query_flow and not self.auto_acquire:
            raise ValueError("后端查询流程必须开启正文准备")
        return self


def get_discovery_service():
    try:
        external = load_tavily_provider()
        if external is None:
            external = load_free_search_provider()
    except TavilyConfigurationError as error:
        raise HTTPException(503, str(error)) from None
    return DiscoveryService(settings.data_dir / "official-discovery", external_provider=external)


@router.post("/search")
def discover(
    request: DiscoveryRequest,
    background: BackgroundTasks,
    service: Annotated[DiscoveryService, Depends(get_discovery_service)],
    dossier: Annotated[ProjectDossierService, Depends(get_dossier_service)],
    acquisition: Annotated[AcquisitionService, Depends(get_acquisition_service)],
    operation_id: Annotated[str | None, Header(alias="X-Archspec-Operation-Id")] = None,
):
    try:
        packet = dossier.build(request.project_id) if request.project_id else {}
        topics = packet.get("topics", [])
        # 地域取自已核项目，不接受客户端另传城市或私有地址。
        region_options = {}
        if packet.get("project"):
            from archspec.official_documents.lookup import prepare_lookup_plan
            plan = prepare_lookup_plan(request.query, project_region=packet["project"])
            if plan.region is not None:
                region_options["region"] = plan.region
        if request.public_titles:
            region_options["public_titles"] = request.public_titles
        if request.query_flow:
            from archspec.official_discovery.query_flow import OfficialQueryFlow
            flow = OfficialQueryFlow(service, acquisition)
            ticket, created = flow.start(request.query, project_id=request.project_id,
                refresh=request.refresh, topics=topics, operation_id=operation_id, **region_options)
            if created:
                background.add_task(flow.execute, ticket["query_flow"]["id"])
            return JSONResponse(ticket, status_code=202, background=background)
        run = getattr(service, "run_for_acquisition", service.run) if request.auto_acquire else service.run
        result = run(request.query, topics=topics, refresh=request.refresh, **region_options)
        # 地方页面仍是独立线索，没有FULL_TEXT附件，不能创建看似在准备正文的空任务。
        if (request.auto_acquire
                and result.get("local_coverage") not in {"ENTRY_ONLY", "CLUES_ONLY"}
                and not ("external_clues" in result and not result.get("results"))):
            # 附加到本次响应，而非修改已经保存的历史公告快照。
            limitations = [
                "公开附件的正文准备情况见任务状态；处理完成仍待核验，也没有调用大模型"
                if line.startswith("附件仅发现链接") else line
                for line in result.get("limitations", [])
            ]
            result = {**result, "limitations": limitations,
                      "acquisition": acquisition.submit(result, refresh=request.refresh)}
        return result
    except ProjectNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/runs/{run_id}")
def history(
    run_id: str,
    service: Annotated[DiscoveryService, Depends(get_discovery_service)],
    acquisition: Annotated[AcquisitionService, Depends(get_acquisition_service)],
    project_id: str | None = None,
    background: BackgroundTasks = None,
):
    try:
        from archspec.official_discovery.query_flow import OfficialQueryFlow
        if getattr(service, "cache_dir", None) is not None:
            flow = OfficialQueryFlow(service, acquisition)
            # 单一入口区分真缺失与验证失败，避免exists/get各自重复审计云端记录。
            result = flow.get_if_present(run_id, project_id=project_id, background=background)
            if result is not None:
                return result
        return service.get(run_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
