"""有限问答证据的选择，不改检索、不查文件、不赋予邻接片段摘录资格。"""

import re

from archspec.official_documents.evidence_relations import table_label
from archspec.official_qa.context_groups import build_context_groups
from archspec.official_qa.evidence_requirements import complete_outgoing_dependency
from archspec.official_qa.intents import fire_area_question, height_measurement_question
from archspec.official_qa.local_scope import sunlight_spacing_question
from archspec.official_qa.local_spacing import unit_key


def _measurement_packets(query, contexts, evidence):
    """只用已经核对的真实计量组；目录、标签及别书同号不提供优先信号。"""
    normalized_query = re.sub(r"\s+", "", query)
    packets, used = [], set()
    for context in contexts:
        ids = {fragment["id"] for fragment in context["fragments"]}
        rows = [row for row in evidence if row["chunk_id"] in ids]
        coverage = context.get("selected_clause_coverage", {})
        core = [row for row in rows if unit_key(row) == unit_key(coverage)
                and row["role"] != "RELATED_CONTEXT"]
        expected = coverage.get("expected_fragment_count")
        # 不能把读到一半的命中打包成完整阅读；后面仍允许有限的原文入口。
        if type(expected) is not int or expected < 1 or len(core) != expected:
            continue
        ledger = {row["evidence_id"]: row for row in rows}
        for group in build_context_groups(query, [context], rows):
            heading = ledger[group["heading_evidence_id"]]
            # 完整真实题名需与问题匹配；不靠“高度”两字推定任意章节。
            subject = re.sub(r"^\d+(?:\.\d+)?\s+", "", heading["text"].strip())
            if not subject or re.sub(r"\s+", "", subject) not in normalized_query:
                continue
            if heading["chunk_id"] in used:
                continue
            members = [ledger[key] for key in group["evidence_ids"]]
            numbers = {row["clause_number"] for row in members}
            wanted = {heading["chunk_id"], *(row["chunk_id"] for row in core)}
            # 不只保有“高度”一词的续页；同条已读的全部文字一同进入。
            wanted.update(row["chunk_id"] for row in rows if row["clause_number"] in numbers)
            wanted.update(page["chunk_id"] for ref in context.get("table_references", [])
                          for page in ref["pages"])
            wanted.update(part["chunk_id"] for ref in context.get("table_references", [])
                          for page in ref["pages"] for part in page.get("reading_parts", []))
            wanted.update(chunk for ref in context.get("clause_references", []) for chunk in ref["chunk_ids"])
            packets.append((subject, context, [row for row in rows if row["chunk_id"] in wanted]))
            used.add(heading["chunk_id"])
    # 同样匹配时保持原命中顺序；更具体的真实题名优先，不用规范名称排序。
    return sorted(packets, key=lambda packet: -len(packet[0]))


def _duplicate_original_key(row):
    """只认所属固定档案；相同原件是预算信号，不证明OCR文字或版本等价。"""
    metadata = row.get("source_metadata")
    number = row.get("clause_number")
    start, end = row.get("page_start"), row.get("page_end")
    if (row.get("role") not in {"MATCH", "CONTINUATION"}
            or not isinstance(number, str) or not re.fullmatch(r"\d+(?:\.\d+){2,3}", number)
            or unit_key(row) != number or type(start) is not int or type(end) is not int
            or not 1 <= start <= end
            or not isinstance(metadata, dict) or metadata.get("schema_version") != "FIXED_SOURCE_V1"):
        return None
    own_fields = ("source_id", "snapshot_id", "snapshot_fingerprint", "standard_code", "title", "source_url")
    if any(not isinstance(row.get(field), str) or not row[field]
           or metadata.get(field) != row[field] for field in own_fields):
        return None
    digest = metadata.get("file_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None
    fields = metadata.get("fields")
    if not isinstance(fields, dict):
        return None
    recorded = []
    for name in ("level", "region", "edition", "withdrawal"):
        field = fields.get(name)
        if (not isinstance(field, dict) or "value" not in field
                or not isinstance(field.get("state"), str) or not field["state"]
                or (field["value"] is not None and
                    (not isinstance(field["value"], str) or not field["value"]))):
            return None
        # UNKNOWN/null只是双方记录相同，不能升级为地域或现行性已经核验。
        recorded.append((field["value"], field["state"]))
    return digest, row["standard_code"], number, start, end, tuple(recorded)


def _admitted_packet_core(context, packet_rows, seen):
    """只取已经实际纳入的完整核心；邻接、未齐续文或未纳入包不能成为依据。"""
    coverage = context.get("selected_clause_coverage", {})
    number, expected = coverage.get("clause_number"), coverage.get("expected_fragment_count")
    if (coverage.get("complete") is not True or not isinstance(number, str)
            or unit_key(coverage) != number or type(expected) is not int or expected < 1):
        return []
    core = [row for row in packet_rows if row.get("clause_number") == number
            and row.get("role") in {"MATCH", "CONTINUATION"}
            and all(row.get(field) == context.get(field) for field in
                    ("source_id", "snapshot_id", "snapshot_fingerprint"))]
    identities = {row["chunk_id"] for row in core}
    if len(core) != expected or len(identities) != expected or not identities <= seen:
        return []
    fragments = {part["id"]: part for part in context["fragments"]}
    if any(any(row.get(field) != context.get(field) for field in
               ("source_id", "snapshot_id", "snapshot_fingerprint", "standard_code", "title", "source_url"))
           or row["chunk_id"] not in fragments
           or any(row.get(field) != fragments[row["chunk_id"]].get(field) for field in
                  ("text", "clause_number", "page_start", "page_end"))
           for row in core):
        return []
    return core


def _outgoing_packets(contexts, evidence, priority_core_keys):
    """逐上下文验证后投影原账本；同锚点全部目标共用一次预算，绝不补读。"""
    by_id = {row["chunk_id"]: row for row in evidence}
    if len(by_id) != len(evidence):
        return []
    groups, blocked = {}, set()
    for context in contexts:
        coverage = context.get("selected_clause_coverage", {})
        key = context["source_id"], context["snapshot_id"], unit_key(coverage)
        if key not in priority_core_keys:
            continue
        dependency = complete_outgoing_dependency(context)
        if dependency is None:
            continue
        core, targets = dependency
        admitted = _admitted_packet_core(context, evidence, set(by_id))
        if not admitted or not any(row["role"] == "MATCH" for row in admitted):
            continue
        wanted = set(core["chunk_ids"])
        wanted.update(chunk for target in targets for chunk in target["chunk_ids"])
        fragments = {part["id"]: part for part in context["fragments"]}
        if any(chunk not in by_id or chunk not in fragments
               or any(by_id[chunk].get(field) != context.get(field) for field in (
                   "source_id", "snapshot_id", "snapshot_fingerprint", "standard_code", "title", "source_url"))
               or any(by_id[chunk].get(field) != fragments[chunk].get(field) for field in (
                   "text", "clause_number", "page_start", "page_end", "reading_unit_key", "reading_unit_label"))
               for chunk in wanted):
            continue
        saved = groups.setdefault(key, {"context": context, "core": set(core["chunk_ids"]), "ids": set()})
        if saved["core"] != set(core["chunk_ids"]):
            blocked.add(key)
        saved["ids"].update(wanted)
    return [(value["context"], [row for row in evidence if row["chunk_id"] in value["ids"]])
            for key, value in groups.items() if key not in blocked]


def select_context_evidence(query, contexts, evidence, *, priority_core_keys=()):
    """先保真实标题对应的有限阅读包，再沿原顺序补充；片段全文/身份/角色不变。"""
    limited, seen, gaps, characters = [], set(), [], 0
    # 火灾面积和地方间距有自己的必选条件，混合问法仍沿用既有顺序。
    prioritize = (height_measurement_question(query) and not fire_area_question(query)
                  and not sunlight_spacing_question(query))
    admitted_packet_rows = []

    def fits(rows):
        return len(limited) + len(rows) <= 8 and characters + sum(len(row["text"]) for row in rows) <= 16000

    def admit(rows):
        nonlocal characters
        limited.extend(rows)
        seen.update(row["chunk_id"] for row in rows)
        characters += sum(len(row["text"]) for row in rows)

    # 仅既有高度门采用已认可核心的明确同书依赖；其它领域的原表/例外顺序不变。
    if prioritize and priority_core_keys:
        for context, packet in _outgoing_packets(contexts, evidence, priority_core_keys):
            remaining = [row for row in packet if row["chunk_id"] not in seen]
            if fits(remaining):
                admit(remaining)
            else:
                gaps.append(f"《{context['title']}》的完整核心及明确同书引用超过本次上限，未整包纳入。")
    for subject, context, packet in _measurement_packets(query, contexts, evidence):
        remaining = [row for row in packet if row["chunk_id"] not in seen]
        if fits(remaining):
            admit(remaining)
            if prioritize:
                admitted_packet_rows.extend(remaining)
        else:
            gaps.append(f"《{context['title']}》的“{subject}”相关阅读包超过本次上限，未整包纳入。")
    # 同一包可容纳另一个实际独立已读的完整核心；普通邻居不能因同在包中成为依据。
    packet_cores = [row for context in contexts
                    for row in _admitted_packet_core(context, admitted_packet_rows, seen)] if prioritize else []
    # 比较集合在回填前冻结；普通回填不会再产生新的暂缓依据，不合并任何片段。
    originals = [(row["source_id"], key) for row in packet_cores
                 if (key := _duplicate_original_key(row)) is not None]
    ordinary, deferred = [], []
    for row in evidence:
        if row["chunk_id"] in seen:
            continue
        key = _duplicate_original_key(row) if originals else None
        duplicate = key is not None and any(source != row["source_id"] and saved == key
                                            for source, saved in originals)
        (deferred if duplicate else ordinary).append(row)
    # 两组各自保持原顺序；有余量时暂缓入口仍纳入，身份、角色和原文保持原样。
    for row in ordinary + deferred:
        if fits([row]):
            admit([row])
        else:
            gaps.append("回答证据达到本次上限，尚有上下文未纳入。")
    for context in contexts:
        coverage = context.get("selected_clause_coverage", {})
        number, selected_unit = coverage.get("clause_number"), unit_key(coverage)
        # 同为None的邻接文字不属于本单元；预算缺口只数已核对的正文续文。
        tail = [fragment for fragment in context["fragments"]
                if unit_key(fragment) == selected_unit and fragment["role"] != "RELATED_CONTEXT"
                and fragment["id"] not in seen]
        if tail:
            included = any(unit_key(fragment) == selected_unit and fragment["role"] != "RELATED_CONTEXT"
                           and fragment["id"] in seen for fragment in context["fragments"])
            if not included:
                label = (f"第{number}条" if number else
                         f"“{coverage.get('reading_unit_label') or '所选片段'}”")
                gaps.append(f"《{context['title']}》{label}的{len(tail)}个已读片段均未纳入本次预算，请对照固定原页。")
            elif str(selected_unit or "").startswith("named:"):
                label = coverage.get("reading_unit_label") or "所选计算单元"
                gaps.append(f"《{context['title']}》的“{label}”尚有{len(tail)}个已读片段未纳入，续文须在固定原页继续核对。")
            elif number:
                gaps.append(f"《{context['title']}》第{number}条尚有{len(tail)}个已读片段未纳入，续文须在固定原页继续核对。")
            else:
                # 未证明命名单元时只给片段入口，不把相邻未知文字称作同条续文。
                gaps.append(f"《{context['title']}》的所选片段尚有{len(tail)}个已读片段未纳入，请对照固定原页。")
        for ref in context.get("table_references", []):
            owners = {page["chunk_id"] for page in ref["pages"]}
            owners.update(part["chunk_id"] for page in ref["pages"] for part in page.get("reading_parts", []))
            if owners - seen:
                gaps.append(f"《{context['title']}》{table_label(ref['table_number'])}的原表、表内文字或表注达到本次上限，未纳入部分须核对固定原页。")
    return limited, list(dict.fromkeys(gaps))
