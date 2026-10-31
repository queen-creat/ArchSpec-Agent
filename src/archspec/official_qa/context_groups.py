"""把已核对的高度计量出处编成阅读组，不补搜、不生成规范结论。"""

import re

from archspec.official_documents.page_bounds import valid_page_range
from archspec.official_qa.intents import height_measurement_question

IDENTITY = ("source_id", "snapshot_id", "snapshot_fingerprint", "standard_code")
BODY = re.compile(r"\d+(?:\.\d+){2,3}")
HEADING = re.compile(r"(\d+(?:\.\d+)?)\s+([^\r\n]+)")


def _index(row):
    """按固定快照片段位置排列；条号相同不代表同一个出处。"""
    source, snapshot, chunk = row.get("source_id"), row.get("snapshot_id"), row.get("chunk_id")
    if not isinstance(chunk, str):
        return None
    for prefix in (f"{source}:{snapshot}:", f"{snapshot}:"):
        if chunk.startswith(prefix):
            suffix = chunk[len(prefix):]
            return int(suffix) if re.fullmatch(r"0|[1-9]\d{0,5}", suffix) else None
    return None


def _section(row):
    text = row.get("text", "").strip()
    match = HEADING.fullmatch(text)
    # 实际单行标题才是锚点。目录页号、点线、混合多行不靠heading_path猜归属。
    # 原生PDF切分器可能未给标题写clause_number；从真实标题读号，不改旧快照。
    if (not match or row.get("clause_number") not in (None, match[1])
            or "目录" in match[2]
            or re.search(r"[.…·]{2,}|\d\s*$", match[2])):
        return None
    return match[1], text


def _heading(row):
    section = _section(row)
    return section if section and "建筑高度" in section[1] else None


def _measurement(row):
    text = re.sub(r"\s+", "", row["text"])
    return (BODY.fullmatch(row.get("clause_number") or "")
            and not re.search(r"[.…·]{2,}\d+(?:\D|$)", text)
            and not any(word in text for word in ("室内净高", "疏散"))
            and any(word in text for word in ("建筑高度", "檐口高度", "屋脊高度"))
            and any(word in text for word in ("计算", "计量", "测量", "计入")))


def build_context_groups(query, contexts, evidence):
    """只引用同一次有限证据账本，原角色/全文保持不变，组不是完整性证明。"""
    if not height_measurement_question(query):
        return []
    if len(evidence) > 8 or sum(len(row.get("text", "")) for row in evidence) > 16000:
        return []
    # 不同编号冒用同一片段会令引用歧义；不能选择其中一份文字蒙混过去。
    if (len({row.get("evidence_id") for row in evidence}) != len(evidence)
            or len({row.get("chunk_id") for row in evidence}) != len(evidence)):
        return []

    def proved(row):
        """成员必须逐字、逐页对应传入的真实上下文，不能只按同条号回填。"""
        if (_index(row) is None or row.get("role") not in
                {"MATCH", "CONTINUATION", "RELATED_CONTEXT"}
                or any(not row.get(key) for key in IDENTITY)
                or type(row.get("page_start")) is not int
                or type(row.get("page_end")) is not int
                or not valid_page_range(row)):
            return False
        return any(
            all(row[key] == context.get(key) for key in IDENTITY)
            and any(fragment.get("id") == row["chunk_id"] and all(
                fragment.get(key) == row.get(key)
                for key in ("text", "clause_number", "page_start", "page_end"))
                for fragment in context.get("fragments", []))
            for context in contexts)

    rows = [row for row in evidence if proved(row)]
    groups, used = [], set()
    for heading in rows:
        anchor = _heading(heading)
        if not anchor:
            continue
        section, title = anchor
        identity = tuple(heading[key] for key in IDENTITY)
        if (*identity, section) in used:
            continue
        # 后面的真实章节是归属边界，即使其中引用前节同号条文也不能混回本组。
        # 只用本次已核对上下文，不读取整本书；边界不受证据展示预算影响。
        boundaries = []
        for context in contexts:
            if not all(context.get(key) == heading[key] for key in IDENTITY):
                continue
            for fragment in context.get("fragments", []):
                candidate = {**context, **fragment, "chunk_id": fragment.get("id")}
                index = _index(candidate)
                if index is None or index <= _index(heading):
                    continue
                # 原生切分器有时把下一节标题接在上一条末尾。逐行查真实标题，
                # 保留该条完整原文，只禁止更后的片段继续归入前节；不改旧切分。
                for position, line in enumerate(fragment.get("text", "").splitlines()):
                    if _section({"text": line, "clause_number": None}):
                        boundaries.append(index + (1 if position else 0))
        end = min(boundaries) if boundaries else float("inf")
        members = [row for row in rows if tuple(row[key] for key in IDENTITY) == identity
                   and (row.get("clause_number") or "").startswith(section + ".")
                   and _index(heading) < _index(row) < end
                   and row["page_start"] >= heading["page_start"] and _measurement(row)]
        members.sort(key=_index)
        matches = [row for row in members if row["role"] == "MATCH"]
        # 标题和实际命中需在同一个读取上下文出现，且原页邻近。
        anchored = any(
            row["page_start"] <= heading["page_end"] + 1
            and any(all(context.get(key) == heading[key] for key in IDENTITY)
                    and {heading["chunk_id"], row["chunk_id"]} <= {
                        fragment.get("id") for fragment in context.get("fragments", [])}
                    for context in contexts)
            for row in matches)
        if len(members) < 2 or not anchored:
            continue
        groups.append({
            "kind": "MEASUREMENT_CONTEXT", "title": title,
            "source_id": heading["source_id"], "snapshot_id": heading["snapshot_id"],
            "standard_code": heading["standard_code"],
            "heading_evidence_id": heading["evidence_id"],
            "evidence_ids": [row["evidence_id"] for row in members],
            "coverage": "READ_CONTEXT_ONLY",
        })
        used.add((*identity, section))
    return groups
