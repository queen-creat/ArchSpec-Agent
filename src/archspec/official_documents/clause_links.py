"""明确同书条文关系的兼容索引接口；语法及原字证明仅由唯一发现器生成。"""

from archspec.official_documents.evidence_relations import (
    BODY,
    REFERENCE,
    bind_relation_proof,
    discover_relations,
    reference_numbers,
)

__all__ = ["BODY", "REFERENCE", "admitted_links", "clause_links", "references"]

def references(text):
    return reference_numbers(text)


def clause_links(rows, selected_indices, *, source_id=None, snapshot_id=None, relations=None):
    """每向最多三条，一跳；旧索引字段保留，proof等待实际预算绑定。"""
    if relations is None:
        fragments = [{**row, "index": index, "selected": index in selected_indices}
                     for index, row in enumerate(rows)]
        relations = discover_relations(source_id, snapshot_id, fragments)
    proofs = [proof for proof in relations
              if proof["kind"] == "CLAUSE_REFERENCE" and proof["direction"] is not None]
    groups = {"OUTGOING": [], "INCOMING": []}
    used = set()
    for proof in proofs:
        key = proof["direction"], proof["related_unit_key"]
        if key in used:
            continue
        used.add(key)
        groups[key[0]].append(proof)
    links = []
    for direction in ("OUTGOING", "INCOMING"):
        for proof in groups[direction][:3]:
            indices = [int(value.rsplit(":", 1)[1]) if isinstance(value, str) else value
                       for value in proof["related_chunk_ids"]]
            links.append({"direction": direction, "clause_number": proof["related_unit_key"],
                          "expected_fragment_count": len(indices), "chunk_indices": indices,
                          "proof": proof})
    ordered = list(dict.fromkeys(index for link in links for index in link["chunk_indices"]))
    return ordered, links, any(len(group) > 3 for group in groups.values())


def admitted_links(links, admitted_indices, source_id, snapshot_id):
    """旧status/count不变；成品proof分别呈现实际读取覆盖。"""
    result, gaps = [], []
    for link in links:
        indices = link["chunk_indices"]
        included = [index for index in indices if index in admitted_indices]
        status = "NOT_LOCATED" if not indices else (
            "LOCATED" if len(included) == len(indices) else "BUDGET_OMITTED")
        current = {key: link[key] for key in
                   ("direction", "clause_number", "expected_fragment_count")} | {
                       "chunk_ids": [f"{source_id}:{snapshot_id}:{index}" for index in included],
                       "status": status}
        if "proof" in link:
            current["proof"] = bind_relation_proof(
                link["proof"], admitted_indices, link["proof"]["anchor_unit_key"],
                source_id=source_id, snapshot_id=snapshot_id)
        result.append(current)
        if status != "LOCATED":
            reason = ("未能在同一固定正文唯一定位目标条文" if not indices else
                      "达到读取上限，关联条文尚未读齐")
            gaps.append(f"关联条文{link['clause_number']}：{reason}。")
    return result, gaps
