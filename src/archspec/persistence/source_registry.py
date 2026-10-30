"""来源登记事务后端；已有接口复用，不隐式建空库、回本地或启用REMOTE。"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from threading import get_ident

from sqlalchemy import (
    CheckConstraint,
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

from archspec.official_documents.registry import SOURCES, validate_source_pair
from archspec.persistence import sql_namespace
from archspec.persistence.control_migration import installation_scope

VERSION = "OFFICIAL_REGISTRY_SQL_V1"
_SAFE = "来源登记事务或完整历史无法核验，未回退本地、覆盖历史或签收成功"
_LOCK = 0x4152434853505352
_MAX_BYTES = 8 * 1024 * 1024
_META = MetaData()
_EVENTS = Table("official_registry_events", _META,
    Column("revision", Integer, primary_key=True),
    Column("event_sha", String(64), nullable=False, unique=True),
    Column("parent_sha", String(64)),
    Column("payload_json", Text, nullable=False),
    Column("builtin_sha", String(64), nullable=False),
    Column("migration_id", String(128), nullable=False),
    Column("source_cut_sha256", String(64), nullable=False),
    Column("contract_version", String(40), nullable=False),
    CheckConstraint("revision > 0"))
_HEAD = Table("official_registry_head", _META,
    Column("id", Integer, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("event_sha", String(64), nullable=False),
    CheckConstraint("id = 1"), CheckConstraint("revision > 0"))
_TABLES = (_EVENTS, _HEAD)


class SourceRegistryUnavailable(RuntimeError):
    """提交未知、身份歧义或缺迁移必须失败关闭，原值不进入错误。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload(value):
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise SourceRegistryUnavailable(_SAFE)
    fixed = json.loads(encoded)
    if not isinstance(fixed, dict) or not isinstance(fixed.get("sources"), list) or len(fixed["sources"]) > 20_000:
        raise SourceRegistryUnavailable(_SAFE)
    seen = set()
    for item in fixed["sources"]:
        name = item["source_id"]
        if not isinstance(name, str) or not re.fullmatch(r"official-[a-f0-9]{32}", name) or name in seen:
            raise SourceRegistryUnavailable(_SAFE)
        validate_source_pair(item["page_url"], item["pdf_url"],
            publication_kind=(item.get("source_metadata") or {}).get("provenance"))
        seen.add(name)
    return encoded


def _event(revision, payload, migration, cut, parent):
    value = {"revision": revision, "parent_sha": parent, "payload_json": payload,
             "builtin_sha": _sha(_json(SOURCES)), "migration_id": migration,
             "source_cut_sha256": cut, "contract_version": VERSION}
    return {**value, "event_sha": _sha(_json(value))}


def _physical(connection, *, check=True):
    tables = {t.name: t for t in sql_namespace.bind_tables(connection, _TABLES)}
    if check:
        sql_namespace.validate_columns(connection, tuple(tables.values()))
    return tables


@contextmanager
def _transaction(engine, *, write):
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            elif engine.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                if write:
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                fn = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
                connection.execute(text(f"SELECT pg_catalog.{fn}(:lock)"), {"lock": _LOCK})
            else:
                raise SourceRegistryUnavailable(_SAFE)
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except SourceRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001 - SQL/commit原值不进入前端或日志
        raise SourceRegistryUnavailable(_SAFE) from None


def _state(connection):
    tables = _physical(connection)
    heads = connection.execute(select(tables[_HEAD.name])).mappings().all()
    rows = connection.execute(select(tables[_EVENTS.name]).order_by(tables[_EVENTS.name].c.revision)).mappings().all()
    if len(heads) != 1 or heads[0]["id"] != 1 or not 1 <= len(rows) <= 50_000:
        raise SourceRegistryUnavailable(_SAFE)
    previous = None
    total = 0
    for number, row in enumerate(rows, 1):
        if (row["revision"] != number or row["parent_sha"] != previous
                or row["contract_version"] != VERSION
                or row["builtin_sha"] != _sha(_json(SOURCES))
                or not re.fullmatch(r"[a-f0-9]{64}", row["source_cut_sha256"])
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", row["migration_id"])
                or row["migration_id"] != rows[0]["migration_id"]
                or row["source_cut_sha256"] != rows[0]["source_cut_sha256"]
                or _payload(json.loads(row["payload_json"])) != row["payload_json"]):
            raise SourceRegistryUnavailable(_SAFE)
        expected = _event(number, row["payload_json"], row["migration_id"], row["source_cut_sha256"], previous)
        if dict(row) != expected:
            raise SourceRegistryUnavailable(_SAFE)
        total += len(_json(expected).encode("utf-8"))
        if total > _MAX_BYTES:
            raise SourceRegistryUnavailable(_SAFE)
        previous = row["event_sha"]
    if heads[0]["revision"] != len(rows) or heads[0]["event_sha"] != previous:
        raise SourceRegistryUnavailable(_SAFE)
    return tables, [dict(row) for row in rows]


def install_schema(engine, *, initial_payload, migration_id, source_cut_sha256, connection=None):
    """只在显式迁移调用初始化；缺来源不能由查询路径补成空库。"""
    try:
        payload = _payload(initial_payload)
        if (not isinstance(migration_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration_id)
                or not isinstance(source_cut_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", source_cut_sha256)):
            raise SourceRegistryUnavailable(_SAFE)
        first = _event(1, payload, migration_id, source_cut_sha256, None)
        with installation_scope(engine, connection, _transaction, pg_lock=_LOCK) as selected:
            tables = _physical(selected, check=False)
            inspector = inspect(selected)
            present = [inspector.has_table(t.name, schema=t.schema) for t in tables.values()]
            if any(present):
                _, history = _state(selected)
                if history[0] != first:
                    raise SourceRegistryUnavailable(_SAFE)
                return
            if engine.dialect.name == "postgresql":
                selected.execute(CreateSchema(sql_namespace.PG_SCHEMA, if_not_exists=True))
            next(iter(tables.values())).metadata.create_all(selected)
            tables = _physical(selected)
            selected.execute(insert(tables[_EVENTS.name]).values(**first))
            selected.execute(insert(tables[_HEAD.name]).values(id=1, revision=1, event_sha=first["event_sha"]))
            _state(selected)
    except SourceRegistryUnavailable:
        raise
    except Exception:  # noqa: BLE001
        raise SourceRegistryUnavailable(_SAFE) from None


class SQLSourceRegistryBackend:
    """当前指针和完整历史共事务，非默认应用的远端激活证明。"""
    def __init__(self, engine):
        self.engine = engine
        self._scope = ContextVar("official_registry_connection", default=None)
        self._read_only = ContextVar("official_registry_read_only", default=False)
        self._verified_rows = ContextVar("official_registry_verified_read_rows", default=None)
        with _transaction(engine, write=False) as connection:
            _state(connection)

    @contextmanager
    def borrowed_read(self, connection, *, reuse_state=False):
        """在原目录事务复核来源，只借同Engine连接，不提交也不允许修改来源。"""
        from sqlalchemy.engine import Connection
        if (not isinstance(connection, Connection) or connection.engine is not self.engine
                or connection.closed or not connection.in_transaction() or self._scope.get() is not None
                or reuse_state and not sql_namespace.is_read_phase_connection(connection)):
            raise SourceRegistryUnavailable(_SAFE)
        token = self._scope.set((get_ident(), connection, connection.get_transaction()))
        readonly = self._read_only.set(True)
        verified = self._verified_rows.set(None)
        try:
            _, rows = _state(connection)
            # 只有真实只读短阶段允许复用；发布借用写事务仍每次复核历史。
            if reuse_state:
                self._verified_rows.set(rows)
            yield
        finally:
            self._verified_rows.reset(verified)
            self._read_only.reset(readonly)
            self._scope.reset(token)

    @contextmanager
    def mutation(self):
        if self._scope.get() is not None:
            raise SourceRegistryUnavailable(_SAFE)
        with _transaction(self.engine, write=True) as connection:
            _state(connection)
            token = self._scope.set((get_ident(), connection, connection.get_transaction()))
            try:
                yield
                _state(connection)
            finally:
                self._scope.reset(token)

    def _connection(self):
        value = self._scope.get()
        if value is None:
            return None
        if (value[0] != get_ident() or value[1].closed or not value[1].in_transaction()
                or value[1].get_transaction() is not value[2]):
            raise SourceRegistryUnavailable(_SAFE)
        return value[1]

    def history(self):
        try:
            connection = self._connection()
            if connection is not None:
                rows = self._verified_rows.get()
                if rows is None or connection.in_nested_transaction():
                    _, rows = _state(connection)
            else:
                with _transaction(self.engine, write=False) as connection:
                    _, rows = _state(connection)
            return [{**{k: v for k, v in row.items() if k != "payload_json"},
                     "payload": json.loads(row["payload_json"])} for row in rows]
        except SourceRegistryUnavailable:
            raise
        except Exception:  # noqa: BLE001
            raise SourceRegistryUnavailable(_SAFE) from None

    def load(self):
        return self.history()[-1]["payload"]

    def save(self, payload):
        try:
            if self._read_only.get():raise SourceRegistryUnavailable(_SAFE)
            connection = self._connection()
            if connection is None:
                raise SourceRegistryUnavailable(_SAFE)
            tables, rows = _state(connection)
            encoded = _payload(payload)
            old = rows[-1]
            if encoded == old["payload_json"]:
                return
            new = _event(old["revision"] + 1, encoded, old["migration_id"],
                         old["source_cut_sha256"], old["event_sha"])
            connection.execute(insert(tables[_EVENTS.name]).values(**new))
            count = connection.execute(update(tables[_HEAD.name]).where(
                tables[_HEAD.name].c.id == 1, tables[_HEAD.name].c.revision == old["revision"],
                tables[_HEAD.name].c.event_sha == old["event_sha"]).values(
                    revision=new["revision"], event_sha=new["event_sha"])).rowcount
            if count != 1:
                raise SourceRegistryUnavailable(_SAFE)
            _state(connection)
        except SourceRegistryUnavailable:
            raise
        except Exception:  # noqa: BLE001
            raise SourceRegistryUnavailable(_SAFE) from None
