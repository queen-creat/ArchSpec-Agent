"""本地管理员获取官方正文；不允许任意网址，不自动批准证据。"""

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from archspec.config import settings
from archspec.official_documents.download_errors import download_failure_message
from archspec.official_documents.metadata_filter import SourceMetadataFilter
from archspec.official_documents.service import OfficialDocumentService
from archspec.persistence.business_files import BusinessFilesUnavailable
from archspec.projects.router import get_project_service
from archspec.projects.service import ProjectNotFound, ProjectService

router = APIRouter(prefix="/api/official-documents", tags=["官方正文待审核"])


class AcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    retrieval_mode: Literal["KEYWORD", "SEMANTIC"] = "KEYWORD"
    lookup_context: StrictBool = False
    lookup_scope: Literal["RAW", "PLANNED"] = "RAW"
    project_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def planned_lookup_requires_context(self):
        if self.retrieval_mode == "SEMANTIC" and self.lookup_scope != "PLANNED":
            raise ValueError("按意思补查须沿当前问题范围查阅")
        if self.lookup_scope == "PLANNED" and not self.lookup_context:
            raise ValueError("按问题筛选查阅范围必须开启查阅说明")
        return self


class RereadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str = Field(min_length=1, max_length=240)
    row_index: int = Field(ge=0, le=100000, strict=True)


class CellRereadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str = Field(min_length=1, max_length=240)
    table_id: str = Field(pattern=r"^table-[1-9][0-9]{0,2}$")
    row: int = Field(ge=1, le=40, strict=True)
    column: int = Field(ge=1, le=40, strict=True)


class UnifiedSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=20)
    ranking_version: Literal["V1", "V2"] = "V1"
    # 可选来源范围先过滤再排名；旧请求省略时保留整个旧行为。
    project_id: str | None = Field(default=None, min_length=1, max_length=100)
    metadata_filter: SourceMetadataFilter | None = None
    retrieval_mode: Literal["KEYWORD", "SEMANTIC"] = "KEYWORD"
    lookup_context: StrictBool = False
    lookup_scope: Literal["RAW", "PLANNED", "RELATED"] = "RAW"
    source_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, min_length=1, max_length=20,
    )

    @model_validator(mode="after")
    def planned_lookup_requires_context(self):
        if self.retrieval_mode == "SEMANTIC" and self.lookup_scope != "PLANNED":
            raise ValueError("按意思补查须沿当前问题范围查阅")
        if self.lookup_scope in {"PLANNED", "RELATED"} and not self.lookup_context:
            raise ValueError("按问题筛选查阅范围必须开启查阅说明")
        return self


def get_service(request: Request = None):
    # 仅服务器实例注入；没有客户端backend或数据库地址参数。
    from archspec.official_documents.source_context import current_registry
    registry = current_registry(settings.data_dir / "official-cache")
    if registry is None and request is not None:
        registry = getattr(request.app.state, "official_source_registry", None)
    if registry is not None:
        from archspec.official_documents.registry import OfficialSourceRegistry
        if not isinstance(registry, OfficialSourceRegistry):
            raise RuntimeError("服务器来源登记实例无效，未回退本地")
    return OfficialDocumentService(settings.data_dir / "official-cache", registry=registry)


def get_quality_service(
    request: UnifiedSearchRequest,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    # 仅请求 V2 时查询门槛；原排序不读取题库或运行评测。
    if request.ranking_version != "V2":
        return None
    from archspec.official_evaluations.service import OfficialEvaluationService

    return OfficialEvaluationService(settings.data_dir, service)


@router.get("")
def sources(service: Annotated[OfficialDocumentService, Depends(get_service)], response: Response,
            include_metadata: bool = False):
    response.headers["Cache-Control"] = "private, no-store"
    return service.list_sources(include_metadata=True) if include_metadata else service.list_sources()


@router.post("/search")
def search_all(
    request: UnifiedSearchRequest,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
    quality: Annotated[object, Depends(get_quality_service)],
    projects: Annotated[ProjectService, Depends(get_project_service)] = None,
):
    try:
        if request.ranking_version == "V2" and not quality.status()["quality_gate_passed"]:
            raise HTTPException(409, "搜索检查已过期或未通过，请用原排序或重新运行固定题库检查")
        options = {"ranking_version": request.ranking_version}
        if request.retrieval_mode == "SEMANTIC":
            options["retrieval_mode"] = "SEMANTIC"
        plan = None
        scope = request.metadata_filter
        if request.lookup_scope in {"PLANNED", "RELATED"} or request.project_id:
            from archspec.official_documents.lookup import prepare_lookup_plan
            if request.project_id:
                project = projects.get(request.project_id)
                plan = prepare_lookup_plan(request.query, project_region={
                    "city": project.city, "province": project.province}, source_ids=request.source_ids)
            else:
                plan = prepare_lookup_plan(request.query, source_ids=request.source_ids)
            # 同一计划约束检索前地域与检索后说明，不重复在UI猜地名。
            # 主动关联仍核项目身份，但不把推导地域加到用户明确的缓存范围上。
            if request.lookup_scope != "RELATED" and plan.region is not None:
                planned_scope = SourceMetadataFilter(region=plan.region)
                if scope is None:
                    scope = planned_scope
                elif scope.region is None:
                    scope = scope.model_copy(update={"region": plan.region})
                elif scope.resolved_region != planned_scope.resolved_region:
                    raise ValueError("问题地域与显式筛选范围冲突，请统一地域后再查")
        if scope is not None:
            options["metadata_filter"] = scope
        retrieval_query = request.query
        if plan is not None:
            # 项目RAW沿旧词面/概念合同；只有显式PLANNED消费条号与来源前置范围。
            retrieval_query = (plan.retrieval_query if request.lookup_scope == "PLANNED"
                               else plan.legacy_retrieval_query)
            if request.lookup_scope == "PLANNED" and plan.question_plan.explicit_source_scope is not None:
                options["question_plan"] = plan.question_plan
        if plan is not None and request.lookup_scope == "PLANNED" and request.retrieval_mode == "KEYWORD":
            parallel = plan.parallel_queries
            if parallel or plan.question_plan.query_focus is not None:
                # 与问答/后台流程共用原问绑定的完整条文优先择取。
                options["lookup_plan"] = plan
        result = service.search_all(retrieval_query, request.top_k, request.source_ids, **options)
        if plan is not None:
            result.update(query=request.query, retrieval_query=retrieval_query)
        # 后台可能在检查之后刚好更新正文。返回前复核，不能拿旧成绩批准新资料。
        if request.ranking_version == "V2" and not quality.status()["quality_gate_passed"]:
            raise HTTPException(409, "检索期间正文或题库已变化，搜索检查已过期，请改用原排序")
        if request.lookup_context:
            from archspec.official_documents.lookup import attach_lookup
            result = attach_lookup(request.query, result, plan=plan, source_ids=request.source_ids,
                                   related_only=request.lookup_scope == "RELATED")
            if request.lookup_scope in {"PLANNED", "RELATED"} or request.project_id:
                from archspec.official_documents.network_policy import network_decision
                result["lookup"] = {
                    **result["lookup"], "scope": "RELATED" if request.lookup_scope == "RELATED" else "PLANNED",
                    "network": network_decision(request.query, result["lookup"]),
                }
        return result
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{source_id}")
def get_snapshot(source_id: str, service: Annotated[OfficialDocumentService, Depends(get_service)]):
    try:
        result = service.get(source_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "正文缓存无法安全读取，请检查资料状态") from error
    if result is None:
        raise HTTPException(404, "这份来源尚未获取正文")
    return result


@router.post("/{source_id}/acquire")
def acquire(
    source_id: str,
    request: AcquireRequest,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    try:
        return service.acquire(source_id, refresh=request.refresh)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(502, download_failure_message(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/{source_id}/search")
def search(
    source_id: str,
    request: SearchRequest,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
    projects: Annotated[ProjectService, Depends(get_project_service)] = None,
):
    try:
        plan = None
        if request.lookup_scope == "PLANNED":
            from archspec.official_documents.lookup import prepare_lookup_plan

            project = projects.get(request.project_id) if request.project_id else None
            region = {"city": project.city, "province": project.province} if project else None
            plan = prepare_lookup_plan(request.query, project_region=region, source_ids=[source_id])
            options = {"ranking_version": "V1"}
            if request.retrieval_mode == "SEMANTIC":
                options["retrieval_mode"] = "SEMANTIC"
            if plan.region is not None:
                options["metadata_filter"] = SourceMetadataFilter(region=plan.region)
            if plan.question_plan.explicit_source_scope is not None:
                options["question_plan"] = plan.question_plan
            # 固定单书也复用共同读取/地域/许可门；URL范围不因原问提及另一书而扩大。
            result = service.search_all(plan.retrieval_query, request.top_k, [source_id], **options)
            result.update(query=request.query, retrieval_query=plan.retrieval_query, source_id=source_id)
            pins = {row["snapshot_id"] for row in result["coverage"]
                    if row.get("source_id") == source_id and row.get("state") == "SEARCHED"}
            if len(pins) == 1:
                result["snapshot_id"] = next(iter(pins))
        else:
            result = service.search(source_id, request.query, request.top_k)
        if request.lookup_context:
            from archspec.official_documents.lookup import attach_lookup
            result = attach_lookup(request.query, result, plan=plan, source_ids=[source_id])
            if plan is not None:
                from archspec.official_documents.network_policy import network_decision

                result["lookup"] = {**result["lookup"], "scope": "PLANNED",
                                    "network": network_decision(request.query, result["lookup"])}
        return result
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{source_id}/snapshots/{snapshot_id}/pages/{page_number}.png")
def page_preview(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    page_number: Annotated[int, Path(ge=1, le=200)],
    service: Annotated[OfficialDocumentService, Depends(get_service)],
    quality: Literal["preview", "reading"] = "preview",
):
    try:
        content = service.page_image(source_id, snapshot_id, page_number, quality=quality)
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/{source_id}/snapshots/{snapshot_id}/original")
def fixed_original(source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    service: Annotated[OfficialDocumentService, Depends(get_service)]):
    try:
        kind, content = service.original_content(source_id, snapshot_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定原件暂不可读取") from error
    suffix, media = ("html", "text/html") if kind == "HTML" else ("pdf", "application/pdf")
    return Response(content, media_type=media, headers={"Cache-Control": "private, no-store",
        "Content-Disposition": f'attachment; filename="{source_id}.{suffix}"',
        "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})


@router.get("/{source_id}/snapshots/{snapshot_id}/book-pages")
def book_pages(
    source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response, service: Annotated[OfficialDocumentService, Depends(get_service)],
    chunk_index: Annotated[int | None, Query(ge=0, le=999999)] = None,
):
    """先核固定书目的真实原件页数；各原页仍独立核对读取权限。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return service.book_pages(source_id, snapshot_id, **({"chunk_index": chunk_index} if chunk_index is not None else {}))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定原页范围暂不可读取") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/index")
def book_index(
    source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response, service: Annotated[OfficialDocumentService, Depends(get_service)],
    query: Annotated[str, Query(max_length=80)] = "",
    offset: Annotated[int, Query(ge=0, le=999999)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """显式读取固定书目索引；导航预览不能替代完整条文或当前权限。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return service.book_index(source_id, snapshot_id, query=query, offset=offset, limit=limit)
    except LookupError as error:
        raise HTTPException(404, "该书固定快照尚未取得，未切换其它版本") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "本书索引无法安全读取，原资料保留") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/find")
def find_in_book(
    source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response, service: Annotated[OfficialDocumentService, Depends(get_service)],
    query: Annotated[str, Query(min_length=1, max_length=80)],
    offset: Annotated[int, Query(ge=0, le=999999)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
):
    """在固定版本已有文字中定位，不触发全库检索、下载、模型或新识别。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return service.find_in_book(source_id, snapshot_id, query=query, offset=offset, limit=limit)
    except LookupError as error:
        raise HTTPException(404, "该书固定快照尚未取得，未改查其它版本") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定书内查找无法安全完成，原资料保留") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/chunks/{chunk_index}")
def fixed_fragment(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    chunk_index: Annotated[int, Path(ge=0, le=999999)],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    """刷新只按固定身份重读；不查询latest、不重排名、不下载或组织新回答。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        chunk_id = f"{source_id}:{snapshot_id}:{chunk_index}"
        context = service.read_context(source_id, snapshot_id, chunk_id)
        fragment = next(row for row in context["fragments"] if row["id"] == chunk_id)
        result = {key: context[key] for key in (
            "source_id", "snapshot_id", "title", "standard_code", "source_url",
            "snapshot_fingerprint", "source_metadata", "source_validity", "source_format")}
        result.update(fragment, status="PENDING_REVIEW", evidence_mode="CANDIDATE_ONLY", is_citable=False)
        # 完整原件、固定片段和档案已核；返回前仍核当前读取许可，不复用旧允许结论。
        service._check_reads(source_id, snapshot_id)
        return result
    except LookupError as error:
        raise HTTPException(404, "该固定出处尚未取得，未改用其它版本") from error
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise HTTPException(422, "固定出处无法安全核对，未改用其它条文") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/chunks/{chunk_index}/standard-mentions")
def fixed_standard_mentions(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    chunk_index: Annotated[int, Path(ge=0, le=999999)],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    """显式读取固定原文提及的规范；目标仅为导航候选，不升级为回答依据。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return service.standard_mentions(source_id, snapshot_id, chunk_index)
    except LookupError as error:
        raise HTTPException(404, "该固定条文尚未取得，未改查其它版本") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定条文规范提及无法安全读取，原资料保留") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/chunks/{chunk_index}/context")
def fixed_reader_context(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    chunk_index: Annotated[int, Path(ge=0, le=999999)],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    """普通阅读复用有限补读，不另做检索、识别或回答。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        context = service.read_context(source_id, snapshot_id, f"{source_id}:{snapshot_id}:{chunk_index}")
        # 返回前重核当前许可，不能凭旧阅读结果绕过撤回。
        service._check_reads(source_id, snapshot_id)
        return context
    except LookupError as error:
        raise HTTPException(404, "该固定上下文尚未取得，未切换版本") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定上下文无法安全核对，未组织新回答") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/metadata")
def source_metadata(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
    metadata_id: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
):
    """浏览器只能读取或核对已有身份，不能提交机关、日期或新的下载网址。"""
    try:
        result = service.source_metadata(source_id, snapshot_id, metadata_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "固定来源档案无法核对，请检查资料详情") from error
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get("/{source_id}/snapshots/{snapshot_id}/notice-changes")
def saved_notice_changes(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    """显式只读既存公告线索，不授予维护核验或新的正文使用许可。"""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return service.notice_changes(source_id, snapshot_id)
    except LookupError as error:
        raise HTTPException(404, "未找到该固定正文，未切换版本") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "当前公告调整线索无法核对") from error


@router.get("/{source_id}/snapshots/{snapshot_id}/validity")
def source_validity(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    """只读派生状态，不接受浏览器提交核验声明或外部网址。"""
    try:
        result = service.source_validity(source_id, snapshot_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "当前来源状态无法核对，请检查资料详情") from error
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get("/{source_id}/snapshots/{snapshot_id}/pages/{page_number}/layout")
def page_reading_layout(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    page_number: Annotated[int, Path(ge=1, le=200)],
    chunk_id: Annotated[str, Query(min_length=1, max_length=240)],
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    try:
        content = service.page_layout(source_id, snapshot_id, page_number, chunk_id)
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    response.headers["Cache-Control"] = "private, no-store"
    return content


@router.post("/{source_id}/snapshots/{snapshot_id}/pages/{page_number}/reread")
def reread_region(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    page_number: Annotated[int, Path(ge=1, le=200)],
    request: RereadRequest,
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    from archspec.official_documents.region_reread import RegionRereadBusy
    try:
        result = service.reread_issue(source_id, snapshot_id, page_number, request.chunk_id, request.row_index)
    except RegionRereadBusy as error:
        raise HTTPException(409, str(error)) from error
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(503, "局部重读结果无法保存或读取，原资料保留") from error
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.post("/{source_id}/snapshots/{snapshot_id}/pages/{page_number}/reread-cell")
def reread_table_cell(
    source_id: str,
    snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    page_number: Annotated[int, Path(ge=1, le=200)],
    request: CellRereadRequest,
    response: Response,
    service: Annotated[OfficialDocumentService, Depends(get_service)],
):
    from archspec.official_documents.region_reread import RegionRereadBusy
    try:
        result = service.reread_cell(source_id, snapshot_id, page_number, request.chunk_id,
                                     request.table_id, request.row, request.column)
    except RegionRereadBusy as error:
        raise HTTPException(409, str(error)) from error
    except (LookupError, ProjectNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(503, "单元格补读无法保存或读取，原资料保留") from error
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get("/{source_id}/originals/{snapshot_id}")
def original_metadata(source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    service: Annotated[OfficialDocumentService, Depends(get_service)]):
    from archspec.official_documents.originals import get_original
    try:
        return get_original(service, source_id, snapshot_id)
    except BusinessFilesUnavailable:
        raise HTTPException(503, "原件存储暂不可用，已有资料保留") from None
    except (OSError, ValueError, LookupError):
        raise HTTPException(404, "原件快照暂不可读取") from None


@router.get("/{source_id}/originals/{snapshot_id}/pages/{page_number}.png")
def original_preview(source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    page_number: Annotated[int, Path(ge=1, le=500)], service: Annotated[OfficialDocumentService, Depends(get_service)],
    quality: Annotated[Literal["preview", "reading"], Query()] = "preview"):
    from archspec.official_documents.originals import get_original, preview_image
    try:
        if quality == "preview" and page_number > 3:
            raise HTTPException(422, "预览仅提供前三页，请选择原页阅读")
        if quality == "reading":
            from archspec.official_documents.original_pages import reading_page
            image = reading_page(service, source_id, snapshot_id, page_number)
        else:
            record = get_original(service, source_id, snapshot_id)
            image = preview_image(service, record, page_number)
        return Response(image, media_type="image/png", headers={"Cache-Control": "private, no-store"})
    except BusinessFilesUnavailable:
        raise HTTPException(503, "原页存储暂不可用，请稍后查看") from None
    except (OSError, ValueError, LookupError):
        raise HTTPException(404, "原页预览暂不可读取，请下载原件查看") from None


@router.get("/{source_id}/originals/{snapshot_id}/file.html")
@router.get("/{source_id}/originals/{snapshot_id}/file.pdf")
def original_file(source_id: str, snapshot_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    service: Annotated[OfficialDocumentService, Depends(get_service)]):
    from starlette.responses import StreamingResponse

    from archspec.official_documents.originals import get_original, original_parts
    try:
        record = get_original(service, source_id, snapshot_id)
    except BusinessFilesUnavailable:
        raise HTTPException(503, "原件存储暂不可用，请稍后查看") from None
    except (OSError, ValueError, LookupError):
        raise HTTPException(404, "原件暂不可读取") from None
    web = record.get("source_format") == "HTML"
    suffix, media = ("html", "text/html") if web else ("pdf", "application/pdf")
    disposition = "attachment" if web else "inline"
    return StreamingResponse(original_parts(service, record), media_type=media,
        headers={"Content-Disposition": f'{disposition}; filename="{source_id}.{suffix}"',
                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "sandbox; default-src 'none'"})


@router.post("/{source_id}/originals")
def acquire_original(source_id: str, service: Annotated[OfficialDocumentService, Depends(get_service)]):
    """显式获取原件和少量预览；不修改已有正文快照，也不启动OCR或向量计算。"""
    from archspec.official_documents.downloader import download_pdf_file
    from archspec.official_documents.originals import save_original
    from archspec.official_documents.policy import policy_for_source
    from archspec.official_documents.processing_cache import ProcessingCache
    source = service._source(source_id)
    if source["kind"] == "UNCONFIRMED":
        raise HTTPException(409, "附件身份尚未确认，不能获取原件")
    folder = service.cache_dir / source_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        context = ({"page_url": source["page_url"]}
                   if (source.get("source_metadata") or {}).get("provenance") == "OFFICIAL_COPY" else {})
        downloaded = download_pdf_file(source["pdf_url"], folder, max_bytes=policy_for_source(source).max_bytes, **context)
        raw = ProcessingCache(service.cache_dir).publish_blob(downloaded.path, downloaded.sha256)
        downloaded.path.unlink(missing_ok=True)
        return save_original(service, source_id, raw, downloaded.sha256, downloaded.size_bytes)
    except (BusinessFilesUnavailable, OSError, ValueError, httpx.HTTPError):
        raise HTTPException(503, "原件暂未完整保存，请稍后查看；已有资料保留") from None
