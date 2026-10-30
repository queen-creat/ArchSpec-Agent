"""显式治理迁移：一致备份、新表事务、幂等登记；从不审批旧数据。"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.orm import Session

from archspec.database_schema import Base
from archspec.standard_governance.models import MigrationLedgerRow, VerificationRecordRow

MIGRATION_ID = "2026-10-08-SOURCE_ADMISSION_V1"


class GovernanceMigration:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "archspec.db"
        if not self.path.is_file():
            raise ValueError("没有找到项目现有数据库；预览/迁移不会新建空数据库")

    def _engine(self, readonly=False):
        if readonly:
            # SQLite URI mode=ro 保证预览不会在调用意外变动后静默写数据。
            uri = self.path.as_uri() + "?mode=ro"
            return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
        return create_engine(f"sqlite+pysqlite:///{self.path}")

    def preview(self):
        from archspec.standard_governance.imports import GovernanceImportService, stable_hash

        engine = self._engine(readonly=True)
        try:
            with Session(engine) as session:
                importer = GovernanceImportService(session, self.data_dir)
                observations, skipped = [], []
                for kind, key, snapshot_id in importer.record_keys():
                    try:
                        description = importer.describe(kind, key, snapshot_id)
                        observations.append({"kind": kind, "key": key, "snapshot_id": snapshot_id,
                                             "manifest_sha256": description["legacy_manifest"],
                                             "has_artifact": description["artifact"] is not None,
                                             "issues": description["runtime_issues"]})
                    except (ValueError, LookupError, OSError, KeyError, TypeError) as error:
                        skipped.append({"kind": kind, "key": key, "reason": str(error)[:300]})
                return {"mode": "DRY_RUN", "records": observations, "skipped": skipped,
                        "manifest_sha256": stable_hash(observations),
                        "notice": "仅预览，不下载、不识别、不核验、不创建治理表"}
        finally:
            engine.dispose()

    def _backup(self):
        folder = self.data_dir / "governance-backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = folder / f"before-{MIGRATION_ID}-{stamp}-{uuid4().hex[:8]}.sqlite"
        # backup API 对活跃库建立一致快照，而非直接复制可能半写入的文件。
        with sqlite3.connect(self.path) as original, sqlite3.connect(destination) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份完整性检查未通过，停止迁移")
        return destination

    def apply(self):
        from archspec.standard_governance.imports import GovernanceImportService

        preview = self.preview()
        backup = self._backup()
        engine = self._engine()
        try:
            with engine.connect() as connection:
                # 显式 BEGIN 使 SQLite 的 DDL/数据登记一同回滚，避免半迁移。
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    Base.metadata.create_all(connection, tables=[
                        table for table in Base.metadata.sorted_tables
                        if table.name.startswith("governance_")
                    ])
                    with Session(bind=connection, expire_on_commit=False) as session:
                        importer = GovernanceImportService(session, self.data_dir)
                        for entry in preview["records"]:
                            importer.from_record(entry["kind"], entry["key"], entry["snapshot_id"],
                                                 commit=False)
                        ledger = session.get(MigrationLedgerRow, MIGRATION_ID)
                        if ledger is None:
                            session.add(MigrationLedgerRow(
                                migration_id=MIGRATION_ID,
                                manifest_sha256=preview["manifest_sha256"],
                                details_json=json.dumps({"skipped": preview["skipped"],
                                                         "automatically_verified": False},
                                                        ensure_ascii=False),
                            ))
                        session.flush()
                        counts = {table.name: connection.scalar(select(func.count()).select_from(table))
                                  for table in Base.metadata.tables.values()
                                  if table.name.startswith("governance_")}
                        events = session.scalar(select(func.count()).select_from(VerificationRecordRow))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return {"mode": "APPLIED", "migration_id": MIGRATION_ID, "counts": counts,
                    "verification_records": events, "backup_path": str(backup),
                    "skipped": preview["skipped"],
                    "notice": "只建立声明及兼容映射，真实资料没有新增核验事件"}
        finally:
            engine.dispose()


_CLOUD_SCHEMA = "GOVERNANCE_SCHEMA_EXTENSION_V1"
_CLOUD_SAFE = "云端治理安装凭据或结构无法核对，未重建权限或修改原代次"


class GovernanceInstallationUnavailable(RuntimeError):
    """显式安装失败或提交未知；只能先只读复验，不能自动重试补表。"""


def _cloud_definitions():
    # 仅导入无副作用的模型声明；云端操作入口不能先选择默认本地配置。
    from archspec.standard_governance import models  # noqa: F401

    return tuple(sorted((table for table in Base.metadata.tables.values()
                         if table.name.startswith("governance_")), key=lambda table: table.name))


def _cloud_receipt_body(active, metadata, original_sha):
    from archspec.persistence import control_image

    definitions = _cloud_definitions()
    if len(definitions) != 9:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    body = {"schema_version": _CLOUD_SCHEMA, "activation_sha256": active.activation_sha256,
        "candidate_id": active.candidate_id, "migration_id": active.migration_id,
        "generation": active.generation, "source_cut_sha256": metadata["source_cut_sha256"],
        "original_metadata_sha256": original_sha,
        "table_names": [table.name for table in definitions],
        "table_schema_sha256": control_image._sha(control_image._json([control_image._description(table) for table in definitions]))}
    key = "gov-v1:" + control_image._sha(control_image._json(body))[:56]
    return definitions, body, key


def _cloud_binding(connection, active, metadata, *, verify_structure=True):
    from archspec.persistence import activation, control_image, sql_namespace
    from archspec.persistence.migration_flow import _read_metadata, _table

    if type(active) is not activation.ActiveReceipt or not isinstance(metadata, dict):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    tables = activation._tables(connection.engine)
    if verify_structure:
        activation._ensure_schema(connection, tables)
    else:
        # 已恢复runtime只复核同物理列合同；所有身份行仍在本连接重新SELECT。
        sql_namespace.validate_columns(connection, [tables[-1], tables[-2]])
    if connection.scalar(select(tables[-1].c.activation_sha256)) != active.activation_sha256:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    row = connection.execute(select(tables[-2]).where(
        tables[-2].c.activation_sha256 == active.activation_sha256)).mappings().one()
    if any(row[key] != getattr(active, key) for key in
           ("candidate_id", "migration_id", "generation", "previous_activation_sha256")):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    candidate = activation._candidate(migration_id=active.migration_id, generation=active.generation,
        source_cut_sha256=metadata["source_cut_sha256"], component_sha256s=metadata["component_sha256s"])
    if candidate.candidate_id != active.candidate_id:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    manifest_table = _table(connection, check=verify_structure)
    if not verify_structure:
        sql_namespace.validate_columns(connection, [manifest_table])
    original = connection.execute(select(manifest_table).where(
        manifest_table.c.candidate_id == active.candidate_id)).mappings().one()
    if (original["metadata_json"] != control_image._json(metadata)
            or control_image._sha(original["metadata_json"]) != original["metadata_sha256"]):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    if verify_structure and _read_metadata(connection, candidate) != metadata:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    definitions, body, key = _cloud_receipt_body(active, metadata, original["metadata_sha256"])
    return definitions, body, key, manifest_table


def _runtime_cloud_context(connection, active, metadata):
    """每次从实际恢复声明推导预期身份；不保存当前控制行。"""
    from archspec.persistence import activation, control_image
    from archspec.persistence.migration_flow import _GENERATIONS

    if type(active) is not activation.ActiveReceipt or not isinstance(metadata, dict):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    candidate = activation._candidate(migration_id=active.migration_id, generation=active.generation,
        source_cut_sha256=metadata["source_cut_sha256"], component_sha256s=metadata["component_sha256s"])
    if candidate.candidate_id != active.candidate_id:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    canonical = control_image._json(metadata)
    original_sha = control_image._sha(canonical)
    definitions, expected, key = _cloud_receipt_body(active, metadata, original_sha)
    declared = metadata["source_identity"]["registration"]["main_table_names"]
    if (not isinstance(declared, (list, tuple)) or len(declared) != len(set(declared))
            or metadata["main_table_names"] != declared):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    known = {table.name for table in definitions}
    original_names = {name for name in declared if name.startswith("governance_")}
    if not original_names <= known:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    ledger = activation._tables(connection.engine)
    combined = [ledger[-1], ledger[-2], _GENERATIONS, *definitions]
    return {"active": active, "combined": combined, "known": known, "original_names": original_names,
        "canonical": canonical, "original_sha": original_sha, "key": key, "expected": expected}


def _runtime_control_statement(tables, active):
    """同一控制UNION同时供既有核验和联合准入使用，不复制收据判据。"""
    from sqlalchemy import BigInteger, String, Text, cast, literal, union_all

    from archspec.persistence.migration_flow import _GENERATIONS

    pointer = tables["external_state_active"]
    publication = tables["external_state_publications"]
    manifest = tables[_GENERATIONS.name]
    def fields(kind, identifier, sha, migration=None, generation=None, previous=None, content=None):
        values = (literal(kind), identifier, sha, literal(None) if migration is None else migration,
            literal(None) if generation is None else generation,
            literal(None) if previous is None else previous, literal(None) if content is None else content)
        names = ("kind", "identifier", "sha", "migration_id", "generation", "previous", "content")
        types = (String(), String(), String(), String(), BigInteger(), String(), Text())
        # 显式类型使无行/空值分支仍可安全组合，不由NULL推断整数或文本。
        return [cast(value, datatype).label(name) for value, datatype, name in zip(values, types, names, strict=True)]
    current = select(*fields("ACTIVE", literal("1"), pointer.c.activation_sha256)).where(pointer.c.id == 1)
    published = select(*fields("PUBLICATION", publication.c.candidate_id, publication.c.activation_sha256,
        publication.c.migration_id, publication.c.generation, publication.c.previous_activation_sha256)).select_from(
            publication.join(pointer, publication.c.activation_sha256 == pointer.c.activation_sha256)).where(pointer.c.id == 1)
    original = select(*fields("ORIGINAL", manifest.c.candidate_id, manifest.c.metadata_sha256,
        content=manifest.c.metadata_json)).where(manifest.c.candidate_id == active.candidate_id)
    installed = select(*fields("GOVERNANCE", manifest.c.candidate_id, manifest.c.metadata_sha256,
        content=manifest.c.metadata_json)).where(manifest.c.candidate_id.like("gov-v1:%"))
    return union_all(current, published, original, installed)


def _verify_runtime_control(connection, proof, context, rows):
    """对真实读取逐项核验；目录和控制来自本调用，不接受外部可用性布尔值。"""
    from archspec.persistence import control_image, sql_namespace
    from archspec.persistence.control_migration import _decode

    active = context["active"]
    known, original_names = context["known"], context["original_names"]
    canonical, original_sha, key, expected = (context[name] for name in ("canonical", "original_sha", "key", "expected"))
    schema = sql_namespace.physical_schema(connection)
    actual = {name for name in proof.inventory[schema] if name.startswith("governance_")}
    fixed = {}; found = False
    for row in rows:
        if row["kind"] != "GOVERNANCE":
            if row["kind"] in fixed:
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            fixed[row["kind"]] = row
            continue
        content = row["content"]
        body = _decode(content.encode("utf-8"))
        if (control_image._sha(content) != row["sha"] or body.get("schema_version") != _CLOUD_SCHEMA
                or row["identifier"] != "gov-v1:" + control_image._sha(control_image._json(body))[:56]):
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        if body.get("candidate_id") == active.candidate_id:
            if found or row["identifier"] != key or body != expected:
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            found = True
    if set(fixed) != {"ACTIVE", "PUBLICATION", "ORIGINAL"}:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    published = fixed["PUBLICATION"]; original = fixed["ORIGINAL"]
    if (fixed["ACTIVE"]["sha"] != active.activation_sha256
            or published["sha"] != active.activation_sha256
            or published["identifier"] != active.candidate_id
            or any(published[name] != getattr(active, name) for name in ("migration_id", "generation"))
            or published["previous"] != active.previous_activation_sha256
            or original["identifier"] != active.candidate_id or original["sha"] != original_sha
            or original["content"] != canonical or control_image._sha(original["content"]) != original["sha"]):
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    names = known if found else original_names
    if actual != names:
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
    proof.require(connection, context["combined"])
    return tuple(sorted(names))


def _read_runtime_cloud_governance(connection, active, metadata):
    from archspec.persistence import sql_namespace

    context = _runtime_cloud_context(connection, active, metadata)
    with sql_namespace.column_contract(connection, context["combined"]) as proof:
        rows = connection.execute(_runtime_control_statement(proof.tables, active)).mappings()
        return _verify_runtime_control(connection, proof, context, rows)


def read_cloud_governance_records(connection, *, active, metadata, statement_factory):
    """PG热路径同语句读取当前治理控制与权限值，完整验证后才返回业务记录。"""
    from sqlalchemy import cast, literal, null, union_all
    from sqlalchemy.sql.selectable import Select

    from archspec.persistence import sql_namespace

    try:
        if connection.dialect.name != "postgresql" or not callable(statement_factory):
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        context = _runtime_cloud_context(connection, active, metadata)
        labels = []
        control_prefix = "__governance_"
        def factory(tables):
            payload = statement_factory(tables)
            if not isinstance(payload, Select):
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            labels.extend(column.key for column in payload.selected_columns)
            if not labels or len(labels) != len(set(labels)) or any(
                    not isinstance(name, str) or name.startswith(control_prefix) for name in labels):
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            controls = _runtime_control_statement(tables, active).subquery()
            values = payload.subquery()
            control_query = select(*(column.label(control_prefix + column.key) for column in controls.c),
                *(cast(null(), column.type).label(column.key) for column in values.c))
            record_query = select(literal("PAYLOAD").label(control_prefix + "kind"),
                *(cast(null(), column.type).label(control_prefix + column.key) for column in controls.c if column.key != "kind"),
                *values.c)
            combined = union_all(control_query, record_query).subquery()
            return select(*combined.c)
        with sql_namespace.column_contract_records(connection, context["combined"], factory) as (proof, rows):
            controls, records = [], []
            for row in rows:
                if row[control_prefix + "kind"] == "PAYLOAD":
                    if any(value is not None for name, value in row.items()
                            if name.startswith(control_prefix) and name != control_prefix + "kind"):
                        raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
                    records.append({label: row[label] for label in labels})
                else:
                    controls.append({name[len(control_prefix):]: value for name, value in row.items()
                                     if name.startswith(control_prefix)})
            names = _verify_runtime_control(connection, proof, context, controls)
            return names, records
    except GovernanceInstallationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 同语句失败不返回SQL、权限原文或驱动消息。
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE) from None


def read_cloud_governance_contract(connection, *, active, metadata, verify_structure=True):
    """只读核原声明与独立安装凭据；全治理表丢失也不能退回旧兼容许可。"""
    from archspec.persistence import control_image, sql_namespace
    from archspec.persistence.control_migration import _decode

    try:
        if type(verify_structure) is not bool:
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        if not verify_structure:
            return _read_runtime_cloud_governance(connection, active, metadata)
        definitions, expected, key, ledger = _cloud_binding(connection, active, metadata)
        physical = sql_namespace.bind_tables(connection, definitions)
        known = {table.name: table for table in physical}
        declared = metadata["source_identity"]["registration"]["main_table_names"]
        if (not isinstance(declared, (list, tuple)) or len(declared) != len(set(declared))
                or metadata["main_table_names"] != declared):
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        original = {name for name in declared if name.startswith("governance_")}
        if not original <= known.keys():
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        receipts = connection.execute(select(ledger).where(
            ledger.c.candidate_id.like("gov-v1:%"))).mappings().all()
        installed = False
        for row in receipts:
            content = row["metadata_json"].encode("utf-8")
            body = _decode(content)
            if (control_image._sha(row["metadata_json"]) != row["metadata_sha256"]
                    or body.get("schema_version") != _CLOUD_SCHEMA):
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            # 其他已发布代次的扩展可保留；每一行仍必须有自身域隔离键和完整摘要。
            if row["candidate_id"] != "gov-v1:" + control_image._sha(control_image._json(body))[:56]:
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            if body.get("candidate_id") == active.candidate_id:
                if installed or row["candidate_id"] != key or body != expected:
                    raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
                installed = True
        names = set(known) if installed else original
        schema = sql_namespace.physical_schema(connection)
        actual = {name for name in inspect(connection).get_table_names(schema=schema)
                  if name.startswith("governance_")}
        if actual != names:
            raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
        if names:
            # 启动/安装仍逐项完整核验物理合同，仅将同一批目录读取合并，绝不缓存结构事实。
            control_image._physical_schemas(connection, [known[name] for name in sorted(names)])
        return tuple(sorted(names))
    except GovernanceInstallationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 不回显连接串、SQL、权限原文或驱动错误。
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE) from None


def install_cloud_governance(engine, *, expected_active_sha256):
    """显式安装空治理结构和独立凭据；不导入旧规范、不审批、不改预算。"""
    from archspec.persistence import activation, budget, control_image, sql_namespace
    from archspec.persistence.migration_flow import _read_metadata

    try:
        activation._digest(expected_active_sha256)
        ledger = activation.SQLActivationLedger(engine)
        with control_image.image_transaction(engine, write=True) as connection:
            if connection.dialect.name == "postgresql":
                # 和发布指针共用锁；取得锁后重新读取active，禁止绑定过期代次。
                connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"),
                                   {"lock": activation._LOCK})
            # RR在等待锁前可能已取得旧快照；锁住指针行使并发更新触发序列化失败。
            connection.execute(select(ledger.tables[-1]).with_for_update()).all()
            active = ledger._active(connection)
            if active is None or active.activation_sha256 != expected_active_sha256:
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            candidate = ledger._candidate(connection, active.candidate_id)
            metadata = _read_metadata(connection, candidate)
            definitions, body, key, receipt_table = _cloud_binding(connection, active, metadata)
            before_budget = budget._state(connection)
            existing = connection.execute(select(receipt_table).where(
                receipt_table.c.candidate_id == key)).mappings().one_or_none()
            names = read_cloud_governance_contract(connection, active=active, metadata=metadata)
            if existing is None:
                # 有原治理表的旧代次不走此空扩展入口，也不自动接管未知/部分表。
                if names:
                    raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
                physical = sql_namespace.bind_tables(connection, definitions)
                physical[0].metadata.create_all(connection, tables=physical)
                image = control_image.capture_control_image(connection, list(definitions))
                if any(entry["rows"] for entry in image.to_dict()["tables"]):
                    raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
                content = control_image._json(body)
                connection.execute(insert(receipt_table).values(candidate_id=key,
                    metadata_sha256=control_image._sha(content), metadata_json=content))
            read_cloud_governance_contract(connection, active=active, metadata=metadata)
            image = control_image.capture_control_image(connection, list(definitions))
            if budget._state(connection) != before_budget or ledger._active(connection) != active:
                raise GovernanceInstallationUnavailable(_CLOUD_SAFE)
            result = {"state": "INSTALLED" if existing is None else "ALREADY_INSTALLED",
                "table_count": len(definitions),
                "row_count": sum(len(entry["rows"]) for entry in image.to_dict()["tables"]),
                "receipt_sha256": control_image._sha(control_image._json(body))}
        # 必须收到事务提交确认才返回；结果未知由调用方显式只读复验。
        return result
    except GovernanceInstallationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 安全失败，不自动重试或重置任何权限/预算。
        raise GovernanceInstallationUnavailable(_CLOUD_SAFE) from None
