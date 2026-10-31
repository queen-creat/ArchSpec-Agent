"""已复核账本里的原表与相关片段，只生成阅读入口，不生成表格数值。"""

import re

from archspec.official_documents.clause_units import CLAUSE
from archspec.official_documents.evidence_relations import table_label
from archspec.official_documents.page_bounds import valid_page_range
from archspec.official_documents.table_context import TABLE, captions, compact
from archspec.official_qa.clause_groups import relation_basis
from archspec.official_qa.context_groups import BODY, IDENTITY, _index
from archspec.official_qa.relation_proofs import (
    context_ledger,
    proof_for_ledger,
    verify_relation_proof,
)


def build_table_groups(contexts, evidence):
    """组成员逐字逐页对照固定上下文；另一书同号、改字和别名不能混入。"""
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
                    ("text", "clause_number", "page_start", "page_end"))
                    for fragment in context.get("fragments", [])))

    groups, used = [], set()
    for context in contexts:
        anchor = next((row for row in evidence if row.get("chunk_id") == context.get("selected_chunk_id")
                       and row.get("role") == "MATCH" and proved(row, context)), None)
        if not anchor or not CLAUSE.fullmatch(anchor.get("clause_number") or ""):
            continue
        referenced = set()
        if any("proof" not in ref for ref in context.get("table_references", ())):
            # 仅旧无proof合成合同兼容原行为；生产proof已由唯一后端发现器提出。
            original_parts = [row["text"] for row in context["fragments"]
                              if row.get("clause_number") == anchor["clause_number"]]
            referenced = {compact(match[1]) for text in original_parts for match in TABLE.finditer(text)}
        for ref in context.get("table_references", []):
            number = ref.get("table_number")
            key = (anchor["source_id"], anchor["snapshot_id"], number)
            if key in used:
                continue
            proof = None
            if "proof" in ref:
                proof = proof_for_ledger(ref["proof"], context, evidence)
                if (proof is None or proof["kind"] != "TABLE_REFERENCE"
                        or proof["target_unit_key"] != f"table:{number}"):
                    # 仅冻结已识别的读取不一致；合法局部预算缺片仍允许后续完整实际读取。
                    local = verify_relation_proof(ref["proof"], (context,), context_ledger(context))
                    if (local is None or local["kind"] != "TABLE_REFERENCE"
                            or local["target_unit_key"] != f"table:{number}"):
                        used.add(key)
                    continue
            elif number not in referenced:
                continue
            entries, reading = [], []
            for entry in ref.get("pages", []):
                row = next((item for item in evidence if item.get("chunk_id") == entry.get("chunk_id")
                            and proved(item, context)), None)
                page = entry.get("pdf_page")
                if (not row or type(page) is not int or not row["page_start"] <= page <= row["page_end"]
                        or (proof is None and not any(value["table_number"] == number and value["caption"] == entry.get("caption")
                            and value["continuation"] == entry.get("continuation") for value in captions(row["text"])))):
                    continue
                entries.append({"evidence_id": row["evidence_id"], "pdf_page": page,
                                "caption": entry["caption"], "continuation": entry["continuation"]})
                # 文字块需要自己的实际片段，不因原表页已纳入就假定所有表注也纳入。
                for part in entry.get("reading_parts", []):
                    owner = next((item for item in evidence if item.get("chunk_id") == part.get("chunk_id")
                                  and proved(item, context)), None)
                    text, kind = part.get("text"), part.get("kind")
                    if (owner and kind in {"TABLE_TEXT", "TABLE_NOTE"} and isinstance(text, str) and text
                            and text in owner["text"] and owner["page_start"] <= page <= owner["page_end"]
                            and (kind != "TABLE_NOTE" or re.match(r"^注(?:\d+)?[:：]", compact(text)))):
                        reading.append({"kind": kind, "text": text,
                                        "evidence_id": owner["evidence_id"], "pdf_page": page})
            if not entries:
                continue
            # 原表页存在不代表表注也进入最终账本；后一次总预算可能只留下表题。
            known_parts = sum(len(page.get("reading_parts", [])) for page in ref["pages"])
            incomplete = (len(entries) != len(ref["pages"]) or len(reading) != known_parts
                          or any(page.get("reading_parts_status") == "BUDGET_OMITTED"
                                 for page in ref["pages"]))
            incomplete = incomplete or (proof is not None and proof["status"] != "LOCATED")
            table_ids = {entry["evidence_id"] for entry in entries}
            section = anchor["clause_number"].rsplit(".", 1)[0] + "."
            related = [row for row in evidence if proved(row, context)
                       and row["role"] == "RELATED_CONTEXT" and row["evidence_id"] not in table_ids
                       and BODY.fullmatch(row.get("clause_number") or "")
                       and row["clause_number"].startswith(section)
                       and re.search(r"应|不得|不宜|当|除|设置", row["text"])
                       and not re.search(r"[.…⋯·]{2,}\s*\d", row["text"])]
            related.sort(key=_index)
            groups.append({"kind": "TABLE_CONTEXT", "title": f"{table_label(number)}与相关片段",
                "source_id": anchor["source_id"], "snapshot_id": anchor["snapshot_id"],
                "standard_code": anchor["standard_code"], "anchor_evidence_id": anchor["evidence_id"],
                "table_number": number, "table_pages": entries,
                "reading_parts": reading,
                "related_evidence_ids": [row["evidence_id"] for row in related],
                "coverage": "READ_CONTEXT_ONLY",
                "table_status": ("BUDGET_OMITTED" if incomplete else
                                 "ORIGINAL_REQUIRED" if any(page.get("original_review") == "ORIGINAL_REQUIRED"
                                                            for page in ref["pages"]) else ref["status"])}
                | ({"proof": {name: value for name, value in proof.items()
                              if name != "admitted_evidence_ids"},
                    "proof_basis": relation_basis(context)} if proof is not None else {}))
            used.add(key)
    return groups
