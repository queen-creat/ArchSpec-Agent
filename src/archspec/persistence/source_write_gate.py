"""同进程源停写门；API与正文worker显式接入，内存许可不证明跨实例排空或业务切面。"""

from __future__ import annotations

import math
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock, current_thread
from time import monotonic
from uuid import uuid4

KINDS = frozenset({"HTTP", "MODEL", "WORKER", "GOVERNANCE",
                   "CATALOG", "BUDGET", "METADATA", "ASSET"})


class GateConflict(ValueError):
    """过期、伪造、他门或错误归属的内部句柄；不改变当前有效状态。"""


class GatePaused(RuntimeError):
    """暂停后不允许新根写入，旧活跃许可仅用于完成已在途工作。"""


class GateBusy(GatePaused):
    """冻结短SQL持锁时不能阻塞事件循环，登记立即失败关闭。"""


class GateTimeout(TimeoutError):
    """有限等待结束仍未取得冻结事实；保持当前关闭状态。"""


class GateUnavailable(RuntimeError):
    """许可清理结果未知；保守关闭，不发冻结回执。"""


@dataclass(frozen=True)
class WritePermit:
    permit_id: str
    epoch: str
    kind: str


@dataclass(frozen=True)
class ChildGrant:
    nonce: str
    epoch: str
    parent_permit_id: str


@dataclass(frozen=True)
class PauseHandle:
    epoch: str
    nonce: str


@dataclass(frozen=True)
class FreezeReceipt:
    epoch: str
    nonce: str
    active_writers: int = 0


@dataclass(frozen=True)
class _LivePermit:
    permit: WritePermit
    owner: object


class _WriteScope:
    def __init__(self, gate, kind, parent, blocking):
        self.gate, self.kind, self.parent = gate, kind, parent
        self.blocking = blocking
        self.entered = self.exited = False
        self.permit = None

    def __enter__(self):
        if self.entered:
            raise GateConflict("写入作用域不能重复进入")
        self.permit = self.gate._acquire(self.kind, self.parent, blocking=self.blocking)
        self.entered = True
        return self.permit

    def __exit__(self, exc_type, exc, traceback):
        if not self.entered or self.exited:
            raise GateConflict("写入作用域不能重复或提前退出")
        try:
            self.gate._release(self.permit)
            self.exited = True
        except GateConflict:
            if exc is None:
                raise GateConflict("写入许可归属或退出状态无效") from None
            exc.add_note("写入许可退出未确认，停写验收不能据此宣称完成")
        except Exception:  # noqa: BLE001 - 清理错误可能含私密值，不存原文
            self.gate._fault_close()
            self.exited = True
            if exc is None:
                raise GateUnavailable("写入许可释放结果未知，停写门已关闭") from None
            # 保留原业务失败，不能用清理异常掩盖它。
            exc.add_note("写入许可释放结果未知，停写门已关闭，未签收冻结")
        return False


class SourceWriteGate:
    """必须由同进程所有实际副作用入口共同接入后才能作为源切面的一项证据。"""

    def __init__(self):
        self._condition = Condition(RLock())
        self._epoch = str(uuid4())
        self._state = "OPEN"
        self._healthy = True
        self._permits = {}
        self._grants = {}
        self._handle = None
        self._receipt = None
        self._section_depth = 0

    def _require_healthy(self):
        if not self._healthy:
            raise GateUnavailable("写入许可状态不可核验，停写门保持关闭")

    def status(self):
        with self._condition:
            return {
                "state": self._state, "healthy": self._healthy, "epoch": self._epoch,
                "active_writers": len(self._permits),
                "active_by_kind": dict(Counter(
                    value.permit.kind for value in self._permits.values()
                )),
            }

    def write(self, kind, parent=None, *, blocking=True):
        if type(blocking) is not bool:
            raise ValueError("登记等待选项必须为内部布尔值")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("写入类型必须是固定内部枚举")
        return _WriteScope(self, kind, parent, blocking)

    def _live(self, permit, *, require_owner):
        if not isinstance(permit, WritePermit):
            raise GateConflict("写入许可未在当前门登记")
        live = self._permits.get(permit.permit_id)
        if (live is None or live.permit is not permit or permit.epoch != self._epoch
                or (require_owner and live.owner is not current_thread())):
            raise GateConflict("写入许可过期、归属错误或不是当前门签发")
        return live

    def _acquire(self, kind, parent, *, blocking=True):
        if not self._condition.acquire(blocking=blocking):
            raise GateBusy("停写门正在冻结短SQL，当前登记未进入")
        try:
            self._require_healthy()
            grant = None
            if parent is None:
                if self._state != "OPEN":
                    raise GatePaused("源写入已暂停，不接纳新的根写入")
            elif isinstance(parent, WritePermit):
                self._live(parent, require_owner=True)
            elif isinstance(parent, ChildGrant):
                grant = self._grants.get(parent.nonce)
                if grant is not parent or parent.epoch != self._epoch:
                    raise GateConflict("跨线程子许可已失效、伪造或已消费")
                live = self._permits.get(parent.parent_permit_id)
                if live is None:
                    raise GateConflict("跨线程父许可已结束")
                self._live(live.permit, require_owner=False)
            else:
                raise GateConflict("父许可不是当前门签发的内部对象")
            permit = WritePermit(str(uuid4()), self._epoch, kind)
            self._permits[permit.permit_id] = _LivePermit(permit, current_thread())
            if grant is not None:
                del self._grants[grant.nonce]
            return permit
        finally:
            self._condition.release()

    def delegate(self, parent):
        """仅跨线程授权创建一个新子许可，不转移父许可释放权。"""
        with self._condition:
            self._require_healthy()
            self._live(parent, require_owner=True)
            grant = ChildGrant(str(uuid4()), self._epoch, parent.permit_id)
            self._grants[grant.nonce] = grant
            return grant

    def _release(self, permit):
        with self._condition:
            self._live(permit, require_owner=True)
            del self._permits[permit.permit_id]
            # 未消费授权不计执行者；父已结束后全部作废，迟到线程不能开新工作。
            expired = [nonce for nonce, grant in self._grants.items()
                       if grant.parent_permit_id == permit.permit_id]
            for nonce in expired:
                del self._grants[nonce]
            self._condition.notify_all()

    def _fault_close(self):
        with self._condition:
            self._healthy = False
            self._state = "PAUSED"
            self._receipt = None
            self._condition.notify_all()

    def pause(self):
        with self._condition:
            self._require_healthy()
            if self._state != "OPEN":
                raise GateConflict("停写门已有当前暂停，不能复用或替换旧句柄")
            self._state = "PAUSED"
            self._handle = PauseHandle(self._epoch, str(uuid4()))
            self._receipt = None
            return self._handle

    def _check_handle(self, handle):
        if (not isinstance(handle, PauseHandle) or handle is not self._handle
                or handle.epoch != self._epoch or self._state not in {"PAUSED", "FROZEN"}):
            raise GateConflict("暂停句柄过期或不属于当前停写")

    def wait_idle(self, handle, timeout):
        if (type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 <= timeout <= 60):
            raise ValueError("停写等待必须是0至60秒的有限时间")
        deadline = monotonic() + timeout
        # 截止时间也覆盖 frozen_section 持锁等待，不能在获取锁之前无限阻塞。
        acquired = self._condition.acquire(timeout=max(0, deadline - monotonic()))
        if not acquired:
            raise GateTimeout("停写等待超时，未签发新的冻结回执")
        try:
            self._require_healthy()
            self._check_handle(handle)
            while self._permits:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise GateTimeout("仍有实际在途写入，保持暂停，未签收冻结")
                self._condition.wait(timeout=remaining)
                self._require_healthy()
                self._check_handle(handle)
            self._state = "FROZEN"
            if self._receipt is None:
                self._receipt = FreezeReceipt(self._epoch, handle.nonce)
            return self._receipt
        finally:
            self._condition.release()

    def _valid_receipt(self, receipt):
        return bool(
            self._healthy and self._state == "FROZEN" and not self._permits
            and isinstance(receipt, FreezeReceipt) and receipt is self._receipt
            and receipt.epoch == self._epoch and receipt.active_writers == 0
            and self._handle is not None and receipt.nonce == self._handle.nonce
        )

    def validate_frozen(self, receipt):
        """只判断调用瞬间；真实短SQL捕获/发布必须使用 frozen_section。"""
        with self._condition:
            return self._valid_receipt(receipt)

    @contextmanager
    def frozen_section(self, receipt):
        """短SQL临界区；禁止网络/OCR/模型/上传，内存锁不能替代跨实例fence。"""
        with self._condition:
            if not self._valid_receipt(receipt):
                raise GateConflict("冻结回执过期、伪造或不属于当前停写")
            self._section_depth += 1
            try:
                yield receipt
            finally:
                self._section_depth -= 1

    def resume(self, handle):
        with self._condition:
            self._require_healthy()
            self._check_handle(handle)
            # 其他线程被同一锁阻止；同线程也不能用RLock重入绕过短SQL区。
            if self._section_depth:
                raise GateConflict("冻结SQL临界区尚未退出，不得解除停写")
            if self._permits:
                raise GateConflict("在途许可尚未归零，不得解除停写")
            self._epoch = str(uuid4())
            self._state = "OPEN"
            self._handle = self._receipt = None
            self._grants.clear()
            self._condition.notify_all()
