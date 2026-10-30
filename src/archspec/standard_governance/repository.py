"""治理仓储：固定观察只追加，flush 后由业务服务统一提交。"""

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime

from sqlalchemy import inspect, select, text

from archspec.standard_governance.models import (
    SCOPE_SUBJECTS,
    ArtifactBindingRow,
    GovernanceError,
    LegacyBindingRow,
    MigrationLedgerRow,
    ReviewEvidencePolicyRow,
    SourceRecordRow,
    StandardRow,
    StandardVersionRow,
    VerificationRecordRow,
    VersionRelationRow,
)

LOGGER = logging.getLogger(__name__)

_WRITE_LOCK_STATE = "_archspec_governance_write_lock_v1"

SUBJECT_MODELS = {"SOURCE": SourceRecordRow, "VERSION": StandardVersionRow,
                  "ARTIFACT": ArtifactBindingRow, "RELATION": VersionRelationRow}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item))


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def record(row):
    if row is None:
        return None
    return _record_values({column.name: getattr(row, column.name) for column in row.__table__.columns})


def _record_values(values):
    """ORM和复合读取的typed列共用解码规则，不把数据库载荷二次JSON转码。"""
    result = {}
    for key, value in values.items():
        if key.endswith("_json"):
            key, value = key[:-5], json.loads(value)
        elif isinstance(value, datetime):
            value = (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        result[key] = value
    for key in ("manifest_sha256", "observation_sha256", "binding_sha256", "relation_manifest_sha256"):
        if key in result:
            result["manifest_sha256"] = result[key]
            break
    return result


class GovernanceRepository:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def _check_connection_mapping(connection):
        """热联合门复用原连接映射合同；物理Table安全不能替代运行会话身份。"""
        from archspec.persistence import sql_namespace
        from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable

        expected_schema = sql_namespace.physical_schema(connection)
        mapping = connection.get_execution_options().get("schema_translate_map", {})
        if (not isinstance(mapping, Mapping)
                or connection.dialect.name == "postgresql" and mapping.get(None) != expected_schema
                or connection.dialect.name == "sqlite" and mapping.get(None, expected_schema) != expected_schema):
            raise RuntimeBindingsUnavailable("规范治理反射与实际查询的物理范围不同")
        try:
            sql_namespace._translation_safe(connection, {expected_schema})
        except sql_namespace.NamespaceUnavailable:
            raise RuntimeBindingsUnavailable("规范治理反射与实际查询的物理范围不同") from None

    def available(self):
        from archspec.persistence import sql_namespace
        from archspec.persistence.runtime_bindings import (
            RuntimeBindingsUnavailable,
            current_runtime,
        )

        models = (
            StandardRow, StandardVersionRow, SourceRecordRow, ArtifactBindingRow,
            VerificationRecordRow, LegacyBindingRow, VersionRelationRow,
            ReviewEvidencePolicyRow, MigrationLedgerRow,
        )
        try:
            connection = self.session.connection()
            selected = current_runtime()
            strict = selected is not None or connection.dialect.name != "sqlite"
            if selected is not None:
                bind = self.session.get_bind()
                if getattr(bind, "engine", bind) is not selected.engine:
                    raise RuntimeBindingsUnavailable("规范治理与当前运行数据库范围不同")
            # ORM仍执行逻辑Table；Inspector不消费schema映射，二者必须指向同一物理空间。
            self._check_connection_mapping(connection)
            generation = getattr(getattr(selected, "snapshot_publisher", None), "generation", None)
            if generation is not None and getattr(generation, "governance_table_names", ()):
                # 已恢复代次的一体合同已核物理表/列/影子，不在外层重复反射。
                from archspec.standard_governance.migration import read_cloud_governance_contract

                current = read_cloud_governance_contract(connection, active=generation.active,
                                                        metadata=generation.metadata, verify_structure=False)
                if (current != generation.governance_table_names
                        or set(current) != {model.__table__.name for model in models}):
                    raise RuntimeBindingsUnavailable("已安装的规范治理凭据缺失，暂停正文读取")
                return True
            tables = sql_namespace.bind_tables(connection, [model.__table__ for model in models])
            inspector = inspect(connection)
            names = {schema: set(inspector.get_table_names(schema=schema))
                     for schema in {table.schema for table in tables}}
            present = [table.name in names[table.schema] for table in tables]
            if not any(present):
                if generation is not None:
                    declared = set(generation.metadata["main_table_names"]) | set(
                        getattr(generation, "governance_table_names", ()))
                    if declared.intersection(table.name for table in tables):
                        raise RuntimeBindingsUnavailable("已迁入的规范治理结构缺失，暂停正文读取")
                # 源代次从未迁移治理时保留旧语义；不建表、不制造许可历史。
                return False
            if not all(present):
                if not strict:
                    return False
                raise RuntimeBindingsUnavailable("规范治理结构部分缺失，暂停正文读取")
            if selected is not None:
                from archspec.persistence.runtime_bindings import current_read_connection

                if current_read_connection(selected.engine) is connection:
                    sql_namespace.validate_columns(connection, tables)
                    return True
            for table in tables:
                columns = {item["name"] for item in
                           inspector.get_columns(table.name, schema=table.schema)}
                if (columns != set(table.columns.keys()) if strict
                        else not set(table.columns.keys()) <= columns):
                    if not strict:
                        return False
                    raise RuntimeBindingsUnavailable("规范治理列合同不完整，暂停正文读取")
            return True
        except RuntimeBindingsUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 故障复用现有503处理器，不变成空许可或泄露SQL。
            raise RuntimeBindingsUnavailable("规范治理物理结构无法核对，暂停正文读取") from None

    def require_available(self):
        if not self.available():
            raise GovernanceError("GOVERNANCE_NOT_MIGRATED", "规范治理尚未显式迁移", 503)

    def lock_write(self, subject_kind=None, subject_id=None):
        """数据库事务锁：旧全局路径与同对象路径必须相互协调。"""
        subject = self._lock_subject(subject_kind, subject_id)
        if self.session.get_nested_transaction() is not None:
            raise GovernanceError("GOVERNANCE_TRANSACTION_UNSUPPORTED", "治理写锁不允许嵌套事务", 503)
        transaction = self.session.get_transaction()
        state = self.session.info.get(_WRITE_LOCK_STATE)
        if transaction is not None and state is not None and transaction is state[0]:
            # 同一事务已经持有全局锁或同对象锁，不提交、不释放、不清掉待写变更。
            if state[1] is None or subject == state[1]:
                return
            raise GovernanceError("GOVERNANCE_LOCK_SCOPE_CONFLICT", "同一治理事务不可扩大对象锁范围", 409)
        # 上锁前不能让ORM查询自动flush调用者已经修改的对象。
        if self.session.new or self.session.dirty or self.session.deleted:
            raise GovernanceError("GOVERNANCE_PENDING_WRITE", "治理变更必须先取得数据库写锁", 409)
        connection = self.session.connection()
        dialect = connection.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise GovernanceError("GOVERNANCE_UNSUPPORTED_DATABASE", "治理仓库不支持此数据库引擎", 503)
        if dialect == "postgresql" and connection.get_isolation_level() != "READ COMMITTED":
            # 锁后必须看到新事件；旧快照或自动提交不能提供现有CAS合同。
            raise GovernanceError("GOVERNANCE_TRANSACTION_UNSUPPORTED", "治理写入需要READ COMMITTED事务", 503)
        self.require_available()
        try:
            if dialect == "sqlite":
                raw = connection.connection.driver_connection
                if not raw.in_transaction:
                    self.session.execute(text("BEGIN IMMEDIATE"))
                subject = None  # SQLite始终使用既有数据库全局写锁。
            else:
                for setting, value in (("lock_timeout", "5000ms"),
                                       ("statement_timeout", "5000ms"),
                                       ("synchronous_commit", "on")):
                    self.session.execute(text("SELECT set_config(:setting_name, :setting_value, true)"),
                                         {"setting_name": setting, "setting_value": value})
                # 全局登记会修改版本和来源；共享闸门避免它绕过对象核验锁。
                gate = "pg_advisory_xact_lock" if subject is None else "pg_advisory_xact_lock_shared"
                self.session.execute(text(f"SELECT {gate}(:lock_key)"),
                                     {"lock_key": self._lock_key(None)})
                if subject is not None:
                    self.session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"),
                                         {"lock_key": self._lock_key(subject)})
        except Exception as error:  # noqa: BLE001 - 驱动/回调异常可能含凭据，仅输出异常类型
            LOGGER.warning("governance_lock_failed error_type=%s", type(error).__name__)
            # 不暴露SQL、参数、连接地址或驱动异常；调用者负责整个事务的rollback。
            raise GovernanceError("GOVERNANCE_WRITE_LOCK_UNAVAILABLE", "治理写锁未取得，请重新读取后操作", 503) from None
        # 状态绑定Session事务，而非仓库实例；同会话的其他适配器不可绕过锁范围。
        self.session.info[_WRITE_LOCK_STATE] = self.session.get_transaction(), subject
        self.session.expire_all()

    @staticmethod
    def _lock_subject(kind, subject_id):
        if kind is None and subject_id is None:
            return None
        if (not isinstance(kind, str) or kind not in SUBJECT_MODELS
                or not isinstance(subject_id, str) or not subject_id.strip() or len(subject_id) > 36):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "治理对象锁参数不合法")
        return kind, subject_id

    @staticmethod
    def _lock_key(subject):
        # 固定命名空间和确定性64位值，禁止使用进程随机化的Python hash。
        data = canonical_json(["ARCHSPEC_GOVERNANCE_WRITE_V1", subject])
        return int.from_bytes(hashlib.sha256(data.encode("utf-8")).digest()[:8], "big", signed=True)

    def append_event(self, row, *, expected_head_id):
        """核同对象与事件链后只追加；成功返回仍须commit确认才可对外报告。"""
        if (not isinstance(row, VerificationRecordRow)
                or not isinstance(row.scope, str)
                or SCOPE_SUBJECTS.get(row.scope) != row.subject_kind
                or not isinstance(row.decision, str)
                or row.decision not in {"VERIFY", "REJECT", "REVOKE"}
                or (expected_head_id is not None and not isinstance(expected_head_id, str))):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "治理事件追加参数不合法")
        self.lock_write(row.subject_kind, row.subject_id)
        subject = self.get_subject(row.subject_kind, row.subject_id)
        if subject["manifest_sha256"] != row.subject_manifest_sha256:
            raise GovernanceError("MANIFEST_CHANGED", "固定对象已变化，旧事件请求不再适用", 409)
        if row.standard_version_manifest_sha256 != self.version_context_manifest(row.subject_kind, subject):
            raise GovernanceError("MANIFEST_CHANGED", "所属版本已变化，旧事件请求不再适用", 409)
        events = sorted((event for event in self.history(row.subject_kind, row.subject_id)
                         if event["scope"] == row.scope), key=lambda event: event["sequence"])
        head = self.head(row.subject_kind, row.subject_id, row.scope)
        previous = None
        for sequence, event in enumerate(events, 1):
            if (event["sequence"] != sequence
                    or event["supersedes_event_id"] != (previous["id"] if previous else None)):
                raise GovernanceError("GOVERNANCE_EVENT_CHAIN_INVALID", "治理历史链不完整，禁止重建允许状态", 503)
            previous = event
        if (head["id"] if head else None) != (previous["id"] if previous else None):
            raise GovernanceError("GOVERNANCE_EVENT_CHAIN_INVALID", "治理事件头与保留历史不一致", 503)
        head_id = head["id"] if head else None
        if head_id != expected_head_id:
            raise GovernanceError("VERIFICATION_HEAD_CHANGED", "核验事件头已变化，请重新确认", 409)
        if (type(row.sequence) is not int or row.sequence != (head["sequence"] + 1 if head else 1)
                or row.supersedes_event_id != head_id):
            raise GovernanceError("VERIFICATION_HEAD_CHANGED", "新增事件序号或前序绑定不一致", 409)
        self.session.add(row)
        self.session.flush()
        return record(row)

    def version_context_manifest(self, kind, subject):
        """由服务器读取当前所属版本，不信任追加行夹带的版本指纹。"""
        if kind == "RELATION":
            version_ids = [subject["from_version_id"], subject.get("to_version_id")]
            return fingerprint({key: self.get_version(key)["manifest_sha256"] for key in version_ids if key})
        version_id = subject["id"] if kind == "VERSION" else subject.get("standard_version_id")
        return self.get_version(version_id)["manifest_sha256"] if version_id else None

    def commit_write(self):
        """只在提交确认后返回；结果未知不自动重试，也不声称尚未写入。"""
        try:
            self.session.commit()
        except Exception as error:  # noqa: BLE001 - 驱动/回调异常可能含凭据，仅输出异常类型
            LOGGER.warning("governance_commit_unknown error_type=%s", type(error).__name__)
            try:
                self.session.rollback()
            except Exception as rollback_error:  # noqa: BLE001 - 结果未知时不能让rollback原异常覆盖安全提示
                LOGGER.warning("governance_rollback_unconfirmed error_type=%s", type(rollback_error).__name__)
            raise GovernanceError("GOVERNANCE_COMMIT_UNKNOWN", "治理提交结果未知，请重新读取事件，禁止自动重试", 503) from None
        finally:
            self.session.info.pop(_WRITE_LOCK_STATE, None)

    def get_subject(self, kind, subject_id):
        model = SUBJECT_MODELS.get(kind)
        if model is None:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验对象类型不支持")
        result = record(self.session.get(model, subject_id))
        if result is None:
            raise GovernanceError("GOVERNANCE_SUBJECT_NOT_FOUND", "治理对象不存在", 404)
        return result

    def get_version(self, value):
        return self.get_subject("VERSION", value)

    def get_source(self, value):
        return self.get_subject("SOURCE", value)

    def get_artifact(self, value):
        return self.get_subject("ARTIFACT", value)

    def get_relation(self, value):
        return self.get_subject("RELATION", value)

    def list_standards(self):
        if not self.available():
            return []
        return [record(row) for row in self.session.scalars(select(StandardRow).order_by(StandardRow.created_at))]

    def list_versions(self):
        return [record(row) for row in self.session.scalars(select(StandardVersionRow))]

    def list_sources(self):
        return [record(row) for row in self.session.scalars(select(SourceRecordRow))]

    def list_artifacts(self):
        return [record(row) for row in self.session.scalars(select(ArtifactBindingRow))]

    def history(self, kind, subject_id):
        if not self.available():
            return []
        rows = self.session.scalars(select(VerificationRecordRow).where(
            VerificationRecordRow.subject_kind == kind, VerificationRecordRow.subject_id == subject_id,
        ).order_by(VerificationRecordRow.recorded_at, VerificationRecordRow.sequence))
        return [record(row) for row in rows]

    def head(self, kind, subject_id, scope, manifest=None):
        query = select(VerificationRecordRow).where(
            VerificationRecordRow.subject_kind == kind, VerificationRecordRow.subject_id == subject_id,
            VerificationRecordRow.scope == scope,
        )
        if manifest is not None:
            query = query.where(VerificationRecordRow.subject_manifest_sha256 == manifest)
        return record(self.session.scalar(query.order_by(VerificationRecordRow.sequence.desc()).limit(1)))

    def get_event(self, event_id):
        row = self.session.get(VerificationRecordRow, event_id)
        if row is None:
            raise GovernanceError("GOVERNANCE_SUBJECT_NOT_FOUND", "核验事件不存在", 404)
        return record(row)

    def read_legacy_context(self, kind, key, manifest=None):
        """一次完整合同和一次绑定读取；不在调用之间缓存可用性或许可。"""
        if not self.available():
            return {"available": False, "bindings": [], "current": None}
        rows = [record(row) for row in self.session.scalars(select(LegacyBindingRow).where(
            LegacyBindingRow.legacy_kind == kind, LegacyBindingRow.legacy_key == key,
        ))]
        current = next((row for row in rows if row["legacy_manifest_sha256"] == manifest), None)
        return {"available": True, "bindings": rows, "current": current}

    @staticmethod
    def _legacy_permission_statement(tables, kind, keys):
        """只由服务端固定Table映射构造；显式标签可进入同一受控PG复合语句。"""
        models = {"legacy": LegacyBindingRow, "artifact": ArtifactBindingRow,
                  "source": SourceRecordRow, "version": StandardVersionRow,
                  "event": VerificationRecordRow}
        selected = {name: tables[model.__table__.name] for name, model in models.items()}
        binding, artifact, source, version, event = (selected[name] for name in models)
        latest = event.alias("permission_latest_content_use")
        head_id = select(latest.c.id).where(
            latest.c.subject_kind == "SOURCE", latest.c.subject_id == source.c.id,
            latest.c.scope == "CONTENT_USE",
        ).order_by(latest.c.sequence.desc()).limit(1).correlate(source).scalar_subquery()
        columns = [column.label(prefix + "__" + column.name)
                   for prefix, table in selected.items() for column in table.c]
        # LEFT JOIN不能把非空悬空绑定吞成“无治理”；版本沿SOURCE的实际所属版本。
        return select(*columns).select_from(binding.outerjoin(
            artifact, artifact.c.id == binding.c.artifact_binding_id,
        ).outerjoin(source, source.c.id == artifact.c.source_record_id).outerjoin(
            version, version.c.id == source.c.standard_version_id,
        ).outerjoin(event, event.c.id == head_id)).where(
            binding.c.legacy_kind == kind, binding.c.legacy_key.in_(keys),
        ).execution_options(populate_existing=True)

    @staticmethod
    def _permission_record(row, prefix, model):
        """先核缺行、typed列与JSON形状；损坏载荷不能伪装无绑定或未知许可。"""
        from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable

        try:
            if row[prefix + "__id"] is None:
                return None
            values = {column.name: row[prefix + "__" + column.name] for column in model.__table__.columns}
            for column in model.__table__.columns:
                value = values[column.name]
                if value is None:
                    if not column.nullable:
                        raise ValueError
                elif (not isinstance(value, column.type.python_type)
                      or column.type.python_type is int and isinstance(value, bool)):
                    raise ValueError
            decoded = _record_values(values)
            for name in ("declared_usage_right", "statement"):
                if name in decoded and not isinstance(decoded[name], dict):
                    raise ValueError
            if "statement" in decoded and not isinstance(decoded["statement"].get("operations", {}), dict):
                raise ValueError
            return decoded
        except (KeyError, TypeError, ValueError):
            raise RuntimeBindingsUnavailable("规范治理使用条件载荷无法核对，暂停正文读取") from None

    def read_legacy_permissions(self, kind, keys):
        """每门读取全谱系当前用途；不跨门缓存合同、主体、事件或允许结论。"""
        from archspec.persistence.runtime_bindings import (
            RuntimeBindingsUnavailable,
            current_runtime,
        )

        keys = tuple(keys)
        if not keys or len(keys) > 512 or any(not isinstance(key, str) or not key for key in keys):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "固定正文使用条件谱系无效")
        result = {key: [] for key in keys}
        connection = self.session.connection()
        selected = current_runtime()
        generation = getattr(getattr(selected, "snapshot_publisher", None), "generation", None)
        if (connection.dialect.name == "postgresql" and generation is not None
                and getattr(generation, "governance_table_names", ())):
            from archspec.standard_governance.migration import (
                GovernanceInstallationUnavailable,
                read_cloud_governance_records,
            )

            bind = self.session.get_bind()
            if getattr(bind, "engine", bind) is not selected.engine:
                raise RuntimeBindingsUnavailable("规范治理与当前运行数据库范围不同")
            self._check_connection_mapping(connection)
            # Session原SELECT会autoflush；受控Connection执行仍保留此调用方事务语义。
            if self.session.autoflush and (self.session.new or self.session.dirty or self.session.deleted):
                # 待写会话沿用原完整预检顺序，失效控制凭据或TEMP影子不得先收到DML。
                if not self.available():
                    raise RuntimeBindingsUnavailable("已安装的规范治理凭据缺失，暂停正文读取")
                self.session.flush()
            try:
                names, rows = read_cloud_governance_records(
                    connection, active=generation.active, metadata=generation.metadata,
                    statement_factory=lambda tables: self._legacy_permission_statement(tables, kind, keys))
            except GovernanceInstallationUnavailable:
                raise RuntimeBindingsUnavailable("规范治理当前控制凭据无法核对，暂停正文读取") from None
            expected = {model.__table__.name for model in (
                StandardRow, StandardVersionRow, SourceRecordRow, ArtifactBindingRow,
                VerificationRecordRow, LegacyBindingRow, VersionRelationRow,
                ReviewEvidencePolicyRow, MigrationLedgerRow)}
            if names != generation.governance_table_names or set(names) != expected:
                raise RuntimeBindingsUnavailable("已安装的规范治理凭据缺失，暂停正文读取")
        else:
            if not self.available():
                return {"available": False, "contexts": result}
            tables = {model.__table__.name: model.__table__ for model in (
                LegacyBindingRow, ArtifactBindingRow, SourceRecordRow, StandardVersionRow, VerificationRecordRow)}
            # typed列直接来自新语句，不读取Session identity map中可能陈旧的实体。
            rows = self.session.execute(self._legacy_permission_statement(tables, kind, keys)).mappings().all()
            # 非热迁移分支保留原完整合同；前置True不能代替末尾实时核验。
            if not self.available():
                raise RuntimeBindingsUnavailable("规范治理合同在使用条件读取期间变化，暂停正文读取")
        for row in rows:
            values = {prefix: self._permission_record(row, prefix, model) for prefix, model in (
                    ("legacy", LegacyBindingRow), ("artifact", ArtifactBindingRow),
                    ("source", SourceRecordRow), ("version", StandardVersionRow),
                    ("event", VerificationRecordRow))}
            binding = values["legacy"]
            if binding["artifact_binding_id"] is None:
                continue  # 旧只读门原本跳过没有artifact的观察，不把它变成批准。
            if (values["artifact"] is None or values["source"] is None
                    or values["source"]["standard_version_id"] is not None and values["version"] is None):
                raise GovernanceError("GOVERNANCE_SUBJECT_NOT_FOUND", "治理对象不存在", 404)
            result[binding["legacy_key"]].append(values)
        return {"available": True, "contexts": result}

    def bindings_for_legacy(self, kind, key):
        return self.read_legacy_context(kind, key)["bindings"]

    def resolve_legacy(self, kind, key, manifest):
        return self.read_legacy_context(kind, key, manifest)["current"]

    def list_relations(self, version_id=None):
        query = select(VersionRelationRow)
        if version_id is not None:
            query = query.where(VersionRelationRow.from_version_id == version_id)
        return [record(row) for row in self.session.scalars(query)]

    def save_review_policy(self, job_id, evidence_mode="AUTHORITATIVE", admission_policy_version="SOURCE_ADMISSION_V1"):
        self.require_available()
        row = self.session.get(ReviewEvidencePolicyRow, job_id)
        if row is not None:
            if row.evidence_mode != evidence_mode or row.admission_policy_version != admission_policy_version:
                raise GovernanceError("MANIFEST_CHANGED", "审查策略不能覆盖", 409)
            return record(row)
        row = ReviewEvidencePolicyRow(job_id=job_id, evidence_mode=evidence_mode,
                                      admission_policy_version=admission_policy_version)
        self.session.add(row)
        self.session.flush()
        return record(row)

    def get_review_policy(self, job_id):
        return record(self.session.get(ReviewEvidencePolicyRow, job_id)) if self.available() else None
