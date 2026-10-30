"""分项存储状态由部署访问门保护，不加入公开健康检查。"""
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from archspec.persistence.diagnostics import check_storage_status, storage_status

router = APIRouter(prefix="/api/storage", tags=["存储连接状态"])


@router.get("/status")
def get_storage_status(request: Request):
    """只显示当前服务器配置状态，不因页面读取发出远程请求。"""
    return _selected_status(request, storage_status())


@router.post("/check")
async def check_storage(request: Request):
    """客户端不能传地址或密钥；显式检查只使用服务器自己的环境配置。"""
    # 此接口没有请求体：发现第一个非空分块立即拒绝，避免把上传完整缓冲进内存。
    rejected = HTTPException(status_code=422, detail={
        "code": "STORAGE_CHECK_INPUT_REJECTED", "message": "存储检查不接收客户端配置。",
    })
    if request.url.query:
        raise rejected
    async for chunk in request.stream():
        if chunk:
            raise rejected
    # 现有同步HTTP适配器放在线程池，有限读取不占用页面请求的事件循环。
    result = await run_in_threadpool(check_storage_status)
    return _selected_status(request, result)


def _selected_status(request, result):
    """状态接口使用应用实际绑定；环境配置齐全不能证明网站已经切换。"""
    if getattr(request.app.state, "persistence_backend", "LOCAL") != "REMOTE":
        return result
    from archspec.persistence.remote_startup import RestoredCloudGeneration
    from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable, current_runtime
    restored = getattr(request.app.state, "cloud_generation", None)
    runtime = current_runtime()
    if (type(restored) is not RestoredCloudGeneration
            or runtime is not getattr(request.app.state, "runtime_bindings", None)):
        raise RuntimeBindingsUnavailable("云端启动身份缺失，未回退本地状态")
    return {**result, **restored.runtime_status(runtime)}
