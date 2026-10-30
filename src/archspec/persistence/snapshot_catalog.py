"""当前固定正文的SQL选择与完整历史；不作为对象/向量已迁入的激活证明。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.schema import CreateSchema

from archspec.persistence import sql_namespace
from archspec.persistence.control_migration import installation_scope

VERSION = "OFFICIAL_SNAPSHOT_CATALOG_V1"
_SAFE = "当前正文快照状态无法核验，未回退本地指针或覆盖历史"
_LOCK = 0x4152434853505343
_MAX_BYTES = 8 * 1024 * 1024
_META = MetaData()
_CONTROL = Table(
    "official_snapshot_catalog",
    _META,
    Column("id", Integer, primary_key=True),
    Column("migration_id", String(128), nullable=False),
    Column("source_cut_sha256", String(64), nullable=False),
    Column("initial_json", Text, nullable=False),
    Column("contract_version", String(40), nullable=False),
)
_EVENTS = Table(
    "official_snapshot_events",
    _META,
    Column("revision", Integer, primary_key=True),
    Column("source_id", String(80), nullable=False),
    Column("snapshot_id", String(64), nullable=False),
    Column("snapshot_sha256", String(64), nullable=False),
    Column("document_sha256", String(64), nullable=False),
    Column("parent_sha256", String(64)),
    Column("event_sha256", String(64), nullable=False, unique=True),
)
_HEADS = Table(
    "official_snapshot_heads",
    _META,
    Column("source_id", String(80), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("event_sha256", String(64), nullable=False),
)
_TABLES = (_CONTROL, _EVENTS, _HEADS)
_KEYS = {"source_id", "snapshot_id", "snapshot_sha256", "document_sha256"}


class SnapshotCatalogUnavailable(RuntimeError):
    """缺schema、损坏、数据库故障或未知提交必须停止，错误不含路径或上游响应。"""


class SnapshotCatalogConflict(ValueError):
    """旧指针或不可变快照内容冲突；调用者不能覆盖当前选择。"""


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _source(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:gb55031|official-[a-f0-9]{32})", value) is None
    ):
        raise SnapshotCatalogUnavailable(_SAFE)
    return value


def _pointer(value):
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise SnapshotCatalogUnavailable(_SAFE)
    _source(value["source_id"])
    if any(not _digest(value[k]) for k in _KEYS - {"source_id"}):
        raise SnapshotCatalogUnavailable(_SAFE)
    return dict(value)


def _initial(values):
    if not isinstance(values, list) or len(values) > 20_000:
        raise SnapshotCatalogUnavailable(_SAFE)
    checked = [_pointer(p) for p in values]
    if len({p["source_id"] for p in checked}) != len(checked):
        raise SnapshotCatalogUnavailable(_SAFE)
    return sorted(checked, key=lambda p: p["source_id"])


def _event(pointer, revision, parent, control):
    row = {**pointer, "revision": revision, "parent_sha256": parent}
    return {**row, "event_sha256": _sha({"event": row, "control": control})}


def _tables(connection, *, check=True):
    tables = sql_namespace.bind_tables(connection, list(_TABLES))
    if check:
        sql_namespace.validate_columns(connection, tables)
    return tables


@contextmanager
def _transaction(engine, *, write=False):
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            elif connection.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                if write:
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                lock = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
                connection.execute(text(f"SELECT pg_catalog.{lock}(:lock)"), {"lock": _LOCK})
            else:
                raise SnapshotCatalogUnavailable(_SAFE)
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (SnapshotCatalogUnavailable, SnapshotCatalogConflict):
        raise
    except Exception as error:
        from archspec.persistence.acquisition_state import AcquisitionLeaseLost
        from archspec.standard_governance.compatibility import ContentUseDenied
        if isinstance(error, (AcquisitionLeaseLost, ContentUseDenied)):
            raise
        raise SnapshotCatalogUnavailable(_SAFE) from None


def _state(connection):
    control_table, event_table, head_table = _tables(connection)
    controls = connection.execute(select(control_table)).mappings().all()
    if len(controls) != 1:
        raise SnapshotCatalogUnavailable(_SAFE)
    control = dict(controls[0])
    if (
        control["id"] != 1
        or control["contract_version"] != VERSION
        or not isinstance(control["migration_id"], str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", control["migration_id"]) is None
        or not _digest(control["source_cut_sha256"])
    ):
        raise SnapshotCatalogUnavailable(_SAFE)
    initial = _initial(json.loads(control["initial_json"]))
    if _json(initial) != control["initial_json"]:
        raise SnapshotCatalogUnavailable(_SAFE)
    rows = [
        dict(r)
        for r in connection.execute(select(event_table).order_by(event_table.c.revision)).mappings()
    ]
    if not len(initial) <= len(rows) <= 50_000 or len(_json(rows).encode("utf-8")) > _MAX_BYTES:
        raise SnapshotCatalogUnavailable(_SAFE)
    parent = None
    heads = {}
    fixed = {}
    for number, row in enumerate(rows, 1):
        descriptor = _pointer({k: row[k] for k in _KEYS})
        if row != _event(descriptor, number, parent, control):
            raise SnapshotCatalogUnavailable(_SAFE)
        if number <= len(initial) and descriptor != initial[number - 1]:
            raise SnapshotCatalogUnavailable(_SAFE)
        identity = (row["source_id"], row["snapshot_id"])
        if identity in fixed and fixed[identity] != descriptor:
            raise SnapshotCatalogUnavailable(_SAFE)
        fixed[identity] = descriptor
        heads[row["source_id"]] = {
            "source_id": row["source_id"],
            "revision": number,
            "event_sha256": row["event_sha256"],
        }
        parent = row["event_sha256"]
    actual = {r["source_id"]: dict(r) for r in connection.execute(select(head_table)).mappings()}
    if actual != heads:
        raise SnapshotCatalogUnavailable(_SAFE)
    return control, rows, heads, (control_table, event_table, head_table)


def install_schema(engine, *, initial_pointers, migration_id, source_cut_sha256, connection=None):
    """显式迁入已核固定选择；查询路径不得创建空schema或重置原迁移历史。"""
    initial = _initial(initial_pointers)
    control = {
        "id": 1,
        "migration_id": migration_id,
        "source_cut_sha256": source_cut_sha256,
        "initial_json": _json(initial),
        "contract_version": VERSION,
    }
    if (
        not isinstance(migration_id, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration_id) is None
        or not _digest(source_cut_sha256)
        or len(control["initial_json"].encode("utf-8")) > _MAX_BYTES
    ):
        raise SnapshotCatalogUnavailable(_SAFE)
    with installation_scope(engine, connection, _transaction, pg_lock=_LOCK) as selected:
        tables = _tables(selected, check=False)
        inspector = inspect(selected)
        if any(inspector.has_table(t.name, schema=t.schema) for t in tables):
            current, *_ = _state(selected)
            if current != control:
                raise SnapshotCatalogConflict("当前快照迁移身份或初始选择不同，未重置")
            return
        if selected.dialect.name == "postgresql":
            selected.execute(CreateSchema(sql_namespace.PG_SCHEMA, if_not_exists=True))
        tables[0].metadata.create_all(selected)
        selected.execute(insert(tables[0]).values(**control))
        parent = None
        for number, descriptor in enumerate(initial, 1):
            row = _event(descriptor, number, parent, control)
            selected.execute(insert(tables[1]).values(**row))
            selected.execute(
                insert(tables[2]).values(
                    source_id=row["source_id"], revision=number, event_sha256=row["event_sha256"]
                )
            )
            parent = row["event_sha256"]
        _state(selected)


class SQLSnapshotCatalog:
    """绑定同Engine与同缓存；保存选择和字节身份，不把它当完整远端可用性收据。"""

    def __init__(self, engine, cache_dir, *, publisher=None):
        self.engine = engine
        self.publisher = publisher
        self.cache_dir = Path(cache_dir).resolve()
        self._read_scope = ContextVar("official_snapshot_read_scope", default=None)
        self.metadata()

    @contextmanager
    def borrowed_read(self, connection):
        """只借受控短读事务；完整目录历史只核一次，退出后不保留状态。"""
        from sqlalchemy.engine import Connection
        if (not isinstance(connection, Connection) or connection.engine is not self.engine
                or connection.closed or not connection.in_transaction()
                or self._read_scope.get() is not None
                or not sql_namespace.is_read_phase_connection(connection)):
            raise SnapshotCatalogUnavailable(_SAFE)
        if connection.dialect.name == "postgresql":
            # 不能仅凭ContextVar或READ ONLY标志推断锁；核本后端真实64位目录共享锁。
            held = connection.execute(text(
                "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype='advisory' "
                "AND pid=pg_catalog.pg_backend_pid() AND classid::bigint=:high "
                "AND objid::bigint=:low AND objsubid=1 AND mode='ShareLock' AND granted"),
                {"high": _LOCK >> 32, "low": _LOCK & 0xffffffff}).scalar_one()
            if held != 1:
                raise SnapshotCatalogUnavailable(_SAFE)
        # 目录写入及发布收据提交共用_LOCK独占；本阶段已持共享锁。
        # 仅存不可变source/pin身份集合，不保存权限或整份发布资产。
        value = (get_ident(), connection, connection.get_transaction(), _state(connection), {}, self.publisher)
        token = self._read_scope.set(value)
        try:
            yield
        finally:
            self._read_scope.reset(token)

    @contextmanager
    def _read_state(self):
        scope = self._read_scope.get()
        if scope is not None:
            thread, connection, transaction, state = scope[:4]
            if (thread != get_ident() or connection.closed or connection.engine is not self.engine
                    or connection.get_transaction() is not transaction or not transaction.is_active):
                raise SnapshotCatalogUnavailable(_SAFE)
            # SAVEPOINT可回滚其后取得的锁，不复用阶段列/历史校验。
            yield _state(connection) if connection.in_nested_transaction() else state
        else:
            with _transaction(self.engine) as connection:
                yield _state(connection)

    def _publication_read_scope(self, connection, publisher):
        """发布读取只消费同目录、同publisher的受锁只读根阶段，退出即失效。"""
        scope = self._read_scope.get()
        if scope is None:
            return None
        thread, borrowed, transaction, state, identities, selected = scope
        if (thread != get_ident() or connection is not borrowed or connection.engine is not self.engine
                or connection.closed or connection.get_transaction() is not transaction
                or not transaction.is_active or publisher is not self.publisher or publisher is not selected):
            raise SnapshotCatalogUnavailable(_SAFE)
        if connection.in_nested_transaction():
            return None
        if not sql_namespace.is_read_phase_connection(connection):
            raise SnapshotCatalogUnavailable(_SAFE)
        readonly = (connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
                    if connection.dialect.name == "postgresql"
                    else connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1)
        if not readonly:
            raise SnapshotCatalogUnavailable(_SAFE)
        # ACCESS SHARE锁保护已核列；命名空间和TEMP歧义仍每个使用点即时拒绝。
        sql_namespace.bind_tables(connection, list(_TABLES))
        return state, identities

    def _publication_state(self, connection):
        scope = self._publication_read_scope(connection, self.publisher)
        if scope is None:
            raise SnapshotCatalogUnavailable(_SAFE)
        return scope[0]

    def metadata(self):
        with self._read_state() as state:
            control = state[0]
            return {k: control[k] for k in ("migration_id", "source_cut_sha256", "contract_version")}

    def history(self):
        with self._read_state() as state:
            return [dict(row) for row in state[1]]

    def get(self, source_id):
        _source(source_id)
        with self._read_state() as state:
            _, rows, heads, _ = state
            head = heads.get(source_id)
            return dict(rows[head["revision"] - 1]) if head is not None else None

    def publish(self, descriptor, *, expected_event_sha256, publication=None):
        """固定JSON已发布后更新当前选择；外部资产/向量的整代次签收另行执行。"""
        descriptor = _pointer(descriptor)
        if expected_event_sha256 is not None and not _digest(expected_event_sha256):
            raise SnapshotCatalogUnavailable(_SAFE)
        if self.publisher is not None:
            self.publisher.verify(publication, descriptor, expected_event_sha256)
        elif publication is not None:
            raise SnapshotCatalogUnavailable(_SAFE)
        with _transaction(self.engine, write=True) as connection:
            from archspec.persistence.acquisition_state import guard_current_execution
            guard_current_execution(connection, descriptor['source_id'])
            control, rows, heads, tables = _state(connection)
            head = heads.get(descriptor["source_id"])
            current = head["event_sha256"] if head is not None else None
            if current != expected_event_sha256:
                raise SnapshotCatalogConflict("当前快照已改变，未覆盖新的选择")
            for old in rows:
                if (old["source_id"], old["snapshot_id"]) == (
                    descriptor["source_id"],
                    descriptor["snapshot_id"],
                ) and {k: old[k] for k in _KEYS} != descriptor:
                    raise SnapshotCatalogConflict("固定快照字节身份冲突，未重绑")
            if head is not None and {k: rows[head["revision"] - 1][k] for k in _KEYS} == descriptor:
                guard_current_execution(connection, descriptor['source_id'])
                return dict(rows[head["revision"] - 1])
            row = _event(
                descriptor, len(rows) + 1, rows[-1]["event_sha256"] if rows else None, control
            )
            connection.execute(insert(tables[1]).values(**row))
            next_head = {
                "source_id": row["source_id"],
                "revision": row["revision"],
                "event_sha256": row["event_sha256"],
            }
            if head is None:
                connection.execute(insert(tables[2]).values(**next_head))
            elif (
                connection.execute(
                    update(tables[2])
                    .where(
                        tables[2].c.source_id == head["source_id"],
                        tables[2].c.event_sha256 == current,
                    )
                    .values(**next_head)
                ).rowcount
                != 1
            ):
                raise SnapshotCatalogConflict("当前快照提交冲突，未覆盖")
            if self.publisher is not None:
                self.publisher.commit(connection, row, publication,
                    expected_event_sha256=expected_event_sha256)
            _state(connection)
            # 发布内的SQL也可能耗时；到期则在同事务回滚事件及当前指针。
            guard_current_execution(connection, descriptor['source_id'])
            return row
