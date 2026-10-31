"""把正文获取放进可恢复的单线程队列，避免网页等待整本扫描件识别。"""

import copy
import json
import logging
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import UUID, uuid4, uuid5

import httpx

from archspec.official_documents.download_errors import download_failure_message
from archspec.official_documents.fallbacks import POLICY, document_key, fallback_action
from archspec.persistence.source_write_gate import GatePaused, GateUnavailable

logger = logging.getLogger(__name__)
ACTIVE_STATES = {"QUEUED", "DOWNLOADING", "PROCESSING"}
_OPERATION_BATCH_NAMESPACE = UUID("1d08284e-f5bf-4f76-86a5-a4cfb5dd7391")


def _now():
    return datetime.now(UTC).isoformat()


def _valid_id(value: str):
    try:
        if str(UUID(value)) != value:
            raise ValueError("invalid identifier")
    except (ValueError, TypeError, AttributeError) as error:
        raise LookupError("正文准备记录不存在") from error



def batch_id_for_operation(operation_id):
    """流程先持久保存这个固定编号；未知提交后只读查同一批次，不盲重送。"""
    _valid_id(operation_id)
    return str(uuid5(_OPERATION_BATCH_NAMESPACE, operation_id))


def _failure_for(error):
    """只有已知安全异常类可细分；其它类型不读取动态code或异常原文。"""
    from archspec.official_documents.download_errors import AttachmentInputRejected
    from archspec.official_documents.processing_cache import ProcessingLimited
    from archspec.persistence.acquisition_state import validated_failure
    from archspec.persistence.business_files import BusinessFilesUnavailable
    from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable
    from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
    from archspec.persistence.source_registry import SourceRegistryUnavailable

    # 只认下载器签发的确切类型，不解析错误文案或任意异常的同名code属性。
    if type(error) is AttachmentInputRejected:
        code = {"SIZE_LIMIT": "ATTACHMENT_SIZE_LIMIT", "INVALID_PDF": "ATTACHMENT_INVALID_PDF"}.get(
            error.code, "UNEXPECTED")
    elif isinstance(error, BusinessFilesUnavailable):
        try:
            code = BusinessFilesUnavailable(code=error.code).code
        except Exception:  # noqa: BLE001 - 异常属性也可能损坏，仍只签固定兜底类别。
            code = "BUSINESS_FILES_UNAVAILABLE"
    elif isinstance(error, ProcessingLimited):
        code = "PROCESSING_LIMITED"
    elif isinstance(error, RuntimeBindingsUnavailable):
        code = "SERVICE_RUNTIME"
    elif isinstance(error, SnapshotCatalogUnavailable):
        code = "SNAPSHOT_CATALOG"
    elif isinstance(error, SourceRegistryUnavailable):
        code = "SOURCE_REGISTRY"
    else:
        code = "UNEXPECTED"
    failure = {"code": code}
    if code == "SNAPSHOT_CATALOG":
        from archspec.persistence.acquisition_state import _FAILURE_LOCATION_FILES
        from archspec.persistence.migration_operator import safe_diagnostic

        # 复用已有脱敏回溯；仅保留白名单文件/行号，连异常类名和函数名也不落库。
        # from None不能抹去排错所需的内部因果链，也不能借此回显异常原文。
        locations, seen = [], set()
        for cause in safe_diagnostic(error)["chain"]:
            for frame in cause["frames"]:
                identity = frame["file"], frame["line"]
                if frame["file"] in _FAILURE_LOCATION_FILES and identity not in seen:
                    seen.add(identity)
                    locations.append({"file": frame["file"], "line": frame["line"]})
        if locations:
            failure["locations"] = locations
    return validated_failure(failure)


class AcquisitionService:
    """一座服务只有一个工人；所选后端保留每份文件的处理进度。"""

    def __init__(self, cache_dir: Path, registry, documents, max_pending=50, *, gate=None, state_repository=None):
        self.cache_dir = cache_dir
        self.registry = registry
        self.documents = documents
        self.max_pending = max_pending
        self.gate = gate
        self._lock = RLock()
        self._wake = Event()
        self._stop = Event()
        self._thread = None
        self._worker_error = None
        from archspec.persistence.runtime_bindings import (
            RuntimeBindingsUnavailable,
            current_runtime,
        )
        selected = current_runtime()
        if selected is not None:
            if state_repository is not None and state_repository is not selected.acquisition_state:
                raise RuntimeBindingsUnavailable("准备状态与选定服务配置不同，未回退本地")
            if registry is not selected.registry:
                raise RuntimeBindingsUnavailable("准备来源与选定服务配置不同，未回退本地")
            state_repository = selected.acquisition_state
        self.state_repository = state_repository
        self.instance_uuid = selected.instance_uuid if selected is not None else str(uuid4())
        self._state_depth = 0
        self._state_revision = None
        self._jobs = {} if state_repository is not None else self._load_all("jobs")
        self._batches = {} if state_repository is not None else self._load_all("batches")
        if state_repository is None:
            from archspec.official_documents.fallbacks import checked_fallback
            if not all(checked_fallback(job, self._jobs) for job in self._jobs.values()):
                raise ValueError("备用正文任务身份无法核对，未执行下载")
        if state_repository is not None:
            from archspec.persistence.acquisition_state import SQLAcquisitionState
            if not isinstance(state_repository, SQLAcquisitionState):
                raise ValueError("正文准备状态后端无效，未回退本地")
            self._refresh_state()

    def _refresh_state(self):
        if self.state_repository is None or self._state_depth:
            return
        state = self.state_repository.read()
        # 保留工人已持有的同id字典对象；读写均使用SQL最新记录，不借本机JSON。
        for name in ("jobs", "batches"):
            target = self._jobs if name == "jobs" else self._batches
            for identity in set(target) - set(state[name]):
                del target[identity]
            for identity, value in state[name].items():
                if identity in target:
                    target[identity].clear()
                    target[identity].update(value)
                else:
                    target[identity] = value
        self._state_revision = state["revision"]

    @contextmanager
    def _state_change(self):
        if self.state_repository is None or self._state_depth:
            yield
            return
        self._refresh_state()
        previous = copy.deepcopy({"jobs": self._jobs, "batches": self._batches})
        self._state_depth = 1
        try:
            yield
            self._state_revision = self.state_repository.replace(
                {"jobs": self._jobs, "batches": self._batches}, expected_revision=self._state_revision)
        except BaseException:
            # 提交未知不返回成功，内存候选撤回；下次GET重新读取数据库实际结果。
            self._jobs, self._batches = previous["jobs"], previous["batches"]
            raise
        finally:
            self._state_depth = 0

    def bind_gate(self, gate):
        """只给尚无在途工人的实例绑定应用同门，不能把旧活跃下载冒称已覆盖。"""
        # 已安装的非空门禁止替换；同一对象的重复核对不等待正文提交持有的业务锁。
        # 首次/不同/空门仍走原锁内检查，保留二次身份核对以处理并发首次绑定。
        if gate is not None and self.gate is gate:
            return
        with self._lock:
            if self.gate is gate:
                return
            if self.gate is not None:
                raise GateUnavailable("正文工人已绑定不同停写门，不能确认统一停写")
            if self._thread is not None and self._thread.is_alive():
                raise GateUnavailable("正文工人已在无许可下运行，不能确认统一停写")
            self.gate = gate

    def _load_all(self, directory):
        values = {}
        for path in (self.cache_dir / directory).glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                _valid_id(value["id"])
                if directory == "jobs" and not {
                    "state", "source_id", "created_at", "refresh", "cache_hit", "snapshot_id",
                }.issubset(value):
                    raise ValueError("任务记录缺少必要字段")
                if "failure" in value:
                    from archspec.persistence.acquisition_state import validated_failure
                    validated_failure(value["failure"])
                    if directory == "jobs" and value["state"] not in {"FAILED", "INTERRUPTED"}:
                        raise ValueError("非失败任务不能带失败分类")
                values[value["id"]] = value
            except (OSError, ValueError, LookupError, KeyError, TypeError):
                logger.warning("正文准备记录无法读取：%s", path.name)
        return values

    def _save(self, directory, value):
        if self.state_repository is not None:
            return  # 当前候选由_state_change一次发布，不逐条写本地JSON。
        destination = self.cache_dir / directory / f"{value['id']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 先完整写临时文件，再替换，避免断电留下半份 JSON。
        temporary = destination.with_suffix(f".{uuid4()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _worker_scope(self):
        # 应用只传一个门；无门保留旧本机/独立构造合同，不擅自新建第二个门。
        return (self.gate.write("WORKER", blocking=False)
                if self.gate is not None else nullcontext())

    def start(self):
        """恢复旧任务也属于写入；暂停或短 SQL 锁忙时不改盘，之后显式 start 重试。"""
        with self._lock:
            try:
                with self._worker_scope():
                    self._start_locked()
            except (GatePaused, GateUnavailable):
                return

    def _start_locked(self):
        """调用方持实例锁及许可，保证绑定、恢复和创建线程不能错开。"""
        self._refresh_state()
        if self._worker_error:
            return
        if self._thread is not None and self._thread.is_alive():
            # 同一运行实例在下载未结束前重新开启，应继续原工人，不开第二个。
            self._stop.clear()
            self._wake.set()
            return
        if self.state_repository is not None:
            self.state_repository.recover_expired()
            self._refresh_state()
        else:
            for job in self._jobs.values():
                if job["state"] in {"DOWNLOADING", "PROCESSING"}:
                    try:
                        self._update(job, "INTERRUPTED", error="上次运行中断，请点击重试；旧缓存保留")
                    except OSError:
                        if not self._storage_failure(job):
                            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="official-document-worker", daemon=True)
        self._thread.start()
        self._wake.set()

    def is_stopped(self):
        """同时核主处理线程与其SQL续期，join超时不能冒充已结束。"""
        if self._thread is not None and self._thread.is_alive():
            return False
        return self.state_repository is None or not self.state_repository.has_active_renewals()

    def stop(self):
        """退出时不强行终止 OCR；若程序被关闭，下一次启动会显示中断状态。"""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _update(self, job, state, **details):
        if self.state_repository is not None:
            from archspec.persistence.acquisition_state import current_execution
            claim = current_execution(self.state_repository)
            if claim is not None:
                if job['id'] != claim.job_id:
                    from archspec.persistence.acquisition_state import AcquisitionLeaseLost
                    raise AcquisitionLeaseLost("工人不能更新另一正文任务")
                result = self.state_repository.checkpoint(claim, state, **details)
                job.clear(); job.update(result)
                self._refresh_state()
                return
        with self._state_change():
            current = self._jobs.get(job["id"])
            if current is None:
                raise LookupError("正文准备任务不存在")
            self._update_record(current, state, **details)

    def _update_record(self, job, state, **details):
        if "failure" in details:
            from archspec.persistence.acquisition_state import validated_failure
            details["failure"] = validated_failure(details["failure"])
            if state not in {"FAILED", "INTERRUPTED"}:
                raise ValueError("只有失败终态可保存失败分类")
        job.update(details)
        if state in {"QUEUED", "READY"}:
            job.pop("failure", None)
        job.update({"state": state, "updated_at": _now()})
        if state in {"READY", "ORIGINAL_ONLY", "FAILED", "INTERRUPTED", "SKIPPED"}:
            job["finished_at"] = job["updated_at"]
        self._save("jobs", job)

    def _latest_job(self, source_id):
        candidates = [job for job in self._jobs.values() if job["source_id"] == source_id]
        return max(candidates, key=lambda job: job["created_at"], default=None)

    def _source_item(self, notice, source):
        return {"id": str(uuid4()), "job_id": None, "source_id": source["source_id"],
                "notice_url": notice.get("url"), "title": source.get("title", "规范附件"),
                "standard_code": source.get("standard_code"), "kind": source["kind"],
                "reason": source.get("reason"), "error": None, "cache_hit": False,
                "snapshot_id": None, "created_at": _now(), "updated_at": _now(),
                "finished_at": None, "review_status": "PENDING_REVIEW", "reused": False}

    def _enqueue(self, notice, source, refresh, *, fallback_for=None):
        item = self._source_item(notice, source)
        if not source.get("auto_acquire"):
            return {**item, "state": "NEEDS_CONFIRMATION"}
        previous = self._latest_job(source["source_id"])
        try:
            cached = self.documents.get(source["source_id"])
            if cached is not None:
                if not isinstance(cached, dict) or not isinstance(cached.get("snapshot_id"), str):
                    raise ValueError("缓存快照记录无效")
                # 真正的正文服务还有页码、出处与片段检查；不能把能读的坏 JSON 标成完成。
                validate = getattr(self.documents, "_validate_snapshot", None)
                if callable(validate):
                    validate(cached, source["source_id"])
        except (ValueError, OSError, KeyError, TypeError):
            # 只隔离缓存读取；不能把损坏文件当成“没有缓存”并自动覆盖。
            logger.warning("官方正文缓存无法读取，暂停该来源准备：%s", source["source_id"])
            return {**item, "state": "FAILED",
                    "error": "这份正文缓存无法读取，未自动下载或覆盖；请检查本地缓存，其他附件继续处理"}
        processor = getattr(self.documents, "_processor_version", None)
        cache_current = cached is not None and (
            processor is None or cached.get("processor_version") == processor()
        )
        # 排队任务始终复用；失败任务不会因用户反复搜索而连续重试。
        reuse_ready = (previous and previous["state"] == "READY" and not refresh
                       and cache_current and previous["snapshot_id"] == cached["snapshot_id"])
        if previous and previous["state"] != "SKIPPED" and (previous["state"] != "READY" or reuse_ready):
            return {**item, "job_id": previous["id"], "id": previous["id"], "reused": True,
                    "cache_hit": previous["state"] == "READY"}
        pending_count = sum(job["state"] in ACTIVE_STATES for job in self._jobs.values())
        if pending_count >= self.max_pending and (refresh or not cache_current):
            return {**item, "state": "LIMIT_REACHED",
                    "reason": "后台排队已达到上限，本附件未省略；处理结束后再次查询可加入队列"}
        job = {**item, "job_id": item["id"], "refresh": refresh,
               "state": "READY" if cache_current and not refresh else "QUEUED"}
        if fallback_for is not None:
            job["fallback_for"] = fallback_for
        if cached:
            job["snapshot_id"] = cached["snapshot_id"]
        if job["state"] == "READY":
            job.update({"cache_hit": True, "finished_at": _now()})
        self._jobs[job["id"]] = job
        self._save("jobs", job)
        return {**item, "job_id": job["id"]}

    def submit(self, discovery: dict, refresh=False, *, operation_id=None):
        batch_id = batch_id_for_operation(operation_id) if operation_id is not None else None
        if operation_id is not None:
            _valid_id(discovery.get("id"))
            if type(refresh) is not bool:
                raise ValueError("流程正文准备的refresh必须是布尔值")
        with self._lock:
            with self._state_change():
                existing = self._batches.get(batch_id) if batch_id is not None else None
                if existing is not None:
                    # 实际SQL当前状态已在_state_change读取；不再登记来源、排队或唤醒工人。
                    if (existing.get("query_flow_id") != operation_id
                            or existing.get("discovery_run_id") != discovery["id"]
                            or existing.get("refresh") is not refresh):
                        raise RuntimeError("同一查询流程已绑定不同的正文准备条件，未重复执行")
                    batch = existing
                elif operation_id is not None:
                    batch = self._submit(discovery, refresh, operation_id=operation_id)
                else:
                    batch = self._submit(discovery, refresh)
            return self.get(batch["id"])

    def _submit(self, discovery: dict, refresh=False, *, operation_id=None):
        """只接受官方发现服务产生的记录；客户端没有提交网址的入口。"""
        with self._lock:
            if self._worker_error:
                raise RuntimeError(self._worker_error)
            items = []
            bounded = discovery.get("acquisition_policy") == POLICY
            primary, used_backups = {}, set()
            fresh = discovery.get("status") in {"COMPLETE", "PARTIAL"}
            for notice in discovery.get("results", []):
                # 新合同只自动准备实际公告主规范匹配的附件；关联/未知线索仍保留在发现记录。
                # 历史无计划记录沿用原合同，不能借新字段伪造其曾经核对过目标。
                planned = discovery.get("discovery_plan", {}).get("version") == "DISCOVERY_PLAN_V1"
                if planned and notice.get("target_match") != "TARGET":
                    items.append({"id": str(uuid4()), "job_id": None, "source_id": None,
                                  "notice_url": notice.get("url"), "title": notice.get("title"),
                                  "kind": "ASSOCIATED_NOTICE" if notice.get("target_match") == "ASSOCIATED"
                                  else "UNCONFIRMED", "state": "SKIPPED",
                                  "reason": notice.get("target_match_message")
                                  or "公告主规范未确认对应本次目标，未登记或自动准备其正文"})
                    continue
                if not fresh:
                    items.append({"id": str(uuid4()), "job_id": None, "source_id": None,
                                  "notice_url": notice.get("url"), "title": notice.get("title"),
                                  "kind": "UNCONFIRMED", "state": "SKIPPED",
                                  "reason": "公告查询失败或缓存已过期，不自动发起新的正文下载"})
                    continue
                sources = self.registry.register_notice(notice, discovery["id"])
                if not sources:
                    items.append({"id": str(uuid4()), "job_id": None, "source_id": None,
                                  "notice_url": notice.get("url"), "title": notice.get("title"),
                                  "kind": "UNCONFIRMED", "state": "NO_ATTACHMENT",
                                  "reason": "这份公告没有可登记的公开附件；不代表没有规范要求"})
                for source in sources:
                    key = document_key(source) if bounded and source.get("kind") == "FULL_TEXT" else None
                    parent = primary.get(key) if key else None
                    backup = (bounded and notice.get("notice_type") == "OFFICIAL_COPY"
                              and (key is None and bool(primary) or key is not None
                                   and any(old[1] == key[1] for old in primary)))
                    if bounded and key in primary and not backup:
                        items.append({**self._source_item(notice, source), "state": "SKIPPED",
                            "reason": "同版本正文已排队；保留公告供核对，未重复下载"})
                        continue
                    if backup and (parent is None or parent in used_backups):
                        items.append({**self._source_item(notice, source), "state": "SKIPPED",
                            "reason": "备用与已选正文版本未一致核对，或本轮已有一个备用；未自动下载"})
                        continue
                    item = (self._enqueue(notice, source, refresh, fallback_for=parent) if backup
                            else self._enqueue(notice, source, refresh))
                    items.append(item)
                    if backup:
                        used_backups.add(parent)
                    elif key and item.get("job_id"):
                        primary.setdefault(key, item["job_id"])
            batch = {"id": batch_id_for_operation(operation_id) if operation_id is not None else str(uuid4()),
                     "discovery_run_id": discovery["id"], "created_at": _now(), "items": items}
            if operation_id is not None:
                batch.update(query_flow_id=operation_id, refresh=refresh)
            self._batches[batch["id"]] = batch
            self._save("batches", batch)
            self._wake.set()
            return self.get(batch["id"])

    def get(self, batch_id):
        _valid_id(batch_id)
        with self._lock:
            self._refresh_state()
            batch = copy.deepcopy(self._batches.get(batch_id))
            if batch is None:
                raise LookupError("正文准备记录不存在")
            if ("query_flow_id" in batch and (
                    batch_id_for_operation(batch["query_flow_id"]) != batch_id
                    or type(batch.get("refresh")) is not bool)):
                raise RuntimeError("流程正文批次的固定身份无法核对，未重复执行")
            for item in batch["items"]:
                if item.get("job_id") in self._jobs:
                    reference = dict(item)
                    item.update(copy.deepcopy(self._jobs[item["job_id"]]))
                    # 同一下载任务可能出现在多次查询里，公告上下文和复用提示取本次引用。
                    item["notice_url"] = reference["notice_url"]
                    item["reused"] = reference.get("reused", False)
                    item["cache_hit"] = item["cache_hit"] or reference.get("cache_hit", False)
                elif item.get("job_id"):
                    item.update({"state": "INTERRUPTED", "job_id": None, "cache_hit": False,
                                 "error": "任务记录缺失或无法读取，不能确认处理结果；请重新查询，旧缓存未删除"})
                if self._worker_error and item.get("state") in ACTIVE_STATES:
                    item.update({"state": "INTERRUPTED", "error": self._worker_error})
            for item in batch["items"]:
                if "failure" in item:
                    from archspec.persistence.acquisition_state import validated_failure
                    validated_failure(item["failure"])
            states = [item["state"] for item in batch["items"]]
            batch["status"] = ("EMPTY" if not states else "RUNNING" if any(
                state in ACTIVE_STATES for state in states
            ) else "COMPLETE" if all(state == "READY" for state in states) else "ATTENTION")
            batch["worker_status"] = "STORAGE_UNAVAILABLE" if self._worker_error else "AVAILABLE"
            if self._worker_error:
                batch["worker_error"] = self._worker_error
            return batch

    def retry(self, job_id):
        with self._lock:
            with self._state_change():
                result = self._retry(job_id)
            return result

    def _retry(self, job_id):
        _valid_id(job_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise LookupError("正文准备任务不存在")
            if self._worker_error:
                raise RuntimeError(self._worker_error)
            # 原件兜底也可明确重新准备；普通查询仍复用终态，不自动反复下载。
            if job["state"] not in {"FAILED", "INTERRUPTED", "ORIGINAL_ONLY"}:
                raise ValueError("只有失败、中断或仅保存原件的任务可以重新准备正文")
            if sum(value["state"] in ACTIVE_STATES for value in self._jobs.values()) >= self.max_pending:
                raise RuntimeError("正文准备队列已满，请等待现有任务完成")
            self._update(job, "QUEUED", error=None, finished_at=None)
            self._wake.set()
            return copy.deepcopy(job)

    def _run(self):
        from archspec.persistence.acquisition_state import AcquisitionCapacityBusy

        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            if self._stop.is_set():
                return
            try:
                # 先登记根许可再选任务，下载/进度/成功失败及存储补救全在同一作用域。
                with self._worker_scope():
                    if self._stop.is_set():
                        return
                    if self.state_repository is not None:
                        # 所有实例原子领取；进度不再借整份状态的旧revision覆盖其它任务。
                        claim = self.state_repository.claim_next(self.instance_uuid)
                        if claim is None:
                            self._wake.clear()
                            continue
                        with self._lock:
                            self._refresh_state()
                            job = self._jobs[claim.job_id]
                        with self.state_repository.execution(claim):
                            self._process(job)
                    else:
                        with self._lock:
                            pending = []
                            for candidate in self._jobs.values():
                                if candidate["state"] != "QUEUED":
                                    continue
                                action = fallback_action(candidate, self._jobs)
                                if action == "SKIP":
                                    self._update(candidate, "SKIPPED", reason="主来源已可用或不属于可替代的下载失败；未下载备用")
                                elif action == "RUN":
                                    pending.append(candidate)
                            if not pending:
                                self._wake.clear()
                                continue
                            job = min(pending, key=lambda value: value["created_at"])
                        try:
                            with self._lock:
                                self._update(job, "DOWNLOADING", started_at=_now())
                            self._process(job)
                        except OSError:
                            if not self._storage_failure(job):
                                return
            except AcquisitionCapacityBusy:
                # 本轮零事务、零claim；回到有限正常等待，不重放下载或未知提交。
                self._stop.wait(0.5)
            except OSError:
                # SQL提交/续期结果未知时停止领取，不以另一无归属写入猜测FAILED。
                if self.state_repository is None:
                    raise
                self._worker_error = "正文准备存储暂不可核对，工人已暂停；已有记录保留"
                self._stop.set()
                return
            except GatePaused:
                # 锁忙同属 GatePaused；保持 QUEUED，有限等待也能及时响应 stop。
                self._stop.wait(0.5)
            except GateUnavailable:
                # 许可事实不可核验时停止领取任务；不借故障门补写未经许可的记录。
                return

    def _storage_failure(self, job):
        """只尝试保存一次明确失败状态；持续不能写时暂停，不能无限循环。"""
        with self._lock:
            message = "本地任务存储写入失败，本任务未继续处理；已有成功缓存保留"
            try:
                self._update(job, "FAILED", error=message)
                return True
            except OSError:
                self._worker_error = (
                    "本地任务存储持续不可写，正文准备已暂停；请恢复存储并重启服务，旧成功缓存保留"
                )
                # 故障状态只能留在内存：这里不承诺已经写入损坏或不可写的磁盘。
                job.update({"state": "FAILED", "error": self._worker_error,
                            "updated_at": _now(), "finished_at": _now()})
                self._stop.set()
                logger.error("正文准备已暂停：任务存储持续不可写，任务 %s", job["id"])
                return False

    def _process(self, job):
        def progress(state, details):
            with self._lock:
                if state in {"DOWNLOADING", "PROCESSING"}:
                    self._update(job, state, progress=details)

        try:
            result = self.documents.acquire(job["source_id"], refresh=job["refresh"], progress=progress)
            with self._lock:
                self._update(job, "ORIGINAL_ONLY" if result.get("body_status") == "ORIGINAL_ONLY" else "READY",
                             snapshot_id=result["snapshot_id"],
                             cache_hit=result.get("cache_hit", False), error=result.get("processing_error"),
                             review_status="PENDING_RECOGNITION" if result.get("body_status") in {"ORIGINAL_ONLY", "NATIVE_TEXT_WITH_IMAGE_GAPS"} else "PENDING_REVIEW")
        except Exception as error:  # noqa: BLE001 - 隔离任务失败，分类和日志仅使用固定枚举。
            from archspec.persistence.acquisition_state import AcquisitionLeaseLost
            if isinstance(error, AcquisitionLeaseLost):
                # 过期工人既不能报READY，也不能把新工人的任务改成FAILED。
                return
            # 任务失败不能杀死整个工人；不把凭据、请求头或堆栈传给网页。
            failure = _failure_for(error)
            if isinstance(error, httpx.HTTPError):
                message = download_failure_message(error)
            elif isinstance(error, (ValueError, LookupError)):
                message = str(error)[:500]
            elif isinstance(error, OSError):
                message = "正文缓存写入或读取失败，请检查本地存储；已有成功缓存保留"
            else:
                message = "正文处理失败，请查看服务日志并重试；已有成功缓存保留"
                logger.error("正文准备失败，任务 %s，类别 %s", job["id"], failure["code"])
            with self._lock:
                try:
                    self._update(job, "FAILED", error=message, failure=failure)
                except AcquisitionLeaseLost:
                    return
