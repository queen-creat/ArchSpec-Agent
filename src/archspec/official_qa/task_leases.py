"""任务仓库租约候选：复用原task操作合同，统一预算原语，不改变默认LOCAL流程。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import get_ident
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    inspect,
    select,
    text,
    update,
)

from archspec.official_qa.task_models import QAOperationRow, QATaskRow
from archspec.official_qa.task_repository import (
    QATaskRepository,
    TaskConflict,
    _json,
    _operation,
    _task,
    _utc,
)
from archspec.persistence import budget, control_image, sql_namespace
from archspec.persistence.control_migration import installation_scope
from archspec.projects.models import DesignProjectRow

_LEASE_LOCK = 0x415243485350544C
_SCHEMA = "archspec_control"
_META = MetaData()
# 只复制表描述以解析FK；安装入口从不重建原任务、项目或预算表。
DesignProjectRow.__table__.to_metadata(_META)
QATaskRow.__table__.to_metadata(_META)
QAOperationRow.__table__.to_metadata(_META)
_LEASES = Table(
    "qa_execution_leases", _META,
    Column("task_id", String(36), ForeignKey("qa_tasks.task_id"), primary_key=True),
    Column("instance_uuid", String(36), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("operation_id", String(36), nullable=False),
    Column("operation_digest", String(64), nullable=False),
    Column("expected_revision", Integer, nullable=False),
    Column("lease_until", DateTime(timezone=True), nullable=False),
    Column("released", Boolean, nullable=False),
)
_PHASES = Table(
    "qa_model_phases", _META,
    Column("task_id", String(36), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("phase", String(16), primary_key=True),
    Column("call_token", String(36), unique=True, nullable=False),
    Column("model_digest", String(64), nullable=False),
    Column("operation_digest", String(64), nullable=False),
    Column("expected_revision", Integer, nullable=False),
    Column("fence", Integer, nullable=False),
    ForeignKeyConstraint(["task_id", "operation_id"],
                         ["qa_operations.task_id", "qa_operations.operation_id"]),
)


class LeaseUnavailable(RuntimeError):
    """schema、时钟、连接或未知提交失败，停止执行器，不泄露提供方细节。"""


@dataclass(frozen=True)
class ExecutionLease:
    task_id: str
    operation_id: str
    instance_uuid: str
    fence: int
    operation_digest: str
    expected_revision: int
    expires_at: datetime


@dataclass(frozen=True)
class LeaseClaim:
    task: object
    operation: object
    replay: bool
    handle: ExecutionLease | None


def _identity(value):
    if not isinstance(value, str):
        raise ValueError("任务、操作与实例身份必须为UUID")  # noqa: TRY004 - 身份输入统一业务拒绝
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("任务、操作与实例身份必须为UUID") from None


def model_phase_token(task_id, operation_id, phase):
    if phase not in {"UNDERSTAND", "SELECT"}:
        raise ValueError("模型阶段只允许UNDERSTAND或SELECT")
    # digest/实例/fence不进入身份；同逻辑阶段恢复不得换号续发。
    return str(uuid5(NAMESPACE_URL,
        f"archspec-qa-model-phase-v1:{_identity(task_id)}:{_identity(operation_id)}:{phase}"))


def _mapped(engine):
    if (engine.dialect.name != "postgresql"
            or engine.get_execution_options().get("schema_translate_map", {}).get(None) == _SCHEMA):
        return engine
    return engine.execution_options(schema_translate_map={None: _SCHEMA})


def _bound_schema(connection):
    # 与控制状态导入共享物理schema和TEMP拒绝规则；FK指向同一受信命名空间。
    return {table.name: table for table in sql_namespace.bind_tables(
        connection, [DesignProjectRow.__table__, QATaskRow.__table__,
                     QAOperationRow.__table__, _LEASES, _PHASES])}


def _ensure(connection):
    """读取原有租约结构，不补表；缺列、约束或物理重定向都拒绝。"""
    try:
        tables = _bound_schema(connection)
        inspector = inspect(connection)
        for name in (_LEASES.name, _PHASES.name):
            table = tables[name]
            if not inspector.has_table(name, schema=table.schema):
                raise LeaseUnavailable("任务租约schema尚未显式安装")
            control_image._physical_schema(connection, table)
    except LeaseUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 元信息和SQL驱动错误可能携带连接信息
        raise LeaseUnavailable("任务租约schema合同无法核对") from None


def install_lease_schema(engine, *, connection=None):
    """显式安装/借用整批迁移事务；只创建新租约协议，不重建原项目/任务。"""
    try:
        if engine.dialect.name not in {"sqlite", "postgresql"}:
            raise LeaseUnavailable("任务租约安装只支持SQLite或PostgreSQL")
        with installation_scope(engine, connection, control_image.image_transaction,
                                pg_lock=_LEASE_LOCK) as selected_connection:
            if selected_connection.dialect.name == "postgresql":
                selected_connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"),
                                            {"lock": _LEASE_LOCK})
            tables = _bound_schema(selected_connection)
            inspector = inspect(selected_connection)
            for name in ("design_projects", "qa_tasks", "qa_operations"):
                table = tables[name]
                if not inspector.has_table(name, schema=table.schema):
                    raise LeaseUnavailable("缺少已恢复的原业务任务表")
                control_image._physical_schema(selected_connection, table)
            selected_tables = [tables[_LEASES.name], tables[_PHASES.name]]
            existing = [inspector.has_table(table.name, schema=table.schema) for table in selected_tables]
            if any(existing) and not all(existing):
                raise LeaseUnavailable("任务租约历史部分缺失，拒绝自动补建空表")
            if not any(existing):
                # 同一MetaData含已恢复项目表的Enum；只建租约表仍会触发类型DDL。
                # 保留上方部分缺失拒绝和同事务锁，查询既有类型而非重复CREATE TYPE。
                selected_tables[0].metadata.create_all(selected_connection, tables=selected_tables, checkfirst=True)
            _ensure(selected_connection)
    except LeaseUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 创建/锁/未知提交均脱敏，不宣布安装成功
        raise LeaseUnavailable("任务租约安装失败或提交结果未知") from None


def _handle(row):
    return ExecutionLease(
        row["task_id"], row["operation_id"], row["instance_uuid"], row["fence"],
        row["operation_digest"], row["expected_revision"], _utc(row["lease_until"]))


class BudgetReservationScope:
    """仅暴露固定阶段的一次预留，不给回调Connection/事务/驱动等能力。"""

    __slots__ = ("__active", "__connection", "__digest", "__journal", "__receipt",
                 "__thread", "__token", "__used")

    def __init__(self, journal, connection, token, digest):
        self.__journal = journal
        self.__connection, self.__token, self.__digest = connection, token, digest
        self.__active, self.__used = True, False
        self.__receipt = None
        self.__thread = get_ident()

    def reserve(self, token, digest):
        if (not self.__active or self.__used or get_ident() != self.__thread
                or token != self.__token or digest != self.__digest):
            raise LeaseUnavailable("预算scope已失效、跨线程、重复使用或绑定不一致")
        self.__used = True
        # 共同仓库原语持有同可信Engine；没有另开连接、初始化空库或提前commit。
        receipt = self.__journal.reserve_on_connection(self.__connection, token, digest)
        # 只在共同原语成功后记录本轮回执；失败尝试不能认领旧RESERVED或伪造等值凭证。
        self.__receipt = receipt
        return receipt

    def _owns_receipt(self, receipt):
        # callback结束后仍核身份，确保返回的是本轮唯一委托的实际对象。
        return self.__receipt is not None and receipt is self.__receipt

    def _invalidate(self):
        self.__active = False


class LeaseQATaskRepository(QATaskRepository):
    """原请求/去重/revision合同加租约，不用跨主机PID，也不借请求读事务。"""

    def __init__(self, session, *, clock=None, lease_seconds=90, budget_journal=None):
        self.session = session
        bind = session.get_bind()
        engine = getattr(bind, "engine", bind)
        if engine.dialect.name not in {"sqlite", "postgresql"}:
            raise ValueError("任务租约只支持SQLite算法与PG候选")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("租约必须为1至300秒的有界时长")
        if engine.dialect.name == "sqlite" and not callable(clock):
            raise ValueError("SQLite算法验收必须明确注入时钟")
        if engine.dialect.name == "postgresql" and clock is not None:
            raise ValueError("PG租约只能在取得数据库锁后读取数据库时钟")
        self.engine = _mapped(engine)
        self.clock, self.lease_seconds = clock, lease_seconds
        self._connection = ContextVar("lease_connection", default=None)
        self._instant = ContextVar("lease_instant", default=None)
        self._no_request_transaction()
        with self.engine.connect() as connection:
            _ensure(connection)
        # 初始化发生在进入lease短事务之前；Scope构造/回调不能另占pool1连接。
        if budget_journal is None:
            self.budget_journal = budget.SQLBudgetJournal(self.engine)
        elif (not isinstance(budget_journal, budget.SQLBudgetJournal)
                or budget_journal.engine is not self.engine):
            raise LeaseUnavailable("任务租约预算仓库必须共享同一可信Engine")
        else:
            self.budget_journal = budget_journal

    def _no_request_transaction(self):
        if self.session.in_transaction():
            raise LeaseUnavailable("请求只读Session须先结束；不借用或隐式回滚它的事务")

    def _refresh_time(self, connection):
        # 每次受保护写入即时再读；预算锁等待/SQL回调后不能沿用先前时刻。
        now = (self.clock() if connection.dialect.name == "sqlite" else
               connection.scalar(text("SELECT clock_timestamp()")))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise LeaseUnavailable("租约时钟必须是明确UTC时间")
        self._instant.set(now.astimezone(UTC))
        return self._instant.get()

    @contextmanager
    def _transaction(self):
        parent = self._connection.get()
        if parent is not None:
            # 复用原仓库原语时统一同一短事务，不提前commit。
            yield parent
            return
        self._no_request_transaction()
        try:
            with self.engine.connect() as connection:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                    connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"),
                                       {"lock": _LEASE_LOCK})
                # PG必须在锁已获得之后读clock_timestamp，不用事务开始时刻now()。
                now = (self.clock() if connection.dialect.name == "sqlite" else
                       connection.scalar(text("SELECT clock_timestamp()")))
                if not isinstance(now, datetime) or now.tzinfo is None:
                    raise LeaseUnavailable("租约时钟必须是明确UTC时间")
                ct = self._connection.set(connection)
                nt = self._instant.set(now.astimezone(UTC))
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    self._instant.reset(nt)
                    self._connection.reset(ct)
        except (TaskConflict, ValueError, LeaseUnavailable):
            raise
        except Exception:  # noqa: BLE001 - SQL/回调/未知commit异常不能传播提供方响应
            raise LeaseUnavailable("任务租约事务失败或提交结果未知；未继续执行模型") from None

    def get(self, task_id):
        self._no_request_transaction()
        return super().get(task_id)

    def get_operation(self, task_id, operation_id):
        self._no_request_transaction()
        return super().get_operation(task_id, operation_id)

    @staticmethod
    def _lease_row(connection, task_id):
        return connection.execute(select(_LEASES).where(
            _LEASES.c.task_id == task_id)).mappings().first()

    def lease_for(self, task_id):
        self._no_request_transaction()
        with self.engine.connect() as connection:
            row = self._lease_row(connection, _identity(task_id))
            return _handle(row) if row is not None and not row["released"] else None

    def _running(self, connection, task):
        row = self._lease_row(connection, task["task_id"])
        if row is None or row["released"]:
            raise LeaseUnavailable("既有RUNNING任务缺少完整租约，不能重建或覆盖旧owner")
        current = _handle(row)
        # 旧task/op/revision先核齐；lease存在不能掩盖被替换的操作。
        super()._active(connection, current.task_id, current.operation_id,
                        current.instance_uuid)
        operation = _operation(connection, current.task_id, current.operation_id)
        actual_digest = hashlib.sha256(_json({
            "request": json.loads(task["request_json"]), "mode": task["mode"],
            "revision": task["revision"], "action": "QA",
        }).encode()).hexdigest()
        if (actual_digest != operation["request_digest"]
                or operation["request_digest"] != current.operation_digest
                or task["revision"] != current.expected_revision
                or type(current.fence) is not int or current.fence < 1):
            raise TaskConflict("LEASE_BINDING_CONFLICT", "租约与实际操作摘要或revision不一致")
        return row

    def _validate(self, connection, handle, *, alive=True):
        if not isinstance(handle, ExecutionLease):
            raise ValueError("任务更新须携带完整执行租约")  # noqa: TRY004 - 执行输入统一业务拒绝
        if (type(handle.fence) is not int or handle.fence < 1
                or type(handle.expected_revision) is not int or handle.expected_revision < 0):
            raise ValueError("执行fence及revision必须为明确整数，不接受布尔替代")
        self._refresh_time(connection)
        task = _task(connection, _identity(handle.task_id))
        if task is None:
            raise TaskConflict("TASK_NOT_FOUND", "任务不存在")
        row = self._running(connection, task)
        current = _handle(row)
        keys = ("task_id", "operation_id", "instance_uuid", "fence",
                "operation_digest", "expected_revision")
        if any(getattr(handle, key) != getattr(current, key) for key in keys):
            raise TaskConflict("FENCE_CONFLICT", "旧实例、执行号或操作绑定已失效")
        if alive and current.expires_at <= self._instant.get():
            raise TaskConflict("LEASE_EXPIRED", "执行租约已到期；不接受迟到结果或续发模型")
        return task, row

    def _prior_model_phases_resolved(self, connection, task_id):
        phases = connection.execute(select(_PHASES).where(
            _PHASES.c.task_id == task_id)).mappings().all()
        if not phases:
            return
        _, calls = budget._state(connection)
        indexed = {row["call_token"]: row for row in calls}
        for phase in phases:
            token = model_phase_token(task_id, phase["operation_id"], phase["phase"])
            recorded = indexed.get(token)
            operation = _operation(connection, task_id, phase["operation_id"])
            if (token != phase["call_token"] or recorded is None or operation is None
                    or recorded["request_digest"] != phase["model_digest"]
                    or operation["request_digest"] != phase["operation_digest"]
                    or operation["expected_revision"] != phase["expected_revision"]):
                raise LeaseUnavailable("历史模型阶段与实际预算/操作绑定不完整")
            if recorded["status"] == "RESERVED":
                # 不能用新operation UUID绕过旧未决调用；租约到期不等于模型未执行。
                raise TaskConflict("MODEL_OUTCOME_UNRESOLVED",
                                   "该任务存在未决模型预留，暂停新操作续发；旧费用预留保留")

    def claim(self, task_id, operation_id, expected_revision, request, mode, instance_uuid):
        task_id, operation_id = _identity(task_id), _identity(operation_id)
        instance_uuid = _identity(instance_uuid)
        with self._transaction() as connection:
            prior_task = _task(connection, task_id)
            if prior_task is not None and prior_task["state"] == "RUNNING":
                self._running(connection, prior_task)
            if mode == "MODEL" and _operation(connection, task_id, operation_id) is None:
                self._prior_model_phases_resolved(connection, task_id)
            claimed = super().claim(task_id, operation_id, expected_revision,
                                    request, mode, instance_uuid)
            if claimed.replay:
                return LeaseClaim(claimed.task, claimed.operation, True, None)
            old = self._lease_row(connection, task_id)
            fence = old["fence"] + 1 if old is not None else 1
            values = {
                "task_id": task_id, "operation_id": operation_id,
                "instance_uuid": instance_uuid, "fence": fence,
                "operation_digest": claimed.operation.request_digest,
                "expected_revision": claimed.task.revision,
                "lease_until": self._instant.get() + timedelta(seconds=self.lease_seconds),
                "released": False,
            }
            if old is None:
                connection.execute(insert(_LEASES).values(**values))
            else:
                if not old["released"]:
                    raise TaskConflict("LEASE_ACTIVE", "旧执行租约尚未释放")
                connection.execute(update(_LEASES).where(_LEASES.c.task_id == task_id,
                    _LEASES.c.fence == old["fence"], _LEASES.c.released.is_(True)).values(**values))
            return LeaseClaim(claimed.task, claimed.operation, False, _handle(values))

    def checkpoint(self, handle, **kwargs):
        with self._transaction() as connection:
            self._validate(connection, handle)
            return super().checkpoint(handle.task_id, handle.operation_id,
                                      handle.instance_uuid, **kwargs)

    def finish(self, handle, **kwargs):
        with self._transaction() as connection:
            self._validate(connection, handle)
            result = super().finish(handle.task_id, handle.operation_id,
                                    handle.instance_uuid, **kwargs)
            connection.execute(update(_LEASES).where(_LEASES.c.task_id == handle.task_id,
                _LEASES.c.fence == handle.fence,
                _LEASES.c.instance_uuid == handle.instance_uuid).values(released=True))
            return result

    def renew(self, handle):
        with self._transaction() as connection:
            self._validate(connection, handle)
            expires = self._instant.get() + timedelta(seconds=self.lease_seconds)
            connection.execute(update(_LEASES).where(_LEASES.c.task_id == handle.task_id,
                _LEASES.c.fence == handle.fence,
                _LEASES.c.instance_uuid == handle.instance_uuid).values(lease_until=expires))
            return _handle(self._lease_row(connection, handle.task_id))

    def interrupt_expired(self, task_id):
        task_id = _identity(task_id)
        with self._transaction() as connection:
            task = _task(connection, task_id)
            if task is None or task["state"] != "RUNNING":
                return 0
            row = self._running(connection, task)
            if _utc(row["lease_until"]) > self._instant.get():
                return 0
            handle = _handle(row)
            super().finish(task_id, handle.operation_id, handle.instance_uuid,
                           state="INTERRUPTED", trace=json.loads(task["trace_json"]),
                           current_step="LEASE_EXPIRED")
            connection.execute(update(_LEASES).where(_LEASES.c.task_id == task_id,
                _LEASES.c.fence == handle.fence).values(released=True))
            # 不更改预算预留，不根据租约到期猜测模型有没有收到请求。
            return 1

    def mark_interrupted(self, owner_token):
        raise TaskConflict("LEASE_REQUIRED", "跨实例中断只允许核验过期租约，不使用PID或owner扫描")

    def reserve_model_phase(self, handle, phase, model_digest, *, reserve_on_connection):
        token = model_phase_token(handle.task_id, handle.operation_id, phase)
        if not isinstance(model_digest, str) or re.fullmatch(r"[a-f0-9]{64}", model_digest) is None:
            raise ValueError("模型阶段须携带实际载荷SHA256")
        with self._transaction() as connection:
            task, _ = self._validate(connection, handle)
            if task["mode"] != "MODEL":
                raise TaskConflict("MODEL_MODE_REQUIRED", "本地任务不能预留模型阶段")
            if connection.dialect.name == "postgresql":
                # 与独立预算仓库用同一个锁域；统一顺序始终task锁→budget锁。
                connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"),
                                   {"lock": budget._BUDGET_LOCK})
            # 预算锁可能有等待；锁获得后重新核实时租约，不签发过期阶段。
            self._validate(connection, handle)
            existing = connection.execute(select(_PHASES).where(
                _PHASES.c.task_id == handle.task_id,
                _PHASES.c.operation_id == handle.operation_id,
                _PHASES.c.phase == phase)).mappings().first()
            if existing is not None:
                raise TaskConflict("MODEL_PHASE_ALREADY_RESERVED", "该逻辑阶段已有预留，不会重发或换号")
            try:
                scope = BudgetReservationScope(self.budget_journal, connection, token, model_digest)
                try:
                    receipt = reserve_on_connection(scope, token, model_digest)
                finally:
                    scope._invalidate()
                if (not scope._owns_receipt(receipt) or not isinstance(receipt, budget.BudgetReservation)
                        or receipt.call_token != token or receipt.request_digest != model_digest
                        or receipt.reserved_cents != 10 or not connection.in_transaction()):
                    raise LeaseUnavailable("同连接预算回调未返回核验过的预留凭证")
                _, rows = budget._state(connection)
                actual = next((row for row in rows if row["call_token"] == token), None)
                if (actual is None or actual["request_digest"] != model_digest
                        or actual["status"] != "RESERVED" or actual["reserved_cents"] != 10
                        or actual["legacy_id"] is not None):
                    raise LeaseUnavailable("预算凭证与同事务实际预留不一致")
            except (TaskConflict, budget.BudgetConflict):
                raise
            except Exception:  # noqa: BLE001 - 不存或回显任何模型消息、密钥或提供方响应
                raise LeaseUnavailable("模型阶段预留未完成，事务已停止且不会发送模型") from None
            # 可信SQL回调也可能耗时或误改对象；写阶段映射前再核全部事实。
            self._validate(connection, handle)
            values = {
                "task_id": handle.task_id, "operation_id": handle.operation_id, "phase": phase,
                "call_token": token, "model_digest": model_digest,
                "operation_digest": handle.operation_digest,
                "expected_revision": handle.expected_revision, "fence": handle.fence,
            }
            connection.execute(insert(_PHASES).values(**values))
            actual_phase = connection.execute(select(_PHASES).where(
                _PHASES.c.task_id == handle.task_id,
                _PHASES.c.operation_id == handle.operation_id,
                _PHASES.c.phase == phase)).mappings().one()
            if dict(actual_phase) != values:
                raise LeaseUnavailable("已写模型阶段映射与实际预算绑定不一致")
            return receipt


class LeaseQATaskFlow:
    """真实有限Flow的租约适配，仍只调用一次原service，不引入新Agent循环。"""

    def __init__(self, repository, instance_uuid):
        self.repository, self.instance_uuid = repository, _identity(instance_uuid)

    def get(self, task_id):
        self.repository.interrupt_expired(task_id)
        return self.repository.get(task_id)

    def run(self, request, mode, metadata, factory):
        from archspec.official_qa.question_plan import thaw_value
        from archspec.official_qa.task_flow import (
            condition_trace,
            final_state,
            public_task,
            safe_trace,
            waiting_keys,
        )
        self.get(metadata.task_id)
        claim = self.repository.claim(metadata.task_id, metadata.operation_id,
            metadata.expected_revision, request, mode, self.instance_uuid)
        if claim.replay:
            return {"status": "TASK_RECEIPT", "summary": "该操作已有记录，未再次执行问答",
                    "qa_task": public_task(claim.task, claim.operation, replayed=True)}
        handle, progress = claim.handle, []
        def checkpoint(proposal):
            self.repository.checkpoint(handle, proposal=proposal)
        def step(name, state):
            nonlocal handle
            handle = self.repository.renew(handle)
            progress.append({"step": name, "state": state})
            self.repository.checkpoint(handle, trace=progress[-20:], current_step=name)
        try:
            service = factory(handle)
            if mode == "MODEL":
                result = service.answer(request,
                    resume_proposal=(thaw_value(claim.task.proposal)
                        if claim.task.proposal is not None else None),
                    on_proposal=checkpoint, on_step=step)
            else:
                result = service.answer(request)
        except Exception:
            self.repository.finish(handle, state="FAILED",
                trace=[{"step": "QA_EXECUTE", "state": "FAILED"}])
            raise
        trace = safe_trace(result.get("trace") or [])
        from archspec.official_qa.reading_receipts import capture_positions
        result['reading_positions_record'] = capture_positions(service, claim.task, metadata, result)
        trace = [row for row in (progress + trace) if row.get("step") != "QA_CONDITIONS"][-19:]
        trace.append(condition_trace(result))
        finished = self.repository.finish(handle, state=final_state(result),
            question_keys=waiting_keys(result), trace=trace)
        operation = self.repository.get_operation(finished.task_id, metadata.operation_id)
        return {**result, "qa_task": public_task(finished, operation)}
