"""原生SQLite短事务任务仓库；执行器与模型调用均在事务之外。"""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from archspec.official_qa.condition_fields import CONDITION_KEYS
from archspec.official_qa.question_plan import ModelSemanticProposal, thaw_value
from archspec.official_qa.schemas import QuestionRequest
from archspec.official_qa.task_models import (
    OperationRecord,
    QAOperationRow,
    QATaskRow,
    TaskClaim,
    TaskRecord,
)
from archspec.projects.models import DesignProjectRow

CONTRACT_VERSION = "QA_TASK_V1"
FINAL_STATES = frozenset({"WAITING_CONDITIONS", "WAITING_SOURCE", "COMPLETED",
                          "INTERRUPTED", "FAILED"})
_METADATA_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_OWNER_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TASKS = QATaskRow.__table__
_OPERATIONS = QAOperationRow.__table__


class TaskConflict(ValueError):
    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason
        super().__init__(reason)


class TaskNotFound(LookupError):
    code = "TASK_NOT_FOUND"
    reason = "官方问答任务不存在"

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(self.reason)


def _json(value):
    return json.dumps(thaw_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _identity(value):
    if not isinstance(value, str):
        raise TypeError("任务和操作编号必须为UUID")
    return str(UUID(value))


def _owner(value):
    if not isinstance(value, str) or not _OWNER_TOKEN.fullmatch(value):
        raise ValueError("运行身份必须为有限服务器标识")
    return value


def _token(value):
    if not isinstance(value, str) or not _METADATA_TOKEN.fullmatch(value):
        raise ValueError("安全步骤只接受有限状态标识")
    return value


def _trace(value):
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        raise ValueError("安全步骤记录最多20项")
    result = []
    for item in value:
        if (not isinstance(item, Mapping) or "step" not in item
                or set(item) - {"step", "state", "error_code", "elapsed_ms"}):
            raise ValueError("安全步骤不能含正文、消息或任意额外字段")
        event = {key: _token(part) for key, part in item.items() if key != "elapsed_ms"}
        if "elapsed_ms" in item:
            elapsed = item["elapsed_ms"]
            if (type(elapsed) not in {int, float} or not math.isfinite(elapsed)
                    or not 0 <= elapsed <= 86_400_000):
                raise ValueError("步骤耗时必须为有限非负数")
            event["elapsed_ms"] = elapsed
        result.append(event)
    return _json(result)


def _questions(value):
    if (not isinstance(value, (list, tuple)) or len(value) > 3
            or any(not isinstance(key, str) or key not in CONDITION_KEYS for key in value)
            or len(set(value)) != len(value)):
        raise ValueError("等待问题必须是最多3个不重复的共同条件字段")
    return _json(value)


def _proposal(value):
    if isinstance(value, ModelSemanticProposal):
        value = value.semantic_payload()
    return _json(ModelSemanticProposal.model_validate(thaw_value(value)).semantic_payload())


def _utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _task_record(row):
    return TaskRecord(
        task_id=row["task_id"], project_id=row["project_id"], mode=row["mode"],
        state=row["state"], revision=row["revision"], request=json.loads(row["request_json"]),
        proposal=json.loads(row["proposal_json"]) if row["proposal_json"] is not None else None,
        question_keys=tuple(json.loads(row["question_keys_json"])),
        trace=tuple(json.loads(row["trace_json"])), current_step=row["current_step"],
        retry_count=row["retry_count"], owner_token=row["owner_token"],
        active_operation_id=row["active_operation_id"], created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]), contract_version=row["contract_version"])


def _operation_record(row):
    return OperationRecord(
        task_id=row["task_id"], operation_id=row["operation_id"],
        expected_revision=row["expected_revision"], request_digest=row["request_digest"],
        owner_token=row["owner_token"], state=row["state"], result_revision=row["result_revision"],
        created_at=_utc(row["created_at"]), finished_at=_utc(row["finished_at"]))


def _task(connection, task_id):
    return connection.execute(select(_TASKS).where(_TASKS.c.task_id == task_id)).mappings().first()


def _operation(connection, task_id, operation_id):
    return connection.execute(select(_OPERATIONS).where(
        _OPERATIONS.c.task_id == task_id,
        _OPERATIONS.c.operation_id == operation_id)).mappings().first()


class QATaskRepository:
    """仅操作新任务表；不创建表、不推断owner死亡、不复用外部会话事务。"""

    def __init__(self, session: Session):
        self.session = session
        bind = session.get_bind()
        self.engine = getattr(bind, "engine", bind)
        if self.engine.dialect.name != "sqlite":
            raise ValueError("当前任务仓库只支持已验证的SQLite事务")

    @contextmanager
    def _transaction(self):
        # 数据库写锁覆盖领取和CAS，跨连接、跨进程均不依赖Python内存锁。
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def get(self, task_id):
        task_id = _identity(task_id)
        with self.engine.connect() as connection:
            row = _task(connection, task_id)
            return _task_record(row) if row is not None else None

    def reading_operation(self, task_id, project_id):
        """同一只读连接核当前任务归属与已提交版本，不认领、修复或重放执行。"""
        task_id = _identity(task_id)
        with self.engine.connect() as connection:
            row = _task(connection, task_id)
            if row is None or row['project_id'] != project_id:
                return None, None
            if project_id is not None and connection.scalar(select(DesignProjectRow.id).where(
                    DesignProjectRow.id == project_id)) is None:
                return None, None
            task = _task_record(row)
            if task.state not in {'COMPLETED', 'WAITING_CONDITIONS', 'WAITING_SOURCE'}:
                return task, None
            operations = connection.execute(select(_OPERATIONS).where(
                _OPERATIONS.c.task_id == task_id, _OPERATIONS.c.result_revision == task.revision,
                _OPERATIONS.c.state == task.state)).mappings().fetchmany(2)
            return task, _operation_record(operations[0]) if len(operations) == 1 else None

    def list_summaries(self, project_id=None, *, limit=20, offset=0):
        """只读当前项目的有限历史摘要；复用实际运行Engine，不触发认领或租约修复。"""
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("历史列表分页参数超出范围")
        query = select(_TASKS.c.task_id, _TASKS.c.project_id, _TASKS.c.mode,
                       _TASKS.c.state, _TASKS.c.revision, _TASKS.c.updated_at, _TASKS.c.request_json).where(
            _TASKS.c.project_id == project_id, _TASKS.c.contract_version == CONTRACT_VERSION).order_by(
            _TASKS.c.updated_at.desc(), _TASKS.c.task_id.desc()).offset(offset).limit(limit + 1)
        with self.engine.connect() as connection:
            # 项目存在性与摘要共用同一只读连接；不占住请求Session再申请第二个云池连接。
            if project_id is not None and connection.scalar(select(DesignProjectRow.id).where(
                    DesignProjectRow.id == project_id)) is None:
                raise LookupError("当前项目不存在")
            rows = connection.execute(query).mappings().all()
        items = []
        for row in rows[:limit]:
            request = QuestionRequest.model_validate(json.loads(row["request_json"]))
            if request.project_id != project_id:
                raise ValueError("历史问题归属无法核对")
            items.append({"id": row["task_id"], "project_id": row["project_id"],
                          "query": request.query, "mode": row["mode"], "state": row["state"],
                          "revision": row["revision"], "updated_at": _utc(row["updated_at"]).isoformat()})
        return {"project_id": project_id, "items": items, "has_more": len(rows) > limit}

    def get_operation(self, task_id, operation_id):
        task_id, operation_id = _identity(task_id), _identity(operation_id)
        with self.engine.connect() as connection:
            row = _operation(connection, task_id, operation_id)
            return _operation_record(row) if row is not None else None

    def claim(self, task_id, operation_id, expected_revision, request, mode, owner_token):
        task_id, operation_id = _identity(task_id), _identity(operation_id)
        owner_token = _owner(owner_token)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("预期revision必须为非负整数")
        if mode not in {"LOCAL", "MODEL"}:
            raise ValueError("任务模式未登记")
        raw_request = request.model_dump() if isinstance(request, QuestionRequest) else thaw_value(request)
        if isinstance(request, QuestionRequest) and "model_sharing_confirmed" in raw_request:
            # 当前明确同意仍由模型路由即时核对；任务仅保存严格公共请求。
            from archspec.official_qa.model_layer import ModelQuestionRequest

            if isinstance(request, ModelQuestionRequest):
                raw_request = ModelQuestionRequest.model_validate(raw_request).model_dump(
                    exclude={"model_sharing_confirmed"})
        payload = QuestionRequest.model_validate(raw_request).model_dump(exclude_none=True)
        request_json = _json(payload)
        digest = hashlib.sha256(_json({"request": payload, "mode": mode,
                                     "revision": expected_revision, "action": "QA"}).encode()).hexdigest()
        now = datetime.now(UTC)
        with self._transaction() as connection:
            task = _task(connection, task_id)
            existing = _operation(connection, task_id, operation_id)
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise TaskConflict("OPERATION_PAYLOAD_CONFLICT", "同一操作编号不能改变请求内容")
                return TaskClaim(_task_record(task), _operation_record(existing), True)
            if task is None:
                if expected_revision != 0:
                    raise TaskNotFound(task_id)
                if task_id != operation_id:
                    raise TaskConflict("INITIAL_BINDING", "首次任务编号必须等于操作编号")
                connection.execute(insert(_TASKS).values(
                    task_id=task_id, project_id=payload.get("project_id"), mode=mode,
                    state="RUNNING", revision=0, retry_count=0, request_json=request_json,
                    proposal_json=None, question_keys_json="[]", trace_json="[]",
                    current_step="CLAIMED", owner_token=owner_token,
                    active_operation_id=operation_id, contract_version=CONTRACT_VERSION,
                    created_at=now, updated_at=now))
            else:
                if task["contract_version"] != CONTRACT_VERSION:
                    raise TaskConflict("CONTRACT_VERSION", "任务合同版本不兼容")
                original = json.loads(task["request_json"])
                if (task["mode"] != mode or any(original.get(key) != payload.get(key)
                                               for key in ("query", "project_id", "source_ids"))):
                    raise TaskConflict("IMMUTABLE_REQUEST", "原问题、项目、来源范围和模式不可变更")
                if task["revision"] != expected_revision:
                    raise TaskConflict("REVISION_CONFLICT", "任务revision已变化，请先读取当前状态")
                if task["state"] == "RUNNING" or task["active_operation_id"] is not None:
                    raise TaskConflict("ACTIVE_OPERATION", "该任务已有正在执行的操作")
                retry_count = task["retry_count"]
                if task["state"] in {"FAILED", "INTERRUPTED"}:
                    if retry_count >= 2:
                        raise TaskConflict("RETRY_LIMIT", "失败或中断后的显式重试最多两次")
                    retry_count += 1
                changed = connection.execute(update(_TASKS).where(
                    _TASKS.c.task_id == task_id, _TASKS.c.revision == expected_revision,
                    _TASKS.c.active_operation_id.is_(None)).values(
                        state="RUNNING", retry_count=retry_count, request_json=request_json,
                        question_keys_json="[]", trace_json="[]", current_step="CLAIMED",
                        owner_token=owner_token, active_operation_id=operation_id, updated_at=now))
                if changed.rowcount != 1:
                    raise TaskConflict("REVISION_CONFLICT", "任务在认领期间发生变化")
            connection.execute(insert(_OPERATIONS).values(
                task_id=task_id, operation_id=operation_id, expected_revision=expected_revision,
                request_digest=digest, owner_token=owner_token, state="RUNNING",
                result_revision=None, created_at=now, finished_at=None))
            return TaskClaim(_task_record(_task(connection, task_id)),
                             _operation_record(_operation(connection, task_id, operation_id)), False)

    def _active(self, connection, task_id, operation_id, owner_token):
        task, operation = _task(connection, task_id), _operation(connection, task_id, operation_id)
        if task is None:
            raise TaskNotFound(task_id)
        if (operation is None or task["state"] != "RUNNING" or operation["state"] != "RUNNING"
                or task["active_operation_id"] != operation_id
                or task["owner_token"] != owner_token or operation["owner_token"] != owner_token):
            raise TaskConflict("OWNER_CONFLICT", "操作不属于当前运行身份或已经结束")
        if task["revision"] != operation["expected_revision"]:
            raise TaskConflict("REVISION_CONFLICT", "任务版本与领取操作不一致")
        return task

    @staticmethod
    def _proposal_values(task, proposal_json):
        if proposal_json is None:
            return {}
        if task["proposal_json"] is not None and task["proposal_json"] != proposal_json:
            raise TaskConflict("PROPOSAL_CONFLICT", "已验证语义提案不能在恢复时替换")
        return {"proposal_json": proposal_json}

    def checkpoint(self, task_id, operation_id, owner_token, *, proposal=None,
                   trace=None, current_step=None):
        task_id, operation_id = _identity(task_id), _identity(operation_id)
        owner_token = _owner(owner_token)
        proposal_json = _proposal(proposal) if proposal is not None else None
        values = {"updated_at": datetime.now(UTC)}
        if trace is not None:
            values["trace_json"] = _trace(trace)
        if current_step is not None:
            values["current_step"] = _token(current_step)
        with self._transaction() as connection:
            task = self._active(connection, task_id, operation_id, owner_token)
            values.update(self._proposal_values(task, proposal_json))
            connection.execute(update(_TASKS).where(
                _TASKS.c.task_id == task_id, _TASKS.c.revision == task["revision"],
                _TASKS.c.active_operation_id == operation_id,
                _TASKS.c.owner_token == owner_token).values(**values))
            return _task_record(_task(connection, task_id))

    def finish(self, task_id, operation_id, owner_token, *, state, proposal=None,
               question_keys=(), trace=(), current_step=None):
        task_id, operation_id = _identity(task_id), _identity(operation_id)
        owner_token = _owner(owner_token)
        if state not in FINAL_STATES:
            raise ValueError("完成操作必须选择已登记的结束或等待状态")
        proposal_json = _proposal(proposal) if proposal is not None else None
        values = {"state": state, "question_keys_json": _questions(question_keys),
                  "trace_json": _trace(trace), "updated_at": datetime.now(UTC),
                  "owner_token": None, "active_operation_id": None}
        if current_step is not None:
            values["current_step"] = _token(current_step)
        with self._transaction() as connection:
            task = self._active(connection, task_id, operation_id, owner_token)
            values.update(self._proposal_values(task, proposal_json))
            revision = task["revision"] + 1
            values["revision"] = revision
            changed = connection.execute(update(_TASKS).where(
                _TASKS.c.task_id == task_id, _TASKS.c.revision == task["revision"],
                _TASKS.c.active_operation_id == operation_id,
                _TASKS.c.owner_token == owner_token).values(**values))
            if changed.rowcount != 1:
                raise TaskConflict("OWNER_CONFLICT", "完成时任务认领已发生变化")
            connection.execute(update(_OPERATIONS).where(
                _OPERATIONS.c.task_id == task_id, _OPERATIONS.c.operation_id == operation_id,
                _OPERATIONS.c.owner_token == owner_token, _OPERATIONS.c.state == "RUNNING").values(
                    state=state, result_revision=revision, finished_at=values["updated_at"]))
            return _task_record(_task(connection, task_id))

    def mark_interrupted(self, owner_token):
        owner_token, now = _owner(owner_token), datetime.now(UTC)
        count = 0
        with self._transaction() as connection:
            tasks = connection.execute(select(_TASKS).where(
                _TASKS.c.owner_token == owner_token, _TASKS.c.state == "RUNNING")).mappings().all()
            for task in tasks:
                self._active(connection, task["task_id"], task["active_operation_id"], owner_token)
                revision = task["revision"] + 1
                connection.execute(update(_TASKS).where(
                    _TASKS.c.task_id == task["task_id"], _TASKS.c.revision == task["revision"],
                    _TASKS.c.owner_token == owner_token).values(
                        state="INTERRUPTED", revision=revision, owner_token=None,
                        active_operation_id=None, updated_at=now))
                connection.execute(update(_OPERATIONS).where(
                    _OPERATIONS.c.task_id == task["task_id"],
                    _OPERATIONS.c.operation_id == task["active_operation_id"],
                    _OPERATIONS.c.owner_token == owner_token, _OPERATIONS.c.state == "RUNNING").values(
                        state="INTERRUPTED", result_revision=revision, finished_at=now))
                count += 1
        return count