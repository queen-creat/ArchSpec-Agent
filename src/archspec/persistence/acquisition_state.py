"""正文准备任务/批次同事务状态；普通读取不建表、不回退本地JSON。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from uuid import UUID

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
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.schema import CreateSchema

from archspec.persistence import control_image, sql_namespace
from archspec.persistence.control_migration import installation_scope

VERSION = "ACQUISITION_STATE_SQL_V1"
_SAFE = "正文准备状态无法完整核验或提交结果未知，未回退本地或返回成功"
_LOCK = 0x4152434853504151
# PAQ只固定owner/fence；所有状态写另用此锁串行，续期不排在PAQ独占等待者之后。
_STATE_WRITE_LOCK = 0x415243485352454E
_DIAGNOSTICS = frozenset({"STATE_CHECK", "CONNECTION", "CONNECTION_CAPACITY", "SQL_LOCK", "SQL", "COMMIT_UNKNOWN"})
_LOGGER = logging.getLogger(__name__)
_MAX_BYTES = 8 * 1024 * 1024
_EXECUTION = ContextVar("archspec_acquisition_execution", default=None)
_LEASE_VERSION = "ACQUISITION_EXECUTION_V1"
_RUNNING = {"DOWNLOADING", "PROCESSING"}
_META = MetaData()
_STATE = Table("official_acquisition_state", _META,
    Column("id", Integer, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("migration_id", String(128), nullable=False),
    Column("source_cut_sha256", String(64), nullable=False),
    Column("contract_version", String(40), nullable=False),
    CheckConstraint("id = 1"), CheckConstraint("revision > 0"))


class AcquisitionStateUnavailable(OSError):
    """持久状态/驱动故障安全错误，不携带SQL、路径或上游数据。"""
    code = "ACQUISITION_STATE_UNAVAILABLE"

    def __init__(self, message, *, diagnostic="STATE_CHECK"):
        # 固定分类仅供安全日志定位；不接受驱动消息、SQL、路径或动态异常名。
        super().__init__(message)
        self.diagnostic = diagnostic if type(diagnostic) is str and diagnostic in _DIAGNOSTICS else "STATE_CHECK"


class AcquisitionCapacityBusy(AcquisitionStateUnavailable):
    """尚未取得连接，不存在本轮事务或领取；仅允许工人回到正常等待。"""
    code = "ACQUISITION_CAPACITY_BUSY"


class AcquisitionStateConflict(ValueError):
    """旧revision或历史身份冲突；不自动重试或覆盖另一个运行实例。"""


class AcquisitionLeaseLost(RuntimeError):
    """处理权过期、归属变化或续期未知；旧工人不能再发布结果。"""
    code = "ACQUISITION_LEASE_LOST"


_FAILURE_CODES = frozenset({"SERVICE_RUNTIME", "SNAPSHOT_CATALOG", "SOURCE_REGISTRY", "UNEXPECTED",
                            "PROCESSING_LIMITED"})


# 历史故障只保存这些公开代码位置；不接受路径、动态类名、消息或SQL。
# 白名单保留历史文件名，不能因以后移动模块而让旧任务无法恢复。
_FAILURE_LOCATION_FILES = frozenset({
    "snapshot_catalog.py", "snapshot_publication.py", "service.py", "processing_cache.py",
    "semantic_experiment.py", "page_images.py", "runtime_bindings.py", "source_registry.py",
    "sql_namespace.py", "business_files.py", "control_image.py", "migration_flow.py",
    "qdrant.py", "supabase.py", "chunked.py", "provider.py", "source_metadata.py",
    "remote_startup.py", "activation.py", "control_migration.py",
})


def validated_failure(value):
    """队列、SQL写入和恢复共用诊断合同；旧的单code记录仍原样可读。"""
    from archspec.persistence.business_files import _FAILURE_CODES as business_codes

    if (type(value) is not dict or not {"code"} <= set(value) <= {"code", "locations"}
            or type(value["code"]) is not str
            or value["code"] not in _FAILURE_CODES | business_codes):
        raise ValueError("正文失败分类无效，未保存未经核对的诊断")
    result = {"code": value["code"]}
    if "locations" in value:
        rows = value["locations"]
        if (value["code"] != "SNAPSHOT_CATALOG" or type(rows) is not list
                or not 1 <= len(rows) <= 120):
            raise ValueError("正文失败位置无法安全核对")
        checked, seen = [], set()
        for row in rows:
            if (type(row) is not dict or set(row) != {"file", "line"}
                    or type(row["file"]) is not str or row["file"] not in _FAILURE_LOCATION_FILES
                    or type(row["line"]) is not int or not 1 <= row["line"] <= 1_000_000):
                raise ValueError("正文失败位置无法安全核对")
            identity = row["file"], row["line"]
            if identity in seen:
                raise ValueError("正文失败位置不能重复")
            seen.add(identity)
            checked.append(dict(row))
        result["locations"] = checked
    return result


@dataclass(frozen=True)
class AcquisitionClaim:
    job_id: str
    source_id: str
    owner: str
    fence: int


def _lease(value):
    if (not isinstance(value, dict)
            or set(value) != {"version", "owner", "fence", "deadline_us"}
            or value['version'] != _LEASE_VERSION or not _valid_id(value['owner'])
            or type(value['fence']) is not int or value['fence'] < 1
            or type(value['deadline_us']) is not int or value['deadline_us'] < 1):
        raise AcquisitionStateUnavailable(_SAFE)
    return value


def _canonical(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise AcquisitionStateUnavailable(_SAFE)
    return encoded


def _decode(encoded):
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise AcquisitionStateUnavailable(_SAFE)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise AcquisitionStateUnavailable(_SAFE)
            value[key] = item
        return value
    value = json.loads(encoded, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(AcquisitionStateUnavailable(_SAFE)))
    if not isinstance(value, dict) or set(value) != {"jobs", "batches"}:
        raise AcquisitionStateUnavailable(_SAFE)
    return _validate(value)


def _valid_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _validate(value):
    if (not isinstance(value, dict) or set(value) != {"jobs", "batches"}
            or any(not isinstance(value[key], dict) for key in value)
            or sum(len(value[key]) for key in value) > 50_000):
        raise AcquisitionStateUnavailable(_SAFE)
    jobs, batches = value["jobs"], value["batches"]
    for identity, job in jobs.items():
        if (not _valid_id(identity) or not isinstance(job, dict)
                or not {"id", "job_id", "source_id", "state", "created_at", "refresh", "cache_hit", "snapshot_id"}.issubset(job)
                or job.get("id") != identity
                or job.get("job_id") != identity
                or not isinstance(job.get("source_id"), str)
                or re.fullmatch(r"gb55031|official-[a-f0-9]{32}", job["source_id"]) is None
                or job.get("state") not in {"QUEUED", "DOWNLOADING", "PROCESSING", "READY", "ORIGINAL_ONLY", "FAILED", "INTERRUPTED"}
                or not isinstance(job.get("created_at"), str) or not job["created_at"]
                or type(job.get("refresh")) is not bool or type(job.get("cache_hit")) is not bool
                or (job.get("state") == "READY" and job.get("snapshot_id") is None)
                or (job.get("snapshot_id") is not None
                    and (not isinstance(job["snapshot_id"], str) or re.fullmatch(r"[a-f0-9]{64}", job["snapshot_id"]) is None))):
            raise AcquisitionStateUnavailable(_SAFE)
        if "failure" in job:
            try:
                validated_failure(job["failure"])
                if job["state"] not in {"FAILED", "INTERRUPTED"}:
                    raise ValueError
            except ValueError:
                raise AcquisitionStateUnavailable(_SAFE) from None
        if "execution" in job:
            _lease(job["execution"])
    for identity, batch in batches.items():
        if (not _valid_id(identity) or not isinstance(batch, dict) or batch.get("id") != identity
                or not isinstance(batch.get("items"), list)
                or not isinstance(batch.get("created_at"), str)
                or not isinstance(batch.get("discovery_run_id"), str)):
            raise AcquisitionStateUnavailable(_SAFE)
        for item in batch["items"]:
            if not isinstance(item, dict):
                raise AcquisitionStateUnavailable(_SAFE)
            reference = item.get("job_id")
            if reference is not None and (reference not in jobs or item.get("source_id") != jobs[reference]["source_id"]):
                raise AcquisitionStateUnavailable(_SAFE)
    return json.loads(_canonical(value))


def _sha(row):
    return hashlib.sha256(_canonical({key: value for key, value in row.items() if key != "payload_sha256"}).encode("utf-8")).hexdigest()


def _table(connection, *, check=True):
    table = sql_namespace.bind_tables(connection, [_STATE])[0]
    if check:
        inspector = inspect(connection)
        if not inspector.has_table(table.name, schema=table.schema):
            raise AcquisitionStateUnavailable(_SAFE)
        control_image._physical_schema(connection, table)
    return table


@contextmanager
def _transaction(engine, *, write, allow_capacity_wait=False, renewal=False):
    diagnostic = "CONNECTION"
    try:
        if renewal and not write:
            raise AcquisitionStateUnavailable(_SAFE)

        try:
            connection = engine.connect()
        except SQLAlchemyTimeoutError as error:
            # 仅接受已安装QueuePool的固定耗尽类别，且Connection尚未返回。
            # 驱动超时、进入事务后的锁/SQL/提交异常仍走原未知状态保护。
            if allow_capacity_wait and error.code == "3o7r":
                raise AcquisitionCapacityBusy("数据库连接正忙，本轮尚未领取正文任务", diagnostic="CONNECTION_CAPACITY") from None
            raise
        with connection:
            diagnostic = "SQL"
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            elif connection.dialect.name == "postgresql":
                connection.begin()
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                if write:
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                # 普通写：PAQ独占→状态写锁；发布：PAQ共享；续期：仅状态写锁。
                # 若续期也排PAQ共享，较早排队的owner写方会按公平队列再次阻断心跳。
                # 等PAQ的写方尚未持状态写锁；所有JSON写同锁串行，无反向取锁。
                if renewal and connection.get_isolation_level() != "READ COMMITTED":
                    raise AcquisitionStateUnavailable(_SAFE)
                diagnostic = "SQL_LOCK"
                if not renewal:
                    fn = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
                    connection.execute(text(f"SELECT pg_catalog.{fn}(:lock)"), {"lock": _LOCK})
                if write:
                    connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"), {"lock": _STATE_WRITE_LOCK})
            else:
                raise AcquisitionStateUnavailable(_SAFE)
            try:
                diagnostic = "SQL"
                yield connection
                diagnostic = "COMMIT_UNKNOWN"
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (AcquisitionStateUnavailable, AcquisitionStateConflict, AcquisitionLeaseLost):
        raise
    except Exception:  # noqa: BLE001 -- SQL/提交异常不可回显凭据或内部状态
        raise AcquisitionStateUnavailable(_SAFE, diagnostic=diagnostic) from None


def _read(connection):
    table = _table(connection)
    rows = connection.execute(select(table)).mappings().all()
    if len(rows) != 1:
        raise AcquisitionStateUnavailable(_SAFE)
    row = dict(rows[0])
    if (row["id"] != 1 or type(row["revision"]) is not int or row["revision"] < 1
            or row["contract_version"] != VERSION
            or not isinstance(row["migration_id"], str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", row["migration_id"]) is None
            or not isinstance(row["source_cut_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", row["source_cut_sha256"]) is None
            or row["payload_sha256"] != _sha(row)):
        raise AcquisitionStateUnavailable(_SAFE)
    state = _decode(row["payload_json"])
    return table, row, state


def install_schema(engine, *, initial_state, migration_id, source_cut_sha256, connection=None):
    """显式安装/导入；已有状态必须同身份、同历史，禁止空初始化覆盖任务。"""
    payload = _canonical(_validate(initial_state))
    if (not isinstance(migration_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration_id) is None
            or not isinstance(source_cut_sha256, str) or re.fullmatch(r"[a-f0-9]{64}", source_cut_sha256) is None):
        raise AcquisitionStateUnavailable(_SAFE)
    with installation_scope(engine, connection, _transaction, pg_lock=_LOCK) as selected:
        if selected.dialect.name == "postgresql":
            # 借用外层安装事务也遵守PAQ→状态写锁；独立安装同事务重入安全。
            selected.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(:lock)"), {"lock": _STATE_WRITE_LOCK})
            selected.execute(CreateSchema("archspec_control", if_not_exists=True))
        table = _table(selected, check=False)
        if inspect(selected).has_table(table.name, schema=table.schema):
            _, row, existing = _read(selected)
            if (row["migration_id"] != migration_id or row["source_cut_sha256"] != source_cut_sha256
                    or existing != initial_state):
                raise AcquisitionStateConflict("已有准备状态不同，拒绝覆盖或空初始化")
            return
        table.create(selected)
        row = {"id": 1, "revision": 1, "payload_json": payload, "migration_id": migration_id,
               "source_cut_sha256": source_cut_sha256, "contract_version": VERSION}
        row["payload_sha256"] = _sha(row)
        selected.execute(insert(table).values(**row))
        _read(selected)


class SQLAcquisitionState:
    def __init__(self, engine, *, clock=None, lease_seconds=90):
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 300:
            raise ValueError("正文处理租约必须为3至300秒")
        if clock is not None and (not callable(clock) or engine.dialect.name != 'sqlite'):
            raise ValueError("自定义时钟仅用于SQLite算法验证")
        self.engine = engine
        self.clock = clock
        self.lease_seconds = lease_seconds
        self._renewals_lock = RLock()
        self._renewals = set()

    def _now_us(self, connection):
        # PG在锁内读取实时数据库时钟，不能用事务开始时间或网页提供的时间。
        value = (connection.scalar(text('SELECT pg_catalog.clock_timestamp()'))
                 if connection.dialect.name == 'postgresql'
                 else self.clock() if self.clock is not None else datetime.now(UTC))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AcquisitionStateUnavailable(_SAFE)
        return int(value.timestamp() * 1_000_000)

    def _write(self, connection, table, row, state):
        candidate = _validate(state)
        if candidate == _decode(row['payload_json']):
            return row['revision']
        updated = {**row, 'revision': row['revision'] + 1, 'payload_json': _canonical(candidate)}
        updated['payload_sha256'] = _sha(updated)
        changed = connection.execute(update(table).where(
            table.c.id == 1, table.c.revision == row['revision']).values(**updated))
        if changed.rowcount != 1:
            raise AcquisitionStateConflict("准备状态已变化，未签收本次提交")
        _read(connection)
        return updated['revision']

    def _check_claim(self, connection, state, claim, *, source_id=None, terminal=False):
        if (type(claim) is not AcquisitionClaim or not _valid_id(claim.owner)
                or type(claim.fence) is not int or claim.fence < 1):
            raise AcquisitionLeaseLost("正文处理归属无法核对，未提交")
        execution = _EXECUTION.get()
        if (execution is not None and execution[0] is self and execution[1] == claim
                and execution[2].is_set()):
            raise AcquisitionLeaseLost("正文处理续期结果未知，未提交")
        job = state['jobs'].get(claim.job_id)
        lease = job.get('execution') if job else None
        if (not lease or job['source_id'] != claim.source_id
                or source_id is not None and source_id != claim.source_id
                or lease['owner'] != claim.owner or lease['fence'] != claim.fence
                or lease['deadline_us'] <= self._now_us(connection)
                or not terminal and job['state'] not in _RUNNING):
            raise AcquisitionLeaseLost("正文处理权已过期或归属变化，旧结果未提交")
        return job

    def assert_claim_on_connection(self, connection, claim, source_id):
        """快照发布借用同一SQL事务核处理权；不另开连接造成检查后再失效。"""
        if connection.engine is not self.engine:
            raise AcquisitionLeaseLost("正文与处理权不在同一数据库")
        if connection.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_catalog.pg_advisory_xact_lock_shared(:lock)'), {'lock': _LOCK})
        _, _, state = _read(connection)
        self._check_claim(connection, state, claim, source_id=source_id)

    def claim_next(self, owner):
        """短事务领取一个排队任务；互斥写锁内生成单调fence并保留原job/batch。"""
        if not _valid_id(owner):
            raise ValueError("处理实例必须是UUID")
        with _transaction(self.engine, write=True, allow_capacity_wait=True) as connection:
            table, row, state = _read(connection)
            recovered = self._recover_expired(connection, state)
            pending = [job for job in state['jobs'].values() if job['state'] == 'QUEUED']
            if not pending:
                if recovered:
                    self._write(connection, table, row, state)
                return None
            job = min(pending, key=lambda value: (value['created_at'], value['id']))
            now = self._now_us(connection)
            old = job.get('execution', {})
            fence = old.get('fence', 0) + 1
            job.update(state='DOWNLOADING', started_at=datetime.fromtimestamp(now/1_000_000, UTC).isoformat(),
                updated_at=datetime.fromtimestamp(now/1_000_000, UTC).isoformat(),
                execution={'version': _LEASE_VERSION, 'owner': owner, 'fence': fence,
                           'deadline_us': now + self.lease_seconds * 1_000_000})
            self._write(connection, table, row, state)
            claim = AcquisitionClaim(job['id'], job['source_id'], owner, fence)
            self._check_claim(connection, state, claim)
        # 真实提交出口完成后才给工人处理权；未知提交不执行正文。
        return claim

    def checkpoint(self, claim, state, **details):
        """进度和终态只更新当前job，保留并发追加的其它任务和批次。"""
        if state not in _RUNNING | {'READY', 'ORIGINAL_ONLY', 'FAILED'} or set(details) - {
                'progress', 'snapshot_id', 'cache_hit', 'error', 'review_status', 'started_at', 'failure'}:
            raise ValueError("正文处理更新字段无效")
        if 'failure' in details:
            details['failure'] = validated_failure(details['failure'])
            if state != 'FAILED':
                raise ValueError("只有失败终态可保存失败分类")
        with _transaction(self.engine, write=True) as connection:
            table, row, current = _read(connection)
            job = self._check_claim(connection, current, claim)
            now = datetime.fromtimestamp(self._now_us(connection)/1_000_000, UTC).isoformat()
            job.update(details, state=state, updated_at=now)
            if state == 'READY':
                job.pop('failure', None)
            if state in {'READY', 'ORIGINAL_ONLY', 'FAILED'}:
                job['finished_at'] = now
            self._write(connection, table, row, current)
            self._check_claim(connection, current, claim, terminal=True)
            result = json.loads(_canonical(job))
        return result

    def renew(self, claim):
        # 和领取共用“尚未取得连接”的容量分类；未执行SQL时不误判续期提交未知。
        with _transaction(self.engine, write=True, allow_capacity_wait=True, renewal=True) as connection:
            table, row, state = _read(connection)
            job = self._check_claim(connection, state, claim, terminal=True)
            if job['state'] not in _RUNNING:
                return False
            previous_deadline = job['execution']['deadline_us']
            job['execution']['deadline_us'] = self._now_us(connection) + self.lease_seconds * 1_000_000
            self._write(connection, table, row, state)
            # 新期限尚未确认提交，写入过程中不能越过旧期限再靠新值复活。
            if previous_deadline <= self._now_us(connection):
                raise AcquisitionLeaseLost("正文续期写入期间原处理权已过期，未提交")
            self._check_claim(connection, state, claim)
        return True

    def _recover_expired(self, connection, state):
        now = self._now_us(connection); count = 0
        for job in state['jobs'].values():
            if job['state'] in _RUNNING and (not job.get('execution')
                    or job['execution']['deadline_us'] <= now):
                stamp = datetime.fromtimestamp(now/1_000_000, UTC).isoformat()
                job.update(state='INTERRUPTED', updated_at=stamp, finished_at=stamp,
                    error='正文处理权已过期或旧归属无法核对，请重试；旧缓存保留')
                count += 1
        return count

    def recover_expired(self):
        """启动及周期领取共用恢复规则；有效其它实例任务和完成历史保持原样。"""
        with _transaction(self.engine, write=True) as connection:
            table, row, state = _read(connection)
            count = self._recover_expired(connection, state)
            if count:
                self._write(connection, table, row, state)
        return count

    def has_active_renewals(self):
        """实际续期线程仍占用连接时不能把工人整体视为结束。"""
        with self._renewals_lock:
            self._renewals = {worker for worker in self._renewals if worker.is_alive()}
            return bool(self._renewals)

    @contextmanager
    def execution(self, claim):
        """长下载/OCR以有限SQL续期保活；未知续期阻止后续进度和快照发布。"""
        failed = Event(); stop = Event()
        def heartbeat():
            while not stop.wait(self.lease_seconds / 3):
                try:
                    if not self.renew(claim):
                        return
                except AcquisitionCapacityBusy:
                    # 仅确定未开始的续期回到原有限等待；下次仍由DB时钟核原owner/fence。
                    # 过期不能续活，SQL/提交结果未知仍在下方停止，绝不重领或重放下载。
                    continue
                except Exception as failure:  # noqa: BLE001 - 固定阶段日志，不回显异常原文或重新领取。
                    failed.set()  # 先禁止后续发布；日志故障不能让心跳退出却保留使用权。
                    diagnostic = (failure.diagnostic if isinstance(failure, AcquisitionStateUnavailable)
                        else "LEASE_REJECTED" if isinstance(failure, AcquisitionLeaseLost) else "UNKNOWN")
                    if type(diagnostic) is not str or diagnostic not in _DIAGNOSTICS | {"LEASE_REJECTED", "UNKNOWN"}:
                        diagnostic = "UNKNOWN"
                    try:
                        _LOGGER.warning("ACQUISITION_RENEWAL_FAILED:%s", diagnostic)
                    except Exception:  # noqa: BLE001 - 日志写入失败仍保留失租标记，不回显处理器原文。
                        return
                    return
        worker = Thread(target=heartbeat, name='official-acquisition-renew', daemon=True)
        token = _EXECUTION.set((self, claim, failed))
        try:
            with self._renewals_lock:
                self._renewals.add(worker)
                worker.start()
            yield
        finally:
            stop.set()
            if worker.ident is not None:
                worker.join(timeout=15)
            with self._renewals_lock:
                if worker.is_alive():
                    failed.set()  # 有限等待结束仍保留实际线程的归属。
                else:
                    self._renewals.discard(worker)
            _EXECUTION.reset(token)

    def metadata(self):
        with _transaction(self.engine, write=False) as connection:
            _, row, _ = _read(connection)
            return {key: row[key] for key in ("revision", "migration_id", "source_cut_sha256", "contract_version")}

    def read(self):
        with _transaction(self.engine, write=False) as connection:
            _, row, state = _read(connection)
            return {"revision": row["revision"], **state}

    def replace(self, state, *, expected_revision):
        """候选在锁外生成；只在短事务核当前revision并发布完整job+batch。"""
        candidate = _validate(state)
        with _transaction(self.engine, write=True) as connection:
            table, row, previous = _read(connection)
            if type(expected_revision) is not int or row["revision"] != expected_revision:
                raise AcquisitionStateConflict("准备状态已变化，请读取当前状态后重试")
            if any(not set(previous[key]).issubset(candidate[key]) for key in previous):
                raise AcquisitionStateConflict("已有准备历史不能删除")
            if any('execution' in candidate['jobs'][identity]
                   for identity in set(candidate['jobs']) - set(previous['jobs'])):
                raise AcquisitionStateConflict("新任务不能携带自报处理权")
            for identity, job in previous["jobs"].items():
                if any(candidate["jobs"][identity].get(key) != job.get(key) for key in ("id", "job_id", "source_id", "created_at", "refresh")):
                    raise AcquisitionStateConflict("已有任务身份不能重绑")
                following = candidate['jobs'][identity]
                if following.get('execution') != job.get('execution'):
                    raise AcquisitionStateConflict("正文处理权只能由领取/续期事务修改")
                if job.get('execution') and job['state'] in _RUNNING and following != job:
                    raise AcquisitionStateConflict("活动正文任务只能由持有处理权的工人更新")
            if any(candidate["batches"][identity] != batch for identity, batch in previous["batches"].items()):
                raise AcquisitionStateConflict("已有批次引用不能重绑")
            return self._write(connection, table, row, candidate)


def guard_current_execution(connection, source_id):
    """现有快照发布入口调用；普通人工操作无工人上下文，继续原权限检查。"""
    selected = _EXECUTION.get()
    if selected is not None:
        store, claim, _ = selected
        store.assert_claim_on_connection(connection, claim, source_id)


def current_execution(repository):
    selected = _EXECUTION.get()
    return selected[1] if selected is not None and selected[0] is repository else None
