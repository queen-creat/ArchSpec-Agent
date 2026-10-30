"""冻结同一组完整正文比较两版排序，成绩变化不能掩盖资料缺失。"""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from archspec.official_documents.search import algorithm_identifiers, search_snapshots
from archspec.official_documents.service import MAX_SEARCH_CHUNKS, MAX_SEARCH_SOURCES
from archspec.official_evaluations.revisions import (
    reviewed_page_manifest,
    validate_originals,
    validate_revision,
)
from archspec.standard_governance.compatibility import ContentUseDenied

TARGET_STANDARDS = [
    ("民用建筑通用规范", "GB 55031-2022"),
    ("建筑与市政工程无障碍通用规范", "GB 55019-2021"),
    ("办公建筑设计标准", "JGJ/T 67-2019"),
    ("建筑防火通用规范", "GB 55037-2022"),
    ("汽车库、修车库、停车场设计防火规范", "GB 50067-2014"),
    ("车库建筑设计规范", "JGJ 100-2015"),
]
_RUN_LOCK = Lock()


class EvaluationBusyError(RuntimeError):
    """同一进程已有检查运行，不能并行重复评分。"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _code(value):
    return re.sub(r"\s+", "", value).upper()


def _pages_overlap(left, right):
    return left["page_start"] <= right["page_end"] and right["page_start"] <= left["page_end"]


def matches_expected(expected, results):
    """同条款号但不同书、快照或原页，不能算作正确命中。"""
    for hit in results[:5]:
        for target in expected:
            # 合并条款可能不连续，不能把首尾页之间的空缺页伪装成命中。
            if (all(hit[field] == target[field]
                    for field in ("source_id", "snapshot_id", "clause_number"))
                    and any(_pages_overlap(part, target) for part in hit.get("fragments", [hit]))):
                return True
    return False


def passes_quality_gate(coverage, boundary_passed, holdout):
    """只判断固定题库定位门槛；不是回答正确率，也不是默认排序切换许可。"""
    return (len(coverage) == 6 and all(row["state"] == "READY" for row in coverage)
            and boundary_passed and holdout["optimized_hit_at_5"] >= 80
            and holdout["optimized_hit_at_5"] >= holdout["baseline_hit_at_5"])


def _retrieval_fingerprint(snapshot):
    # 复用提示、最后联网时间不是检索内容：重复查询同一正文不能误使成绩过期。
    content = {key: snapshot.get(key) for key in ("source_id", "snapshot_id", "title",
               "standard_code", "page_url", "processor_version", "page_count", "pages", "chunks")}
    return hashlib.sha256(_canonical(content).encode()).hexdigest()


class OfficialEvaluationService:
    def __init__(self, data_dir, documents, dataset_path=None, review_root=None):
        self.root = Path(data_dir) / "official-evaluations"
        self.documents = documents
        self.dataset_path = (Path(dataset_path) if dataset_path is not None
                             else Path(__file__).parent / "fixtures" / "official-cases-v1.json")
        self.index_dir = Path(data_dir) / "official-indexes"
        self.review_root = (Path(review_root) if review_root is not None
                            else Path(__file__).resolve().parents[3])

    def _dataset(self):
        try:
            content = self.dataset_path.read_bytes()
            dataset = json.loads(content)
        except (OSError, ValueError) as error:
            raise ValueError("固定官方题库尚未准备或无法读取，不能生成成绩") from error
        try:
            cases = dataset["cases"]
            if (not isinstance(dataset["version"], str) or not dataset["version"]
                    or dataset["review_status"] != "DEVELOPER_CHECKED"
                    or not isinstance(cases, list)):
                raise ValueError
            ids = [case["id"] for case in cases]
            if len(set(ids)) != len(ids) or not all(isinstance(value, str) and value for value in ids):
                raise ValueError
            positive = [case for case in cases if case["split"] in {"TUNING", "HOLDOUT"}]
            boundary = [case for case in cases if case["split"] == "BOUNDARY"]
            if (len(positive) != 20 or sum(case["split"] == "TUNING" for case in positive) != 10
                    or sum(case["split"] == "HOLDOUT" for case in positive) != 10
                    or not boundary or len(positive) + len(boundary) != len(cases)):
                raise ValueError
            for case in cases:
                if (not isinstance(case["query"], str) or not 1 <= len(case["query"].strip()) <= 500
                        or not isinstance(case["review_note"], str) or not case["review_note"].strip()):
                    raise ValueError
                if case["split"] == "BOUNDARY":
                    if case["expected_behavior"] not in {"NO_RESULTS", "EXACT_CLAUSE_ONLY", "MISSING_COVERAGE"}:
                        raise ValueError
                    if case["expected_behavior"] == "MISSING_COVERAGE":
                        missing = case["expected_coverage"]
                        if (not isinstance(missing["standard_code"], str) or not missing["standard_code"]
                                or missing["state"] not in {"NOT_DISCOVERED", "NOT_ACQUIRED"}
                                or ("source_id" in missing and not isinstance(missing["source_id"], str))):
                            raise ValueError
                        continue
                    if case["expected_behavior"] == "NO_RESULTS":
                        continue
                if not isinstance(case["expected"], list) or not case["expected"]:
                    raise ValueError
                for target in case["expected"]:
                    if (not isinstance(target["source_id"], str) or not target["source_id"]
                            or not re.fullmatch(r"[a-f0-9]{64}", target["snapshot_id"])
                            or not re.fullmatch(r"\d+(?:\.\d+){2,3}", target["clause_number"])
                            or type(target["page_start"]) is not int
                            or type(target["page_end"]) is not int
                            or not 1 <= target["page_start"] <= target["page_end"]):
                        raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("固定题库须有20道开发者核对正例、10/10分组及明确边界题，答案须完整") from error
        validate_revision(dataset, self.dataset_path)
        reviewed_page_manifest(dataset, self.review_root)
        return dataset, hashlib.sha256(content).hexdigest()

    def _check_evidence(self, snapshot, source_id, *, validated=False):
        """解析暂存不能替代即时原件和操作许可核对。"""
        self.documents._check_cached_file(snapshot, source_id, validated=validated)
        for operation in ("LOCAL_FULL_TEXT_READ", "LOCAL_SEARCH", "LOCAL_EXCERPT"):
            self.documents._check_read(source_id, snapshot["snapshot_id"], operation)

    def _freeze(self):
        """有限语料批读共用短SQL阶段；后续比较和最终复核不继承其状态。"""
        with self.documents.read_phase(sql=True):
            return self._freeze_in_phase()

    def _freeze_in_phase(self):
        """每本只取一次完整缓存，再复制冻结；两个排序版本共享这些对象。"""
        try:
            entries = self.documents.registry.list()
        except (ValueError, OSError, KeyError, TypeError):
            entries = []
            registry_error = True
        else:
            registry_error = False
        snapshots, failures, states = [], [], {}
        for entry in sorted(entries, key=lambda row: row["source_id"]):
            if entry.get("kind") != "FULL_TEXT":
                continue
            source_id = entry["source_id"]
            try:
                # 登记枚举已经核对来源；私有加载器仍核SQL指针、固定字节和当前预算。
                # 三种操作许可统一在实际原件核验后即时读取，避免get提前重复同一个读门。
                snapshot = self.documents._read_snapshot(source_id)
                if snapshot is None:
                    states[source_id] = ("NOT_ACQUIRED", "正文尚未准备，本次检查不触发下载或识别")
                    continue
                # 仅紧邻加载器的私有记录复用结构结论；原PDF字节、SHA和解码照常核验。
                self._check_evidence(snapshot, source_id, validated=True)
                snapshots.append(copy.deepcopy(snapshot))
                states[source_id] = ("READY", "完整缓存可读；文字、版本和使用范围仍待专业核验")
            except (ValueError, OSError, LookupError, KeyError, TypeError, ContentUseDenied):
                states[source_id] = ("CACHE_ERROR", "正文原件或使用许可无法安全核对，请检查本地资料")
                failures.append(source_id)
        coverage = []
        for title, code in TARGET_STANDARDS:
            matching = [entry for entry in entries if entry.get("kind") == "FULL_TEXT"
                        and _code(entry.get("standard_code", "")) == _code(code)]
            if registry_error:
                state, reason = "CACHE_ERROR", "官方来源登记无法读取，请检查本地资料"
                failures.append(code)
            elif not matching:
                state, reason = "NOT_DISCOVERED", "还没有登记可确认的完整正文附件，不代表没有规范要求"
            else:
                matching.sort(key=lambda entry: (states[entry["source_id"]][0] != "READY",
                                                 entry["source_id"]))
                state, reason = states[matching[0]["source_id"]]
            coverage.append({"title": title, "standard_code": code, "state": state,
                             "reason": reason, "source_ids": [entry["source_id"] for entry in matching]})
        budget_error = len(snapshots) > MAX_SEARCH_SOURCES or sum(
            len(snapshot["chunks"]) for snapshot in snapshots
        ) > MAX_SEARCH_CHUNKS
        identities = [{"source_id": snapshot["source_id"], "snapshot_id": snapshot["snapshot_id"],
                       "standard_code": snapshot["standard_code"], "page_count": snapshot["page_count"],
                       "original_sha256": snapshot["sha256"], "size_bytes": snapshot["size_bytes"],
                       "cache_sha256": _retrieval_fingerprint(snapshot)}
                      for snapshot in snapshots]
        source_context = {"entries": entries, "states": states, "registry_error": registry_error,
                          "inventory": [{"source_id": entry["source_id"],
                                         "standard_code": entry.get("standard_code"),
                                         "state": states[entry["source_id"]][0]}
                                        for entry in sorted(entries, key=lambda row: row["source_id"])
                                        if entry.get("kind") == "FULL_TEXT"]}
        return snapshots, identities, coverage, failures, budget_error, source_context

    def _validate_answers(self, dataset, snapshots):
        validate_originals(dataset, snapshots, self.documents)
        available = {(snapshot["source_id"], snapshot["snapshot_id"]): snapshot
                     for snapshot in snapshots}
        covered_codes = set()
        for case in dataset["cases"]:
            if case["split"] == "BOUNDARY" and case["expected_behavior"] != "EXACT_CLAUSE_ONLY":
                continue
            for expected in case["expected"]:
                snapshot = available.get((expected["source_id"], expected["snapshot_id"]))
                if snapshot is None:
                    raise ValueError("固定题库的目标正文或快照尚不可读，不能生成成绩")
                if expected["page_end"] > snapshot["page_count"] or not any(
                    chunk["clause_number"] == expected["clause_number"]
                    and _pages_overlap(chunk, expected) for chunk in snapshot["chunks"]
                ):
                    raise ValueError("固定题库的答案条款与原页不对应，不能生成成绩")
                if case["split"] != "BOUNDARY":
                    covered_codes.add(_code(snapshot["standard_code"]))
                elif expected["clause_number"] != case["query"].strip():
                    raise ValueError("条款号边界的预期答案必须与问题编号一致")
        if {_code(code) for _, code in TARGET_STANDARDS} - covered_codes:
            raise ValueError("固定正例答案必须真实覆盖六种目标规范，不能只检查缓存是否齐全")

    @staticmethod
    def _missing_coverage(case, source_context):
        expected = case["expected_coverage"]
        entries = [entry for entry in source_context["entries"] if entry.get("kind") == "FULL_TEXT"
                   and _code(entry.get("standard_code", "")) == _code(expected["standard_code"])
                   and (not expected.get("source_id") or entry["source_id"] == expected["source_id"])]
        if source_context["registry_error"]:
            state = "CACHE_ERROR"
        elif not entries:
            state = "NOT_DISCOVERED"
        else:
            actual = [source_context["states"][entry["source_id"]][0] for entry in entries]
            state = "READY" if "READY" in actual else "CACHE_ERROR" if "CACHE_ERROR" in actual else "NOT_ACQUIRED"
        return {"standard_code": expected["standard_code"], "source_id": expected.get("source_id"),
                "state": state, "reason": "正文未就绪不代表没有规范要求；本检查不发起下载或识别"}

    @staticmethod
    def _boundary(case, output, version, identities):
        if case["expected_behavior"] == "NO_RESULTS":
            return not output["results"]
        if (output["method"] != "EXACT_CLAUSE"
                or not re.fullmatch(r"\d+(?:\.\d+){2,3}", case["query"].strip())):
            return False
        keys = [(hit["source_id"], hit["snapshot_id"], hit["clause_number"])
                for hit in output["results"]]
        available = {(identity["source_id"], identity["snapshot_id"]) for identity in identities}
        return (all(key[2] == case["query"].strip() and key[:2] in available for key in keys)
                and (version == "V1" or len(keys) == len(set(keys)))
                and all(matches_expected([target], output["results"]) for target in case["expected"]))

    def _save(self, result):
        directory = self.root / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{result['id']}.json"
        from archspec.persistence.business_files import write_runtime_file
        if write_runtime_file(destination, _canonical(result).encode("utf-8")):
            return
        temporary = destination.with_suffix(f".{uuid4()}.tmp")
        try:
            temporary.write_text(_canonical(result), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def run_compare(self):
        if not _RUN_LOCK.acquire(blocking=False):
            raise EvaluationBusyError("已有搜索检查正在运行，请稍后查看结果")
        try:
            with self.documents.read_phase():
                result = self._compare_frozen()
            # 两版共享开始时的冻结语料；结束时复查身份，避免把中途改变的资料算作当前成绩。
            result = self._assess(result)
            self._save(result)
            return result
        finally:
            _RUN_LOCK.release()

    def _compare_frozen(self):
        dataset, digest = self._dataset()
        reviewed_pages = reviewed_page_manifest(dataset, self.review_root)
        snapshots, identities, coverage, failures, budget_error, source_context = self._freeze()
        if failures:
            raise ValueError("正文缓存存在读取错误，不能对不完整语料生成成绩")
        if budget_error:
            raise ValueError("完整正文超过统一检索预算上限，不能只评测部分书")
        self._validate_answers(dataset, snapshots)
        algorithms = algorithm_identifiers()
        case_results = []
        for case in dataset["cases"]:
            if case.get("expected_behavior") == "MISSING_COVERAGE":
                missing = self._missing_coverage(case, source_context)
                passed = missing["state"] == case["expected_coverage"]["state"]
                case_results.append({**copy.deepcopy(case), "coverage": missing,
                                     "boundary_passed": {"V1": passed, "V2": passed}})
                continue
            output = {version: search_snapshots(snapshots, case["query"].strip(), 5,
                                                ranking_version=version, index_dir=self.index_dir)
                      for version in ("V1", "V2")}
            if case["split"] == "BOUNDARY":
                checks = {version: self._boundary(case, output[version], version, identities)
                          for version in ("V1", "V2")}
                case_results.append({**copy.deepcopy(case), "outputs": output,
                                     "boundary_passed": checks})
            else:
                hits = {version: matches_expected(case["expected"], output[version]["results"])
                        for version in ("V1", "V2")}
                case_results.append({**copy.deepcopy(case), "outputs": output, "hit_at_5": hits})
        metrics = {}
        for split in ("TUNING", "HOLDOUT"):
            items = [case for case in case_results if case["split"] == split]
            metrics[split] = {
                "count": len(items),
                "baseline_hit_at_5": round(sum(item["hit_at_5"]["V1"] for item in items) * 100 / len(items), 2),
                "optimized_hit_at_5": round(sum(item["hit_at_5"]["V2"] for item in items) * 100 / len(items), 2),
            }
        boundary_passed = all(all(case["boundary_passed"].values()) for case in case_results
                              if case["split"] == "BOUNDARY")
        holdout = metrics["HOLDOUT"]
        gate = passes_quality_gate(coverage, boundary_passed, holdout)
        return {"id": str(uuid4()), "status": "CURRENT", "stale_reasons": [],
                "created_at": datetime.now(UTC).isoformat(), "dataset_sha256": digest,
                "dataset_version": dataset["version"], "review_status": "DEVELOPER_CHECKED",
                "reviewed_pages": reviewed_pages,
                "algorithm_identifiers": algorithms, "corpus": identities, "coverage": coverage,
                "source_inventory": source_context["inventory"],
                "tuning": metrics["TUNING"], "holdout": holdout,
                "tuning_count": metrics["TUNING"]["count"], "holdout_count": holdout["count"],
                "baseline_hit_at_5": holdout["baseline_hit_at_5"],
                "optimized_hit_at_5": holdout["optimized_hit_at_5"],
                "boundary_passed": boundary_passed, "quality_gate_passed": gate,
                "case_results": case_results,
                "notice": "前五命中比例仅衡量固定题库检索，不是规范正确率；开发者核对不替代专业审核。"}

    def _check_saved_corpus(self, result):
        """只按成绩固定身份复核，不能用健康的latest释放旧原文。"""
        corpus = result["corpus"]
        if not corpus or len(corpus) > MAX_SEARCH_SOURCES:
            raise ValueError("历史正文身份缺失或超限")
        seen, chunks, verified = set(), 0, {}
        for identity in corpus:
            source_id, snapshot_id = identity["source_id"], identity["snapshot_id"]
            if (not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id)
                    or not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_id)
                    or (source_id, snapshot_id) in seen):
                raise ValueError("历史正文身份无效")
            if self.documents.registry.get(source_id).get("kind") != "FULL_TEXT":
                raise ValueError("历史正文来源已不可核对")
            path = self.documents.cache_dir / source_id / f"{snapshot_id}.json"
            snapshot, _ = self.documents._load_snapshot(path, source_id)
            if (snapshot["snapshot_id"] != snapshot_id
                    or snapshot["sha256"] != identity["original_sha256"]
                    or snapshot["size_bytes"] != identity["size_bytes"]
                    or _retrieval_fingerprint(snapshot) != identity["cache_sha256"]
                    or snapshot["standard_code"] != identity["standard_code"]
                    or snapshot["page_count"] != identity["page_count"]):
                raise ValueError("历史正文固定内容已改变")
            self._check_evidence(snapshot, source_id)
            chunks += len(snapshot["chunks"])
            if chunks > MAX_SEARCH_CHUNKS:
                raise ValueError("历史正文超过统一检索预算")
            seen.add((source_id, snapshot_id))
            verified[(source_id, snapshot_id)] = snapshot
        self._check_saved_outputs(result, verified)

    @staticmethod
    def _fixed_output_part(part, verified):
        """结果ID仅用于解引用固定片段，不能代替正文与页码核对。"""
        if not isinstance(part, dict):
            raise TypeError("历史输出片段格式无效")
        key = (part["source_id"], part["snapshot_id"])
        if (not all(isinstance(value, str) for value in key) or key not in verified):
            raise ValueError("历史输出来源不在已核对的完整语料内")
        snapshot = verified[key]
        prefix = f"{key[0]}:{key[1]}:"
        identifier = part["id"]
        if not isinstance(identifier, str) or not identifier.startswith(prefix):
            raise ValueError("历史输出片段ID与固定身份不对应")
        suffix = identifier[len(prefix):]
        if not re.fullmatch(r"0|[1-9][0-9]*", suffix):
            raise ValueError("历史输出片段索引无效")
        index = int(suffix)
        if index >= len(snapshot["chunks"]):
            raise ValueError("历史输出片段索引超出固定正文")
        return key, snapshot, snapshot["chunks"][index]

    @staticmethod
    def _check_output_fields(part, original):
        if (not isinstance(part["text"], str)
                or type(part["page_start"]) is not int or type(part["page_end"]) is not int
                or any(part[field] != original[field]
                       for field in ("text", "clause_number", "page_start", "page_end"))):
            raise ValueError("历史输出原文、条号或页码与固定正文不一致")

    def _check_saved_outputs(self, result, verified):
        """复核已有结果，不重新搜索、评分或改变历史排序。"""
        for case in result["case_results"]:
            if not isinstance(case, dict):
                raise TypeError("历史逐题输出格式无效")
            if case.get("expected_behavior") == "MISSING_COVERAGE" and "outputs" not in case:
                continue
            outputs = case["outputs"]
            if not isinstance(outputs, dict) or set(outputs) != {"V1", "V2"}:
                raise ValueError("历史逐题输出缺少完整排序版本")
            for version, output in outputs.items():
                if not isinstance(output, dict) or not isinstance(output["results"], list):
                    raise TypeError("历史搜索输出格式无效")
                for hit in output["results"]:
                    key, snapshot, anchor = self._fixed_output_part(hit, verified)
                    if (hit["title"] != snapshot["title"]
                            or hit["standard_code"] != snapshot["standard_code"]
                            or hit["source_url"] != snapshot["page_url"]):
                        raise ValueError("历史输出书名、编号或来源与固定正文不一致")
                    if "fragments" not in hit:
                        # 无续片清单的旧结果只按单个原片段核对，不重算旧排序。
                        self._check_output_fields(hit, anchor)
                        continue
                    parts = hit["fragments"]
                    clause = anchor["clause_number"]
                    if (version != "V2" or not isinstance(parts, list) or not parts
                            or not isinstance(clause, str)
                            or not re.fullmatch(r"\d+(?:\.\d+){2,3}", clause)):
                        raise ValueError("历史合并条款缺少可核对的续片清单")
                    for part in parts:
                        part_key, _, original = self._fixed_output_part(part, verified)
                        if part_key != key or original["clause_number"] != clause:
                            raise ValueError("历史续片不是同书同快照同条款")
                        self._check_output_fields(part, original)
                    prefix = f"{key[0]}:{key[1]}:"
                    expected_ids = [f"{prefix}{index}" for index, chunk in enumerate(snapshot["chunks"])
                                    if chunk["clause_number"] == clause]
                    if [part["id"] for part in parts] != expected_ids:
                        raise ValueError("历史续片清单与固定条款的完整原序不一致")
                    merged = {"text": "\n\n".join(dict.fromkeys(part["text"] for part in parts)),
                              "clause_number": clause,
                              "page_start": min(part["page_start"] for part in parts),
                              "page_end": max(part["page_end"] for part in parts)}
                    self._check_output_fields(hit, merged)

    def _assess(self, result, frozen=None):
        # 结束复核与初读不共享解析阶段，权限和实际文件始终重新核对。
        with self.documents.read_phase():
            return self._assess_in_phase(result, frozen)

    def _assess_in_phase(self, result, frozen=None):
        reasons = []
        dataset = None
        try:
            dataset, digest = self._dataset()
            if digest != result["dataset_sha256"]:
                reasons.append("固定题库内容已改变")
            reviewed = reviewed_page_manifest(dataset, self.review_root)
            if (dataset.get("binding_revision") is not None
                    and "reviewed_pages" not in result):
                reasons.append("历史成绩缺少实际核对原页清单")
            elif reviewed != result.get("reviewed_pages", {}):
                reasons.append("核对原页清单已改变")
        except ValueError:
            reasons.append("固定题库或核对原页无法读取或已失效")
        if algorithm_identifiers() != result["algorithm_identifiers"]:
            reasons.append("排序实现已改变")
        snapshots, identities, coverage, failures, budget_error, source_context = frozen or self._freeze()
        if failures or budget_error:
            reasons.append("当前正文无法完整读取或超过检索预算")
        if identities != result["corpus"]:
            reasons.append("正文来源、快照或缓存内容已改变")
        if source_context["inventory"] != result.get("source_inventory"):
            reasons.append("完整正文的登记或准备状态已改变")
        if dataset is not None and not failures and not budget_error:
            try:
                self._validate_answers(dataset, snapshots)
            except (ValueError, OSError, LookupError, KeyError, TypeError, ContentUseDenied):
                reasons.append("题库答案或新旧原件无法再次核对")
        updated = copy.deepcopy(result)
        try:
            self._check_saved_corpus(result)
        except (ValueError, OSError, LookupError, KeyError, TypeError, ContentUseDenied):
            reasons.append("历史固定正文、输出绑定或使用许可无法核对，已隐藏逐题原文")
            updated["case_results"] = []
            updated["notice"] = (result.get("notice", "")
                                 + " 历史固定正文、输出绑定或使用许可无法核对，逐题原文已隐藏；历史指标保留，旧文件未改写。")
        updated.update({"status": "STALE" if reasons else "CURRENT", "stale_reasons": reasons})
        # 历史记录只能提供历史成绩，不能带入旧的排序切换指令。
        updated.update({"evaluation_scope": "TARGET_LOCATING_ONLY",
                        "default_switch_allowed": False, "current_ranking_version": "V1"})
        if reasons or not all(row["state"] == "READY" for row in coverage):
            updated["quality_gate_passed"] = False
        return updated

    def get(self, run_id):
        try:
            if str(UUID(run_id)) != run_id:
                raise ValueError
            result = self._read_run(self.root / "runs" / f"{run_id}.json")
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
            raise LookupError("搜索检查记录不存在或无法读取") from error
        return self._assess(result)

    @staticmethod
    def _read_run(path):
        """坏历史记录不显示假成绩，也不能拖垮另外一条完整历史。"""
        from archspec.persistence.business_files import assert_runtime_file
        assert_runtime_file(path)
        result = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(result, dict) or str(UUID(result["id"])) != path.stem
                or not isinstance(result["created_at"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", result["dataset_sha256"])
                or type(result["quality_gate_passed"]) is not bool
                or result["holdout_count"] != 10 or result["tuning_count"] != 10
                or not isinstance(result["algorithm_identifiers"], dict)
                or not all(isinstance(result["algorithm_identifiers"][version], str)
                           for version in ("V1", "V2"))
                or not all(isinstance(result[key], list)
                           for key in ("corpus", "source_inventory", "case_results"))
                or not all(type(result[key]) in {int, float} and 0 <= result[key] <= 100
                           for key in ("baseline_hit_at_5", "optimized_hit_at_5"))):
            raise ValueError("搜索检查记录字段不完整")
        datetime.fromisoformat(result["created_at"])
        return result

    def _latest(self):
        results = []
        for path in (self.root / "runs").glob("*.json"):
            try:
                results.append(self._read_run(path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        return max(results, key=lambda result: result["created_at"], default=None)

    def status(self):
        with self.documents.read_phase():
            return self._status_in_phase()

    def _status_in_phase(self):
        reason = ""
        dataset = None
        try:
            dataset, _ = self._dataset()
        except ValueError as error:
            reason = str(error)
        saved = self._latest()
        # 历史和题库读取期间正文、登记或权限可能改变；唯一语料核验必须放在其后。
        # 同一有限只读事务同时服务覆盖状态与成绩复核，不能返回一份较早的READY覆盖。
        with self.documents.read_phase(sql=True):
            frozen = self._freeze_in_phase()
            snapshots, _, coverage, failures, budget_error, _ = frozen
            if dataset is not None:
                try:
                    self._validate_answers(dataset, snapshots)
                    if failures or budget_error:
                        raise ValueError("正文缓存无法完整读取或超过检索预算，请检查资料")
                except ValueError as error:
                    reason = str(error)
            # 此frozen是在历史读取之后的新核验；历史固定原文仍由_assess独立按旧身份核对。
            latest = self._assess(saved, frozen=frozen) if saved else None
            summary = ({key: latest[key] for key in ("id", "status", "created_at", "holdout_count",
                        "baseline_hit_at_5", "optimized_hit_at_5", "stale_reasons")}
                       if latest else None)
            if summary:
                summary["dataset_version"] = latest.get("dataset_version")
            return {"status": "UNAVAILABLE" if reason else "AVAILABLE", "coverage": coverage,
                    "evaluation_available": not bool(reason), "dataset_reason": reason,
                    "dataset_version": dataset["version"] if dataset else None,
                    "reviewed_binding_count": len(dataset.get("binding_revision", {}).get("reviews", []))
                    if dataset else 0,
                    "evaluation_scope": "TARGET_LOCATING_ONLY",
                    "default_switch_allowed": False, "current_ranking_version": "V1",
                    "latest_run": summary,
                    "quality_gate_passed": bool(latest and latest["quality_gate_passed"] and not reason),
                    "notice": "只读官方完整缓存，不下载、识别或调用模型；定位达标也不表示回答正确，当前保留原排序。"}
