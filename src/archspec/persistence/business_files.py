"""业务文件统一发布账本：私有字节回读后签收，上传可借用原文档SQL事务。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, RLock
from uuid import uuid4

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    func,
    insert,
    select,
    text,
    true,
    update,
)

from archspec.persistence import control_image, evidence_manifest, sql_namespace
from archspec.persistence.migration_flow import _canonical, _sha

_SAFE = "业务文件尚未完整持久保存或恢复，未回退本地或覆盖历史"
_VERSION = "RUNTIME_BUSINESS_FILES_V1"
# 业务账本独占串行；目录共享锁同时保护出版物及全局存储配额。
_LOCK = 0x415243485346494C
_META = MetaData()
_CONTROL = Table("runtime_file_state", _META,
    Column("id", Integer, primary_key=True), Column("migration_id", String(128), nullable=False),
    Column("source_cut_sha256", String(64), nullable=False), Column("revision", Integer, nullable=False),
    Column("head_sha256", String(64)))
_EVENTS = Table("runtime_file_events", _META,
    Column("revision", Integer, primary_key=True), Column("body_json", Text, nullable=False),
    Column("event_sha256", String(64), nullable=False, unique=True))
_UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
_FLOW = re.compile(rf"official-discovery/flows/({_UUID})\.json")
_OBSERVATION = re.compile(r"official-discovery/local-clues/observations/([a-f0-9]{64})\.json")
_ALLOWED = re.compile(rf"(?:official-qa/readings/{_UUID}/{_UUID}\.json|official-cache/original-only/(?:gb55031|official-[a-f0-9]{{32}})/[a-f0-9]{{64}}/(?:record\.json|original-[0-9]{{4}}\.part|page-[1-3]\.png)|official-discovery/web-bodies/[a-f0-9]{{64}}\.html|uploads/{_UUID}\.(?:txt|pdf|docx)|official-discovery/(?:runs/{_UUID}|flows/{_UUID}|queries/[a-f0-9]{{64}}|local-clues/observations/[a-f0-9]{{64}})\.json|official-evaluations/runs/{_UUID}\.json)")


# 诊断只来自代码内固定枚举；不读取异常原文/SQLSTATE/URL/密钥或其派生值。
_FAILURE_CODES = frozenset({
    "BUSINESS_FILES_UNAVAILABLE", "BUSINESS_FILES_WRITE_BINDING", "BUSINESS_FILES_WRITE_PATH",
    "BUSINESS_FILES_WRITE_IDENTITY", "BUSINESS_FILES_WRITE_LEDGER", "BUSINESS_FILES_WRITE_STAGE",
    "BUSINESS_FILES_OBJECT_HEALTH", "BUSINESS_FILES_OBJECT_PUBLISH", "BUSINESS_FILES_OBJECT_READBACK",
    "BUSINESS_FILES_SQL_LOCK", "BUSINESS_FILES_SQL_APPEND", "BUSINESS_FILES_SQL_COMMIT",
    "BUSINESS_FILES_LOCAL_REPLACE", "BUSINESS_FILES_READ_BINDING", "BUSINESS_FILES_READ_PATH",
    "BUSINESS_FILES_READ_LEDGER", "BUSINESS_FILES_READ_BYTES",
})


class BusinessFilesUnavailable(Exception):
    """独立安全异常，固定类别供既有流程记录；HTTP503仍使用原统一文案。"""
    def __init__(self, message=_SAFE, *, code="BUSINESS_FILES_UNAVAILABLE"):
        super().__init__(_SAFE)
        self.code = code if type(code) is str and code in _FAILURE_CODES else "BUSINESS_FILES_UNAVAILABLE"



def _check_observation(relative, content):
    """官网观察仍只是线索；文件名、内嵌身份与原 HTML 字节必须一起成立。"""
    match = _OBSERVATION.fullmatch(relative)
    if match is None:
        return
    body = json.loads(content)
    if (not isinstance(body, dict) or body.get("schema_version") != "LOCAL_CLUE_V1"
            or body.get("observation_id") != match[1]
            or body.get("kind") != "OFFICIAL_PAGE_CLUE" or body.get("scope") != "LOCAL_CLUE"
            or body.get("body_status") != "NOT_ACQUIRED"
            or body.get("attachment_status") != "NOT_INSPECTED"
            or not isinstance(body.get("html"), str) or not isinstance(body.get("visible_text"), str)
            or hashlib.sha256(body["html"].encode("utf-8")).hexdigest() != body.get("page_sha256")
            or _sha({key: item for key, item in body.items() if key != "observation_id"}) != match[1]):
        raise BusinessFilesUnavailable(_SAFE)



def _check_flow(relative, content):
    """通用文件账本只核流程外壳；查询、项目、操作者和引用由真实协调器复核。"""
    match = _FLOW.fullmatch(relative)
    if match is None:
        return
    body = json.loads(content)
    if (not isinstance(body, dict) or body.get("schema_version") != "OFFICIAL_QUERY_FLOW_V1"
            or body.get("id") != match[1] or type(body.get("revision")) is not int
            or body["revision"] < 1):
        raise BusinessFilesUnavailable(_SAFE)


def _check_qa_reading(relative, content):
    """私有任务位置不进入公开官方资产包；文件路径与内嵌操作身份一起核对。"""
    match = re.fullmatch(rf"official-qa/readings/({_UUID})/({_UUID})\.json", relative)
    if match is None:
        return
    from archspec.official_qa.reading_receipts import validate_receipt_bytes

    value = validate_receipt_bytes(content)
    if value.task_id != match[1] or value.operation_id != match[2]:
        raise BusinessFilesUnavailable(_SAFE)


def _check_expected(entry, expected_sha256):
    if expected_sha256 is None:
        return
    if not isinstance(expected_sha256, str) or (
            expected_sha256 != "" and re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None):
        raise BusinessFilesUnavailable(_SAFE)
    if expected_sha256 == "":
        if entry is not None:
            raise BusinessFilesUnavailable(_SAFE)
    elif entry is None or entry["sha256"] != expected_sha256:
        raise BusinessFilesUnavailable(_SAFE)


def _tables(connection, *, check=True, _schema_review=None):
    tables = sql_namespace.bind_tables(connection, [_CONTROL, _EVENTS])
    if check:
        if _schema_review is None:control_image._physical_schemas(connection,tables)
        else:control_image._assert_schema_review(_schema_review,connection,tables)
    return tables


def install_schema(engine, *, connection, migration_id, source_cut_sha256):
    """仅在原整批迁移事务安装；启动不补空历史。"""
    if connection.engine is not engine or not connection.in_transaction():
        raise BusinessFilesUnavailable(_SAFE)
    control, events = _tables(connection, check=False)
    control.metadata.create_all(connection, tables=[control, events])
    previous = connection.execute(select(control)).mappings().all()
    if not previous:
        connection.execute(insert(control).values(id=1, migration_id=migration_id,
            source_cut_sha256=source_cut_sha256, revision=0, head_sha256=None))
    state = read_state(connection)
    if any(state[k] != value for k, value in
           (("migration_id", migration_id), ("source_cut_sha256", source_cut_sha256))):
        raise BusinessFilesUnavailable(_SAFE)


def _mutable(path):
    return path.startswith("official-discovery/queries/") or _FLOW.fullmatch(path) is not None


def read_state(connection, *, _schema_review=None):
    """完整核对有界事件链与独立头记录，空表不能掩盖已签收文件。"""
    control, events = _tables(connection,_schema_review=_schema_review)
    # READ COMMITTED下头、上限与事件必须来自同一条语句快照。不能因允许
    # 进度写与正文读并行，就把旧头和新事件拼在一起；超限时不载入正文列。
    stats = select(func.count().label("event_count"),
        func.coalesce(func.sum(func.length(events.c.body_json)), 0).label("event_size")
        ).select_from(events).subquery()
    heads = select(*control.c,
        select(func.count()).select_from(control).scalar_subquery().label("control_count")
        ).subquery()
    rows = connection.execute(select(heads, stats,
        events.c.revision.label("event_revision"), events.c.body_json, events.c.event_sha256)
        .select_from(heads.join(stats, true()).outerjoin(events, and_(
            stats.c.event_count <= evidence_manifest.MAX_FILES,
            stats.c.event_size <= 32 * 1024 * 1024)))
        .order_by(events.c.revision).limit(evidence_manifest.MAX_FILES + 1)).mappings().all()
    if not rows or rows[0]["control_count"] != 1 or rows[0]["id"] != 1:
        raise BusinessFilesUnavailable(_SAFE)
    first = rows[0]
    header = {column.name: first[column.name] for column in control.c}
    count, size = first["event_count"], first["event_size"]
    if count > evidence_manifest.MAX_FILES or size > 32 * 1024 * 1024:
        raise BusinessFilesUnavailable(_SAFE)
    parent = None
    history = []
    event_rows = rows if count else []
    for number, row in enumerate(event_rows, 1):
        body = json.loads(row["body_json"])
        manifest = body["asset_manifest"]
        entries = evidence_manifest._check_manifest(manifest)
        if (row["event_revision"] != number or body["revision"] != number or body["parent_sha256"] != parent
                or body["schema_version"] != _VERSION or _sha(body) != row["event_sha256"]
                or any(body[k] != header[k] for k in ("migration_id", "source_cut_sha256"))
                or manifest["schema_version"] != evidence_manifest.SERVER_SCHEMA or len(entries) != 1
                or not _ALLOWED.fullmatch(entries[0]["path"])):
            raise BusinessFilesUnavailable(_SAFE)
        parent = row["event_sha256"]
        history.append(body)
    if count != len(history) or header["revision"] != len(history) or header["head_sha256"] != parent:
        raise BusinessFilesUnavailable(_SAFE)
    return {**header, "history": history}


def merged_assets(metadata, publications, state):
    """保留所有旧原件/记录；只有查询索引可选择新版本，旧字节仍按SHA保留。"""
    from archspec.persistence.snapshot_publication import merge_assets
    if any(state[k] != metadata[k] for k in ("migration_id", "source_cut_sha256")):
        raise BusinessFilesUnavailable(_SAFE)
    base = merge_assets(metadata["asset_manifest"], publications)
    entries = {e["path"]: e for e in base["entries"]}
    retained = list(entries.values())
    for body in state["history"]:
        entry = body["asset_manifest"]["entries"][0]
        previous = entries.get(entry["path"])
        if previous is not None and previous != entry and not _mutable(entry["path"]):
            raise BusinessFilesUnavailable(_SAFE)
        entries[entry["path"]] = entry
        retained.append(entry)
    # 查询索引的历史版本也计入存储护栏，不只统计当前可见文件。
    unique = {e["object_key"]: e["size_bytes"] for e in retained}
    if len(retained) > evidence_manifest.MAX_FILES or sum(unique.values()) > evidence_manifest.MAX_TOTAL_BYTES:
        raise BusinessFilesUnavailable(_SAFE)
    manifest = {"schema_version": base["schema_version"], "entries": [entries[p] for p in sorted(entries)]}
    manifest["manifest_id"] = _sha(manifest)
    evidence_manifest._check_manifest(manifest)
    return manifest


def _lock(connection):
    """目录共享→业务账本独占；正文发布仍以目录独占保护共同配额。"""
    from archspec.persistence.snapshot_catalog import _LOCK as catalog_lock
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
        connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock_shared(:lock)"), {"lock": catalog_lock})
        connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"), {"lock": _LOCK})
    else:raise BusinessFilesUnavailable(_SAFE)


@contextmanager
def _write_transaction(engine):
    """独立签收与借用Session共用同一锁合同，提交后才原子替换本机字节。"""
    with engine.connect() as connection:
        try:
            _lock(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


class RuntimeBusinessFiles:
    def __init__(self, generation, objects):
        self.generation, self.engine, self.objects = generation, generation.engine, objects
        self.root = generation.data_dir
        self._path_locks_guard = Lock()
        self._path_locks = {}

    @contextmanager
    def _path_guard(self, relative):
        """只串行同实例同路径的签收/消费；最后一个等待者退出即移除，未知ID不积累锁。"""
        with self._path_locks_guard:
            slot = self._path_locks.setdefault(relative, [RLock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                yield
        finally:
            with self._path_locks_guard:
                slot[1] -= 1
                if slot[1] == 0:
                    del self._path_locks[relative]

    def validate_binding(self, engine, cache_dir):
        from archspec.persistence.remote_startup import _ISSUER, RestoredCloudGeneration
        owner = self.generation
        if (type(owner) is not RestoredCloudGeneration or owner._issuer is not _ISSUER
                or owner.closed or owner.closing or engine is not self.engine or owner.engine is not engine
                or Path(cache_dir).resolve().parent != self.root or self.objects is None):
            raise BusinessFilesUnavailable(_SAFE)

    @contextmanager
    def _asset_audit(self, connection, state=None, publications=None):
        """只在当前组合操作内共用完整审计；退出后凭据失效，不跨上传或事务。"""
        from archspec.persistence.snapshot_publication import _RECORDS, read_publications

        tables = sql_namespace.bind_tables(connection, [_CONTROL, _EVENTS, _RECORDS])
        with control_image._schema_review_scope(connection, tables) as review:
            state = read_state(connection, _schema_review=review) if state is None else state
            publications = (read_publications(connection, _schema_review=review)
                            if publications is None else publications)
            yield state, publications, review

    def _assets(self, connection, state=None, publications=None, *, _schema_review=None):
        # 兼容原完整读取入口；仅当前受控组合审计可传入已有结构凭据与已核数据。
        if _schema_review is None:
            with self._asset_audit(connection, state, publications) as (checked_state, checked_publications, _):
                return merged_assets(self.generation.metadata, checked_publications, checked_state)
        from archspec.persistence.snapshot_publication import _RECORDS, read_publications

        tables = sql_namespace.bind_tables(connection, [_CONTROL, _EVENTS, _RECORDS])
        control_image._assert_schema_review(_schema_review, connection, tables)
        state = read_state(connection, _schema_review=_schema_review) if state is None else state
        publications = (read_publications(connection, _schema_review=_schema_review)
                        if publications is None else publications)
        return merged_assets(self.generation.metadata, publications, state)

    def write(self, path, content, *, session=None, expected_sha256=None):
        """先回读私有对象；文档原件收据与UPLOADED行一同提交，其余文件独立短签收。"""
        stage = "BUSINESS_FILES_WRITE_BINDING"
        try:
            self.validate_binding(self.engine, self.root / "official-cache")
            if not self.generation.metadata.get("source_closure", {}).get("authorization", {}).get("archive_private_files"):
                raise BusinessFilesUnavailable(_SAFE)
            stage = "BUSINESS_FILES_WRITE_PATH"
            path = evidence_manifest._root(path)
            relative = path.relative_to(self.root).as_posix()
            evidence_manifest._relative(relative, schema=evidence_manifest.SERVER_SCHEMA)
            if not _ALLOWED.fullmatch(relative) or not isinstance(content, bytes):raise BusinessFilesUnavailable(_SAFE)
            stage = "BUSINESS_FILES_WRITE_IDENTITY"
            if relative.startswith("official-discovery/web-bodies/") and (
                    not 0 < len(content) <= 2 * 1024 * 1024
                    or relative.rsplit("/", 1)[1] != hashlib.sha256(content).hexdigest() + ".html"):
                raise BusinessFilesUnavailable(_SAFE)
            _check_observation(relative, content)
            _check_flow(relative, content)
            _check_qa_reading(relative, content)
            if expected_sha256 is not None and _FLOW.fullmatch(relative) is None:
                raise BusinessFilesUnavailable(_SAFE)
            if session is not None and session.get_bind() is not self.engine:raise BusinessFilesUnavailable(_SAFE)
            stage = "BUSINESS_FILES_WRITE_STAGE"
            with TemporaryDirectory(prefix="business-file-", dir=self.generation.scratch_dir) as temporary:
                staging = Path(temporary);staged = staging / relative
                staged.parent.mkdir(parents=True);staged.write_bytes(content)
                manifest = evidence_manifest.build_manifest(staging, [relative], schema=evidence_manifest.SERVER_SCHEMA)
                entry = manifest["entries"][0]
                # 同一上传前映像一起核路径身份和完整历史配额；暂存不等于已上传/签收。
                stage = "BUSINESS_FILES_WRITE_LEDGER"
                with (control_image.image_transaction(self.engine) as connection,
                      self._asset_audit(connection) as (state, publications, review)):
                    assets = self._assets(connection, state, publications, _schema_review=review)
                    existing = {e["path"]: e for e in assets["entries"]}.get(relative)
                    stage = "BUSINESS_FILES_WRITE_IDENTITY"
                    _check_expected(existing, expected_sha256)
                    if existing is not None and existing != entry and not _mutable(relative):
                        raise BusinessFilesUnavailable(_SAFE)
                    if (existing is not None and existing != entry and _FLOW.fullmatch(relative)
                            and expected_sha256 is None):
                        raise BusinessFilesUnavailable(_SAFE)
                    stage = "BUSINESS_FILES_WRITE_LEDGER"
                    projected = {**state, "history": state["history"] + [{"asset_manifest": manifest}]}
                    self._assets(connection, projected, publications, _schema_review=review)
                stage = "BUSINESS_FILES_OBJECT_HEALTH"
                self.objects.store.health_check()
                stage = "BUSINESS_FILES_OBJECT_PUBLISH"
                evidence_manifest.publish_manifest(staging, manifest, self.objects)
                stage = "BUSINESS_FILES_OBJECT_READBACK"
                evidence_manifest.restore_manifest(manifest, staging / "readback", self.objects)
                stage = "BUSINESS_FILES_WRITE_STAGE"
            # 网络上传/回读已全部结束。先取得SQL写锁，再取路径锁，避免与已有
            # catalog共享读阶段反向等待；路径锁跨过独立SQL提交直到本机原子替换完成。
            with ExitStack() as held:
                stage = "BUSINESS_FILES_SQL_LOCK"
                if session is None:
                    with _write_transaction(self.engine) as connection:
                        held.enter_context(self._path_guard(relative))
                        stage = "BUSINESS_FILES_SQL_APPEND"
                        self._append(connection, manifest, existing, expected_sha256=expected_sha256)
                        stage = "BUSINESS_FILES_SQL_COMMIT"
                else:
                    connection = session.connection()
                    _lock(connection)
                    held.enter_context(self._path_guard(relative))
                    stage = "BUSINESS_FILES_SQL_APPEND"
                    self._append(connection, manifest, existing, expected_sha256=expected_sha256)
                # 不可变原件保护仍由签收前的完整身份/CAS核验负责；落盘失败诚实拒绝。
                stage = "BUSINESS_FILES_LOCAL_REPLACE"
                if evidence_manifest._root(path) != path:raise BusinessFilesUnavailable(_SAFE)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as output:output.write(content)
                    temporary.replace(path)
                finally:temporary.unlink(missing_ok=True)
        except BusinessFilesUnavailable as failure:
            code = failure.code
            if type(code) is str and code == "BUSINESS_FILES_UNAVAILABLE":code = stage
            # 即使上游手动改过异常属性，也只能重新签发固定枚举，不能透传任意code。
            raise BusinessFilesUnavailable(code=code) from None
        except Exception:  # noqa: BLE001 - 隔离驱动、对象存储与私有路径原文。
            raise BusinessFilesUnavailable(code=stage) from None

    def _append(self, connection, manifest, expected, *, expected_sha256=None):
        self.validate_binding(connection.engine, self.root / "official-cache")
        # 调用方已持目录共享和业务账本独占锁；上传后仍从新事务完整核验head。
        with self._asset_audit(connection) as (state, publications, review):
            entry = manifest["entries"][0]
            assets = self._assets(connection, state, publications, _schema_review=review)
            existing = {e["path"]: e for e in assets["entries"]}.get(entry["path"])
            _check_expected(existing, expected_sha256)
            if existing != expected:
                raise BusinessFilesUnavailable(_SAFE)
            if existing == entry:
                return
            body = {"schema_version": _VERSION, "migration_id": state["migration_id"],
                    "source_cut_sha256": state["source_cut_sha256"], "revision": state["revision"] + 1,
                    "parent_sha256": state["head_sha256"], "asset_manifest": manifest}
            # 发布历史在此受锁事务内未变；投影仍完整检查全部历史与存储护栏。
            projected = {**state, "history": state["history"] + [body]}
            self._assets(connection, projected, publications, _schema_review=review)
            digest = _sha(body)
            control, events = _tables(connection, _schema_review=review)
            connection.execute(insert(events).values(revision=body["revision"],
                body_json=_canonical(body).decode(), event_sha256=digest))
            result = connection.execute(update(control).where(control.c.id == 1,
                control.c.revision == state["revision"]).values(revision=body["revision"], head_sha256=digest))
            if result.rowcount != 1:
                raise BusinessFilesUnavailable(_SAFE)
        # 写后必须重新读取实际SQL全链和完整结构；不以投影或已失效的凭据代替。
        read_state(connection)

    def _verified(self, path, *, missing_ok, content_required):
        stage = "BUSINESS_FILES_READ_BINDING"
        try:
            self.validate_binding(self.engine, self.root / "official-cache")
            stage = "BUSINESS_FILES_READ_PATH"
            path = evidence_manifest._root(path)
            relative = path.relative_to(self.root).as_posix()
            # JSON控制记录和显式限额/内容寻址的网页原件共用同一次签收读取。
            html_body = re.fullmatch(r"official-discovery/web-bodies/[a-f0-9]{64}\.html", relative)
            if content_required and path.suffix != ".json" and html_body is None:
                raise BusinessFilesUnavailable(_SAFE)
            from archspec.persistence.runtime_bindings import publication_read_scope
            # 先取得/借用连接，避免持路径锁等待连接池。独立RR事务在路径锁后才
            # 首次读取账本；借用的短读阶段已持catalog共享锁，写方同样遵守SQL先行。
            stage = "BUSINESS_FILES_READ_LEDGER"
            # 独立读取先占连接、再等路径锁、最后BEGIN；借用读阶段保留原共享锁顺序。
            with publication_read_scope(self.engine, guard=self._path_guard(relative)) as connection:
                entry = {e["path"]: e for e in self._assets(connection)["entries"]}.get(relative)
                stage = "BUSINESS_FILES_READ_BYTES"
                if entry is None and missing_ok and not path.exists():
                    stage = "BUSINESS_FILES_READ_LEDGER"
                    return False, None
                actual = evidence_manifest._file(self.root, relative, schema=evidence_manifest.SERVER_SCHEMA)
                if entry is None or actual.stat().st_size != entry["size_bytes"]:
                    raise BusinessFilesUnavailable(_SAFE)
                content = None
                if content_required or _OBSERVATION.fullmatch(relative) or _FLOW.fullmatch(relative):
                    with actual.open("rb") as stream:
                        content = stream.read(entry["size_bytes"] + 1)
                    if (len(content) != entry["size_bytes"]
                            or hashlib.sha256(content).hexdigest() != entry["sha256"]):
                        raise BusinessFilesUnavailable(_SAFE)
                    _check_observation(relative, content)
                    _check_flow(relative, content)
                    _check_qa_reading(relative, content)
                elif evidence_manifest._sha(actual) != entry["sha256"]:
                    # 大型PDF/上传原件沿用流式SHA，不因JSON深读接口整件进入内存。
                    raise BusinessFilesUnavailable(_SAFE)
                stage = "BUSINESS_FILES_READ_LEDGER"
                return True, content
        except BusinessFilesUnavailable as failure:
            code = failure.code
            if type(code) is str and code == "BUSINESS_FILES_UNAVAILABLE":code = stage
            # 即使上游手动改过异常属性，也只能重新签发固定枚举，不能透传任意code。
            raise BusinessFilesUnavailable(code=code) from None
        except Exception:  # noqa: BLE001 - 隔离驱动、对象存储与私有路径原文。
            raise BusinessFilesUnavailable(code=stage) from None

    def read(self, path, *, missing_ok=False):
        """返回签收核验的控制JSON或固定HTML原件；不接受其它二进制路径。"""
        return self._verified(path, missing_ok=missing_ok, content_required=True)[1]

    def assert_readable(self, path, *, missing_ok=False):
        return self._verified(path, missing_ok=missing_ok, content_required=False)[0]


def selected_files():
    from archspec.persistence.runtime_bindings import current_runtime
    runtime = current_runtime()
    if runtime is None:
        if os.getenv("ARCHSPEC_PERSISTENCE_BACKEND", "LOCAL") != "LOCAL":raise BusinessFilesUnavailable(_SAFE)
        return None
    runtime.validate_selection()
    return runtime.business_files


def write_runtime_file(path, content, *, session=None, expected_sha256=None):
    selected = selected_files()
    if selected is None:return False
    selected.write(path, content, session=session, expected_sha256=expected_sha256)
    return True


def assert_runtime_file(path, *, missing_ok=False):
    selected = selected_files()
    if selected is not None:return selected.assert_readable(path, missing_ok=missing_ok)
    return None


def read_runtime_file(path, *, missing_ok=False):
    """区分LOCAL显式回退与REMOTE真正缺失；已签收损坏始终失败。"""
    selected = selected_files()
    if selected is None:return False, None
    return True, selected.read(path, missing_ok=missing_ok)
