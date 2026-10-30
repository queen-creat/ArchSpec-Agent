"""后端拥有一次冷查询；复用发现、正文队列及签名业务记录，不重复执行浏览器步骤。"""

import copy
import hashlib
import json
import logging
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID, uuid4

from archspec.official_discovery.plan import REGION_NAMES
from archspec.official_discovery.public_queries import TOPICS, validated_public_titles
from archspec.official_documents.jobs import batch_id_for_operation
from archspec.official_documents.lookup import (
    attach_lookup,
    checked_observations,
    prepare_lookup_plan,
)
from archspec.official_documents.metadata_filter import SourceMetadataFilter
from archspec.official_documents.network_policy import network_decision
from archspec.official_qa.task_runtime import OWNER_TOKEN, owner_is_dead
from archspec.persistence.acquisition_state import AcquisitionStateUnavailable
from archspec.persistence.business_files import (
    BusinessFilesUnavailable,
    read_runtime_file,
    write_runtime_file,
)
from archspec.persistence.runtime_bindings import (
    RuntimeBindingsUnavailable,
    current_runtime,
    runtime_scope,
)
from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
from archspec.persistence.source_registry import SourceRegistryUnavailable
from archspec.standard_governance.compatibility import ContentUseDenied

VERSION = "OFFICIAL_QUERY_FLOW_V1"
_ACTIVE = {"RUNNING", "WAITING_BODY"}
_STATES = _ACTIVE | {"READY", "NO_BODY", "NO_EVIDENCE", "FAILED", "INTERRUPTED"}
_STEPS = ("LOCAL_LOOKUP", "OFFICIAL_DISCOVERY", "ACQUISITION", "RETRIEVE_AGAIN", "REFERENCE_CHECK")
_LOCK = RLock()
_COMPLETING = set()
_LOGGER = logging.getLogger(__name__)
_COMPLETION_CODES = {
    "CONTENT_USE_NOT_AUTHORIZED", "ACQUISITION_STATE_UNAVAILABLE", "BUSINESS_FILES_UNAVAILABLE",
    "SERVICE_RUNTIME_UNAVAILABLE", "SNAPSHOT_CATALOG_UNAVAILABLE", "SOURCE_REGISTRY_UNAVAILABLE",
    "QUERY_COMPLETION_REFERENCE_UNAVAILABLE", "QUERY_COMPLETION_UNAVAILABLE",
}
_SAFE = "查询流程记录无法核对；未重新联网或发送模型"


class _BatchIdentityChanged(RuntimeBindingsUnavailable):
    """已读批次属于另一绑定；不得为旧flow确认批次或追加恢复诊断。"""


def _instance_id():
    runtime = current_runtime()
    return runtime.instance_uuid if runtime is not None else None


def _now():
    return datetime.now(UTC).isoformat()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError(_SAFE)
    except (ValueError, AttributeError):
        raise ValueError(_SAFE) from None
    return value


class OfficialQueryFlow:
    """同一操作只发起一次；GET只观察已有队列并重做免费的当前权限检索。"""

    def __init__(self, discovery, acquisition):
        self.discovery = discovery
        self.acquisition = acquisition
        self.folder = discovery.cache_dir / "flows"

    @property
    def documents(self):
        # 历史公告GET并不需要正文服务；只在本次流程确实检索时取得它。
        return self.acquisition.documents

    def _path(self, flow_id):
        return self.folder / f"{_uuid(flow_id)}.json"

    def exists(self, flow_id):
        try:
            path = self._path(flow_id)
            managed, content = read_runtime_file(path, missing_ok=True)
            return content is not None if managed else path.is_file()
        except ValueError:
            return False

    def _read(self, flow_id, *, missing_ok=False):
        path = self._path(flow_id)
        managed, content = read_runtime_file(path, missing_ok=True)
        try:
            if not managed:
                content = path.read_bytes()
            elif content is None:
                raise FileNotFoundError
        except FileNotFoundError:
            if missing_ok:
                return None
            raise LookupError("查询流程不存在") from None
        try:
            row = json.loads(content)
            request = row["request"]
            valid = (row["schema_version"] == VERSION and row["id"] == flow_id
                     and type(row["revision"]) is int and row["revision"] > 0
                     and row["state"] in _STATES and row["phase"] in _STEPS
                     and isinstance(request, dict) and set(request) - {"public_titles"} in ({"query", "project_id", "refresh", "topics"}, {"query", "project_id", "refresh", "topics", "region"})
                     and ("public_titles" not in request or isinstance(request["public_titles"], list)
                          and tuple(request["public_titles"]) == validated_public_titles(request["public_titles"]))
                     and ("region" not in request or request["region"] in (*REGION_NAMES, "地域待补充"))
                     and isinstance(request["query"], str) and 0 < len(request["query"]) <= 500
                     and (request["project_id"] is None or isinstance(request["project_id"], str)
                          and 0 < len(request["project_id"]) <= 100)
                     and type(request["refresh"]) is bool and isinstance(request["topics"], list)
                     and len(request["topics"]) <= len(TOPICS)
                     and all(isinstance(topic, dict) and set(topic) == {"key"}
                             and topic["key"] in TOPICS for topic in request["topics"])
                     and row["request_digest"] == _digest(request)
                     and isinstance(row["owner_token"], str) and row["owner_token"].startswith("QA_OWNER_V1:")
                     and isinstance(row["trace"], list) and len(row["trace"]) <= len(_STEPS)
                     and len({item["step"] for item in row["trace"]}) == len(row["trace"])
                     and all(set(item) == {"step", "state"} and item["step"] in _STEPS
                             and item["state"] in {"RUNNING", "DONE", "SKIPPED", "FAILED"}
                             for item in row["trace"]))
            for key in ("discovery_run_id", "acquisition_batch_id", "planned_acquisition_batch_id"):
                if row[key] is not None:
                    _uuid(row[key])
            if row["owner_instance_id"] is not None:
                _uuid(row["owner_instance_id"])
            if (row["planned_acquisition_batch_id"] is not None
                    and row["planned_acquisition_batch_id"] != batch_id_for_operation(row["id"])):
                valid = False
            if (row["acquisition_batch_id"] is not None
                    and row["acquisition_batch_id"] != row["planned_acquisition_batch_id"]):
                valid = False
            if row.get("lookup_observations") is not None:
                checked_observations(row["lookup_observations"])
            references = row["references"]
            valid = valid and isinstance(references, list) and len(references) <= 10
            if valid:
                for reference in references:
                    valid = (isinstance(reference, dict)
                             and {"source_id", "snapshot_id"} <= set(reference)
                             and set(reference) <= {"source_id", "snapshot_id", "chunk_id", "metadata_id"}
                             and isinstance(reference["source_id"], str) and 0 < len(reference["source_id"]) <= 100
                             and isinstance(reference["snapshot_id"], str) and len(reference["snapshot_id"]) == 64
                             and all(char in "0123456789abcdef" for char in reference["snapshot_id"]))
                    if not valid:
                        break
            if row["state"] == "READY" and not references:
                valid = False
            if not valid:
                raise ValueError(_SAFE)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(_SAFE) from None
        return row, hashlib.sha256(content).hexdigest()

    def _save(self, row, previous):
        path = self._path(row["id"])
        content = _bytes(row)
        # REMOTE在同一PG事务里比较旧head并签收新私有字节；失败不能确认进度已保存。
        if write_runtime_file(path, content, expected_sha256=previous):
            return
        # LOCAL仅用于明确的本地/假模型测试；仍比较旧字节，禁止覆盖不同操作的进度。
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        if current != previous:
            raise RuntimeError("查询进度已变化，请重新读取；未重复联网")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid4()}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _change(self, flow_id, *, step=None, step_state=None, recovered_queue=False, **changes):
        with _LOCK:
            row, previous = self._read(flow_id)
            if row["state"] not in _ACTIVE and not recovered_queue:
                return row
            row.update(changes)
            if step:
                trace = {item["step"]: item["state"] for item in row["trace"]}
                trace[step] = step_state
                row["trace"] = [{"step": key, "state": trace[key]} for key in _STEPS if key in trace]
                row["phase"] = step
            row["revision"] += 1
            row["updated_at"] = _now()
            self._save(row, previous)
            return row

    def start(self, query, *, project_id=None, refresh=False, topics=(), operation_id=None, region=None,
              public_titles=()):
        flow_id = _uuid(operation_id) if operation_id else str(uuid4())
        # Dossier主题是对象；仅保存既有登记key，私有label/材料全文不进入流程或外部查询。
        keys = dict.fromkeys(topic.get("key") for topic in topics
                             if isinstance(topic, dict) and isinstance(topic.get("key"), str)
                             and topic["key"] in TOPICS)
        request = {"query": query.strip(), "project_id": project_id, "refresh": refresh,
                   "topics": [{"key": key} for key in keys]}
        titles = validated_public_titles(public_titles)
        if titles:
            # 只持久化公开名称；重放绑定同一组建议，GET不能新增模型或补查词。
            request["public_titles"] = list(titles)
        # 新地域随原始意图持久化；旧记录不改写、不重算摘要。
        if region is not None:
            if region not in (*REGION_NAMES, "地域待补充"):
                raise ValueError("地域不属于已登记公开范围")
            request["region"] = region
        if not request["query"]:
            raise ValueError("请先输入需要查找的规范问题")
        with _LOCK:
            if self.exists(flow_id):
                row, _ = self._read(flow_id)
                if row["request_digest"] != _digest(request):
                    raise RuntimeError("同一操作编号已用于其他问题或项目，未重新执行")
                return self._public(row), False
            row = {"schema_version": VERSION, "id": flow_id, "revision": 1,
                   "request": request, "request_digest": _digest(request), "owner_token": OWNER_TOKEN,
                   "owner_instance_id": _instance_id(),
                   "state": "RUNNING", "phase": "LOCAL_LOOKUP", "network": None,
                   "discovery_run_id": None, "acquisition_batch_id": None,
                   "planned_acquisition_batch_id": None, "references": [], "lookup_observations": None,
                   "trace": [{"step": "LOCAL_LOOKUP", "state": "RUNNING"}], "error_code": None,
                   "created_at": _now(), "updated_at": _now()}
            self._save(row, "")
            return self._public(row), True

    def _lookup_request(self, request):
        options = {"region": request["region"]} if "region" in request else {}
        if request.get("public_titles"):
            options["public_titles"] = request["public_titles"]
        return self._lookup(request["query"], **options)

    def _lookup(self, query, *, region=None, public_titles=()):
        # 与公开检索入口共享问题计划，先过滤地域；不拿未归一化BM25分数当置信度。
        plan = prepare_lookup_plan(query, region=region, public_titles=public_titles)
        options = {"ranking_version": "V1"}
        if public_titles:
            options["reading_priority_titles"] = public_titles
        # 后台与显式查阅共用有界并列/焦点择取，不能因词频偏差误判正文缺口。
        if plan.parallel_queries or plan.question_plan.query_focus is not None:
            options["lookup_plan"] = plan
        if plan.question_plan.explicit_source_scope is not None:
            options["question_plan"] = plan.question_plan
        if plan.region is not None:
            options["metadata_filter"] = SourceMetadataFilter(region=plan.region)
        result = self.documents.search_all(plan.retrieval_query, 10, **options)
        result.update(query=query, retrieval_query=plan.retrieval_query)
        found = attach_lookup(query, result, plan=plan)
        # 与回答阶段消费同一真实覆盖；词面命中其它书不能抵消公开建议书的正文缺口。
        discovery = plan.discovery_plan
        if public_titles and discovery and not discovery.explicit_targets(query) and discovery.region is None:
            from archspec.official_documents.public_title_coverage import missing_public_titles

            active_titles = [title for title in public_titles if ("TITLE", title) in discovery.targets]
            if active_titles:
                missing = missing_public_titles(result.get("coverage"), active_titles)
                # A readable suggested book cannot cancel complementary raw-topic
                # discovery. Only a hint-only plan may stop on an empty hint gap.
                complementary = any(term not in active_titles for term, _reason in discovery.queries)
                if missing or not complementary:
                    found["lookup"]["suggested_source_gaps"] = list(missing)
        return found

    def execute(self, flow_id):
        row, _ = self._read(flow_id)
        if row["state"] != "RUNNING" or row["owner_token"] != OWNER_TOKEN:
            return
        try:
            request = row["request"]
            found = self._lookup_request(request)
            decision = network_decision(request["query"], found["lookup"])
            self._change(flow_id, step="LOCAL_LOOKUP", step_state="DONE", network=decision,
                         lookup_observations=found.get("lookup", {}).get("observations"))
            if decision["reason"] in {"REGION_REQUIRED", "SCOPE_REVIEW_REQUIRED", "REFERENCE_ONLY", "LOCAL_READING_REQUIRED"} or (
                    not decision["required"] and not request["refresh"]):
                self._change(flow_id, step="OFFICIAL_DISCOVERY", step_state="SKIPPED")
                self._change(flow_id, step="ACQUISITION", step_state="SKIPPED")
                self._complete(flow_id, found)
                return
            self._change(flow_id, step="OFFICIAL_DISCOVERY", step_state="RUNNING")
            discovery = self.discovery.run(request["query"], topics=request["topics"], refresh=request["refresh"],
                **({"region": request["region"]} if "region" in request else {}),
                **({"public_titles": request["public_titles"]} if request.get("public_titles") else {}))
            self._change(flow_id, step="OFFICIAL_DISCOVERY", step_state="DONE", discovery_run_id=discovery["id"])
            if (discovery.get("status") not in {"COMPLETE", "PARTIAL"}
                    or discovery.get("local_coverage") in {"ENTRY_ONLY", "CLUES_ONLY"}
                    or not discovery.get("results")):
                self._change(flow_id, step="ACQUISITION", step_state="SKIPPED", state="NO_BODY",
                             error_code="OFFICIAL_BODY_NOT_ACQUIRED")
                return
            planned = batch_id_for_operation(flow_id)
            # 先签收稳定批次意图，再提交已有SQL队列；确认丢失后只读同一ID，绝不重送。
            self._change(flow_id, step="ACQUISITION", step_state="RUNNING",
                         planned_acquisition_batch_id=planned)
            batch = self.acquisition.submit(discovery, refresh=request["refresh"], operation_id=flow_id)
            if batch["id"] != planned:
                raise RuntimeError(_SAFE)
            # 响应后的只读收养也可能刚确认同一批次；统一入口不重复签收revision。
            self._confirm_batch(flow_id, batch)
        except Exception as error:  # noqa: BLE001 - 后台流程边界仅保存安全错误码，不回显密钥或HTTP原文。
            if not self._adopt_queue(flow_id):
                self._fail(flow_id, error)

    @staticmethod
    def _adoption_identity(row):
        return (row["request"]["project_id"], row["request_digest"], row["discovery_run_id"],
                row["planned_acquisition_batch_id"], row["owner_token"], row["owner_instance_id"])

    def _confirm_batch(self, flow_id, batch, *, expected=None):
        """execute与只读收养共用确认：fresh身份、同批幂等和原SQL CAS，终态不重开。"""
        with _LOCK:
            row, previous = self._read(flow_id)
            identity = self._adoption_identity(row)
            if expected is not None and identity != expected:
                return False
            if row["state"] not in _ACTIVE:
                return False
            planned = row["planned_acquisition_batch_id"]
            if (not planned or batch["id"] != planned or batch["discovery_run_id"] != row["discovery_run_id"]
                    or batch.get("query_flow_id") != row["id"]
                    or batch.get("refresh") is not row["request"]["refresh"]):
                raise _BatchIdentityChanged(_SAFE)
            if row["acquisition_batch_id"] == planned:
                return True
            if row["acquisition_batch_id"] is not None or row["phase"] != "ACQUISITION":
                raise RuntimeBindingsUnavailable(_SAFE)
            # 已经持同进程协调锁并取得当前字节；不再由_change重复审计同一份row。
            row.update(state="WAITING_BODY", acquisition_batch_id=planned, error_code=None,
                       revision=row["revision"] + 1, updated_at=_now())
            try:
                self._save(row, previous)
            except (BusinessFilesUnavailable, RuntimeError):
                # 跨实例竞争或提交确认丢失只fresh读回；不重放保存/提交正文或签FAILED。
                current, _ = self._read(flow_id)
                if (self._adoption_identity(current) == identity
                        and current["acquisition_batch_id"] == planned):
                    return current["state"] in _ACTIVE
                raise
            return True

    def _adopt_queue(self, flow_id, *, expected=None):
        row, _ = self._read(flow_id)
        if (row["state"] not in _ACTIVE or not row["planned_acquisition_batch_id"]
                or row["phase"] != "ACQUISITION"):
            return False
        identity = self._adoption_identity(row)
        if expected is not None and identity != expected:
            return False
        try:
            # 仅读取固定已签收批次；不存在时不submit、不联网、不领取或重试任务。
            batch = self.acquisition.get(row["planned_acquisition_batch_id"])
        except LookupError:
            return False
        return self._confirm_batch(flow_id, batch, expected=identity)

    def _fail(self, flow_id, error):
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum():
            code = "QUERY_FLOW_FAILED"
        row, _ = self._read(flow_id)
        return self._change(flow_id, step=row["phase"], step_state="FAILED", state="FAILED", error_code=code)

    def _complete(self, flow_id, found=None):
        row, _ = self._read(flow_id)
        if row["state"] not in _ACTIVE:
            return
        found = found if found is not None else self._lookup_request(row["request"])
        row = self._completion_step(row, "RETRIEVE_AGAIN", "DONE")
        row = self._completion_step(row, "REFERENCE_CHECK", "RUNNING")
        rows = found.get("results", [])
        indices = found.get("lookup", {}).get("primary_indices", [])
        references = []
        for index in indices:
            item = rows[index]
            # 每次完成都读取固定快照并重核当前搜索/摘录权限，不信旧候选或搜索摘要。
            self.documents.source_metadata(item["source_id"], item["snapshot_id"], item.get("metadata_id"))
            references.append({key: item[key] for key in ("source_id", "snapshot_id", "chunk_id", "metadata_id") if key in item})
        self._change(flow_id, step="REFERENCE_CHECK", step_state="DONE", references=references,
                     lookup_observations=found.get("lookup", {}).get("observations"),
                     state="READY" if references else "NO_EVIDENCE",
                     error_code=None if references else "NO_MATCHING_CLAUSE")

    def _completion_step(self, row, step, state):
        # 同一免费核验重试仍读当前事实；已签收的阶段事实无需再次追加事件或倒退phase。
        previous = {item["step"]: item["state"] for item in row["trace"]}.get(step)
        if previous == state or previous == "DONE" and state == "RUNNING":
            return row
        return self._change(row["id"], step=step, step_state=state)

    def _checked_batch(self, row, *, skip_changed=False):
        batch = self.acquisition.get(row["acquisition_batch_id"])
        if (batch["id"] != row["acquisition_batch_id"]
                or batch["discovery_run_id"] != row["discovery_run_id"]
                or batch.get("query_flow_id") != row["id"]
                or batch.get("refresh") is not row["request"]["refresh"]):
            if skip_changed:
                return None
            raise RuntimeBindingsUnavailable(_SAFE)
        if batch.get("worker_status") == "STORAGE_UNAVAILABLE":
            raise AcquisitionStateUnavailable("正文准备状态暂不可核对，未判定执行失败")
        return batch

    @staticmethod
    def _batch_active(batch):
        return any(item["state"] in {"QUEUED", "DOWNLOADING", "PROCESSING"}
                   for item in batch["items"])

    def _finish_waiting(self, row, batch):
        if row["state"] != "WAITING_BODY" or self._batch_active(batch):
            return False
        if any(item["state"] == "READY" and item.get("kind") == "FULL_TEXT"
               for item in batch["items"]):
            row = self._completion_step(row, "ACQUISITION", "DONE")
            if row["state"] == "WAITING_BODY":
                self._complete(row["id"])
        else:
            self._change(row["id"], step="ACQUISITION", step_state="FAILED", state="NO_BODY",
                         error_code="OFFICIAL_BODY_NOT_ACQUIRED")
        return True

    def _completion_error(self, flow_id, error, expected, *, adoption=None):
        # 仅签收静态诊断；不把上次拒绝当作当前授权，不输出提供方原文或执行FAILED。
        categories = (
            (ContentUseDenied, "CONTENT_USE_NOT_AUTHORIZED"),
            (AcquisitionStateUnavailable, "ACQUISITION_STATE_UNAVAILABLE"),
            (BusinessFilesUnavailable, "BUSINESS_FILES_UNAVAILABLE"),
            (RuntimeBindingsUnavailable, "SERVICE_RUNTIME_UNAVAILABLE"),
            (SnapshotCatalogUnavailable, "SNAPSHOT_CATALOG_UNAVAILABLE"),
            (SourceRegistryUnavailable, "SOURCE_REGISTRY_UNAVAILABLE"),
            (LookupError, "QUERY_COMPLETION_REFERENCE_UNAVAILABLE"),
        )
        code = next((code for kind, code in categories if isinstance(error, kind)),
                    "QUERY_COMPLETION_UNAVAILABLE")
        try:
            with _LOCK:
                row, previous = self._read(flow_id)
                if adoption is not None:
                    if (self._adoption_identity(row) != adoption
                            or isinstance(error, _BatchIdentityChanged)):
                        return
                    if row["state"] == "RUNNING" and row["acquisition_batch_id"] is None:
                        # 这里只签“原批次尚未核对”的观察错误，不证明队列存在，也不改变执行状态。
                        if row["phase"] == "ACQUISITION" and row["error_code"] != code:
                            row.update(error_code=code, revision=row["revision"] + 1, updated_at=_now())
                            self._save(row, previous)
                        return
                identity = (row["request"]["project_id"], row["request_digest"],
                            row["discovery_run_id"], row["acquisition_batch_id"])
                if identity == expected and row["state"] == "WAITING_BODY" and row["error_code"] != code:
                    # 归属无法再核对时不为旧批次追加诊断；下一HTTP读取仍沿原503拒绝。
                    self._checked_batch(row)
                    self._change(flow_id, error_code=code)
        except Exception:  # noqa: BLE001 - 连诊断也无法签收时只记固定日志，保留原状态。
            _LOGGER.warning("查询免费核验诊断暂不可签收，流程 %s", flow_id)

    def _complete_after_response(self, flow_id, project_id, request_digest, discovery_id, batch_id, *, adoption=None):
        key = (str(self.folder.resolve()), flow_id)
        expected = (project_id, request_digest, discovery_id, batch_id)
        # add_task并不保证执行；只在真正开始时占用，并在所有退出分支释放。
        with _LOCK:
            if key in _COMPLETING:
                return
            _COMPLETING.add(key)
        try:
            bindings = getattr(self.documents, "_runtime_bindings", None)
            selected = current_runtime()
            if selected is not None and selected is not bindings:
                # 外来请求绑定不能被捕获的另一绑定替代，也不能借它签收错误诊断。
                return
            # 后台有独立Session所有权；完成与错误签收均在同一显式REMOTE范围。
            with runtime_scope(bindings):
                if bindings is not None:
                    from archspec.persistence.runtime_bindings import current_read_connection
                    if current_read_connection(bindings.engine) is not None:
                        return
                try:
                    row, _ = self._read(flow_id)
                    if adoption is not None:
                        if row["state"] not in _ACTIVE or self._adoption_identity(row) != adoption:
                            return
                        if row["acquisition_batch_id"] is None:
                            if not self._adopt_queue(flow_id, expected=adoption):
                                # 原实例确已消失且无已签收批次，保留原中断语义；仍先fresh核归属。
                                with _LOCK:
                                    current, _ = self._read(flow_id)
                                    if (current["state"] == "RUNNING"
                                            and self._adoption_identity(current) == adoption):
                                        if current["owner_instance_id"] != _instance_id():
                                            self._change(flow_id, state="INTERRUPTED", error_code="EXECUTION_INSTANCE_CHANGED")
                                        elif owner_is_dead(current["owner_token"]):
                                            self._change(flow_id, state="INTERRUPTED", error_code="QUERY_PROCESS_INTERRUPTED")
                                return
                            row, _ = self._read(flow_id)
                        if self._adoption_identity(row) != adoption:
                            return
                    if row["state"] != "WAITING_BODY":
                        return
                    if (row["request"]["project_id"] != project_id or row["request_digest"] != request_digest
                            or row["discovery_run_id"] != discovery_id or row["acquisition_batch_id"] != batch_id):
                        return
                    batch = self._checked_batch(row, skip_changed=True)
                    if batch is not None:
                        self._finish_waiting(row, batch)
                except Exception as error:  # noqa: BLE001 - 已flush后仅签收固定原因，不改执行FAILED。
                    self._completion_error(flow_id, error, expected, adoption=adoption)
        except Exception:  # noqa: BLE001 - 无法建立原绑定时不借其它范围补写，且不回显异常原文。
            _LOGGER.warning("查询免费核验运行范围暂不可用，流程 %s", flow_id)
        finally:
            with _LOCK:
                _COMPLETING.discard(key)

    def get(self, flow_id, *, project_id=None, background=None):
        result = self.get_if_present(flow_id, project_id=project_id, background=background)
        if result is None:
            raise LookupError("查询流程不存在")
        return result

    def get_if_present(self, flow_id, *, project_id=None, background=None):
        """一次核验后观察；HTTP先返回已核进度，直接调用保留同步完成合同。"""
        try:
            _uuid(flow_id)
        except ValueError:
            raise LookupError("查询流程不存在") from None
        checked = self._read(flow_id, missing_ok=True)
        if checked is None:
            return None
        row, _ = checked
        if row["request"]["project_id"] != project_id:
            raise LookupError("查询流程不存在")
        refresh_row, batch = False, None
        try:
            # 仅确认缺失才读回孤批；已确认终态不会因GET再次回到WAITING。
            if (row["state"] in _ACTIVE and row["planned_acquisition_batch_id"]
                    and row["phase"] == "ACQUISITION" and row["acquisition_batch_id"] is None):
                if background is not None:
                    # HTTP只返回已核的未确认事实。收养可能等待submit锁或写CAS，必须在flush后。
                    background.add_task(self._complete_after_response, flow_id, project_id,
                        row["request_digest"], row["discovery_run_id"], row["planned_acquisition_batch_id"],
                        adoption=self._adoption_identity(row))
                    return self._public(row)
                if self._adopt_queue(flow_id):
                    row, _ = self._read(flow_id)
            if row["state"] == "RUNNING" and row["owner_instance_id"] != _instance_id():
                refresh_row = True
                self._change(flow_id, state="INTERRUPTED", error_code="EXECUTION_INSTANCE_CHANGED")
            elif row["state"] == "RUNNING" and owner_is_dead(row["owner_token"]):
                refresh_row = True
                self._change(flow_id, state="INTERRUPTED", error_code="QUERY_PROCESS_INTERRUPTED")
            elif row["state"] == "WAITING_BODY":
                batch = self._checked_batch(row)
                if not self._batch_active(batch):
                    if background is None:
                        refresh_row = self._finish_waiting(row, batch)
                    else:
                        background.add_task(self._complete_after_response, flow_id, project_id,
                                            row["request_digest"], row["discovery_run_id"],
                                            row["acquisition_batch_id"])
        except (AcquisitionStateUnavailable, BusinessFilesUnavailable, RuntimeBindingsUnavailable,
                SnapshotCatalogUnavailable, SourceRegistryUnavailable, ContentUseDenied, LookupError):
            raise
        except Exception:  # noqa: BLE001 - 未知观察故障仍只返回安全503，保留执行归属及历史。
            raise RuntimeBindingsUnavailable(_SAFE) from None
        if refresh_row:
            row, _ = self._read(flow_id)
        return self._public(row, batch=batch)

    def _current_binding_state(self, references, observation_bindings):
        """历史候选与READY共用一次当前固定读取门；不重检主题，不重跑联网。"""
        selected = {}
        strict = set()
        for binding in [*references, *observation_bindings]:
            key = (binding["source_id"], binding["snapshot_id"])
            existing = selected.get(key)
            if (existing is not None and existing.get("metadata_id") is not None
                    and binding.get("metadata_id") is not None
                    and existing["metadata_id"] != binding["metadata_id"]):
                return "UNAVAILABLE", "CURRENT_EVIDENCE_UNAVAILABLE"
            if existing is None or binding.get("metadata_id") is not None:
                selected[key] = binding
        strict.update((binding["source_id"], binding["snapshot_id"]) for binding in observation_bindings)
        try:
            with self.documents.read_phase(sql=True):
                for key, binding in selected.items():
                    fixed = self.documents.source_metadata(*key, binding.get("metadata_id"))
                    if key in strict and (
                            not isinstance(fixed, dict) or fixed.get("source_id") != key[0]
                            or fixed.get("snapshot_id") != key[1]
                            or binding.get("metadata_id") is not None
                            and fixed.get("metadata_id") != binding["metadata_id"]):
                        raise ValueError("当前来源档案与历史固定绑定不一致")
        except ContentUseDenied:
            return "RESTRICTED", "CONTENT_USE_NOT_AUTHORIZED"
        except (LookupError, OSError, ValueError, RuntimeError, TypeError, KeyError):
            return "UNAVAILABLE", "CURRENT_EVIDENCE_UNAVAILABLE"
        return "AVAILABLE", None

    def _public(self, row, *, batch=None):
        # 不持久化/重放正文摘录。阅读候选必须走现有的当前权限搜索接口。
        result = {"id": row["id"], "queries": [], "results": [], "errors": [], "limitations": []}
        if row["discovery_run_id"]:
            result.update(copy.deepcopy(self.discovery.get(row["discovery_run_id"])))
        result["id"] = row["id"]
        if row["acquisition_batch_id"]:
            result["acquisition"] = batch if batch is not None else self.acquisition.get(row["acquisition_batch_id"])
        result["query_flow"] = {"version": VERSION, **{key: copy.deepcopy(row[key]) for key in (
            "id", "revision", "state", "phase", "network", "discovery_run_id", "acquisition_batch_id",
            "trace", "error_code", "created_at", "updated_at")}}
        if (row["state"] == "RUNNING" and row["acquisition_batch_id"] is None
                and row["error_code"] in _COMPLETION_CODES):
            result["errors"].append({"code": row["error_code"],
                "message": "上次原批次确认暂不可用；后续仅核对原批次，未重复联网或准备正文。"})
        if row["state"] == "WAITING_BODY" and row["error_code"] in _COMPLETION_CODES:
            message = ("上次出处核验未通过使用许可，尚未完成；后续免费检查将重新核对。"
                       if row["error_code"] == "CONTENT_USE_NOT_AUTHORIZED" else
                       "上次免费完成核验暂不可用，尚未完成；后续观察将重新核对。")
            result["errors"].append({"code": row["error_code"], "message": message})
        observations = row.get("lookup_observations")
        bindings = observations["source"]["bindings"] if observations is not None else []
        references = row["references"] if row["state"] == "READY" else []
        current, code = self._current_binding_state(references, bindings) if references or bindings else (None, None)
        # 观察始终标为历史：当前只重核固定绑定能否读取，不把旧主题/版本/OCR冒称fresh。
        if observations is not None:
            result["query_flow"].update(lookup_observations=copy.deepcopy(observations),
                lookup_observations_scope="HISTORICAL",
                current_observation_state=("READABLE" if current == "AVAILABLE" else current)
                    if bindings else "NOT_RECHECKED")
        if row["state"] == "READY":
            # 原终态及READY引用要求不变；任何当前读取失败仅使公开视图STALE。
            result["query_flow"].update(historical_state="READY", current_evidence=current)
            if current != "AVAILABLE":
                result["query_flow"].update(state="STALE", error_code=code)
        return result
