"""服务端同请求组合入口；不安装schema、不签发迁移收据或自动启用REMOTE。"""
import json
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import get_ident
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from archspec.persistence import budget, sql_namespace
from archspec.persistence.source_registry import SQLSourceRegistryBackend

_CURRENT = ContextVar("archspec_service_runtime", default=None)
_EXECUTION = ContextVar("archspec_model_execution_lease", default=None)
_READ_CONNECTION = ContextVar("archspec_borrowed_read_connection", default=None)
_SAFE = "服务运行配置不完整或状态范围不一致，未回退本地"


class RuntimeBindingsUnavailable(RuntimeError):
    code = "SERVICE_RUNTIME_UNAVAILABLE"


@dataclass(frozen=True)
class ServiceRuntimeBindings:
    """只组合已存在的SQL组件；类型/同Engine核验不是六组件完整激活证明。"""
    engine: Engine = field(repr=False)
    cache_dir: Path
    registry: object = field(repr=False)
    journal: object = field(repr=False)
    instance_uuid: str
    clock: object = field(default=None, repr=False)
    lease_seconds: int = 90
    snapshot_catalog: object = field(default=None, repr=False)
    page_image_repository: object = field(default=None, repr=False)
    acquisition_state: object = field(default=None, repr=False)
    snapshot_publisher: object = field(default=None, repr=False)
    business_files: object = field(default=None, repr=False)
    snapshot_read_views: object = field(default=None, repr=False)
    sessions: object = field(init=False, repr=False)

    def __post_init__(self):
        from archspec.official_qa.task_leases import _LEASES, _PHASES, LeaseQATaskRepository
        from archspec.official_qa.task_models import QAOperationRow, QATaskRow
        from archspec.projects.models import DesignProjectRow
        try:
            self.validate_selection(check_sessions=False)
            object.__setattr__(self, "cache_dir", Path(self.cache_dir).resolve())
            object.__setattr__(self, "sessions", sessionmaker(bind=self.engine, expire_on_commit=False))
            # 禁临时影子/物理重定向；只检查原任务与租约表，不补空历史。
            with self.engine.connect() as connection:
                sql_namespace.bind_tables(connection, [DesignProjectRow.__table__, QATaskRow.__table__,
                    QAOperationRow.__table__, _LEASES, _PHASES])
            with self.sessions() as session:
                LeaseQATaskRepository(session, clock=self.clock, lease_seconds=self.lease_seconds,
                                      budget_journal=self.journal)
            self.registry.list()
            self.journal.metadata()
            if self.acquisition_state is not None:
                metadata = self.acquisition_state.metadata()
                source = self.registry.backend.history()[0]
                if any(metadata[key] != source[key] for key in ("migration_id", "source_cut_sha256")):
                    raise RuntimeBindingsUnavailable(_SAFE)
                known = {row["source_id"] for row in self.registry.list()}
                if any(job["source_id"] not in known for job in self.acquisition_state.read()["jobs"].values()):
                    raise RuntimeBindingsUnavailable(_SAFE)
            if self.snapshot_catalog is not None:
                metadata = self.snapshot_catalog.metadata()
                source = self.registry.backend.history()[0]
                if any(metadata[key] != source[key] for key in ("migration_id", "source_cut_sha256")):
                    raise RuntimeBindingsUnavailable(_SAFE)
        except Exception:  # noqa: BLE001 - 不回显DSN、cache路径或提供方原文
            raise RuntimeBindingsUnavailable(_SAFE) from None

    def validate_selection(self, *, check_sessions=True):
        """重核可变Engine/组件指针；固定对象不能掩盖运行中被重定向的数据库。"""
        from archspec.official_documents.registry import OfficialSourceRegistry
        try:
            if not isinstance(self.engine, Engine):raise RuntimeBindingsUnavailable(_SAFE)
            # 请求读事务和租约短事务同时存在；只读探测池不能作为运行池。
            # QueuePool没有公开的overflow上限访问器，按其实际容量核对。
            pool = self.engine.pool
            if isinstance(pool, QueuePool) and pool.size() == 1 and pool._max_overflow == 0:
                raise RuntimeBindingsUnavailable("服务运行连接池不能同时容纳请求读取和任务写入")
            schema = {"sqlite": "main", "postgresql": "archspec_control"}.get(self.engine.dialect.name)
            mapping = self.engine.get_execution_options().get("schema_translate_map")
            # SQLAlchemy编译真实查询后会补等值_none标识；它不是另一数据库。
            # 仍要求None唯一映到既定物理schema，其他映射或不同值全部拒绝。
            if (schema is None or not isinstance(mapping, dict) or mapping.get(None) != schema
                    or any(key not in {None, "_none"} or value != schema for key, value in mapping.items())):
                raise RuntimeBindingsUnavailable(_SAFE)
            if (not isinstance(self.registry, OfficialSourceRegistry)
                    or not isinstance(self.registry.backend, SQLSourceRegistryBackend)
                    or self.registry.backend.engine is not self.engine
                    or not isinstance(self.journal, budget.SQLBudgetJournal)
                    or self.journal.engine is not self.engine):raise RuntimeBindingsUnavailable(_SAFE)
            if self.snapshot_catalog is not None:
                from archspec.persistence.snapshot_catalog import SQLSnapshotCatalog
                if (not isinstance(self.snapshot_catalog, SQLSnapshotCatalog)
                        or self.snapshot_catalog.engine is not self.engine
                        or self.snapshot_catalog.cache_dir != Path(self.cache_dir).resolve()):
                    raise RuntimeBindingsUnavailable(_SAFE)
            if self.acquisition_state is not None:
                from archspec.persistence.acquisition_state import SQLAcquisitionState
                if (not isinstance(self.acquisition_state, SQLAcquisitionState)
                        or self.acquisition_state.engine is not self.engine):
                    raise RuntimeBindingsUnavailable(_SAFE)
            if self.snapshot_publisher is not None:
                from archspec.persistence.snapshot_publication import RuntimeSnapshotPublisher
                if (not isinstance(self.snapshot_publisher, RuntimeSnapshotPublisher)
                        or self.snapshot_catalog is None or self.snapshot_catalog.publisher is not self.snapshot_publisher):
                    raise RuntimeBindingsUnavailable(_SAFE)
                self.snapshot_publisher.validate_binding(self.engine, self.cache_dir)
            if self.business_files is not None:
                from archspec.persistence.business_files import RuntimeBusinessFiles
                if not isinstance(self.business_files, RuntimeBusinessFiles):raise RuntimeBindingsUnavailable(_SAFE)
                self.business_files.validate_binding(self.engine, self.cache_dir)
            elif self.snapshot_publisher is not None:
                raise RuntimeBindingsUnavailable(_SAFE)
            if self.page_image_repository is not None:
                from archspec.official_documents.page_images import PageImageRepository
                if (not isinstance(self.page_image_repository, PageImageRepository)
                        or self.page_image_repository.cache_dir != Path(self.cache_dir).resolve()):
                    raise RuntimeBindingsUnavailable(_SAFE)
            if not isinstance(self.instance_uuid, str) or str(UUID(self.instance_uuid)) != self.instance_uuid:
                raise RuntimeBindingsUnavailable(_SAFE)
            if self.snapshot_read_views is not None:
                from archspec.official_documents.read_views import SnapshotReadViews
                if type(self.snapshot_read_views) is not SnapshotReadViews:raise RuntimeBindingsUnavailable(_SAFE)
                self.snapshot_read_views.validate_binding(self)
            if check_sessions and self.sessions.kw.get("bind") is not self.engine:
                raise RuntimeBindingsUnavailable(_SAFE)
        except Exception:  # noqa: BLE001
            raise RuntimeBindingsUnavailable(_SAFE) from None

    def task_flow(self, request_session):
        """请求读事务保持原样；writer使用同Engine的独立短事务。"""
        from archspec.official_qa.task_leases import LeaseQATaskFlow, LeaseQATaskRepository
        selected = _CURRENT.get()
        if selected is None or selected[0] is not self or request_session.get_bind() is not self.engine:
            raise RuntimeBindingsUnavailable(_SAFE)
        session = self.sessions()
        selected[1].callback(session.close)
        try:
            repository = LeaseQATaskRepository(session, clock=self.clock, lease_seconds=self.lease_seconds,
                                               budget_journal=self.journal)
            return _BoundTaskFlow(self, LeaseQATaskFlow(repository, self.instance_uuid))
        except Exception:  # noqa: BLE001 - 池耗尽/连接异常统一503，不泄露驱动、DSN或回滚请求
            try:
                session.close()
            finally:
                raise RuntimeBindingsUnavailable(_SAFE) from None

    def wrap_gateway(self, gateway):
        return _SharedBudgetGateway(gateway, self)


def current_runtime():
    selected = _CURRENT.get()
    return selected[0] if selected is not None else None


@contextmanager
def runtime_scope(bindings):
    """单一服务器配置管理Session/来源/任务/预算；并发和嵌套退出各自恢复。"""
    from archspec.official_documents.source_context import source_registry_scope
    if bindings is not None and not isinstance(bindings, ServiceRuntimeBindings):
        raise RuntimeBindingsUnavailable(_SAFE)
    if bindings is not None:
        bindings.validate_selection()
    with ExitStack() as resources:
        token = _CURRENT.set((bindings, resources) if bindings is not None else None)
        try:
            resources.enter_context(source_registry_scope(
                bindings.cache_dir if bindings is not None else Path("."),
                bindings.registry if bindings is not None else None))
            yield
        finally:
            _CURRENT.reset(token)


class _BoundTaskFlow:
    def __init__(self, bindings, flow):
        self.bindings, self.flow, self.repository = bindings, flow, flow.repository

    def get(self, task_id):
        return self.flow.get(task_id)

    def run(self, request, mode, metadata, factory):
        holder = {"bindings": self.bindings, "repository": self.repository, "handle": None}
        token = _EXECUTION.set(holder)
        try:
            def construct(handle):
                holder["handle"] = handle
                return factory()
            return self.flow.run(request, mode, metadata, construct)
        finally:
            _EXECUTION.reset(token)


class _SharedBudgetGateway(budget.RemoteDevelopmentBudgetGateway):
    def __init__(self, gateway, bindings):
        self.bindings = bindings
        super().__init__(gateway, bindings.journal)

    def _check_gateway(self):
        self.bindings.validate_selection()
        if getattr(self.gateway, "base_url", None) != "https://api.deepseek.com":
            raise RuntimeBindingsUnavailable("共享模型预算只接受已批准的官方端点")
        super()._check_gateway()

    def reserve_for_packet(self, packet, digest):
        selected = current_runtime()
        if selected is not None and selected is not self.bindings:
            raise RuntimeBindingsUnavailable(_SAFE)
        execution = _EXECUTION.get()
        if execution is None:
            return super().reserve_for_packet(packet, digest)
        if execution["bindings"] is not self.bindings or execution["handle"] is None:
            raise RuntimeBindingsUnavailable(_SAFE)
        try:
            last = packet["messages"][-1]
            if packet["tools"] != [] or last["role"] != "user":raise ValueError
            phase = json.loads(last["content"])["task"]
            if phase not in {"UNDERSTAND", "SELECT"}:raise ValueError
        except (ValueError, TypeError, KeyError, IndexError):
            raise RuntimeBindingsUnavailable("模型阶段无法核对，未预留或发送") from None
        # 实际持久lease的fence/revision/实时有效期由repo重新核验；renew不换fence。
        return execution["repository"].reserve_model_phase(execution["handle"], phase, digest,
            reserve_on_connection=lambda scope, token, request_digest: scope.reserve(token, request_digest))


@contextmanager
def borrowed_read_scope(bindings, connection):
    """目录事务内的来源/权限只读复核共用连接，避免两连接池内再次开第三连接。"""
    from sqlalchemy.engine import Connection

    if (not isinstance(bindings, ServiceRuntimeBindings) or not isinstance(connection, Connection)
            or connection.engine is not bindings.engine or connection.closed
            or not connection.in_transaction() or _READ_CONNECTION.get() is not None):
        raise RuntimeBindingsUnavailable(_SAFE)
    token = _READ_CONNECTION.set((bindings.engine, connection, get_ident(), connection.get_transaction()))
    try:
        with bindings.registry.backend.borrowed_read(connection):
            yield
    finally:
        _READ_CONNECTION.reset(token)


def current_read_connection(engine):
    selected = _READ_CONNECTION.get()
    if selected is None:
        return None
    if (selected[0] is not engine or selected[2] != get_ident()
            or selected[1].closed or not selected[1].in_transaction()
            or selected[1].get_transaction() is not selected[3] or not selected[3].is_active):
        raise RuntimeBindingsUnavailable(_SAFE)
    return selected[1]


@contextmanager
def _read_phase_transaction_scope(bindings):
    """一次短读共用真实只读事务；不跨模型、联网或最终复核阶段保留状态。"""
    if bindings is None:
        yield
        return
    bindings.validate_selection()
    selected = current_runtime()
    if selected is not None and selected is not bindings:
        raise RuntimeBindingsUnavailable(_SAFE)
    # 发布正在借用目录写事务时保持原复核路径，绝不开第三条连接或缓存写状态。
    existing = current_read_connection(bindings.engine)
    if existing is not None:
        yield
        return
    from sqlalchemy import text

    from archspec.persistence import snapshot_catalog, source_registry
    with bindings.engine.connect() as connection:
        sqlite_query_only = None
        try:
            if connection.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                # 发布持有目录写锁再借来源；读取严格沿目录→来源顺序，避免反向死锁。
                for lock in ((snapshot_catalog._LOCK,) if bindings.snapshot_catalog is not None else ()) + (source_registry._LOCK,):
                    connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock_shared(:lock)"), {"lock": lock})
            elif connection.dialect.name == "sqlite":
                sqlite_query_only = connection.exec_driver_sql("PRAGMA query_only").scalar_one()
                connection.rollback()
                connection.exec_driver_sql("PRAGMA query_only = ON")
                connection.exec_driver_sql("BEGIN")
            else:
                raise RuntimeBindingsUnavailable(_SAFE)
            token = _READ_CONNECTION.set((bindings.engine, connection, get_ident(), connection.get_transaction()))
            try:
                with ExitStack() as resources:
                    resources.enter_context(sql_namespace._column_contract_scope(connection))
                    if bindings.snapshot_catalog is not None:
                        resources.enter_context(bindings.snapshot_catalog.borrowed_read(connection))
                    resources.enter_context(bindings.registry.backend.borrowed_read(connection, reuse_state=True))
                    yield
            finally:
                _READ_CONNECTION.reset(token)
        finally:
            connection.rollback()
            if sqlite_query_only is not None:
                connection.exec_driver_sql("PRAGMA query_only = " + str(int(sqlite_query_only)))
                connection.rollback()


@contextmanager
def publication_read_scope(engine, *, guard=None):
    """目录/文件完整审计借同阶段连接；独立调用仍保持原一致映像事务。"""
    connection = current_read_connection(engine)
    if connection is not None:
        # 已有短读阶段持目录共享锁；沿用其事务，仍在同一路径保护内消费字节。
        with ExitStack() as resources:
            if guard is not None:
                resources.enter_context(guard)
            yield connection
    else:
        from archspec.persistence.control_image import image_transaction
        with image_transaction(engine, guard=guard) as connection:
            yield connection


@contextmanager
def read_phase_scope(bindings):
    """数据库/命名空间故障保守返回现有503，不泄露SQL、连接串或驱动原文。"""
    from sqlalchemy.exc import SQLAlchemyError
    try:
        with _read_phase_transaction_scope(bindings):
            yield
    except (SQLAlchemyError, sql_namespace.NamespaceUnavailable):
        raise RuntimeBindingsUnavailable(_SAFE) from None
