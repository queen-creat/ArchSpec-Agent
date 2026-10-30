"""有限原表行列绑定：原始读数不等于经地区修正的项目设计值。"""

import hashlib
import json
import re
from copy import deepcopy

from archspec.official_documents.page_text import _pixel_bounds, native_lines
from archspec.official_documents.reading_layout import _box
from archspec.official_documents.table_context import compact
from archspec.official_qa.garage_conditions import garage_initial_issues
from archspec.standard_governance.compatibility import ContentUseDenied


def _readable(lines):
    # 未知坐标、来源及低置信度文字可以继续阅读，但不能用来组织数值卡片。
    return bool(lines) and all(line.get("box") and (line.get("origin") == "TEXT" or
        (line.get("origin") == "OCR" and type(line.get("score")) in {int, float}
         and .9 <= line["score"] <= 1)) for line in lines)


def _text(cell):
    return "".join(line["text"].strip() for line in cell["lines"])


def _caption(documents, source, snapshot, page_number, expected, tables):
    # 同一个固定页读取边界验证指纹和原页许可；不读取latest，不调用OCR。
    with documents._fixed_page(source, snapshot, page_number) as (record, page):
        documents._check_read(source, snapshot, "LOCAL_EXCERPT")
        saved = record["pages"][page_number - 1]
        lines = saved.get("raw_lines", [])
        if saved.get("method") == "TEXT" and not any(line.get("layout_box") for line in lines):
            lines = native_lines(page)
        found = []
        scale = documents._legacy_reading_scale(record)
        for index, line in enumerate(lines):
            if (not line.get("included_in_text", True)
                    or compact(line.get("text", "")) != compact(expected)):
                continue
            bounds = line.get("layout_box")
            if bounds is None and saved.get("method") == "OCR" and scale is not None:
                bounds = _pixel_bounds(line.get("box"), scale)
            value = {"line_index": index, "text": line["text"],
                "box": _box(bounds, page.rect.width, page.rect.height),
                "origin": line.get("origin", "UNKNOWN"), "score": line.get("score")}
            if _readable([value]):
                found.append(value)
    if len(found) != 1:
        return None
    caption = found[0]
    targets = []
    for table in tables:
        grid = table.get("reading_grid")
        if not grid or grid.get("status") != "GEOMETRY_CANDIDATE" or not grid["cells"]:
            continue
        top = min(cell["box"][1] for cell in grid["cells"])
        x0, _, x1, _ = caption["box"]
        # 表题只对应紧邻其下的唯一网格；另一份表不会因在同页就被借用。
        if 0 <= top - caption["box"][3] <= .08 and table["box"][0] <= x0 < x1 <= table["box"][2]:
            targets.append(table)
    return (caption, targets[0]) if len(targets) == 1 else None


def _cells(table, mode):
    grid = table["reading_grid"]
    cells = grid["cells"]
    rooms = [cell for cell in cells if _readable(cell["lines"])
        and "办公室" in re.split(r"[、,，/；;]", compact(_text(cell)))]
    if len(rooms) != 1:
        return None
    room = rooms[0]
    modes = [cell for cell in cells if _text(cell) == mode + "采光"
        and cell["row"] + cell["row_span"] <= room["row"] and _readable(cell["lines"])]
    if len(modes) != 1:
        return None
    parent = modes[0]
    headers = [cell for cell in cells if cell is not parent and _readable(cell["lines"])
        and "采光系数" in _text(cell) and "标准值" in _text(cell) and "(%)" in _text(cell)
        and parent["column"] <= cell["column"]
        and cell["column"] + cell["column_span"] <= parent["column"] + parent["column_span"]
        and parent["row"] + parent["row_span"] <= cell["row"]
        and cell["row"] + cell["row_span"] <= room["row"] and cell["column_span"] == 1]
    if len(headers) != 1:
        return None
    header = headers[0]
    values = [cell for cell in cells if cell["row"] == room["row"]
        and cell["column"] == header["column"] and cell["row_span"] == cell["column_span"] == 1
        and _readable(cell["lines"]) and len(cell["lines"]) == 1
        and re.fullmatch(r"\d+(?:\.\d+)?", _text(cell))]
    if len(values) != 1 or room["row_span"] != 1:
        return None
    needed = [room, parent, header, values[0]]
    for line in grid.get("unassigned_lines", []):
        box = line.get("box")
        # 关键单元格附近有无法归格文字时不凑答案；全页无位置文字也不能忽略。
        if box is None or any(box[0] < cell["box"][2] and box[2] > cell["box"][0]
            and box[1] < cell["box"][3] and box[3] > cell["box"][1] for cell in needed):
            return None
    return room, [parent, header], values[0]


def _garage_type(conditions):
    """只取明确的一种表列类型；半地下不能误落入地下列，地上不能推成多层。"""
    if conditions.get("building_use") != "汽车库" or garage_initial_issues(conditions):
        return None
    context = conditions["fire_context"]
    if re.search(r"(?:非|不是|并非|不属于)\s*(?:单层|多层|高层|半地下|地下)", context):
        return None
    kinds = set(re.findall(r"半地下|(?<!半)地下|单层|多层|高层", context))
    if len(kinds) != 1:
        return None  # 即使某两种类型同列，也不把“或”当成已确认的项目条件。
    return next(iter(kinds))


def _garage_cells(table, conditions, kind):
    """行列关系来自物理网格；面积只读格中文字，不用条文扁平顺序猜数字。"""
    cells = table["reading_grid"]["cells"]
    grade = re.search(r"([一二三四1234])级", conditions["fire_resistance"])[1]
    grade = {"1": "一", "2": "二", "3": "三", "4": "四"}.get(grade, grade)
    grade_headers = [cell for cell in cells if _readable(cell["lines"])
                     and _text(cell) == "耐火等级" and cell["column_span"] == 1]
    headers = [cell for cell in cells if _readable(cell["lines"])
               and kind + "汽车库" in re.split(r"[、,，]", _text(cell))
               and cell["column_span"] == 1]
    if len(grade_headers) != 1 or len(headers) != 1:
        return None
    label, header = grade_headers[0], headers[0]
    if label["row"] != header["row"] or label["column"] == header["column"]:
        return None
    rows = [cell for cell in cells if _readable(cell["lines"])
            and cell["column"] == label["column"] and cell["column_span"] == cell["row_span"] == 1
            and cell["row"] >= max(label["row"] + label["row_span"], header["row"] + header["row_span"])
            and re.fullmatch(r"[一二三四](?:、[一二三四])*级", _text(cell))
            and grade in _text(cell).removesuffix("级").split("、")]
    if len(rows) != 1:
        return None
    row = rows[0]
    values = [cell for cell in cells if cell["row"] == row["row"]
              and cell["column"] == header["column"] and cell["row_span"] == cell["column_span"] == 1
              and _readable(cell["lines"]) and len(cell["lines"]) == 1
              and re.fullmatch(r"[1-9]\d*(?:\.\d+)?", _text(cell))]
    if len(values) != 1:
        return None  # “不允许”保留原文，不伪造成面积0或随便挑相邻允许格。
    needed = [row, label, header, values[0]]
    for line in table["reading_grid"].get("unassigned_lines", []):
        box = line.get("box")
        if box is None or any(box[0] < cell["box"][2] and box[2] > cell["box"][0]
            and box[1] < cell["box"][3] and box[3] > cell["box"][1] for cell in needed):
            return None
    return row, [label, header], values[0]


def build_table_readings(documents, conditions, groups, evidence):
    """最多读三页/输出四条；复用同一固定原表边界，读数不是项目最终取值。"""
    garage = _garage_type(conditions)
    mode = conditions.get("daylight_mode", "").removesuffix("采光")
    if not garage and (conditions.get("daylight_space") not in {"普通办公室", "办公室"}
                       or mode not in {"侧面", "顶部"}):
        return []
    ledger = {row["evidence_id"]: row for row in evidence}
    readings, layouts, seen = [], {}, set()
    for group in groups:
        anchor = ledger.get(group["anchor_evidence_id"])
        if not anchor or not anchor.get("quote_ready") or anchor["role"] != "MATCH":
            continue
        for entry in group["table_pages"]:
            owner = ledger.get(entry["evidence_id"])
            if not owner:
                continue
            key = (group["source_id"], group["snapshot_id"], entry["pdf_page"], owner["chunk_id"])
            if key not in layouts and len(layouts) >= 3:
                continue
            try:
                if key not in layouts:
                    layouts[key] = documents.page_layout(*key)
                matched = _caption(documents, *key[:3], entry["caption"], layouts[key]["tables"])
            except (LookupError, ValueError, OSError, ContentUseDenied):
                continue  # 原表不可核对时仍保留原来的条款阅读，不伪造读数。
            if not matched:
                continue
            if garage and not re.search(r"汽车库防火分区.*最大允许建筑面积\(m[²2]\)", compact(entry["caption"])):
                continue  # 相似表或缺单位不提供车库读数。
            bound = _garage_cells(matched[1], conditions, garage) if garage else _cells(matched[1], mode)
            if not bound:
                continue
            caption, table = matched
            room, headers, value = bound
            # 发送/显示的每个单元格文字必须已经在本次有限证据中，不能绕预算读全书。
            texts = [caption["text"], *(line["text"] for cell in [room, *headers, value]
                                        for line in cell["lines"])]
            if not all(compact(text) in compact(owner["text"]) for text in texts):
                continue
            result = {"status": "ORIGINAL_TABLE_READING", "source_id": key[0], "snapshot_id": key[1],
                "pdf_page": key[2], "chunk_id": key[3], "evidence_id": owner["evidence_id"],
                "anchor_evidence_id": anchor["evidence_id"], "table_number": group["table_number"],
                "caption": entry["caption"], "caption_line": caption, "table_id": table["id"],
                "row_label": _text(room), "column_label": " / ".join(_text(cell) for cell in headers),
                "value": _text(value), "row_cell": room, "header_cells": headers, "value_cell": value,
                "scope": "BASE_TABLE_ONLY", "is_project_value": False}
            if garage:
                result.update(reading_kind="GARAGE_AREA_BASELINE", unit="m²")
            identity = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            if identity not in seen:
                seen.add(identity)
                readings.append({"id": "tr_" + identity, **deepcopy(result)})
            if len(readings) >= 4:
                return readings
    return readings


def table_ink_issues(documents, contexts, evidence):
    """原表引用与格内未读字迹取交集；同页其它表不能阻断当前条文。"""
    from archspec.official_documents.table_cells import unread_ink_cells
    from archspec.official_qa.table_groups import build_table_groups

    ledger = {row["evidence_id"]: row for row in evidence}
    layouts, inspected, issues = {}, {}, []
    for group in build_table_groups(contexts, evidence):
        for entry in group["table_pages"]:
            owner = ledger.get(entry["evidence_id"])
            if owner is None:
                continue
            key = (group["source_id"], group["snapshot_id"], entry["pdf_page"])
            if key not in layouts and len(layouts) >= 3:
                continue
            try:
                if key not in layouts:
                    layouts[key] = documents.page_layout(*key, owner["chunk_id"])
                matched = _caption(documents, *key, entry["caption"], layouts[key]["tables"])
                if not matched:
                    continue
                table = matched[1]
                identity = (*key, tuple(table["box"]))
                if identity not in inspected:
                    with documents._fixed_page(*key) as (_, page):
                        inspected[identity] = unread_ink_cells(page, table.get("reading_grid"))
                if inspected[identity]:
                    issues.append({"source_id": key[0], "snapshot_id": key[1], "pdf_page": key[2],
                        "anchor_evidence_id": group["anchor_evidence_id"],
                        "table_number": group["table_number"], "state": "UNREAD_CELL_INK",
                        "method": "FIXED_PAGE_GRID_INK_V1", "cells": inspected[identity]})
            except (LookupError, ValueError, OSError, ContentUseDenied):
                continue  # 未能核格仍为未知，不伪造缺值、空白或数字。
    return issues
