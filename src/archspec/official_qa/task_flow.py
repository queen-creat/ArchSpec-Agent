"""单次有限问答的操作认领与等待收据；不保存原文或复用读取许可。"""
import re
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from archspec.official_qa.condition_fields import CONDITION_FIELDS, condition_prompt
from archspec.official_qa.question_plan import thaw_value
from archspec.official_qa.task_repository import QATaskRepository
from archspec.official_qa.task_runtime import OWNER_TOKEN, owner_is_dead

_HEADER_OPERATION = "X-Archspec-Operation-Id"
_HEADER_TASK = "X-Archspec-Task-Id"
_HEADER_REVISION = "X-Archspec-Expected-Revision"


@dataclass(frozen=True)
class OperationMetadata:
    task_id: str
    operation_id: str
    expected_revision: int


def operation_metadata(headers):
    operation, task, revision = (headers.get(name) for name in
                                 (_HEADER_OPERATION, _HEADER_TASK, _HEADER_REVISION))
    if operation is None and task is None and revision is None:
        return None
    if operation is None or (task is None) != (revision is None):
        raise ValueError("任务操作请求头不完整")
    def identity(value):
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError("任务操作编号不合法")
        parsed = str(UUID(value))
        if parsed != value.lower():
            raise ValueError("任务操作编号不合法")
        return parsed
    operation = identity(operation)
    if task is None:
        return OperationMetadata(operation, operation, 0)
    if (not isinstance(revision, str) or len(revision) > 10
            or not re.fullmatch(r"0|[1-9][0-9]*", revision)):
        raise ValueError("任务revision不合法")
    return OperationMetadata(identity(task), operation, int(revision))


def safe_trace(rows):
    """仅持久化有限机器状态，不持久化业务message/原件/pc说明。"""
    result = []
    for row in rows[-20:]:
        if not isinstance(row, dict):
            continue
        item = {key: value for key in ("step", "state", "error_code")
                if isinstance((value := row.get(key)), str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)}
        if "step" in item and "state" in item:
            result.append(item)
    return result


def waiting_keys(result):
    keys = []
    for row in (result.get("questions") or []) + (result.get("condition_followups") or []):
        key = row.get("key") if isinstance(row, dict) else None
        if key in CONDITION_FIELDS and key not in keys:
            keys.append(key)
    return keys[:3]


def condition_trace(result):
    """把必填/选填写进本次操作的有限元数据，不保存回答、不增加数据库列。"""
    required = any(isinstance(row, dict) and row.get("key") in CONDITION_FIELDS
                   for row in (result.get("questions") or []))
    return {"step": "QA_CONDITIONS", "state": (
        "REQUIRED" if required else "OPTIONAL" if waiting_keys(result) else "NONE")}


def task_condition_mode(task):
    if not task.question_keys:
        return "NONE"
    for row in reversed(task.trace):
        if row.get("step") == "QA_CONDITIONS":
            return row["state"] if row.get("state") in {"REQUIRED", "OPTIONAL"} else "UNVERIFIED"
    return "UNVERIFIED"


def legacy_condition_mode(task, repository, root):
    """旧记录仅以同一已提交版本的位置收据补核选填；不修改历史或重放问答。"""
    reader = getattr(repository, "reading_operation", None)
    if task.state != "WAITING_CONDITIONS" or not task.question_keys or not callable(reader):
        return "UNVERIFIED"
    try:
        from sqlalchemy.exc import SQLAlchemyError

        from archspec.official_qa.reading_receipts import load_positions

        current, operation = reader(task.task_id, task.project_id)
        fields = ("task_id", "project_id", "mode", "state", "revision", "question_keys")
        if current is None or operation is None or any(getattr(current, key) != getattr(task, key) for key in fields):
            return "UNVERIFIED"
        # V1位置收据只由REFERENCE_BRIEF/READING_ONLY产生；同版本可读摘录的补充为选填。
        return "OPTIONAL" if load_positions(root, task, operation)["state"] == "AVAILABLE" else "UNVERIFIED"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, SQLAlchemyError):
        return "UNVERIFIED"


def final_state(result):
    if result.get("status") in {"MODEL_ERROR", "EVIDENCE_CHECK_FAILED"}:
        return "FAILED"
    if waiting_keys(result):
        return "WAITING_CONDITIONS"
    if result.get("status") in {"MISSING_SOURCE", "NO_EVIDENCE"}:
        return "WAITING_SOURCE"
    return "COMPLETED"


def public_task(task, operation=None, *, replayed=False):
    result = {"id": task.task_id, "version": task.contract_version,
              "project_id": task.project_id, "mode": task.mode, "state": task.state,
              "revision": task.revision, "question_keys": list(task.question_keys),
              "condition_mode": task_condition_mode(task),
              "questions": [{"key": key, "label": condition_prompt(key)}
                            for key in task.question_keys],
              "current_step": task.current_step, "retry_count": task.retry_count,
              "trace": thaw_value(task.trace)}
    if operation is not None:
        result["operation"] = {"id": operation.operation_id, "state": operation.state,
                               "replayed": replayed}
    return result


class QATaskFlow:
    """先提交认领，再只运行一次原执行器；每次恢复重新核对当前资料。"""

    def __init__(self, session, *, owner_token=OWNER_TOKEN):
        from archspec.persistence.runtime_bindings import current_runtime
        selected = current_runtime()
        self._delegate = selected.task_flow(session) if selected is not None else None
        self.repository = self._delegate.repository if self._delegate is not None else QATaskRepository(session)
        self.owner_token = selected.instance_uuid if selected is not None else owner_token

    def get(self, task_id):
        if self._delegate is not None:
            return self._delegate.get(task_id)
        task = self.repository.get(task_id)
        if task is not None and task.state == "RUNNING" and owner_is_dead(task.owner_token):
            self.repository.mark_interrupted(task.owner_token)
            task = self.repository.get(task_id)
        return task

    def run(self, request, mode, metadata, factory):
        if self._delegate is not None:
            return self._delegate.run(request, mode, metadata, factory)
        self.get(metadata.task_id)
        claim = self.repository.claim(metadata.task_id, metadata.operation_id,
            metadata.expected_revision, request, mode, self.owner_token)
        if claim.replay:
            return {"status": "TASK_RECEIPT",
                    "summary": "该操作已有记录，未再次执行问答；请按当前任务状态继续。",
                    "qa_task": public_task(claim.task, claim.operation, replayed=True)}
        task = claim.task
        started = perf_counter()
        progress, current_step = [], "QA_EXECUTE"
        def record_step(step, state):
            nonlocal current_step
            current_step = step
            progress.append({"step": step, "state": state,
                             "elapsed_ms": round((perf_counter() - started) * 1000, 3)})
            self.repository.checkpoint(task.task_id, metadata.operation_id, self.owner_token,
                trace=progress[-20:], current_step=step)
        def checkpoint(proposal):
            self.repository.checkpoint(task.task_id, metadata.operation_id, self.owner_token,
                proposal=proposal, trace=progress[-20:], current_step=current_step)
        try:
            record_step("QA_EXECUTE", "RUNNING")
            service = factory()
            if mode == "MODEL":
                result = service.answer(request, resume_proposal=(
                    thaw_value(task.proposal) if task.proposal is not None else None),
                    on_proposal=checkpoint, on_step=record_step)
            else:
                result = service.answer(request)
        except Exception:
            progress.append({"step": current_step, "state": "FAILED",
                             "error_code": "QA_EXECUTION_FAILED"})
            self.repository.finish(task.task_id, metadata.operation_id, self.owner_token,
                state="FAILED", trace=progress[-20:], current_step=current_step)
            raise
        trace = safe_trace(result.get("trace") or [])
        step = trace[-1]["step"] if trace else current_step
        if result.get("status") == "MODEL_ERROR":
            model = result.get("model") or {}
            step = ("MODEL_UNDERSTAND" if model.get("understanding_state") == "FAILED"
                    else "MODEL_SELECT")
            trace += safe_trace([{"step": step, "state": "FAILED",
                                 "error_code": model.get("error_code")}])
        trace = [row for row in (progress + trace) if row.get("step") != "QA_CONDITIONS"][-18:]
        trace.append(condition_trace(result))
        trace.append({"step": "QA_EXECUTE", "state": (
            "FAILED" if final_state(result) == "FAILED" else "DONE"),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3)})
        from archspec.official_qa.reading_receipts import capture_positions
        result['reading_positions_record'] = capture_positions(service, task, metadata, result)
        finished = self.repository.finish(task.task_id, metadata.operation_id, self.owner_token,
            state=final_state(result), question_keys=waiting_keys(result),
            trace=trace, current_step=step)
        # 只传本次仍经过原执行器最终复核的结果；不从持久化收据重放引用。
        operation = self.repository.get_operation(task.task_id, metadata.operation_id)
        result["qa_task"] = public_task(finished, operation)
        return result
