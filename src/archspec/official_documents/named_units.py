"""从固定原行核对无数字条号的连续计算单元，不把目录或标签当正文。"""

import re

from archspec.official_documents.processing_cache import checked_digest

MARKER = re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]")


def named_unit(rows, selected_index):
    """实际父标题、括号编号及连续路径共同证明，重复位置不跨段拼接。

    单元编号只表示该快照中的首片段位置；clause_number原值不改变。
    摘录许可与来源核对仍由调用方处理，这个函数不批准文字正确性。
    """
    selected = rows[selected_index]
    # None表示正文没有标题，仍保留普通阅读入口，不虚构命名单元。
    heading = selected.get("heading_path") or ""
    if selected.get("clause_number") is not None or " > " not in heading:
        return None
    parent, leaf = heading.rsplit(" > ", 1)
    marker = MARKER.match(leaf)
    if not marker:
        return None
    start = selected_index
    while start > 0 and rows[start - 1].get("heading_path") == heading and rows[start - 1].get("clause_number") is None:
        start -= 1
    end = selected_index + 1
    while end < len(rows) and rows[end].get("heading_path") == heading and rows[end].get("clause_number") is None:
        end += 1
    text = rows[start]["text"]
    compact = re.sub(r"\s+", "", text)
    if (not compact.startswith(re.sub(r"\s+", "", leaf))
            or re.search(r"[.…⋯·]{2,}\d", compact)
            or not re.search(r"按照|不得|必须|应|规定|[。；]", text[marker.end():])):
        return None
    # 同一路径出现另一个实际编号开头时，不能猜它是续文。
    if any(MARKER.match(rows[i]["text"].strip()) for i in range(start + 1, end)):
        return None
    parent_title = parent.rsplit(" > ", 1)[-1]
    parent_index = start - 1
    while parent_index >= 0:
        row = rows[parent_index]
        if row.get("heading_path") == parent and row["text"].strip() == parent_title:
            return {"indices": list(range(start, end)), "reading_unit_key": f"named:{start}",
                    "reading_unit_label": f"{parent_title} {marker[0]}", "heading_index": parent_index}
        if not str(row.get("heading_path", "")).startswith(parent + " > "):
            break
        parent_index -= 1
    return None


def read_named_contexts(service, source_id, snapshot_id, parent_title, *, max_units=3):
    """只在指定固定正文读一次真实父标题，缺失或重复不根据标签猜位置。"""
    if type(max_units) is not int or not 1 <= max_units <= 3:
        raise ValueError("命名单元读取上限无效")
    checked_digest(snapshot_id)
    source = service._source(source_id)
    if source.get("kind") != "FULL_TEXT":
        raise ValueError("附件不是完整正文")
    record, _ = service._load_snapshot(service.cache_dir / source_id / f"{snapshot_id}.json", source_id)
    service._check_cached_file(record, source_id, validated=True)
    for operation in ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
        service._check_read(source_id, snapshot_id, operation)
    rows = record["chunks"]
    anchors = [i for i, row in enumerate(rows) if row["text"].strip() == parent_title]
    if len(anchors) != 1:
        return [], ["计算父标题未取得或在固定正文中重复，未猜选对应附录。"]
    anchor = anchors[0]
    parent = rows[anchor]["heading_path"]
    indices, keys, gaps = [], set(), []
    for i in range(anchor + 1, len(rows)):
        row = rows[i]
        if not row["heading_path"].startswith(parent + " > "):
            break
        unit = named_unit(rows, i)
        if not unit or unit["heading_index"] != anchor or unit["reading_unit_key"] in keys:
            continue
        keys.add(unit["reading_unit_key"])
        indices.append(unit["indices"][0])
    if not indices:
        return [], ["计算父标题下尚未取得可核对的完整计算单元。"]
    if len(indices) > max_units:
        # 不只读前三项却告诉调用方整个计算附录已齐。
        return [], ["计算附录超过三单元初读上限，尚未完整纳入，请继续对照固定原页。"]
    contexts = [service.read_context(source_id, snapshot_id, f"{source_id}:{snapshot_id}:{i}") for i in indices]
    return contexts, gaps
