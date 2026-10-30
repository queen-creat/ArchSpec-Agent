"""独立持久预算仓库：预留先提交，模型调用在事务外；不激活旧默认入口。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateSchema

from archspec.persistence import sql_namespace
from archspec.persistence.control_migration import installation_scope

CONTRACT_VERSION = "SQL_BUDGET_V1"
_BUDGET_LOCK = 0x4152434853504255
_SCHEMA = MetaData()
_BUDGET = Table(
    "external_development_budget",
    _SCHEMA,
    Column("id", Integer, primary_key=True),
    Column("limit_cents", Integer, nullable=False),
    CheckConstraint("id = 1"),
    CheckConstraint("limit_cents BETWEEN 0 AND 1200"),
)
_CALLS = Table(
    "external_development_calls",
    _SCHEMA,
    Column("call_token", String(36), primary_key=True),
    Column("legacy_id", Integer, nullable=True, unique=True),
    Column("request_digest", String(64), nullable=False),
    Column("reserved_cents", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("actual_model", Text),
    Column("actual_usage", Text),
    Column("request_id", Text),
    CheckConstraint("reserved_cents = 10"),
    CheckConstraint("status IN ('RESERVED', 'COMPLETED', 'FAILED')"),
)
_MIGRATIONS = Table(
    "external_budget_migrations",
    _SCHEMA,
    Column("migration_id", String(128), primary_key=True),
    Column("contract_version", String(32), nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("limit_cents", Integer, nullable=False),
    Column("legacy_call_count", Integer, nullable=False),
    Column("legacy_reserved_cents", Integer, nullable=False),
)


class BudgetUnavailable(RuntimeError):
    """状态库不可核验或提交结果未知；不得继续调用模型。"""


class BudgetConflict(ValueError):
    """预算不足、重复调用或迁移目标冲突；不得覆盖或退款。"""


@dataclass(frozen=True)
class BudgetReservation:
    call_token: str
    request_digest: str
    reserved_cents: int = 10


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _limit(value):
    if type(value) is not int or not 0 <= value <= 1200:
        raise ValueError("累计预算限额必须是0至1200分")
    return value


def _token(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("预算调用编号必须是完整UUID")
    try:
        normalized = str(UUID(value))
    except ValueError:
        raise ValueError("预算调用编号必须是完整UUID") from None
    if normalized != value.lower():
        raise ValueError("预算调用编号必须是完整UUID")
    return normalized


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("预算请求摘要必须是小写SHA256")
    return value


def _usage(turn):
    usage = getattr(turn, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    cached = getattr(usage, "cached_tokens", None)
    if (
        type(prompt) is not int
        or type(completion) is not int
        or prompt < 0
        or completion < 0
        or prompt + completion == 0
        or (cached is not None and (type(cached) is not int or not 0 <= cached <= prompt))
    ):
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached}


def _checked_usage(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("历史用量记录格式无效")  # noqa: TRY004 - 迁移输入统一拒绝为ValueError
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or set(parsed) != {
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
        }:
            raise ValueError
        prompt, completion, cached = (
            parsed[key] for key in ("prompt_tokens", "completion_tokens", "cached_tokens")
        )
        if (
            type(prompt) is not int
            or type(completion) is not int
            or prompt < 0
            or completion < 0
            or prompt + completion <= 0
            or (cached is not None and (type(cached) is not int or not 0 <= cached <= prompt))
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("历史用量记录格式无效")
    return parsed


def _history(records, migration_id):
    result, identities = [], set()
    for record in records:
        if not isinstance(record, dict) or set(record) - {
            "id",
            "request_digest",
            "reserved_cents",
            "status",
            "actual_model",
            "actual_usage",
            "request_id",
        }:
            raise ValueError("历史预算记录字段无效")
        identity = record.get("id")
        if type(identity) is not int or identity <= 0 or identity in identities:
            raise ValueError("历史预算调用编号必须唯一")
        identities.add(identity)
        if record.get("reserved_cents") != 10 or type(record.get("reserved_cents")) is not int:
            raise ValueError("每次历史调用必须保留10分预留")
        if record.get("status") not in {"RESERVED", "COMPLETED", "FAILED"}:
            raise ValueError("历史预算调用状态无效")
        _checked_usage(record.get("actual_usage"))
        for key in ("actual_model", "request_id"):
            if record.get(key) is not None and not isinstance(record[key], str):
                raise ValueError("历史模型元数据格式无效")
        result.append(
            {
                "call_token": str(
                    uuid5(NAMESPACE_URL, f"archspec-budget:{migration_id}:{identity}")
                ),
                "legacy_id": identity,
                "request_digest": _digest(record.get("request_digest")),
                "reserved_cents": 10,
                "status": record["status"],
                "actual_model": record.get("actual_model"),
                "actual_usage": record.get("actual_usage"),
                "request_id": record.get("request_id"),
            }
        )
    return sorted(result, key=lambda row: row["legacy_id"])


@contextmanager
def _transaction(engine, *, write):
    """SQLite离线验收用写锁；PG候选使用短事务锁，均不跨模型网络持锁。"""
    if engine.dialect.name not in {"sqlite", "postgresql"}:
        raise ValueError("预算仓库只支持SQLite与PostgreSQL候选事务")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            else:
                connection.begin()
                if write:
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                    connection.execute(
                        text("SELECT pg_catalog.pg_advisory_xact_lock(:budget_lock)"),
                        {"budget_lock": _BUDGET_LOCK},
                    )
            try:
                yield connection
                # commit返回成功之前不得向调用者发放预留收据。
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (BudgetUnavailable, BudgetConflict):
        raise
    except Exception:  # noqa: BLE001 - SQL及未知commit异常不得泄露DSN或提供方响应
        raise BudgetUnavailable("持久预算事务失败或提交结果未知，模型调用已停止") from None


def _physical_tables(connection):
    """统一反射与执行对象，不让TEMP/search_path/有效schema映射改变账本。"""
    try:
        return {table.name: table for table in sql_namespace.bind_tables(
            connection, (_BUDGET, _CALLS, _MIGRATIONS))}
    except sql_namespace.NamespaceUnavailable:
        raise BudgetUnavailable("持久预算物理命名空间无法唯一核对") from None


def _tables_present(connection):
    tables = _physical_tables(connection)
    inspector = inspect(connection)
    for table in tables.values():
        if not inspector.has_table(table.name, schema=table.schema):
            raise BudgetUnavailable("持久预算schema尚未显式安装")
        if not {column.name for column in table.columns} <= {
            column["name"] for column in inspector.get_columns(table.name, schema=table.schema)
        }:
            raise BudgetUnavailable("持久预算schema列不完整")
    return tables


def _migration_manifest(migration_id, limit_cents, records):
    """收据绑定迁移身份和完整原始历史，不纳入之后的新调用。"""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "migration_id": migration_id,
        "limit_cents": limit_cents,
        "records": records,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _state(connection):
    tables = _tables_present(connection)
    limits = connection.execute(select(tables[_BUDGET.name])).mappings().all()
    migrations = connection.execute(select(tables[_MIGRATIONS.name])).mappings().all()
    rows = connection.execute(select(tables[_CALLS.name])).mappings().all()
    if len(limits) != 1 or limits[0]["id"] != 1 or len(migrations) != 1:
        raise BudgetUnavailable("持久预算额度行或迁移收据缺失")
    limit, receipt = limits[0]["limit_cents"], migrations[0]
    try:
        _limit(limit)
        _digest(receipt["manifest_sha256"])
        if (
            receipt["contract_version"] != CONTRACT_VERSION
            or receipt["limit_cents"] != limit
            or type(receipt["legacy_call_count"]) is not int
            or receipt["legacy_call_count"] < 0
            or receipt["legacy_reserved_cents"] != receipt["legacy_call_count"] * 10
            or sum(row["legacy_id"] is not None for row in rows) != receipt["legacy_call_count"]
        ):
            raise ValueError
        historical = sorted(
            (dict(row) for row in rows if row["legacy_id"] is not None),
            key=lambda row: row["legacy_id"],
        )
        originals = [
            {
                "id": row["legacy_id"],
                **{
                    key: row[key]
                    for key in (
                        "request_digest",
                        "reserved_cents",
                        "status",
                        "actual_model",
                        "actual_usage",
                        "request_id",
                    )
                },
            }
            for row in historical
        ]
        # 重新核对历史UUID派生身份及全字段SHA，合法形状不能代替历史一致性。
        if historical != _history(originals, receipt["migration_id"]) or receipt[
            "manifest_sha256"
        ] != _migration_manifest(receipt["migration_id"], limit, historical):
            raise ValueError
        for row in rows:
            _token(row["call_token"])
            _digest(row["request_digest"])
            _checked_usage(row["actual_usage"])
            if (
                type(row["reserved_cents"]) is not int
                or row["reserved_cents"] != 10
                or row["status"] not in {"RESERVED", "COMPLETED", "FAILED"}
            ):
                raise ValueError
        if sum(row["reserved_cents"] for row in rows) > limit:
            raise ValueError
    except (ValueError, TypeError):
        raise BudgetUnavailable("持久预算迁移收据或累计账本无法核对") from None
    return limit, rows


def install_budget_schema(engine, *, legacy_records, limit_cents, migration_id, connection=None):
    """显式初始化新目标或逐字段幂等核对相同迁移；绝不覆盖不同历史。"""
    limit_cents = _limit(limit_cents)
    if (
        not isinstance(migration_id, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration_id) is None
    ):
        raise ValueError("预算迁移编号必须是有限安全标识")
    records = _history(legacy_records, migration_id)
    if len(records) * 10 > limit_cents:
        raise ValueError("历史预留已超过所提供的累计限额")
    receipt = {
        "migration_id": migration_id,
        "contract_version": CONTRACT_VERSION,
        "manifest_sha256": _migration_manifest(migration_id, limit_cents, records),
        "limit_cents": limit_cents,
        "legacy_call_count": len(records),
        "legacy_reserved_cents": len(records) * 10,
    }
    with installation_scope(engine, connection, _transaction, pg_lock=_BUDGET_LOCK) as selected:
        # 先拒绝歧义；只有显式install创建固定私有schema/表，不改主业务Base或public。
        tables = _physical_tables(selected)
        if selected.dialect.name == "postgresql":
            selected.execute(CreateSchema(sql_namespace.PG_SCHEMA, if_not_exists=True))
        tables[_BUDGET.name].metadata.create_all(selected)
        existing_limits = selected.execute(select(tables[_BUDGET.name])).mappings().all()
        existing_receipts = selected.execute(select(tables[_MIGRATIONS.name])).mappings().all()
        existing_calls = selected.execute(select(tables[_CALLS.name])).mappings().all()
        if existing_limits or existing_receipts or existing_calls:
            historical = sorted(
                (dict(row) for row in existing_calls if row["legacy_id"] is not None),
                key=lambda row: row["legacy_id"],
            )
            if (
                existing_receipts != [receipt]
                or existing_limits != [{"id": 1, "limit_cents": limit_cents}]
                or historical != records
            ):
                raise BudgetConflict("预算迁移目标已有不同历史、收据或额度，禁止覆盖")
            _state(selected)
        else:
            selected.execute(insert(tables[_BUDGET.name]).values(id=1, limit_cents=limit_cents))
            if records:
                selected.execute(insert(tables[_CALLS.name]), records)
            selected.execute(insert(tables[_MIGRATIONS.name]).values(**receipt))
    return receipt


class SQLBudgetJournal:
    def __init__(self, engine):
        self.engine = engine
        with _transaction(engine, write=False) as connection:
            _state(connection)

    def reserve(self, call_token, request_digest) -> BudgetReservation:
        call_token, request_digest = _token(call_token), _digest(request_digest)
        with _transaction(self.engine, write=True) as connection:
            reservation = self.reserve_on_connection(connection, call_token, request_digest)
        # 外层commit已确认才能把收据交给模型网关；primitive自身不能证明已提交。
        return reservation

    def reserve_on_connection(self, connection, call_token, request_digest) -> BudgetReservation:
        """在同可信Engine的现存短事务预留；不打开连接或拥有事务出口。

        回执只是本事务中的预留候选，调用方必须确认外层commit后才发送模型。
        本方法仅消费已签收schema；Runtime验证器负责排除未审核触发器和额外列。
        """
        call_token, request_digest = _token(call_token), _digest(request_digest)
        if (not isinstance(connection, Connection) or connection.engine is not self.engine
                or connection.closed or not connection.in_transaction()
                or connection.get_transaction() is None
                or not connection.get_transaction().is_active):
            raise BudgetUnavailable("同连接预留需要同可信Engine的现存有效事务")
        try:
            tables = _physical_tables(connection)
            if connection.dialect.name == "sqlite":
                if not connection.connection.driver_connection.in_transaction:
                    raise BudgetUnavailable("SQLite预留需要已开始的真实短事务")
                _tables_present(connection)
                # 取得写意图后才核余额，BEGIN DEFERRED也不能凭旧读取快照超支。
                # id=id不改变额度行/限额/历史；目标schema不得有未经审核的触发器。
                limit_table = tables[_BUDGET.name]
                connection.execute(update(limit_table).where(limit_table.c.id == 1).values(id=limit_table.c.id))
            elif connection.dialect.name == "postgresql":
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:budget_lock)"),
                                   {"budget_lock": _BUDGET_LOCK})
            else:
                raise BudgetUnavailable("同连接预算仅支持已验SQLite与PG候选")
            limit, rows = _state(connection)
            if any(row["call_token"] == call_token for row in rows):
                raise BudgetConflict("同一预算调用编号已有记录，不会再次发送模型请求")
            if sum(row["reserved_cents"] for row in rows) + 10 > limit:
                raise BudgetConflict("开发累计预算已用完，不扩大额度或自动重试")
            connection.execute(insert(tables[_CALLS.name]).values(
                call_token=call_token, legacy_id=None, request_digest=request_digest,
                reserved_cents=10, status="RESERVED", actual_model=None,
                actual_usage=None, request_id=None))
            return BudgetReservation(call_token, request_digest)
        except (BudgetUnavailable, BudgetConflict):
            raise
        except Exception:  # noqa: BLE001 - 驱动/未知状态不得传播路径、凭据或上游响应
            raise BudgetUnavailable("同连接预算预留未完成；外层事务须停止，不继续模型") from None

    def finish(self, call_token, status, turn=None) -> None:
        call_token = _token(call_token)
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("预算调用必须登记完成或失败状态")
        usage = _usage(turn)
        with _transaction(self.engine, write=True) as connection:
            _, rows = _state(connection)
            if any(
                row["call_token"] == call_token and row["legacy_id"] is not None for row in rows
            ):
                # 旧RESERVED仍是历史未结；不能用新进程推断其实际调用结果或续发。
                raise BudgetConflict("迁入的预算历史不可改写，旧未结预留继续保留")
            calls_table = _physical_tables(connection)[_CALLS.name]
            changed = connection.execute(
                update(calls_table)
                .where(
                    calls_table.c.call_token == call_token,
                    calls_table.c.status == "RESERVED",
                    calls_table.c.legacy_id.is_(None),
                )
                .values(
                    status=status,
                    actual_model=getattr(turn, "model", None),
                    actual_usage=_json(usage) if usage is not None else None,
                    request_id=getattr(turn, "request_id", None),
                )
            )
            if changed.rowcount != 1:
                raise BudgetConflict("预算调用不存在或已经结束，不能重写终态")

    def metadata(self):
        with _transaction(self.engine, write=False) as connection:
            limit, rows = _state(connection)
        known = [json.loads(row["actual_usage"]) for row in rows if row["actual_usage"] is not None]
        cached = [row["cached_tokens"] for row in known if row["cached_tokens"] is not None]
        return {
            "reserved_cny": sum(row["reserved_cents"] for row in rows) / 100,
            "limit_cny": limit / 100,
            "call_count": len(rows),
            "reservation_cny_per_call": 0.10,
            "budget_mode": "CUMULATIVE_DEVELOPMENT_RESERVATION",
            "actual_cost_cny": None,
            "actual_usage": {
                "reported_calls": len(known),
                "unknown_calls": len(rows) - len(known),
                "prompt_tokens": sum(row["prompt_tokens"] for row in known) if known else None,
                "completion_tokens": sum(row["completion_tokens"] for row in known)
                if known
                else None,
                "cached_tokens": sum(cached) if cached else None,
            },
            "notice": "预留上限不是实际扣费；未知用量和失败仍保留预留，未来费率变更须重新核验。",
        }


class RemoteDevelopmentBudgetGateway:
    def __init__(self, gateway, journal, call_token_factory=None):
        self.gateway, self.journal = gateway, journal
        self.call_token_factory = call_token_factory or (lambda: str(uuid4()))
        self._check_gateway()

    def _check_gateway(self):
        cap = getattr(self.gateway, "max_tokens", None)
        if (
            getattr(self.gateway, "model", None) != "deepseek-flash"
            or type(cap) is not int
            or not 64 <= cap <= 1024
            or getattr(self.gateway, "thinking_disabled", None) is not True
        ):
            raise ValueError("开发预算仅允许deepseek-flash、关闭thinking、64至1024输出token")

    def complete(self, messages, tools):
        self._check_gateway()
        content = json.dumps(
            {"messages": messages, "tools": tools},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(content) > 16000:
            raise ValueError("模型messages与tools合计不能超过16000 UTF-8字节")
        packet = json.loads(content)
        digest = hashlib.sha256(content).hexdigest()
        # 调用者重试同一逻辑调用时须复用工厂的稳定token；重复token永远不能再次发送。
        reservation = self.reserve_for_packet(packet, digest)
        try:
            turn = self.gateway.complete(packet["messages"], packet["tools"])
        except BaseException:
            self.journal.finish(reservation.call_token, "FAILED")
            raise
        self.journal.finish(reservation.call_token, "COMPLETED", turn)
        return turn

    def reserve_for_packet(self, packet, digest):
        """输入已完整规范化；任务运行配置可在同事务认领阶段后再发送。"""
        return self.journal.reserve(self.call_token_factory(), digest)

    def metadata(self):
        return self.journal.metadata()


def export_budget_records(path: Path) -> tuple[int, list[dict]]:
    """显式只读导出旧账本的一致视图；当前实现不会主动对真实data目录调用。"""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("旧预算账本必须是已存在的普通文件")
    try:
        uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            limits = connection.execute("SELECT id, limit_cents FROM main.development_budget").fetchall()
            if len(limits) != 1 or limits[0]["id"] != 1:
                raise BudgetUnavailable("旧预算账本额度行无法核对")
            limit = _limit(limits[0]["limit_cents"])
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, request_digest, reserved_cents, status, actual_model, actual_usage, request_id "
                    "FROM main.development_calls ORDER BY id"
                )
            ]
            _history(rows, "read-only-export")
            if len(rows) * 10 > limit:
                raise BudgetUnavailable("旧预算账本累计预留超过限额")
            return limit, rows
    except BudgetUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 只读迁移异常不含路径或源账本内容
        raise BudgetUnavailable("旧预算账本无法一致只读导出") from None
