"""唯一证据关系词法与原字位置证明；不读取文件、不推断适用性或提高预算。"""

import re
from copy import deepcopy

from archspec.documents.chunker import ARTICLE_NUMBER
from archspec.official_documents.clause_units import CLAUSE as BODY
from archspec.official_documents.clause_units import clause_runs

REFERENCE = re.compile(r"本(?:规范|标准)第(\d+(?:\.\d+){2,3})条")
# 条号和表号分开：保留实际的点号或连字符，不把“6-2”改写成另一表。
# 附表身份保留“附表”原字，不与普通表二或表2互换。
NUMERIC_TABLE = r"(?:\d+(?:\.\d+){2,3}|\d+-\d+)"
APPENDIX_TABLE = r"附表(?:[一二三四五六七八九十百千万零〇]+|\d{1,3})"
TABLE_ID = rf"(?:{NUMERIC_TABLE}|{APPENDIX_TABLE})"
TABLE_NUMBER = re.compile(TABLE_ID)
TABLE_PREFIX = r"(?:表(?=\d)|(?=附表))"
TABLE = re.compile(rf"{TABLE_PREFIX}({TABLE_ID})(?![\d.\-一二三四五六七八九十百千万零〇])")
CAPTION = re.compile(rf"^(续)?{TABLE_PREFIX}({TABLE_ID})(?![\d.\-一二三四五六七八九十百千万零〇])(.+)$")


def table_label(number):
    """编号作内部身份，阅读标签保留附表名称，不重复添加表字。"""
    return number if number.startswith("附表") else "表" + number


# 地方规定常用“按6.3.10条”省略本书和“第”；仍须明确条号、条字及原字证明。
SEED = re.compile(rf"本(?:规范|标准|规定)|按(?=第?\d+(?:\.\d+){{2,3}}条|{ARTICLE_NUMBER})")
ITEM = re.compile(rf"第?(\d+(?:\.\d+){{2,3}})条|({ARTICLE_NUMBER})")
CONNECTOR = re.compile(r"以及|[、，,]|及|和|与|或")
DIRECTORY = re.compile(r"[.…⋯·]{2,}\s*\d")
FINAL_FIELDS = ("kind", "direction", "source_id", "snapshot_id", "anchor_unit_key",
                "origin_unit_key", "target_unit_key", "related_unit_key", "origin_chunk_ids",
                "related_chunk_ids", "expected_fragment_count", "witness")


def compact(text):
    return re.sub(r"\s+", "", text)


def _caption_matches(text):
    """独立表题可分行；附表号独占一行时，只接受紧随的真实短题名。"""
    lines = text.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        literal = line.strip()
        title = compact(literal)
        count = 1
        if re.fullmatch(rf"(?:续)?{APPENDIX_TABLE}", title) and index + 1 < len(lines):
            following = lines[index + 1].strip()
            if (following and len(following) <= 120
                    and not re.match(r"[\d（(]|的|规定|所示|中|应|要求|内|附表|续附表|第", following)
                    and not re.search(r"[。；;！？!?]|[.…⋯·]{2,}", following)):
                literal += "\n" + following
                title = compact(literal)
                count = 2
        match = CAPTION.fullmatch(title)
        if (match and not re.search(r"[.…⋯·]{2,}", literal)
                and not match[3].startswith(("的", "规定", "所示", "中", "应", "要求", "内"))):
            start = offset + len(line) - len(line.lstrip())
            end = offset + sum(len(value) for value in lines[index:index + count])
            end -= len(lines[index + count - 1]) - len(lines[index + count - 1].rstrip())
            yield {"table_number": match[2], "caption": literal,
                   "continuation": bool(match[1])}, start, end
        offset += len(line)


def captions(text):
    """独立真实表题；引用句、目录和空题名不充当表题。"""
    return [value for value, _, _ in _caption_matches(text)]


def _scope(source_id, snapshot_id):
    if source_id is None and snapshot_id is None:
        return
    if (not isinstance(source_id, str)
            or not re.fullmatch(r"gb55031|official-[a-f0-9]{32}", source_id)
            or not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_id)):
        raise ValueError("关系固定来源身份不合法")


def _chunk_id(source_id, snapshot_id, index):
    return f"{source_id}:{snapshot_id}:{index}" if source_id is not None else index


def _fragments(source_id, snapshot_id, rows):
    result, seen = [], set()
    for position, row in enumerate(rows):
        identifier = row.get("id", row.get("chunk_id"))
        index = row.get("index", position)
        if isinstance(identifier, str):
            if source_id is not None and not identifier.startswith(f"{source_id}:{snapshot_id}:"):
                raise ValueError("关系原字片段越过固定来源")
            suffix = identifier.rsplit(":", 1)[-1]
            if not re.fullmatch(r"0|[1-9]\d{0,5}", suffix):
                raise ValueError("关系片段位置不合法")
            index = int(suffix)
        if type(index) is not int or not 0 <= index <= 999999 or index in seen:
            raise ValueError("关系片段位置无效或重复")
        if not isinstance(row.get("text"), str):
            raise TypeError("关系片段没有原字")
        for field, expected in (("source_id", source_id), ("snapshot_id", snapshot_id)):
            if field in row and expected is not None and row[field] != expected:
                raise ValueError("关系原字片段归属不一致")
        seen.add(index)
        number = row.get("clause_number")
        unit = row.get("reading_unit_key") or (number if BODY.fullmatch(number or "")
                                             else f"chunk:{index}")
        selected = (row.get("selected") is True if "selected" in row
                    else row.get("role") in {"MATCH", "CONTINUATION"})
        result.append({**row, "index": index, "id": _chunk_id(source_id, snapshot_id, index),
                       "unit_key": unit, "selected": selected})
    return sorted(result, key=lambda row: row["index"])


def _stream(rows):
    """空白仅作词法忽略；每个非空白码点保留原片段和真实字符偏移。"""
    text, positions = [], []
    for row in rows:
        start = row.get("_scan_start", 0)
        end = row.get("_scan_end", len(row["text"]))
        for offset in range(start, end):
            character = row["text"][offset]
            if not character.isspace():
                text.append(character)
                positions.append((row, offset))
    return "".join(text), positions


def _spans(positions, start, end):
    spans = []
    for row, offset in positions[start:end]:
        if spans and spans[-1]["chunk_id"] == row["id"]:
            spans[-1]["end"] = offset + 1
            spans[-1]["text"] = row["text"][spans[-1]["start"]:offset + 1]
        else:
            spans.append({"chunk_id": row["id"], "start": offset, "end": offset + 1,
                          "text": row["text"][offset:offset + 1]})
    return spans


def _token(purpose, positions, start, end, number=None):
    value = {"purpose": purpose, "spans": _spans(positions, start, end)}
    if number is not None:
        value["number"] = number
    return value


def _runs(rows):
    """目录只隔离实际行，不抹去同片段的正文，也不跨目录继承种子。"""
    runs, current = [], []
    for row in rows:
        if (current and (row["index"] != current[-1]["index"] + 1
                         or row.get("unit_complete") is False
                         or current[-1].get("unit_complete") is False)):
            runs.append(current)
            current = []
        if not DIRECTORY.search(row["text"]):
            current.append(row)
            continue
        offset, start = 0, 0
        for line in row["text"].splitlines(keepends=True):
            if DIRECTORY.search(line):
                if start < offset:
                    current.append({**row, "_scan_start": start, "_scan_end": offset})
                if current:
                    runs.append(current)
                    current = []
                start = offset + len(line)
            offset += len(line)
        if start < offset:
            current.append({**row, "_scan_start": start, "_scan_end": offset})
    if current:
        runs.append(current)
    return runs


def _clause_mentions(rows):
    found = []
    for run in _runs(rows):
        text, positions = _stream(run)
        for seed in SEED.finditer(text):
            # 书名内部文字不建立同书种子；完整显式种子可独立重新开始。
            before = text[:seed.start()]
            if before.rfind("《") > before.rfind("》"):
                continue
            # 省略本书的引用不能继承本句他书书名；只在同一固定快照定位一跳。
            if seed.group() == "按" and "《" in re.split(r"[。；;！？!?]", before)[-1]:
                continue
            first = ITEM.match(text, seed.end())
            if not first:
                continue
            chain = [_token("SAME_BOOK_SEED", positions, seed.start(), seed.end()),
                     _token("REFERENCE_ITEM", positions, first.start(), first.end(), (first[1] or first[2]))]
            found.append({"number": (first[1] or first[2]), "witness": deepcopy(chain)})
            cursor = first.end()
            while True:
                connector = CONNECTOR.match(text, cursor)
                if not connector:
                    break
                following = ITEM.match(text, connector.end())
                if not following:
                    break
                chain += [_token("LIST_CONNECTOR", positions, connector.start(), connector.end()),
                          _token("REFERENCE_ITEM", positions, following.start(), following.end(),
                                 (following[1] or following[2]))]
                found.append({"number": (following[1] or following[2]), "witness": deepcopy(chain)})
                cursor = following.end()
    return found


def reference_mentions(text):
    return _clause_mentions([{"index": 0, "id": 0, "text": text}])


def reference_numbers(text):
    return list(dict.fromkeys(mention["number"] for mention in reference_mentions(text)))


def _table_mentions(rows):
    found = []
    for run in _runs(rows):
        text, positions = _stream(run)
        for match in TABLE.finditer(text):
            before = re.split(r"[。；;！？!?]", text[:match.start()])[-1]
            # 未写他书的表引用保持旧范围；书名内部表号不是正文表引用。
            if before.rfind("《") > before.rfind("》"):
                continue
            if "《" in before and before.rfind("本规范") < before.rfind("》") and (
                    before.rfind("本标准") < before.rfind("》")):
                continue
            found.append({"number": compact(match[1]), "witness": [
                _token("TABLE_REFERENCE", positions, match.start(), match.end(), compact(match[1]))]})
    return found


def table_mentions(text):
    return _table_mentions([{"index": 0, "id": 0, "text": text}])


def caption_mentions(text, chunk_id=0):
    """分行表题的原字仍以真实码点偏移绑定在同一固定片段。"""
    return [{**value, "token": {
        "purpose": "TABLE_CAPTION", "number": value["table_number"], "spans": [
            {"chunk_id": chunk_id, "start": start, "end": end, "text": text[start:end]}]}}
        for value, start, end in _caption_matches(text)]


def unconfirmed_table_parents(text, clause_number):
    """表题不是正文条号证明；异号且无正文引用时不沿用上一条的摘录资格。"""
    if not re.fullmatch(r"\d+(?:\.\d+){2,3}", clause_number or ""):
        return ()
    found = caption_mentions(text)
    caption_spans = [span for item in found for span in item["token"]["spans"]]
    references = set()
    for mention in table_mentions(text):
        spans = [span for token in mention["witness"] for span in token["spans"]]
        if any(not any(caption["start"] <= span["start"] and span["end"] <= caption["end"]
                       for caption in caption_spans) for span in spans):
            references.add(mention["number"])
    return tuple(dict.fromkeys(item["table_number"] for item in found
        if item["table_number"] != clause_number and item["table_number"] not in references))


def _view(proof, anchor):
    result = deepcopy(proof)
    if anchor == result["origin_unit_key"]:
        direction, related, ids = "OUTGOING", result["target_unit_key"], result["_target_chunk_ids"]
    elif result["kind"] == "CLAUSE_REFERENCE" and anchor == result["target_unit_key"]:
        direction, related, ids = "INCOMING", result["origin_unit_key"], result["origin_chunk_ids"]
    else:
        raise ValueError("关系锚点不属于实际方向")
    result.update(anchor_unit_key=anchor, direction=direction, related_unit_key=related,
                  related_chunk_ids=list(ids), expected_fragment_count=len(ids))
    result["_anchor_chunk_ids"] = (result["origin_chunk_ids"] if direction == "OUTGOING"
                                    else result["_target_chunk_ids"])
    return result


def discover_relations(source_id, snapshot_id, ordered_fragments):
    """发现物理edge草稿；selected仅绑定视图，预算须由admitted阶段绑定。"""
    _scope(source_id, snapshot_id)
    rows = _fragments(source_id, snapshot_id, ordered_fragments)
    # 原字证明保留公开条号；内部按实际连续位置分组，避免把远处同号拼成一句。
    runs = clause_runs(rows)
    units = {}
    for row in rows:
        key = (row["unit_key"], runs[row["index"]][0] if row["index"] in runs else None)
        units.setdefault(key, []).append(row)
    selected = {key for key, core in units.items() if any(row["selected"] for row in core)}
    caption_targets = {}
    for row in rows:
        for caption in caption_mentions(row["text"], row["id"]):
            caption_targets.setdefault(caption["table_number"], []).append((row, caption["token"]))
    result, seen = [], set()
    for origin_key, core in units.items():
        origin = origin_key[0]
        mentions = []
        if BODY.fullmatch(origin or ""):
            mentions.extend(("CLAUSE_REFERENCE", mention) for mention in _clause_mentions(core))
        mentions.extend(("TABLE_REFERENCE", mention) for mention in _table_mentions(core))
        for kind, mention in mentions:
            number = mention["number"]
            target = number if kind == "CLAUSE_REFERENCE" else f"table:{number}"
            if kind == "CLAUSE_REFERENCE" and target == origin:
                continue
            identity = kind, origin_key, target
            if identity in seen:
                continue
            seen.add(identity)
            if kind == "CLAUSE_REFERENCE":
                targets = [(key, parts) for key, parts in units.items() if key[0] == target
                           and not any(DIRECTORY.search(row["text"]) for row in parts)]
                # 同号多处时不猜正文/附表的引用方向；保留未定位引用与原页入口。
                target_key, target_rows = targets[0] if len(targets) == 1 else (None, [])
                target_ids = [row["id"] for row in target_rows]
                witness = mention["witness"]
            else:
                targets = caption_targets.get(number, [])
                target_ids = list(dict.fromkeys(row["id"] for row, _ in targets))
                witness = [*mention["witness"], *(token for _, token in targets)]
            proof = {
                "version": "EVIDENCE_RELATION_DRAFT_V1", "kind": kind, "direction": None,
                "source_id": source_id, "snapshot_id": snapshot_id, "anchor_unit_key": None,
                "origin_unit_key": origin, "target_unit_key": target, "related_unit_key": None,
                "origin_chunk_ids": [row["id"] for row in core],
                "related_chunk_ids": [], "expected_fragment_count": len(target_ids),
                "admitted_chunk_ids": [], "status": None, "coverage": "READ_CONTEXT_ONLY",
                "witness": witness, "_target_chunk_ids": target_ids,
            }
            if not selected:
                result.append(proof)
            elif origin_key in selected and (kind != "CLAUSE_REFERENCE" or target_key not in selected):
                result.append(_view(proof, origin))
            elif kind == "CLAUSE_REFERENCE" and target_key in selected and origin_key not in selected:
                result.append(_view(proof, target))
    return result


def bind_relation_proof(proof, admitted_chunk_ids, anchor_unit_key, *, source_id=None,
                        snapshot_id=None):
    """只绑定实际预算身份；输出成品不包含草稿私有定位字段。"""
    source = source_id if source_id is not None else proof.get("source_id")
    snapshot = snapshot_id if snapshot_id is not None else proof.get("snapshot_id")
    _scope(source, snapshot)
    if source is None:
        raise ValueError("成品关系缺少固定来源身份")
    if (proof.get("source_id") not in {None, source}
            or proof.get("snapshot_id") not in {None, snapshot}):
        raise ValueError("关系绑定不能改变固定来源")

    def bound(value):
        if type(value) is int and 0 <= value <= 999999:
            return _chunk_id(source, snapshot, value)
        if isinstance(value, str) and value.startswith(f"{source}:{snapshot}:") and re.fullmatch(
                r"0|[1-9]\d{0,5}", value.rsplit(":", 1)[-1]):
            return value
        raise ValueError("关系证明片段不属于固定来源")

    current = _view(proof, anchor_unit_key)
    current.update(source_id=source, snapshot_id=snapshot)
    for name in ("origin_chunk_ids", "related_chunk_ids", "_anchor_chunk_ids"):
        current[name] = [bound(value) for value in current[name]]
    for token in current["witness"]:
        for span in token["spans"]:
            span["chunk_id"] = bound(span["chunk_id"])
    admitted = {bound(value) for value in admitted_chunk_ids}
    if current["kind"] == "TABLE_REFERENCE":
        # 未纳入的原表题只能保留定位count，不能把其原字span越过本次读取预算传出。
        current["witness"] = [token for token in current["witness"]
            if token["purpose"] != "TABLE_CAPTION"
            or all(span["chunk_id"] in admitted for span in token["spans"])]
    related = current["related_chunk_ids"]
    included = [value for value in related if value in admitted]
    witness_ids = {span["chunk_id"] for token in current["witness"] for span in token["spans"]}
    status = ("NOT_LOCATED" if not related else
              "BUDGET_EXCLUDED" if len(included) != len(related) else
              "PARTIAL" if not set(current["origin_chunk_ids"]) <= admitted
              or not set(current["_anchor_chunk_ids"]) <= admitted
              or not witness_ids <= admitted else "LOCATED")
    return {key: current[key] for key in FINAL_FIELDS} | {
        "version": "EVIDENCE_RELATION_V1", "admitted_chunk_ids": included,
        "status": status, "coverage": "READ_CONTEXT_ONLY"}
