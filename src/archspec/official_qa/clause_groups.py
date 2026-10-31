"""明确同书引用的集中阅读入口；不把关联关系提升为适用性或完整答案。"""

from archspec.official_documents.clause_links import BODY
from archspec.official_documents.page_bounds import valid_page_range
from archspec.official_qa.context_groups import IDENTITY, _index
from archspec.official_qa.question_plan import thaw_value
from archspec.official_qa.relation_proofs import proof_for_ledger


def build_clause_groups(contexts, evidence):
    """只组织最终预算内、逐字逐页复核的片段；任何别书同号不能混入。"""
    if (len(evidence) > 8 or sum(len(row.get("text", "")) for row in evidence) > 16000
            or len({row.get("evidence_id") for row in evidence}) != len(evidence)
            or len({row.get("chunk_id") for row in evidence}) != len(evidence)):
        return []

    def proved(row, context):
        return (_index(row) is not None and all(row.get(key) == context.get(key) and row.get(key)
                for key in IDENTITY if key != "standard_code")
                and isinstance(row.get("standard_code"), str)
                and row.get("standard_code") == context.get("standard_code") and row.get("role") in {"MATCH", "CONTINUATION", "RELATED_CONTEXT"}
                and type(row.get("page_start")) is int and type(row.get("page_end")) is int
                and valid_page_range(row, context)
                and any(fragment.get("id") == row.get("chunk_id") and all(
                    fragment.get(key) == row.get(key) for key in
                    ("text", "clause_number", "page_start", "page_end")) for fragment in context.get("fragments", [])))

    groups, used = [], set()
    for context in contexts:
        anchor = next((row for row in evidence if row.get("chunk_id") == context.get("selected_chunk_id")
            and row.get("role") == "MATCH" and proved(row, context)), None)
        if not anchor or not BODY.fullmatch(anchor.get("clause_number") or ""):
            continue
        key = (anchor["source_id"], anchor["snapshot_id"], anchor["clause_number"])
        if key in used:
            continue
        links = []
        for ref in context.get("clause_references", []):
            rows = [row for row in evidence if row.get("chunk_id") in ref["chunk_ids"] and proved(row, context)
                    and row["clause_number"] == ref["clause_number"]]
            rows.sort(key=_index)
            direction = ref["direction"]
            proof = proof_for_ledger(ref.get("proof"), context, evidence)
            if (direction not in {"OUTGOING", "INCOMING"} or not rows or proof is None
                    or proof["admitted_evidence_ids"] != [row["evidence_id"] for row in rows]):
                continue
            links.append({"direction": direction, "clause_number": ref["clause_number"],
                "evidence_ids": [row["evidence_id"] for row in rows],
                "complete": proof["status"] == "LOCATED",
                "proof": {k: v for k, v in proof.items() if k != "admitted_evidence_ids"}})
        if links:
            groups.append({"kind": "CLAUSE_REFERENCE_CONTEXT", "title": "明确引用的相关条文",
                "source_id": key[0], "snapshot_id": key[1], "standard_code": anchor["standard_code"],
                "anchor_evidence_id": anchor["evidence_id"], "links": links, "coverage": "READ_CONTEXT_ONLY",
                "proof_basis": relation_basis(context)})
            used.add(key)
    return groups


def relation_basis(context):
    """网页核验只需实际身份/覆盖/定位；不重复传正文或私有项目条件。"""
    value = {key: thaw_value(context[key]) for key in
        ("source_id", "snapshot_id", "snapshot_fingerprint", "title", "standard_code",
         "source_url", "selected_chunk_id", "selected_clause_coverage",
         "clause_references", "table_references") if key in context}
    for key in ("clause_references", "table_references"):
        for ref in value.get(key, ()):
            ref.pop("proof", None)
    return value
