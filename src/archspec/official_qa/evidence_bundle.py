"""预算之后的一份固定证据包；完整性、选择与逐字引用共享同一账本。"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from archspec.official_qa.evidence_requirements import (
    EvidenceRequirement,
    complete_clause_keys,
    requirements_for,
)
from archspec.official_qa.local_spacing import evidence_key, unit_key
from archspec.official_qa.question_plan import (
    QuestionPlan,
    SnapshotRecord,
    freeze_value,
    thaw_value,
)


@dataclass(frozen=True)
class EvidenceBundle(SnapshotRecord):
    plan: QuestionPlan
    requirements: tuple[EvidenceRequirement, ...]
    contexts: tuple
    retrieval: Mapping
    budget: Mapping
    evidence: tuple
    usable: tuple
    units: tuple
    table_readings: tuple
    page_readings: tuple
    required_evidence_ids: frozenset[str]
    required_table_reading_ids: frozenset[str]
    require_empty_selection: bool
    coverage: Mapping
    version: str = "EVIDENCE_BUNDLE_V1"

    def __post_init__(self):
        for name in ("contexts", "retrieval", "budget", "evidence", "usable", "units",
                     "table_readings", "page_readings", "coverage"):
            object.__setattr__(self, name, freeze_value(getattr(self, name)))


def _ordered(rows):
    return sorted(rows, key=lambda row: int(row["chunk_id"].rsplit(":", 1)[1]))


def _units(contexts, evidence):
    complete = complete_clause_keys(contexts, evidence)
    expected = {}
    for context in contexts:
        coverage = context.get("selected_clause_coverage", {})
        key = context["source_id"], context["snapshot_id"], unit_key(coverage)
        count = coverage.get("expected_fragment_count")
        if type(count) is int and count > 0:
            expected.setdefault(key, set()).add(count)
    groups = {}
    for row in evidence:
        if row["role"] != "RELATED_CONTEXT":
            groups.setdefault(evidence_key(row), []).append(row)
    return [{
        "key": key, "source_id": key[0], "snapshot_id": key[1], "unit_key": key[2],
        "clause_number": rows[0].get("clause_number"),
        "evidence_ids": tuple(row["evidence_id"] for row in _ordered(rows)),
        "text": "\n".join(row["text"] for row in _ordered(rows)),
        "expected_fragment_count": (next(iter(expected[key])) if len(expected.get(key, ())) == 1
                                    else None),
        "included_fragment_count": len({row["chunk_id"] for row in rows}),
        "complete": key in complete,
    } for key, rows in groups.items()]


def _counts_only(value):
    """预算只保存非负整数计数，不能隐式承载正文或回执。"""
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _counts_only(part) for key, part in value.items())
    return type(value) is int and value >= 0


def _reading_bound(row, ledger, usable_ids):
    anchor = ledger.get(row.get("anchor_evidence_id"))
    owner = ledger.get(row.get("evidence_id"))
    if not anchor or not owner or anchor["evidence_id"] not in usable_ids:
        return False
    if any(owner[key] != anchor[key] for key in ("source_id", "snapshot_id")):
        return False
    if any(key in row and row[key] != owner[key]
           for key in ("source_id", "snapshot_id", "chunk_id")):
        return False
    page = row.get("pdf_page")
    return page is None or (type(page) is int and owner["page_start"] <= page <= owner["page_end"])



def _proved_ledger(contexts, evidence):
    """需求覆盖另核实际context原字/原页，不能靠伪造同号或片段计数声称找到。"""
    return {row["chunk_id"]: row for row in evidence if any(
        all(row.get(key) == context.get(key) for key in
            ("source_id", "snapshot_id", "snapshot_fingerprint"))
        and any(fragment.get("id") == row["chunk_id"] and all(
            fragment.get(key) == row.get(key) for key in
            ("text", "clause_number", "page_start", "page_end", "reading_unit_key"))
            for fragment in context.get("fragments", ()))
        for context in contexts)}


def _target_coverage(target, proved, usable_ids, *, answer_required):
    """读取完整与总预算纳入完整分开；READ_CONTEXT无需升成quote_ready。"""
    ids = target.get("chunk_ids", ())
    rows = [proved[key] for key in ids if key in proved
            and all(proved[key].get(field) == target.get(field)
                    for field in ("source_id", "snapshot_id"))]
    if answer_required:
        rows = [row for row in rows if row["role"] != "RELATED_CONTEXT"
                and evidence_key(row) == (target["source_id"], target["snapshot_id"], target["unit_key"])]
    actual = tuple(row["evidence_id"] for row in rows)
    if target.get("read_state") == "NOT_LOCATED":
        return "NOT_LOCATED", actual
    if not target.get("read_complete"):
        return target.get("read_state", "PARTIAL"), actual
    if len(rows) != len(ids):
        return "BUDGET_TRUNCATED", actual
    if answer_required and any(row["evidence_id"] not in usable_ids for row in rows):
        return "PARTIAL", actual
    return ("FOUND_COMPLETE" if ids else "NOT_RETRIEVED"), actual


def _requirement_coverage(requirement, proved, usable_ids, legacy_core):
    if requirement.kind == "CORE_BODY" and requirement.completeness not in {"FIXED_CORE_UNITS", "PARALLEL_CORE_UNITS"}:
        # 未定targets的旧合同保留PARTIAL，避免改动已冻结的32项兼容断言。
        return legacy_core, tuple(sorted(usable_ids))
    if requirement.kind == "CONDITION_EXCEPTION":
        wanted = set(requirement.fixed_targets[0].get("required_evidence_ids", ())) if requirement.fixed_targets else set(requirement.evidence_ids)
        actual = wanted & {row["evidence_id"] for row in proved.values()}
        state = ("FOUND_COMPLETE" if wanted and wanted <= usable_ids and wanted <= actual
                 else "PARTIAL" if actual else "NOT_LOCATED")
        return state, tuple(sorted(actual))
    results = [_target_coverage(target, proved, usable_ids,
               answer_required=requirement.necessity == "ANSWER_REQUIRED")
               for target in requirement.fixed_targets]
    if not results:
        return ("NOT_RETRIEVED", ()) if requirement.kind == "CORE_BODY" else (requirement.state, requirement.evidence_ids)
    states = {state for state, _ in results}
    # CORE是相关候选中的完整出处；其它需求逐一满足每个实际定位目标。
    if ((requirement.kind == "CORE_BODY" or requirement.source_state is not None)
            and "FOUND_COMPLETE" in states) or states == {"FOUND_COMPLETE"}:
        state = "FOUND_COMPLETE"
    else:
        state = next((value for value in ("BUDGET_TRUNCATED", "ORIGINAL_REQUIRED", "PARTIAL", "NOT_LOCATED", "NOT_RETRIEVED")
                      if value in states), "PARTIAL")
    return state, tuple(sorted({identity for _, identities in results for identity in identities}))

def compile_evidence_bundle(plan, requirements, contexts, retrieval, budget, *, evidence,
                            usable=None, table_readings=(), page_readings=(),
                            required_evidence_ids=(), required_table_reading_ids=(),
                            require_empty_selection=False) -> EvidenceBundle:
    """工厂仅接收已读/预算后输入；政策必选集合由调用者计算一次。"""
    requirements = requirements_for(plan, requirements=requirements)
    if not isinstance(budget, Mapping) or not _counts_only(budget):
        raise ValueError("预算只能包含非负整数计数")
    contexts = list(contexts)
    if plan.effective_source_ids is not None and any(
            context["source_id"] not in plan.effective_source_ids for context in contexts):
        raise ValueError("固定上下文越过本次有效来源范围")
    if any(not plan.matches_explicit_source(context) for context in contexts):
        raise ValueError("固定上下文与原问指定规范不同")
    rows = thaw_value(evidence)
    if (len(rows) > 8 or sum(len(row["text"]) for row in rows) > 16000
            or len({row["evidence_id"] for row in rows}) != len(rows)
            or len({row["chunk_id"] for row in rows}) != len(rows)):
        raise ValueError("证据预算或固定身份重复")
    if plan.effective_source_ids is not None and any(
            row["source_id"] not in plan.effective_source_ids for row in rows):
        raise ValueError("证据越过本次有效来源范围")
    if any(not plan.matches_explicit_source(row) for row in rows):
        raise ValueError("固定证据与原问指定规范不同")
    complete = complete_clause_keys(contexts, rows)
    for row in rows:
        row["quote_ready"] = row["role"] != "RELATED_CONTEXT" and evidence_key(row) in complete
    ledger = {row["evidence_id"]: row for row in rows}
    selected_rows = rows if usable is None else list(usable)
    accepted = []
    for row in selected_rows:
        current = ledger.get(row["evidence_id"])
        if not current or any(current.get(key) != row.get(key) for key in
                              ("chunk_id", "source_id", "snapshot_id", "text",
                               "clause_number", "page_start", "page_end", "role")):
            raise ValueError("可选证据与实际账本不一致")
        accepted.append(current)
    if len({row["evidence_id"] for row in accepted}) != len(accepted):
        raise ValueError("可选证据身份重复")
    required = frozenset(required_evidence_ids)
    usable_ids = {row["evidence_id"] for row in accepted if row["quote_ready"]}
    if not required <= usable_ids:
        raise ValueError("必选编号没有对应完整可选证据")
    readings = thaw_value(table_readings)
    if len({row["id"] for row in readings}) != len(readings):
        raise ValueError("原表读数编号重复")
    reading_ledger = {row["id"]: row for row in readings}
    if any(not _reading_bound(row, ledger, usable_ids) for row in readings):
        raise ValueError("原表读数所有者或完整可选锚点不属于实际同书账本")
    required_readings = frozenset(required_table_reading_ids)
    if not required_readings <= reading_ledger.keys():
        raise ValueError("必选读数不在当前原表账本")
    state = ("FOUND_COMPLETE" if any(row["quote_ready"] for row in accepted)
             else "PARTIAL" if any(row["role"] != "RELATED_CONTEXT" for row in accepted)
             else "NOT_RETRIEVED")
    # 文字片段可逐字引用，但同页未知图片可能承载条件或例外，不能称证据完整。
    image_gap_ids = {row["evidence_id"] for row in accepted
                     if "UNRECOGNIZED_IMAGES" in row.get("warnings", ())}
    if image_gap_ids and state == "FOUND_COMPLETE":
        state = "PARTIAL"
    coverage = {"core_body": state}
    proved = _proved_ledger(contexts, rows)
    evaluated = []
    for requirement in requirements:
        result, actual_ids = _requirement_coverage(requirement, proved, usable_ids, state)
        if result == "FOUND_COMPLETE" and image_gap_ids.intersection(actual_ids):
            result = "PARTIAL"
        coverage[requirement.requirement_id] = result
        if requirement.kind == "CORE_BODY" and requirement.completeness != "PARALLEL_CORE_UNITS":
            coverage["core_body"] = result
        evaluated.append(replace(requirement, state=result, evidence_ids=actual_ids))
    return EvidenceBundle(
        plan=plan, requirements=tuple(evaluated), contexts=tuple(contexts),
        retrieval=retrieval, budget=budget, evidence=tuple(rows), usable=tuple(accepted),
        units=tuple(_units(contexts, rows)), table_readings=tuple(readings),
        page_readings=tuple(page_readings), required_evidence_ids=required,
        required_table_reading_ids=required_readings,
        require_empty_selection=bool(require_empty_selection), coverage=coverage)


def selection_packet(bundle: EvidenceBundle) -> dict:
    """只构造当前选择输入；不包含项目/候选私文，也不代替发送许可核对。"""
    fields = ("evidence_id", "text", "clause_number", "quote_ready", "source_id", "snapshot_id",
              "title", "standard_code", "page_start", "page_end",
              "reading_unit_key", "reading_unit_label")
    reading_fields = ("id", "anchor_evidence_id", "evidence_id", "table_number", "row_label",
                      "column_label", "value", "scope", "unit", "reading_kind")
    return {
        "query": bundle.plan.original_query,
        "conditions": thaw_value(bundle.plan.public_conditions),
        "evidence": [{**{key: thaw_value(row[key]) for key in fields if key in row},
                      **({"warnings": thaw_value(row["warnings"])}
                         if "UNRECOGNIZED_IMAGES" in row.get("warnings", ()) else {})}
                     for row in bundle.usable],
        "table_readings": [{key: thaw_value(row[key]) for key in reading_fields if key in row}
                          for row in bundle.table_readings],
        "required_evidence_ids": sorted(bundle.required_evidence_ids),
        "required_table_reading_ids": sorted(bundle.required_table_reading_ids),
    }


def guard_draft(draft, evidence):
    """纯迁旧_guard：坏quote不丢弃，整片与同条全部续文逐字核对。"""
    if not isinstance(draft, list) or len(draft) > 8:
        raise ValueError("摘录格式无效")
    ledger = {row["evidence_id"]: row for row in evidence}
    items, chosen = [], set()
    for choice in draft:
        if not isinstance(choice, dict) or set(choice) != {"evidence_id", "quote"}:
            raise ValueError("只接受证据编号和原文摘录，不接受自由陈述")
        row = ledger.get(choice["evidence_id"])
        quote = choice["quote"]
        if (not row or not row.get("quote_ready") or row["evidence_id"] in chosen
                or not isinstance(quote, str) or not quote.strip() or quote != row["text"]):
            raise ValueError("引用必须完整对应原文片段，不能截掉否定词或条件")
        chosen.add(row["evidence_id"])
        if len(quote) > 1800:
            raise ValueError("短回答过长，请在阅读区对照")
        citation = {key: thaw_value(value) for key, value in row.items() if key not in {"text", "role"}}
        citation["page_url"] = (f"/api/official-documents/{row['source_id']}/snapshots/"
                                f"{row['snapshot_id']}/pages/{row['page_start']}.png")
        items.append({"quote": quote, "citation": citation})
    for item in items:
        key = evidence_key(item["citation"])
        required = {row["evidence_id"] for row in evidence if row.get("quote_ready")
                    and evidence_key(row) == key}
        if not required.issubset(chosen):
            raise ValueError("整理器只选择了条文开头，遗漏同一固定出处的续文")
    return items


def guard_selection(bundle: EvidenceBundle, selected: dict) -> list[dict]:
    if not isinstance(selected, dict) or set(selected) != {"choices", "table_reading_ids"}:
        raise ValueError("选择结果只能包含原字摘录与真实读数编号")
    items = guard_draft(selected["choices"], bundle.usable)
    chosen = {item["citation"]["evidence_id"] for item in items}
    if (items or bundle.require_empty_selection) and not bundle.required_evidence_ids <= chosen:
        raise ValueError("整理器省略已完整取得的必选证据")
    reading_ids = selected["table_reading_ids"]
    if (not isinstance(reading_ids, list) or len(reading_ids) > 4
            or len(set(reading_ids)) != len(reading_ids)):
        raise ValueError("原表选择编号无效或重复")
    ledger = {row["id"]: row for row in bundle.table_readings}
    if any(identity not in ledger or ledger[identity]["anchor_evidence_id"] not in chosen
           for identity in reading_ids):
        raise ValueError("读数未对应已选完整固定条文")
    if not bundle.required_table_reading_ids <= set(reading_ids):
        raise ValueError("遗漏必选原表读数")
    return items

def verify_relation_proof(proof, contexts, evidence):
    """保持统一bundle接口；独立证明核验不拥有关系发现权。"""
    from archspec.official_qa.relation_proofs import verify_relation_proof as verify

    return verify(proof, contexts, evidence)
