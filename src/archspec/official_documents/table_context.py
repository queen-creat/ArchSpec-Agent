"""原表页面绑定的兼容接口；引用/表题词法由唯一关系发现器提供。"""

import re
from copy import deepcopy

from archspec.official_documents.evidence_relations import (
    CAPTION,
    TABLE,
    bind_relation_proof,
    caption_mentions,
    captions,
    compact,
    discover_relations,
    table_label,
)

__all__ = ["CAPTION", "TABLE", "admitted_tables", "captions", "compact", "reading_parts", "table_context"]

def _split_literal_part(page, text, rows):
    """旧切片可把表内小数另分块；只接受同页固定片段中的唯一连续原字覆盖。"""
    eligible = [(index, row) for index, row in enumerate(rows)
                if row["page_start"] <= page["pdf_page"] <= row["page_end"]]
    stream = "\n".join(row["text"] for _, row in eligible)
    start = stream.find(text)
    if start < 0 or stream.find(text, start + 1) >= 0:
        return []
    end, offset, parts = start + len(text), 0, []
    for index, row in eligible:
        stop = offset + len(row["text"])
        left, right = max(start, offset), min(end, stop)
        if left < right:
            parts.append({"text": stream[left:right], "chunk_index": index})
        offset = stop + 1
    # 插入的换行只连接真实连续片段，不容许跳过漏字、空块或别处相同单元格。
    indices = [part["chunk_index"] for part in parts]
    continuous = bool(parts) and indices == list(range(indices[0], indices[-1] + 1))
    return parts if continuous and "\n".join(part["text"] for part in parts) == text else []


def reading_parts(page, caption, rows):
    """沿原页行序分开表内文字与明确表注，不推断哪个数字属于哪一列。"""
    lines = page["text"].splitlines()
    caption_lines = caption.splitlines()
    starts = [i for i in range(len(lines) - len(caption_lines) + 1)
              if [compact(line) for line in lines[i:i + len(caption_lines)]]
              == [compact(line) for line in caption_lines]]
    if len(starts) != 1:
        return [], "TEXT_NOT_BOUND"
    groups, current, kind = [], [], "TABLE_TEXT"
    for line in lines[starts[0] + len(caption_lines):]:
        normalized = compact(line)
        if (captions(line) or re.match(r"^\d+(?:\.\d+){2,3}(?![\d.])", normalized)
                or re.match(r"^第[一二三四五六七八九十\d]+[章节]", normalized)
                or re.fullmatch(r"[·•]\d+[·•]?", normalized)):
            break
        if kind == "TABLE_TEXT" and re.match(r"^注(?:\d+)?[:：]", normalized):
            if current:
                groups.append((kind, "\n".join(current)))
            current, kind = [], "TABLE_NOTE"
        current.append(line)
    if current and any(line.strip() for line in current):
        groups.append((kind, "\n".join(current)))
    result = []
    for part_kind, text in groups:
        owners = [i for i, row in enumerate(rows)
                  if row["page_start"] <= page["pdf_page"] <= row["page_end"]
                  and text in row["text"]]
        if len(owners) == 1:
            result.append({"kind": part_kind, "text": text, "chunk_index": owners[0]})
        elif not owners:
            split = _split_literal_part(page, text, rows)
            result.extend({"kind": part_kind, **part} for part in split)
    # 一组可有多个原片段；按原字重建每组，不把片段数量当成分组数量。
    reconstructed = [(part_kind, "\n".join(part["text"] for part in result if part["kind"] == part_kind))
                     for part_kind, _ in groups]
    status = "LOCATED" if groups and all(text for _, text in groups) and reconstructed == groups else "TEXT_NOT_BOUND"
    return result, status


def table_context(rows, pages, selected_indices, *, source_id=None, snapshot_id=None, relations=None):
    """实际页上的表题/文字仍独立绑定；最多三个表，不跳另一书或latest。"""
    if relations is None:
        fragments = [{**row, "index": index, "selected": index in selected_indices}
                     for index, row in enumerate(rows)]
        relations = discover_relations(source_id, snapshot_id, fragments)
    proofs = {}
    for proof in relations:
        if proof["kind"] == "TABLE_REFERENCE" and proof["direction"] == "OUTGOING":
            proofs.setdefault(proof["target_unit_key"].removeprefix("table:"), proof)
    references = list(proofs)
    omitted = len(references) > 3
    references = references[:3]
    targets = {number: [] for number in references}
    for page in pages:
        for caption in captions(page["text"]):
            number = caption["table_number"]
            if number not in targets:
                continue
            candidates = [index for index, row in enumerate(rows)
                          if row["page_start"] <= page["pdf_page"] <= row["page_end"]
                          and compact(caption["caption"]) in compact(row["text"])]
            if not candidates:
                continue
            index = min(candidates, key=lambda value: min(
                abs(value - selected) for selected in selected_indices))
            parts, parts_status = reading_parts(page, caption["caption"], rows)
            targets[number].append({**caption, "pdf_page": page["pdf_page"], "chunk_index": index,
                                    "reading_parts": parts, "reading_parts_status": parts_status,
                                    # OCR文字齐全不证明表内每个单元格都识别到了，仍须核原页。
                                    **({"original_review": "ORIGINAL_REQUIRED"}
                                       if page.get("method") in {"OCR", "HYBRID"} else {})})
    result = []
    for number in references:
        proof = deepcopy(proofs[number])
        target_indices = list(dict.fromkeys(index for entry in targets[number]
            for index in [entry["chunk_index"], *(part["chunk_index"] for part in entry["reading_parts"])]))
        proof["_target_chunk_ids"] = [
            f"{source_id}:{snapshot_id}:{index}" if source_id is not None else index
            for index in target_indices]
        # 表题proof仅使用实际页面选出的原片；不借另一处同号标题作证明。
        witness = [token for token in proof["witness"] if token["purpose"] == "TABLE_REFERENCE"]
        used = set()
        for entry in targets[number]:
            index = entry["chunk_index"]
            key = (index, compact(entry["caption"]))
            if key in used:
                continue
            used.add(key)
            identifier = f"{source_id}:{snapshot_id}:{index}" if source_id is not None else index
            tokens = [caption["token"] for caption in caption_mentions(rows[index]["text"], identifier)
                      if caption["table_number"] == number
                      and compact(caption["caption"]) == compact(entry["caption"])]
            # 旧切片可能把页界两侧直接相接；独立原页已确认表题，仍须在实际片段
            # 找到唯一连续原字，不能因同一片段承载两页就漏掉续表的第二个证明。
            if not tokens:
                text, literal = rows[index]["text"], entry["caption"]
                start = text.find(literal)
                if start >= 0 and text.find(literal, start + 1) < 0:
                    tokens = [{"purpose": "TABLE_CAPTION", "number": number, "spans": [
                        {"chunk_id": identifier, "start": start, "end": start + len(literal),
                         "text": literal}]}]
            witness.extend(tokens)
        proof["witness"] = witness
        result.append({"table_number": number, "pages": targets[number], "proof": proof})
    indices = list(dict.fromkeys(index for ref in result for entry in ref["pages"]
                                for index in [entry["chunk_index"],
                                              *(part["chunk_index"] for part in entry["reading_parts"])]))
    return indices, result, omitted


def admitted_tables(references, admitted_indices, source_id, snapshot_id):
    """旧页面/状态合同保持；proof用本次实际fragments绑定固定身份和覆盖。"""
    result, gaps = [], []
    for ref in references:
        all_pages = ref["pages"]
        pages = []
        for entry in all_pages:
            if entry["chunk_index"] not in admitted_indices:
                continue
            parts = [{key: value for key, value in part.items() if key != "chunk_index"}
                     | {"chunk_id": f"{source_id}:{snapshot_id}:{part['chunk_index']}"}
                     for part in entry["reading_parts"] if part["chunk_index"] in admitted_indices]
            pages.append({key: value for key, value in entry.items()
                          if key not in {"chunk_index", "reading_parts", "reading_parts_status"}}
                         | {"chunk_id": f"{source_id}:{snapshot_id}:{entry['chunk_index']}",
                            "reading_parts": parts,
                            "reading_parts_status": entry["reading_parts_status"]
                            if len(parts) == len(entry["reading_parts"]) else "BUDGET_OMITTED"})
        status = "LOCATED" if pages and len(pages) == len(all_pages) else (
            "BUDGET_OMITTED" if all_pages else "NOT_LOCATED")
        if status != "LOCATED":
            detail = "未在固定正文片段中找到表题" if not all_pages else "原表或续表达到读取上限"
            gaps.append(f"{table_label(ref['table_number'])}：{detail}，未纳入部分不能作为表格答案。")
        current = {"table_number": ref["table_number"], "pages": pages, "status": status}
        if "proof" in ref:
            current["proof"] = bind_relation_proof(
                ref["proof"], admitted_indices, ref["proof"]["anchor_unit_key"],
                source_id=source_id, snapshot_id=snapshot_id)
        result.append(current)
    return result, gaps
