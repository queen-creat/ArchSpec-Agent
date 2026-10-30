"""API全生命周期停写许可；不改变业务响应，不公开暂停或数据库入口。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack

from starlette.responses import JSONResponse

from archspec.persistence.source_write_gate import GatePaused, GateUnavailable


async def _complete_despite_cancel(task):
    """外层取消不放弃真实线程/响应后台工作；它们结束后才重抛首次取消。"""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 重复取消不能使许可提前退出，也不替换首次取消的语义。
                continue
            except Exception:  # noqa: BLE001 - 只取回inner异常，首次取消仍保留
                break
        if task.done() and not task.cancelled():
            # 取回inner异常避免未检索告警；不重新抛出、保存或记录提供方内容。
            task.exception()
        raise


class SourceWriteGateMiddleware:
    """所有HTTP API保守计在途；health与纯网页静态旁路，同进程覆盖不等于迁移。"""

    def __init__(self, app, *, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        api = path == "/api" or path.startswith("/api/")
        safe_health = path == "/api/health" and scope.get("method") in {"GET", "HEAD"}
        if scope["type"] != "http" or not api or safe_health:
            await self.app(scope, receive, send)
            return
        with ExitStack() as stack:
            try:
                # 冻结短SQL可能持同步锁，事件循环不等待它：立即503失败关闭。
                stack.enter_context(self.gate.write("HTTP", blocking=False))
            except (GatePaused, GateUnavailable):
                response = JSONResponse(
                    {"detail": {"code": "SOURCE_WRITES_PAUSED",
                                "message": "服务正在冻结运行状态，请稍后重试"}},
                    status_code=503,
                )
                await response(scope, receive, send)
                return
            # 独立Task覆盖同步handler与Starlette BackgroundTask；不把HTTP父许可传给线程池。
            inner = asyncio.create_task(self.app(scope, receive, send))
            await _complete_despite_cancel(inner)
