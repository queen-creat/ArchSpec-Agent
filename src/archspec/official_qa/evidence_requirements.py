"""证据需求和完整同条身份；保留原总预算后的保守计数合同。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from archspec.official_documents.artifact_references import artifact_mentions
from archspec.official_documents.clause_links import references
from archspec.official_documents.clause_units import ARTICLE, CLAUSE
from archspec.official_documents.evidence_relations import TABLE_NUMBER
from archspec.official_documents.table_context import TABLE, captions, compact
from archspec.official_qa.local_spacing import evidence_key, unit_key
from archspec.official_qa.question_plan import QuestionPlan, SnapshotRecord, freeze_value
from archspec.official_qa.relation_proofs import context_ledger, verify_relation_proof


@dataclass(frozen=True)
class EvidenceRequirement(SnapshotRecord):
    requirement_id: str
    kind: Literal["CORE_BODY", "TABLE", "EXPLICIT_REFERENCE", "CONDITION_EXCEPTION", "NAMED_RULE"] = "CORE_BODY"
    necessity: Literal["ANSWER_REQUIRED", "READ_CONTEXT"] = "ANSWER_REQUIRED"
    trigger: Literal["BEFORE_SEARCH", "AFTER_EVIDENCE"] = "AFTER_EVIDENCE"
    subject: str = ""
    reason: str = ""
    conditions_required: tuple[str, ...] = ()
    fixed_targets: tuple = ()
    completeness: str = "FULL_UNIT"
    state: str = "UNRESOLVED"
    evidence_ids: tuple[str, ...] = ()
    source_state: str | None = None

    def __post_init__(self):
        if not self.requirement_id or self.kind not in {
                "CORE_BODY", "TABLE", "EXPLICIT_REFERENCE", "CONDITION_EXCEPTION", "NAMED_RULE"}:
            raise ValueError("证据需求身份或种类未登记")
        if self.source_state is not None and (self.source_state not in {
                "SEARCHED", "MISSING_SOURCE", "NOT_ACQUIRED", "EXCLUDED_FIXED_SCOPE",
                "RESTRICTED", "UNVERIFIED"} or self.kind != "EXPLICIT_REFERENCE"
                or self.trigger != "BEFORE_SEARCH"):
            raise ValueError("来源需求只使用共同的准备状态，不冒称固定正文已读完整")
        for name in ("conditions_required", "fixed_targets", "evidence_ids"):
            object.__setattr__(self, name, freeze_value(getattr(self, name)))


def requirements_for(plan: QuestionPlan, *, requirements=None) -> tuple[EvidenceRequirement, ...]:
    """注册表可传受信需求；此工厂不重新识别原问，不循环导入政策。"""
    rows = (tuple(requirements) if requirements is not None else
            (EvidenceRequirement("core_body", subject=plan.topic, reason="取得对应问题的完整固定正文"),
             *explicit_clause_requirements(plan)))
    if (not all(isinstance(row, EvidenceRequirement) for row in rows)
            or len({row.requirement_id for row in rows}) != len(rows)):
        raise ValueError("证据需求类型或身份重复")
    return rows


def _fixed_named_context(context):
    """命名单元必须绑定当前固定档案，地域名称不提供身份许可。"""
    coverage = context.get("selected_clause_coverage", {})
    metadata = context.get("source_metadata")
    return bool(re.fullmatch(r"named:\d{1,6}", str(unit_key(coverage) or ""))
                and coverage.get("reading_unit_label") and isinstance(metadata, dict)
                and all(context.get(key) and metadata.get(key) == context[key] for key in
                        ("source_id", "snapshot_id", "source_url", "snapshot_fingerprint", "title", "standard_code")))


def complete_clause_keys(contexts, evidence):
    """原service合同纯迁移：同书同快照计数，冲突count和伪named均不算完整。"""
    expected, blocked = {}, set()
    for context in contexts:
        coverage = context.get("selected_clause_coverage", {})
        number, count = coverage.get("clause_number"), coverage.get("expected_fragment_count")
        named = _fixed_named_context(context)
        if (not CLAUSE.fullmatch(number or "") and not named) or type(count) is not int or count < 1:
            continue
        key = context["source_id"], context["snapshot_id"], unit_key(coverage)
        expected.setdefault(key, set()).add(count)
        # 读齐片段不能补证条号归属；同一单元任何一次未核边界都不能借计数转完整。
        if coverage.get("boundary_issues"):
            blocked.add(key)
    admitted = {}
    for row in evidence:
        key = evidence_key(row)
        if row["role"] != "RELATED_CONTEXT" and key in expected:
            admitted.setdefault(key, set()).add(row["chunk_id"])
    return {key for key, counts in expected.items()
            if key not in blocked and len(counts) == 1
            and len(admitted.get(key, set())) == next(iter(counts))}



def _bound_fragments(context):
    """只消费当前context实际片段，别书/别快照chunk编号不能借相同条号入包。"""
    prefix = f"{context['source_id']}:{context['snapshot_id']}:"
    fragments = context.get("fragments", ())
    if any(not isinstance(row.get("id"), str) or not row["id"].startswith(prefix)
           or not row["id"][len(prefix):].isdigit() for row in fragments):
        return {}
    rows = {row["id"]: row for row in fragments}
    return rows if len(rows) == len(fragments) else {}


def _core_target(context, fragments):
    coverage = context.get("selected_clause_coverage", {})
    key, count = unit_key(coverage), coverage.get("expected_fragment_count")
    named = _fixed_named_context(context)
    if ((not CLAUSE.fullmatch(coverage.get("clause_number") or "") and not named)
            or type(count) is not int or count < 1):
        return None
    core = [row for row in fragments.values()
            if row.get("role") != "RELATED_CONTEXT" and unit_key(row) == key]
    if not core or context.get("selected_chunk_id") not in {row["id"] for row in core}:
        return None
    chunk_ids = tuple(sorted({row["id"] for row in core}, key=lambda value: int(value.rsplit(":", 1)[1])))
    return {"source_id": context["source_id"], "snapshot_id": context["snapshot_id"],
            "unit_key": key, "expected_fragment_count": count, "chunk_ids": chunk_ids,
            "read_complete": (coverage.get("complete") is True and len(chunk_ids) == count
                              and coverage.get("included_fragment_count") == len(chunk_ids)),
            "read_state": "PARTIAL"}


def _reference_target(context, fragments, core, ref, ledger):
    number, direction = ref.get("clause_number"), ref.get("direction")
    count, ids = ref.get("expected_fragment_count"), ref.get("chunk_ids", ())
    if (not CLAUSE.fullmatch(number or "") or direction not in {"OUTGOING", "INCOMING"}
            or type(count) is not int or count < 0 or len(set(ids)) != len(ids)
            or any(key not in fragments or fragments[key].get("clause_number") != number for key in ids)):
        return None
    proof = None
    if "proof" in ref:
        proof = verify_relation_proof(ref["proof"], (context,), ledger)
        if (proof is None or proof["kind"] != "CLAUSE_REFERENCE"
                or proof["direction"] != direction or proof["related_unit_key"] != number
                or proof["anchor_unit_key"] != core["unit_key"]):
            return None
    else:
        # 仅旧无proof合成合同保留兼容；已有proof不允许降级为重复扫描。
        referring = [fragments[key] for key in (core["chunk_ids"] if direction == "OUTGOING" else ids)]
        referred = number if direction == "OUTGOING" else core["unit_key"]
        if not any(referred in references(row["text"]) for row in referring):
            return None
    status = ref.get("status")
    if status not in {"LOCATED", "NOT_LOCATED", "BUDGET_OMITTED"}:
        return None
    if (status == "NOT_LOCATED" and (ids or count != 0)) or (status == "LOCATED" and (not ids or len(ids) != count)):
        return None
    return {"source_id": context["source_id"], "snapshot_id": context["snapshot_id"],
            "unit_key": number, "direction": direction, "anchor_unit_key": core["unit_key"],
            "expected_fragment_count": count, "chunk_ids": tuple(ids),
            "read_complete": status == "LOCATED" and (proof is None or proof["status"] == "LOCATED"),
            "read_state": "NOT_LOCATED" if status == "NOT_LOCATED" else "BUDGET_TRUNCATED"}


def complete_outgoing_dependency(context):
    """同一实际上下文的完整引用闭包；复用需求证明，不接受旧无proof降级。"""
    fragments = _bound_fragments(context)
    core = _core_target(context, fragments)
    if core is None or not core["read_complete"]:
        return None
    references = [ref for ref in context.get("clause_references", ())
                  if ref.get("direction") == "OUTGOING"]
    if not references:
        return None
    ledger, targets = context_ledger(context), []
    for ref in references:
        if "proof" not in ref:
            return None
        target = _reference_target(context, fragments, core, ref, ledger)
        if target is None or not target["read_complete"]:
            return None
        targets.append(target)
    return core, tuple(targets)


def _table_target(context, fragments, core, ref, ledger):
    number, status = ref.get("table_number"), ref.get("status")
    proof = None
    if "proof" in ref:
        proof = verify_relation_proof(ref["proof"], (context,), ledger)
        if (proof is None or proof["kind"] != "TABLE_REFERENCE"
                or proof["target_unit_key"] != f"table:{number}"
                or proof["anchor_unit_key"] != core["unit_key"]):
            return None
    else:
        # 旧无proof合成合同仍按原页字面验证，生产proof路径不重新发现表号。
        mentioned = {compact(match[1]) for key in core["chunk_ids"]
                     for match in TABLE.finditer(fragments[key]["text"])}
        if number not in mentioned:
            return None
    if status not in {"LOCATED", "NOT_LOCATED", "BUDGET_OMITTED"}:
        return None
    pages, ids, complete = ref.get("pages", ()), [], status == "LOCATED"
    if status == "NOT_LOCATED" and pages:
        return None
    for page in pages:
        owner = fragments.get(page.get("chunk_id"))
        if (not owner or type(page.get("pdf_page")) is not int
                or not owner["page_start"] <= page["pdf_page"] <= owner["page_end"]
                or (proof is None and not any(caption["table_number"] == number
                           and caption["caption"] == page.get("caption")
                           and caption["continuation"] == page.get("continuation")
                           for caption in captions(owner["text"])))):
            return None
        ids.append(owner["id"])
        for part in page.get("reading_parts", ()):
            row = fragments.get(part.get("chunk_id"))
            if (not row or not isinstance(part.get("text"), str) or part["text"] not in row["text"]
                    or not row["page_start"] <= page["pdf_page"] <= row["page_end"]):
                return None
            ids.append(row["id"])
        complete = (complete and page.get("reading_parts_status") == "LOCATED"
                    and page.get("original_review") != "ORIGINAL_REQUIRED")
    original_required = any(page.get("original_review") == "ORIGINAL_REQUIRED" for page in pages)
    ids = tuple(dict.fromkeys(ids))
    if proof is not None and ids != tuple(proof["admitted_chunk_ids"]):
        return None
    complete = complete and (proof is None or proof["status"] == "LOCATED")
    count = len(ids) if proof is None else proof["expected_fragment_count"]
    return {"source_id": context["source_id"], "snapshot_id": context["snapshot_id"],
            "table_number": number, "chunk_ids": ids, "expected_fragment_count": count,
            "read_complete": bool(pages) and complete,
            "read_state": ("NOT_LOCATED" if status == "NOT_LOCATED" else
                           "BUDGET_TRUNCATED" if status == "BUDGET_OMITTED" else
                           "ORIGINAL_REQUIRED" if original_required else "PARTIAL")}


def requirements_from_contexts(plan, contexts, *, required_evidence_ids=(), core_keys=None,
                               source_coverage=(), sources=None, clause_selection=None):
    """从实际已读定位建立同包需求；ROOT限定相关CORE身份，关系不升级为事实。"""
    contexts = tuple(contexts)
    cores, relations, named = {}, {}, {}
    for context in contexts:
        if plan.effective_source_ids is not None and context["source_id"] not in plan.effective_source_ids:
            raise ValueError("需求上下文越过本次有效来源范围")
        fragments = _bound_fragments(context)
        ledger = context_ledger(context)
        core = _core_target(context, fragments)
        if core is None:
            continue
        key = context["source_id"], context["snapshot_id"], core["unit_key"]
        if key in cores:
            ids = tuple(dict.fromkeys((*cores[key]["chunk_ids"], *core["chunk_ids"])))
            count = core["expected_fragment_count"]
            if cores[key]["expected_fragment_count"] != count:
                # 同身份的读取计数冲突不挑一份有利记录充当完整证明。
                count = None
            core = {**core, "chunk_ids": ids, "expected_fragment_count": count,
                    "read_complete": count is not None and len(ids) == count}
        cores[key] = core
        # 政府条文常用单整数表号及附图。现有表读器没有这些完整行列/图示证明，
        # 原字可以摘录，但不能因正文完整就消除实际提到的图表缺口。
        if ((core_keys is None or key in core_keys) and
                ARTICLE.fullmatch(context["selected_clause_coverage"].get("clause_number") or "")):
            text = "\n".join(fragments[chunk]["text"] for chunk in core["chunk_ids"])
            for match in artifact_mentions(text):
                label, number = match[1], match[2]
                if label == "表" and (TABLE_NUMBER.fullmatch(number)
                        or text[:match.start()].rstrip().endswith("附")
                        and TABLE_NUMBER.fullmatch("附表" + number)):
                    continue  # 已支持的完整表号继续走共同原表证明。
                subject = label + number
                identity = "artifact:" + ":".join(key) + ":" + subject
                relations[identity] = EvidenceRequirement(identity,
                    kind="TABLE" if label == "表" else "EXPLICIT_REFERENCE", necessity="READ_CONTEXT",
                    subject=subject, reason=f"正文实际引用{subject}，尚无完整图表核读记录，请对照固定原件。",
                    completeness="FIXED_ARTIFACT_REFERENCE",
                    fixed_targets=({**core, "read_complete": False, "read_state": "ORIGINAL_REQUIRED"},))
        if str(core["unit_key"]).startswith("named:"):
            identity = "named:" + ":".join(key)
            named[identity] = EvidenceRequirement(identity, kind="NAMED_RULE",
                subject=context["selected_clause_coverage"].get("reading_unit_label", ""),
                reason="实际固定读取的命名单元须完整纳入", fixed_targets=(core,))
        for ref in context.get("table_references", ()):
            target = _table_target(context, fragments, core, ref, ledger)
            if target is not None:
                identity = f"table:{key[0]}:{key[1]}:{target['table_number']}"
                # 相同表多次出现在真实初读中时保留全部证明，预算覆盖逐目标核对。
                previous = relations.get(identity)
                targets = (*previous.fixed_targets, target) if previous else (target,)
                relations[identity] = EvidenceRequirement(identity, kind="TABLE", necessity="READ_CONTEXT",
                    subject=target["table_number"], reason="所选固定正文实际引用原表", fixed_targets=targets)
        for ref in context.get("clause_references", ()):
            target = _reference_target(context, fragments, core, ref, ledger)
            if target is not None:
                identity = f"reference:{key[0]}:{key[1]}:{key[2]}:{target['direction']}:{target['unit_key']}"
                relations[identity] = EvidenceRequirement(identity, kind="EXPLICIT_REFERENCE", necessity="READ_CONTEXT",
                    subject=target["unit_key"], reason="实际明确同书引用，仅供完整原文定位", fixed_targets=(target,))
    selected = set(cores) if core_keys is None else set(core_keys)
    if not selected <= cores.keys():
        raise ValueError("相关CORE身份不属于本次实际所选上下文")
    required = tuple(sorted(set(required_evidence_ids)))
    if any(not isinstance(value, str) or not value for value in required):
        raise ValueError("必选例外编号无效")
    # 明确标记新定位模式；旧requirements_for的无target CORE保留原PARTIAL兼容合同。
    rows = [EvidenceRequirement("core_body", subject=plan.topic,
        reason="取得对应问题的实际相关完整固定正文", completeness="FIXED_CORE_UNITS",
        fixed_targets=tuple(cores[key] for key in cores if key in selected))]
    rows.extend(relations.values())
    rows.extend(named.values())
    if required:
        rows.append(EvidenceRequirement("condition_exception", kind="CONDITION_EXCEPTION",
            reason="政策已确认条件对应的实际完整必选例外",
            fixed_targets=({"required_evidence_ids": required},), evidence_ids=required))
    rows.extend(explicit_source_requirements(plan, source_coverage, sources=sources,
                                             contexts=contexts, core_keys=selected))
    rows.extend(explicit_clause_requirements(plan, contexts=contexts, core_keys=selected,
                                              clause_selection=clause_selection))
    return requirements_for(plan, requirements=rows)


def explicit_source_requirements(plan, coverage, *, sources=None, contexts=None, core_keys=None):
    """查阅和回答共享逐目标准备状态；固定targets只取本轮实际读取的同书正文。"""
    coverage = tuple(row for row in coverage if isinstance(row, Mapping))
    if not coverage and sources is None and contexts is not None:
        # 旧内部调用可只提供实际固定读取；不能把已证明的同书正文当作未登记。
        coverage = tuple({**row, "state": "SEARCHED"} for row in contexts)
    known = tuple(row for row in (coverage if sources is None else sources)
                  if isinstance(row, Mapping) and row.get("kind", "FULL_TEXT") == "FULL_TEXT")
    rows = []
    for target in plan.explicit_source_targets():
        kind, subject = target
        matching = [row for row in known if plan.matches_source_target(target, row)]
        selected = [row for row in matching if plan.requested_source_ids is None
                    or row.get("source_id") in plan.requested_source_ids]
        entries = [row for row in coverage if plan.matches_source_target(target, row)
                   and (plan.requested_source_ids is None
                        or row.get("source_id") in plan.requested_source_ids)]
        states = {row.get("state") for row in entries}
        if plan.requested_source_ids is not None and not selected:
            source_state = "EXCLUDED_FIXED_SCOPE"
        elif not matching:
            source_state = "MISSING_SOURCE"
        elif "SEARCHED" in states:
            source_state = "SEARCHED"
        elif "NOT_ACQUIRED" in states:
            source_state = "NOT_ACQUIRED"
        elif "RESTRICTED" in states:
            source_state = "RESTRICTED"
        else:
            source_state = "UNVERIFIED"
        state, reason = {
            "MISSING_SOURCE": ("MISSING_SOURCE", "未取得本次登记范围内的对应完整正文，未用其他书代替。"),
            "NOT_ACQUIRED": ("MISSING_SOURCE", "已登记但正文尚未准备，未用其他书代替。"),
            "EXCLUDED_FIXED_SCOPE": ("NOT_RETRIEVED", "未包含在本次显式选书范围，未自动扩大范围。"),
            "RESTRICTED": ("DENIED", "当前使用条件明确不允许本轮读取，未绕过许可。"),
            "UNVERIFIED": ("UNRESOLVED", "固定正文或读取条件尚不能核对，不能据此认定缺书或允许准备。"),
            "SEARCHED": ("FOUND_COMPLETE", "本次已检索对应可读正文；版本和项目适用性仍待核验。"),
        }[source_state]
        fixed = []
        if contexts is not None and source_state == "SEARCHED":
            for context in contexts:
                key = context["source_id"], context["snapshot_id"], unit_key(context["selected_clause_coverage"])
                if (plan.matches_source_target(target, context)
                        and (core_keys is None or key in core_keys)):
                    core = _core_target(context, _bound_fragments(context))
                    if core is not None:
                        fixed.append(core)
            state = "UNRESOLVED" if fixed else "NOT_LOCATED"
            reason = "须以本轮对应的完整固定正文核对，已检索不代表所需条文已读齐。"
        rows.append(EvidenceRequirement(
            f"explicit-source:{kind}:{subject}", kind="EXPLICIT_REFERENCE", trigger="BEFORE_SEARCH",
            subject=subject, reason=f"{subject}：{reason}", source_state=source_state,
            completeness="SOURCE_AVAILABILITY" if contexts is None else "FIXED_SOURCE_UNITS",
            state=state, fixed_targets=tuple(fixed)))
    return tuple(rows)


def explicit_clause_requirements(plan, *, contexts=None, core_keys=None, results=(), clause_selection=None):
    """每个明确条号独立占位；定位不能代替同书同快照核心正文及账本证明。"""
    if len(plan.explicit_clause_numbers) < 2:
        return ()
    bound = plan.bound_clause_numbers()
    omitted = set((clause_selection or {}).get("omitted", ()))
    rows = []
    for number in plan.explicit_clause_numbers:
        fixed = []
        if bound and contexts is not None:
            for context in contexts:
                coverage = context.get("selected_clause_coverage", {})
                key = context["source_id"], context["snapshot_id"], unit_key(coverage)
                if (coverage.get("clause_number") == number and plan.matches_explicit_source(context)
                        and (core_keys is None or key in core_keys)):
                    target = _core_target(context, _bound_fragments(context))
                    if target is not None:
                        fixed.append(target)
        if not bound:
            state, reason = "UNRESOLVED", "原问含多书多条号，尚未确定各条号对应哪一本书；未猜测配对。"
        elif number in omitted:
            state, reason = "BUDGET_TRUNCATED", "未纳入本次有限条号子选择，仍需在本地固定正文核读。"
        elif fixed:
            state, reason = "UNRESOLVED", "须由本轮同书同快照完整核心正文和实际证据账本核对。"
        elif contexts is not None:
            located = number in (clause_selection or {}).get("located", ())
            state = "NOT_RETRIEVED" if located else "NOT_LOCATED"
            reason = ("已定位候选，但本轮未取得可核对的对应完整核心正文。" if located else
                      "本轮未定位到该条号的对应核心正文；不代表该要求不存在。")
        elif any(row.get("clause_number") == number and plan.matches_explicit_source(row) for row in results):
            state, reason = "PARTIAL", "已定位缓存候选，仍需读取同书同快照完整正文；定位不代表证据读齐。"
        else:
            state, reason = "NOT_RETRIEVED", "本次缓存检索未定位该条号，仍需在本地固定正文核读。"
        rows.append(EvidenceRequirement(f"explicit-clause:{number}", kind="EXPLICIT_REFERENCE",
            trigger="BEFORE_SEARCH", subject=number, reason=f"第{number}条：{reason}",
            completeness="FIXED_EXPLICIT_CLAUSE", state=state, fixed_targets=tuple(fixed)))
    return tuple(rows)
