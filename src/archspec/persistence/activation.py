"""统一激活协调原型：只绑定可信验证器签收，不证明实际业务闭包或启用 REMOTE。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.schema import CreateSchema

CONTRACT_VERSION = "ACTIVATION_RECEIPT_V1"
COMPONENTS = ("assets", "catalog", "governance", "qa_tasks", "budget", "acquisition")
_LOCK = 0x4152434853504143
_PG_SCHEMA = "archspec_control"
_SAFE_FAILURE = "激活账本不可核验或提交结果未知，未签发成功回执"


class ActivationUnavailable(RuntimeError):
    """缺配置、账本不可核验或提交回执未知；调用者必须独立核对。"""


class ActivationConflict(ValueError):
    """已知验证拒绝或候选冲突；不可覆盖历史或激活该候选。"""


@dataclass(frozen=True)
class CandidateReceipt:
    candidate_id: str
    migration_id: str
    generation: int
    source_cut_sha256: str
    components: tuple[tuple[str, str], ...]
    contract_version: str = CONTRACT_VERSION

    @property
    def component_sha256s(self):
        """返回副本，避免外部字典修改已经签收的候选字段。"""
        return dict(self.components)


@dataclass(frozen=True)
class ComponentReceipt:
    candidate_id: str
    migration_id: str
    generation: int
    component: str
    manifest_sha256: str
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True)
class ActiveReceipt:
    activation_sha256: str
    candidate_id: str
    migration_id: str
    generation: int
    previous_activation_sha256: str | None
    contract_version: str = CONTRACT_VERSION


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("收据摘要必须是小写SHA256")
    return value


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("迁移编号必须是规范UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValueError("迁移编号必须是规范UUID") from None
    return value


def _generation(value):
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise ValueError("代次必须是正整数")
    return value


def _component(value):
    if not isinstance(value, str) or value not in COMPONENTS:
        raise ValueError("组件必须属于固定六组件")
    return value


def _candidate(*, migration_id, generation, source_cut_sha256, component_sha256s):
    if not isinstance(component_sha256s, dict) or set(component_sha256s) != set(COMPONENTS):
        raise ValueError("候选必须恰有固定六组件摘要")
    manifests = tuple((name, _digest(component_sha256s[name])) for name in COMPONENTS)
    body = {
        "migration_id": _uuid(migration_id),
        "generation": _generation(generation),
        "source_cut_sha256": _digest(source_cut_sha256),
        "component_sha256s": dict(manifests),
        "contract_version": CONTRACT_VERSION,
    }
    return CandidateReceipt(_sha(body), body["migration_id"], body["generation"],
                            body["source_cut_sha256"], manifests)


def _tables(engine):
    if engine.dialect.name not in {"sqlite", "postgresql"}:
        raise ValueError("协调账本只支持SQLite和PostgreSQL候选")
    schema = _PG_SCHEMA if engine.dialect.name == "postgresql" else None
    metadata = MetaData(schema=schema)
    candidates = Table(
        "external_state_candidates", metadata,
        Column("candidate_id", String(64), primary_key=True),
        Column("migration_id", String(36), nullable=False),
        Column("generation", BigInteger, nullable=False),
        Column("source_cut_sha256", String(64), nullable=False),
        Column("component_sha256s", Text, nullable=False),
        Column("contract_version", String(32), nullable=False),
        UniqueConstraint("migration_id", "generation"),
        CheckConstraint("generation > 0"),
    )
    components = Table(
        "external_state_components", metadata,
        Column("candidate_id", String(64), primary_key=True),
        Column("component", String(16), primary_key=True),
        Column("migration_id", String(36), nullable=False),
        Column("generation", BigInteger, nullable=False),
        Column("manifest_sha256", String(64), nullable=False),
        Column("contract_version", String(32), nullable=False),
    )
    rejections = Table(
        "external_state_rejections", metadata,
        Column("candidate_id", String(64), primary_key=True),
        Column("reason", String(32), nullable=False),
        Column("contract_version", String(32), nullable=False),
    )
    publications = Table(
        "external_state_publications", metadata,
        Column("activation_sha256", String(64), primary_key=True),
        Column("candidate_id", String(64), nullable=False, unique=True),
        Column("migration_id", String(36), nullable=False),
        Column("generation", BigInteger, nullable=False),
        Column("previous_activation_sha256", String(64), nullable=True),
        Column("contract_version", String(32), nullable=False),
    )
    active = Table(
        "external_state_active", metadata,
        Column("id", Integer, primary_key=True),
        Column("activation_sha256", String(64), nullable=True),
        Column("contract_version", String(32), nullable=False),
        CheckConstraint("id = 1"),
    )
    return metadata, candidates, components, rejections, publications, active


@contextmanager
def _transaction(engine, *, write):
    """只做短SQL核对；可信回调合同禁止在此处下载、OCR或调用模型。"""
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            else:
                # 整批控制映像复核需要同一稳定视图，不能使用默认READ COMMITTED。
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                if write:
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                    connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"),
                                       {"lock": _LOCK})
            try:
                yield connection
                # 未确认提交前不向调用者返回签收或发布收据。
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (ActivationConflict, ActivationUnavailable):
        raise
    except Exception:  # noqa: BLE001 - SQL/提供方异常可能含DSN，不传播原文
        raise ActivationUnavailable(_SAFE_FAILURE) from None


def _ensure_schema(connection, tables):
    metadata, *definitions = tables
    inspector = inspect(connection)
    names = set(inspector.get_table_names(schema=metadata.schema))
    if not {table.name for table in definitions}.issubset(names):
        raise ActivationUnavailable("激活协调schema尚未显式安装或不完整")
    for table in definitions:
        columns = {row["name"] for row in inspector.get_columns(table.name, schema=metadata.schema)}
        if columns != set(table.c.keys()):
            raise ActivationUnavailable("激活协调schema合同不完整")
    rows = connection.execute(select(tables[-1])).mappings().all()
    if len(rows) != 1 or rows[0]["id"] != 1 or rows[0]["contract_version"] != CONTRACT_VERSION:
        raise ActivationUnavailable("活跃指针缺失或版本不符")
    if rows[0]["activation_sha256"] is not None:
        try:
            _digest(rows[0]["activation_sha256"])
        except ValueError:
            raise ActivationUnavailable("活跃指针无效") from None
    elif connection.execute(select(tables[-2].c.activation_sha256).limit(1)).first():
        raise ActivationUnavailable("发布历史存在但活跃指针为空")


def install_schema(engine):
    """显式安装固定私有协调表；部分缺失或旧指针丢失时拒绝补成空库。"""
    tables = _tables(engine)
    metadata, *definitions = tables
    with _transaction(engine, write=True) as connection:
        if metadata.schema:
            connection.execute(CreateSchema(metadata.schema, if_not_exists=True))
        existing = set(inspect(connection).get_table_names(schema=metadata.schema))
        expected = {table.name for table in definitions}
        if expected & existing:
            _ensure_schema(connection, tables)
            return
        metadata.create_all(connection)
        connection.execute(insert(tables[-1]).values(
            id=1, activation_sha256=None, contract_version=CONTRACT_VERSION
        ))
        _ensure_schema(connection, tables)


class SQLActivationLedger:
    """独立协调入口；默认应用未接，真实PG和迁移闭包尚未验收。"""

    def __init__(self, engine, *, verify_component=None, verify_generation=None):
        self.engine = engine
        self.tables = _tables(engine)
        self.verify_component = verify_component
        self.verify_generation = verify_generation
        with _transaction(engine, write=False) as connection:
            _ensure_schema(connection, self.tables)

    def _candidate(self, connection, candidate_id):
        row = connection.execute(select(self.tables[1]).where(
            self.tables[1].c.candidate_id == candidate_id
        )).mappings().one_or_none()
        if row is None:
            raise ActivationConflict("候选不存在")
        try:
            if row["contract_version"] != CONTRACT_VERSION:
                raise ValueError
            manifests = json.loads(row["component_sha256s"])
            result = _candidate(
                migration_id=row["migration_id"], generation=row["generation"],
                source_cut_sha256=row["source_cut_sha256"], component_sha256s=manifests
            )
            if result.candidate_id != row["candidate_id"] or _json(manifests) != row["component_sha256s"]:
                raise ValueError
        except (ValueError, TypeError):
            raise ActivationUnavailable("候选收据字段不一致") from None
        return result

    def _components(self, connection, candidate):
        rows = connection.execute(select(self.tables[2]).where(
            self.tables[2].c.candidate_id == candidate.candidate_id
        )).mappings().all()
        result = {}
        for row in rows:
            name = row["component"]
            if (name not in COMPONENTS or name in result
                    or row["migration_id"] != candidate.migration_id
                    or row["generation"] != candidate.generation
                    or row["contract_version"] != CONTRACT_VERSION
                    or row["manifest_sha256"] != candidate.component_sha256s[name]):
                raise ActivationUnavailable("组件收据代次或摘要不一致")
            result[name] = ComponentReceipt(candidate.candidate_id, candidate.migration_id,
                                            candidate.generation, name, row["manifest_sha256"])
        return tuple(result[name] for name in COMPONENTS if name in result)

    def _blocked(self, connection, candidate):
        row = connection.execute(select(self.tables[3]).where(
            self.tables[3].c.candidate_id == candidate.candidate_id
        )).mappings().one_or_none()
        if row:
            if row["contract_version"] != CONTRACT_VERSION or row["reason"] not in {
                "COMPONENT_REJECTED", "GENERATION_REJECTED", "INCOMPLETE",
                "STALE_POINTER", "NONINCREASING",
            }:
                raise ActivationUnavailable("拒绝收据无效")
            raise ActivationConflict("候选已有拒绝收据")
        if connection.execute(select(self.tables[4].c.activation_sha256).where(
            self.tables[4].c.candidate_id == candidate.candidate_id
        )).first():
            raise ActivationConflict("候选已发布，不得再次写入或改指针")

    def _reject(self, connection, candidate, reason):
        connection.execute(insert(self.tables[3]).values(
            candidate_id=candidate.candidate_id, reason=reason, contract_version=CONTRACT_VERSION
        ))

    @staticmethod
    def _publication_sha(candidate, components, previous):
        return _sha({
            "contract_version": CONTRACT_VERSION,
            "candidate_id": candidate.candidate_id,
            "migration_id": candidate.migration_id,
            "generation": candidate.generation,
            "previous_activation_sha256": previous,
            "components": [
                {"candidate_id": row.candidate_id, "migration_id": row.migration_id,
                 "generation": row.generation, "component": row.component,
                 "manifest_sha256": row.manifest_sha256, "contract_version": row.contract_version}
                for row in components
            ],
        })

    def _active(self, connection):
        pointer = connection.execute(select(self.tables[-1].c.activation_sha256)).scalar_one()
        if pointer is None:
            return None
        current, seen, newest = pointer, set(), None
        previous_generation = None
        # 全部前序发布保留并重新绑定，断链或回退即拒绝；不检查正文及权限业务闭包。
        while current is not None:
            if current in seen:
                raise ActivationUnavailable("发布历史存在循环")
            seen.add(current)
            row = connection.execute(select(self.tables[4]).where(
                self.tables[4].c.activation_sha256 == current
            )).mappings().one_or_none()
            if row is None:
                raise ActivationUnavailable("发布历史缺失")
            try:
                _digest(current)
                candidate = self._candidate(connection, row["candidate_id"])
                components = self._components(connection, candidate)
                previous = row["previous_activation_sha256"]
                if previous is not None:
                    _digest(previous)
                if (len(components) != len(COMPONENTS)
                        or row["migration_id"] != candidate.migration_id
                        or row["generation"] != candidate.generation
                        or row["contract_version"] != CONTRACT_VERSION
                        or self._publication_sha(candidate, components, previous) != current
                        or (previous_generation is not None
                            and candidate.generation >= previous_generation)):
                    raise ValueError
                if connection.execute(select(self.tables[3].c.candidate_id).where(
                    self.tables[3].c.candidate_id == candidate.candidate_id
                )).first():
                    raise ValueError
            except (ValueError, ActivationConflict):
                raise ActivationUnavailable("发布收据字段或完整组件不一致") from None
            receipt = ActiveReceipt(current, candidate.candidate_id, candidate.migration_id,
                                    candidate.generation, previous)
            if newest is None:
                newest = receipt
            previous_generation = candidate.generation
            current = previous
        return newest

    def stage_candidate(self, *, migration_id, generation, source_cut_sha256,
                        component_sha256s):
        candidate = _candidate(
            migration_id=migration_id, generation=generation,
            source_cut_sha256=source_cut_sha256, component_sha256s=component_sha256s
        )
        with _transaction(self.engine, write=True) as connection:
            _ensure_schema(connection, self.tables)
            table = self.tables[1]
            previous = connection.execute(select(table.c.candidate_id).where(
                table.c.migration_id == candidate.migration_id,
                table.c.generation == candidate.generation,
            )).scalar_one_or_none()
            if previous is not None:
                if previous != candidate.candidate_id:
                    raise ActivationConflict("同迁移代次已有不同候选，不得覆盖")
                candidate = self._candidate(connection, previous)
            else:
                connection.execute(insert(table).values(
                    candidate_id=candidate.candidate_id, migration_id=candidate.migration_id,
                    generation=candidate.generation, source_cut_sha256=candidate.source_cut_sha256,
                    component_sha256s=_json(candidate.component_sha256s),
                    contract_version=CONTRACT_VERSION,
                ))
        return candidate

    def get_candidate(self, candidate_id):
        _digest(candidate_id)
        with _transaction(self.engine, write=False) as connection:
            _ensure_schema(connection, self.tables)
            candidate = self._candidate(connection, candidate_id)
        return candidate

    def register_component(self, candidate_id, component):
        _digest(candidate_id)
        _component(component)
        if not callable(self.verify_component):
            raise ActivationUnavailable("缺少可信服务端组件验证器")
        failed = False
        receipt = None
        with _transaction(self.engine, write=True) as connection:
            _ensure_schema(connection, self.tables)
            candidate = self._candidate(connection, candidate_id)
            self._blocked(connection, candidate)
            existing = self._components(connection, candidate)
            receipt = next((row for row in existing if row.component == component), None)
            if receipt is None:
                try:
                    verified = self.verify_component(connection, candidate, component) is True
                except Exception:  # noqa: BLE001 - 回调私密异常只登记固定拒绝枚举
                    verified = False
                if not verified:
                    self._reject(connection, candidate, "COMPONENT_REJECTED")
                    failed = True
                else:
                    receipt = ComponentReceipt(candidate.candidate_id, candidate.migration_id,
                        candidate.generation, component, candidate.component_sha256s[component])
                    connection.execute(insert(self.tables[2]).values(
                        candidate_id=receipt.candidate_id, component=receipt.component,
                        migration_id=receipt.migration_id, generation=receipt.generation,
                        manifest_sha256=receipt.manifest_sha256, contract_version=CONTRACT_VERSION
                    ))
        if failed:
            raise ActivationConflict("可信组件验证拒绝，候选已停止") from None
        return receipt

    def publish(self, candidate_id, *, expected_active_sha256):
        _digest(candidate_id)
        if expected_active_sha256 is not None:
            _digest(expected_active_sha256)
        if not callable(self.verify_generation):
            raise ActivationUnavailable("缺少可信服务端总代次验证器")
        failed = None
        receipt = None
        with _transaction(self.engine, write=True) as connection:
            _ensure_schema(connection, self.tables)
            candidate = self._candidate(connection, candidate_id)
            self._blocked(connection, candidate)
            components = self._components(connection, candidate)
            active = self._active(connection)
            current = active.activation_sha256 if active else None
            if len(components) != len(COMPONENTS):
                failed = "INCOMPLETE"
            elif current != expected_active_sha256:
                failed = "STALE_POINTER"
            elif active is not None and candidate.generation <= active.generation:
                failed = "NONINCREASING"
            else:
                try:
                    verified = self.verify_generation(connection, candidate, components) is True
                except Exception:  # noqa: BLE001 - 回调响应及异常不进入收据
                    verified = False
                if not verified:
                    failed = "GENERATION_REJECTED"
            if failed is None:
                digest = self._publication_sha(candidate, components, current)
                pointer = self.tables[-1]
                cas = connection.execute(update(pointer).where(
                    pointer.c.id == 1, pointer.c.activation_sha256 == current
                ).values(activation_sha256=digest))
                if cas.rowcount != 1:
                    failed = "STALE_POINTER"
                else:
                    receipt = ActiveReceipt(digest, candidate.candidate_id, candidate.migration_id,
                                            candidate.generation, current)
                    connection.execute(insert(self.tables[4]).values(
                        activation_sha256=digest, candidate_id=candidate.candidate_id,
                        migration_id=candidate.migration_id, generation=candidate.generation,
                        previous_activation_sha256=current, contract_version=CONTRACT_VERSION
                    ))
            if failed is not None:
                self._reject(connection, candidate, failed)
        if failed is not None:
            raise ActivationConflict("候选未满足发布条件，已保留拒绝收据") from None
        return receipt

    def read_active(self):
        with _transaction(self.engine, write=False) as connection:
            _ensure_schema(connection, self.tables)
            receipt = self._active(connection)
            if receipt is None:
                raise ActivationUnavailable("尚无已签收活跃代次")
        return receipt
