"""请求级来源绑定；各现有构造器共用实例，不隐式启用远端或扩大权限。"""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path

from starlette.responses import JSONResponse

from archspec.persistence.source_registry import SourceRegistryUnavailable

_CURRENT = ContextVar("official_source_registry_context", default=None)
_SAFE = "当前来源配置不完整或范围不一致，未回退其他登记库"


@contextmanager
def source_registry_scope(cache_dir, registry):
    """只接受服务器登记对象，嵌套未配置应用明确清空外层绑定。"""
    try:
        from archspec.official_documents.registry import OfficialSourceRegistry
        if registry is None:
            binding = None
        elif not isinstance(registry, OfficialSourceRegistry):
            raise SourceRegistryUnavailable(_SAFE)
        else:
            root = Path(cache_dir).resolve()
            if registry.backend is None and registry.path.parent.resolve() != root:
                raise SourceRegistryUnavailable(_SAFE)
            binding = (root, registry)
    except SourceRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 不回显数据路径或提供方原值
        raise SourceRegistryUnavailable(_SAFE) from None
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_registry(cache_dir):
    """未配置保留旧行为；已有绑定时另一个缓存范围必须拒绝。"""
    binding = _CURRENT.get()
    if binding is None:
        return None
    try:
        if Path(cache_dir).resolve() != binding[0]:
            raise SourceRegistryUnavailable(_SAFE)
        return binding[1]
    except SourceRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001
        raise SourceRegistryUnavailable(_SAFE) from None


def resolve_registry(cache_dir, registry=None):
    """统一构造入口：显式实例与当前绑定一致，未配置保留旧本地行为。"""
    selected = current_registry(cache_dir)
    if selected is not None:
        if registry is not None and registry is not selected:
            raise SourceRegistryUnavailable(_SAFE)
        return selected
    if registry is not None:
        return registry
    from archspec.official_documents.registry import OfficialSourceRegistry
    return OfficialSourceRegistry(cache_dir)


class SourceRegistryContextMiddleware:
    """ContextVar随实际ASGI/线程请求传递，结束后恢复；不是跨进程授权证明。"""
    def __init__(self, app, *, registry_provider, cache_dir_provider, runtime_provider=None):
        if not callable(registry_provider) or not callable(cache_dir_provider):
            raise SourceRegistryUnavailable(_SAFE)
        self.app = app
        self.registry_provider = registry_provider
        self.cache_dir_provider = cache_dir_provider
        self.runtime_provider = runtime_provider or (lambda: None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with ExitStack() as stack:
            try:
                from archspec.persistence.runtime_bindings import (
                    RuntimeBindingsUnavailable,
                    runtime_scope,
                )
                selected = self.runtime_provider()
                stack.enter_context(runtime_scope(selected))
                registry = self.registry_provider()
                if selected is not None:
                    if registry is not None and registry is not selected.registry:
                        raise RuntimeBindingsUnavailable("来源实例与选定运行配置不一致")
                    cache, registry = selected.cache_dir, selected.registry
                else:
                    cache = self.cache_dir_provider()
                stack.enter_context(source_registry_scope(cache, registry))
            except Exception as error:  # noqa: BLE001 - 初始化位于业务异常处理器外，给安全503
                code = ("SERVICE_RUNTIME_UNAVAILABLE" if isinstance(error, RuntimeBindingsUnavailable)
                        else "SOURCE_REGISTRY_UNAVAILABLE")
                response = JSONResponse(status_code=503, content={"detail": {
                    "code": code, "message": _SAFE}})
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
