"""只核验已提出的关系证明；不扫描原书重新发现关系或生成规范事实。"""

import re
from itertools import pairwise

from archspec.official_documents.clause_units import ARTICLE
from archspec.official_documents.clause_units import CLAUSE as NUMBER
from archspec.official_documents.evidence_relations import TABLE_NUMBER, table_label
from archspec.official_qa.local_spacing import unit_key
from archspec.official_qa.question_plan import thaw_value

CONNECTORS = {"、", "，", ",", "及", "和", "以及", "与", "或"}
FIELDS = {
    "version", "kind", "direction", "source_id", "snapshot_id", "anchor_unit_key",
    "origin_unit_key", "target_unit_key", "related_unit_key", "origin_chunk_ids",
    "related_chunk_ids", "expected_fragment_count", "admitted_chunk_ids", "status",
    "coverage", "witness",
}


def _compact(text):
    return re.sub(r"\s+", "", text)


def _index(identity):
    return int(identity.rsplit(":", 1)[1])


def _identities(values, prefix):
    return (isinstance(values, (list, tuple)) and len(set(values)) == len(values)
            and all(isinstance(value, str) and value.startswith(prefix)
                    and value[len(prefix):].isdecimal() for value in values))



def _local_scope(stream, start, *, table):
    # 只核验指定token前的书名/句界，不在此发现另一条关系。
    before = _compact(stream[:start])
    if before.rfind("《") > before.rfind("》"):
        return False
    line = stream[stream.rfind("\n", 0, start) + 1:].split("\n", 1)[0]
    if re.search(r"[.…⋯·]{2,}\s*\d", line):
        return False
    if table:
        before = re.split(r"[。；;！？!?]", before)[-1]
        if "《" in before and max(before.rfind("本规范"), before.rfind("本标准")) < before.rfind("》"):
            return False
    return True


def _table_members(ref, proof, proved):
    ids = []
    for page in ref.get("pages", ()):
        row = proved.get(page.get("chunk_id"))
        if row is None:
            continue
        if (type(page.get("pdf_page")) is not int
                or not row["page_start"] <= page["pdf_page"] <= row["page_end"]
                or not isinstance(page.get("caption"), str)
                or type(page.get("continuation")) is not bool):
            return False
        ids.append(page["chunk_id"])
        for part in page.get("reading_parts", ()):
            owner = proved.get(part.get("chunk_id"))
            if owner is None:
                continue
            if (not isinstance(part.get("text"), str)
                    or not part["text"] or part["text"] not in owner["text"]
                    or not owner["page_start"] <= page["pdf_page"] <= owner["page_end"]):
                return False
            ids.append(part["chunk_id"])
    return list(dict.fromkeys(ids)) == list(proof["admitted_chunk_ids"])


def _table_tokens(proof, tokens, ref, stream, first_start):
    number = proof["target_unit_key"][6:]
    if (not tokens or tokens[0] != ("TABLE_REFERENCE", table_label(number), number)
            or not _local_scope(stream, first_start, table=True)
            or any(token[0] != "TABLE_CAPTION" or token[2] != number for token in tokens[1:])):
        return False
    if proof["status"] == "LOCATED" and not tokens[1:]:
        return False
    actual_captions = {(page["chunk_id"], _compact(page["caption"]), page["continuation"])
        for page in ref.get("pages", ()) if page["chunk_id"] in proof["admitted_chunk_ids"]}
    token_captions = set()
    for witness, (_, literal, _) in zip(proof["witness"][1:], tokens[1:], strict=True):
        if len(witness["spans"]) != 1:
            return False
        span = witness["spans"][0]
        key = span["chunk_id"], literal, literal.startswith("续")
        if key not in actual_captions:
            return False
        suffix = re.fullmatch(rf"(?:续)?{re.escape(table_label(number))}(?![\d.\-])(.+)", literal)
        if (not suffix or suffix[1].startswith(("的", "规定", "所示", "中", "应", "要求", "内"))
                or re.search(r"[.…⋯·]{2,}", literal)):
            return False
        token_captions.add(key)
    return token_captions == actual_captions


def _verify(proof, contexts, evidence):
    if (not isinstance(proof, dict) or set(proof) != FIELDS
            or proof["version"] != "EVIDENCE_RELATION_V1"
            or proof["coverage"] != "READ_CONTEXT_ONLY"
            or proof["kind"] not in {"CLAUSE_REFERENCE", "TABLE_REFERENCE"}
            or proof["direction"] not in {"OUTGOING", "INCOMING"}
            or proof["status"] not in {"LOCATED", "NOT_LOCATED", "PARTIAL",
                                      "BUDGET_EXCLUDED", "LIMIT_REACHED"}
            or type(proof["expected_fragment_count"]) is not int
            or proof["expected_fragment_count"] < 0):
        return None
    source, snapshot = proof["source_id"], proof["snapshot_id"]
    if (not re.fullmatch(r"gb55031|official-[a-f0-9]{32}", source)
            or not re.fullmatch(r"[a-f0-9]{64}", snapshot)
            or len(evidence) > 8 or sum(len(row["text"]) for row in evidence) > 16000
            or len({row["evidence_id"] for row in evidence}) != len(evidence)
            or len({row["chunk_id"] for row in evidence}) != len(evidence)):
        return None
    prefix = f"{source}:{snapshot}:"
    if any(not _identities(proof[key], prefix) for key in
           ("origin_chunk_ids", "related_chunk_ids", "admitted_chunk_ids")):
        return None
    if (not proof["origin_chunk_ids"]
            or len(proof["related_chunk_ids"]) != proof["expected_fragment_count"]
            or not set(proof["admitted_chunk_ids"]) <= set(proof["related_chunk_ids"])):
        return None
    context = next((row for row in contexts if row["source_id"] == source
                    and row["snapshot_id"] == snapshot
                    and unit_key(row["selected_clause_coverage"]) == proof["anchor_unit_key"]), None)
    if context is None:
        return None
    fragments = {row["id"]: row for row in context["fragments"]}
    ledger = {row["chunk_id"]: row for row in evidence}
    proved = {}
    for identity, row in ledger.items():
        fragment = fragments.get(identity)
        if (fragment and row["source_id"] == source and row["snapshot_id"] == snapshot
                and isinstance(row.get("standard_code"), str)
                and row.get("standard_code") == context.get("standard_code")
                and all(row.get(key) == context.get(key) and row.get(key)
                        for key in ("snapshot_fingerprint", "title", "source_url"))
                and all(fragment.get(key) == row.get(key) for key in
                        ("text", "clause_number", "page_start", "page_end", "reading_unit_key"))
                and row["role"] in {"MATCH", "CONTINUATION", "RELATED_CONTEXT"}):
            proved[identity] = row
    anchor = proved.get(context["selected_chunk_id"])
    if anchor is None or anchor["role"] != "MATCH":
        return None
    if any(identity not in proved for identity in proof["origin_chunk_ids"]):
        return None
    origin = sorted(proof["origin_chunk_ids"], key=_index)
    if origin != list(proof["origin_chunk_ids"]) or any(
            unit_key(proved[identity]) != proof["origin_unit_key"] for identity in origin):
        return None
    target, related = proof["target_unit_key"], proof["related_unit_key"]
    if proof["kind"] == "CLAUSE_REFERENCE":
        if not NUMBER.fullmatch(target) or not NUMBER.fullmatch(proof["origin_unit_key"]):
            return None
        if (proof["anchor_unit_key"] != (proof["origin_unit_key"]
                if proof["direction"] == "OUTGOING" else target)
                or related != (target if proof["direction"] == "OUTGOING"
                               else proof["origin_unit_key"])):
            return None
        ref = next((row for row in context.get("clause_references", ())
                    if row["direction"] == proof["direction"]
                    and row["clause_number"] == related), None)
        if (ref is None or ref["expected_fragment_count"] != proof["expected_fragment_count"]
                or not set(ref["chunk_ids"]) <= set(proof["related_chunk_ids"])
                or [identity for identity in ref["chunk_ids"] if identity in proved]
                    != list(proof["admitted_chunk_ids"])
                or any(unit_key(proved[identity]) != related
                       for identity in proof["admitted_chunk_ids"] if identity in proved)):
            return None
    else:
        if (proof["direction"] != "OUTGOING" or proof["anchor_unit_key"] != proof["origin_unit_key"]
                or related != target or not target.startswith("table:")
                or not TABLE_NUMBER.fullmatch(target[6:])):
            return None
        ref = next((row for row in context.get("table_references", ())
                    if row["table_number"] == target[6:]), None)
        if ref is None or not _table_members(ref, proof, proved):
            return None
    actual = [identity for identity in proof["related_chunk_ids"] if identity in proved]
    if set(actual) != set(proof["admitted_chunk_ids"]):
        return None
    if proof["status"] == "LOCATED" and (
            not actual or len(actual) != proof["expected_fragment_count"]):
        return None
    if proof["status"] == "NOT_LOCATED" and (actual or proof["expected_fragment_count"] != 0):
        return None
    if proof["status"] == "BUDGET_EXCLUDED" and len(actual) == proof["expected_fragment_count"]:
        return None
    coverage = context["selected_clause_coverage"]
    core_complete = (coverage.get("complete") is True
        and coverage.get("included_fragment_count") == coverage.get("expected_fragment_count")
        and len([row for row in proved.values()
                 if unit_key(row) == proof["anchor_unit_key"]
                 and row["role"] in {"MATCH", "CONTINUATION"}])
            == coverage.get("expected_fragment_count"))
    if proof["status"] == "LIMIT_REACHED" or (proof["status"] == "PARTIAL"
            and core_complete and len(actual) == proof["expected_fragment_count"]):
        return None
    offsets, stream, cursor = {}, "", 0
    for identity in origin:
        offsets[identity] = cursor
        stream += proved[identity]["text"]
        cursor = len(stream)
    tokens, previous_end, first_start = [], None, None
    for witness in proof["witness"]:
        if (not isinstance(witness, dict) or set(witness) - {"purpose", "number", "spans"}
                or not isinstance(witness.get("spans"), (list, tuple)) or not witness["spans"]):
            return None
        raw, first, end, previous_part = "", None, None, None
        for span in witness["spans"]:
            if set(span) != {"chunk_id", "start", "end", "text"}:
                return None
            identity, start, stop = span["chunk_id"], span["start"], span["end"]
            row = proved.get(identity)
            if (row is None or type(start) is not int or type(stop) is not int
                    or not 0 <= start < stop <= len(row["text"])
                    or row["text"][start:stop] != span["text"]):
                return None
            if witness["purpose"] != "TABLE_CAPTION":
                if identity not in offsets:
                    return None
                absolute, last = offsets[identity] + start, offsets[identity] + stop
                if previous_part is not None and _compact(stream[previous_part:absolute]):
                    return None
                if previous_part is not None and absolute < previous_part:
                    return None
                first = absolute if first is None else first
                end, previous_part = last, last
            raw += span["text"]
        normalized = _compact(raw)
        purpose = witness["purpose"]
        if purpose != "TABLE_CAPTION":
            if previous_end is not None and (
                    first < previous_end or _compact(stream[previous_end:first])):
                return None
            if first_start is None:
                first_start = first
            previous_end = end
        tokens.append((purpose, normalized, witness.get("number")))
    if proof["kind"] == "CLAUSE_REFERENCE":
        if len(tokens) < 2 or len(tokens) % 2:
            return None
        if (tokens[0][0] != "SAME_BOOK_SEED" or tokens[0][1] not in {"本规范", "本标准", "本规定", "按"}
                or tokens[0][2] is not None
                or not _local_scope(stream, first_start, table=tokens[0][1] == "按")):
            return None
        for index, (purpose, literal, number) in enumerate(tokens[1:], 1):
            if index % 2:
                if (purpose != "REFERENCE_ITEM" or not isinstance(number, str)
                        or not NUMBER.fullmatch(number)
                        or literal not in ({number} if ARTICLE.fullmatch(number)
                                           else {f"第{number}条", f"{number}条"})):
                    return None
            elif purpose != "LIST_CONNECTOR" or literal not in CONNECTORS or number is not None:
                return None
        if tokens[-1][2] != target:
            return None
    else:
        if not _table_tokens(proof, tokens, ref, stream, first_start):
            return None
    # 跨片词法只能在完整实际引用核心中接续，不能绕开原件的缺片约束。
    used_chunks = {span["chunk_id"] for item in proof["witness"] for span in item["spans"]
                   if item["purpose"] != "TABLE_CAPTION"}
    if len(used_chunks) > 1:
        expected = (context["selected_clause_coverage"]["expected_fragment_count"]
                    if proof["origin_unit_key"] == proof["anchor_unit_key"] else
                    proof["expected_fragment_count"])
        if (len(origin) != expected or not core_complete
                or any(_index(b) != _index(a) + 1 for a, b in pairwise(origin))):
            return None
    result = thaw_value(proof)
    result["admitted_evidence_ids"] = [proved[identity]["evidence_id"]
                                      for identity in proof["admitted_chunk_ids"]]
    return result


def verify_relation_proof(proof, contexts, evidence):
    """无效/伪造证明返回None；只核验实际账本，绝不在此发现新关系。"""
    try:
        return _verify(proof, contexts, evidence)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

def context_ledger(context):
    """由已读实际片段建立局部核验账本；不额外读取或发送正文。"""
    identity = {key: context.get(key) for key in
        ("source_id", "snapshot_id", "snapshot_fingerprint", "title", "standard_code", "source_url")}
    return [{**identity, **fragment, "chunk_id": fragment["id"],
             "evidence_id": "context:" + fragment["id"]} for fragment in context.get("fragments", ())]


def proof_for_ledger(proof, context, evidence):
    """先核实际读取证明，再唯一投影到最终共享账本；不能替伪证明修补原字。"""
    verified = verify_relation_proof(proof, (context,), context_ledger(context))
    if verified is None:
        return None
    current = {key: value for key, value in verified.items() if key != "admitted_evidence_ids"}
    actual = {row.get("chunk_id") for row in evidence}
    current["admitted_chunk_ids"] = [key for key in current["related_chunk_ids"] if key in actual]
    current["witness"] = [token for token in current["witness"]
        if token["purpose"] != "TABLE_CAPTION"
        or all(span["chunk_id"] in actual for span in token["spans"])]
    if len(current["admitted_chunk_ids"]) < current["expected_fragment_count"]:
        current["status"] = "BUDGET_EXCLUDED"
    return verify_relation_proof(current, (context,), evidence)
