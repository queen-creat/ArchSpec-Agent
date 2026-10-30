"""完整固定引用之后的条件定位；不生成事实、不改原文、不计算面积。"""

import re

from archspec.official_qa.intents import fire_area_question
from archspec.official_qa.local_spacing import evidence_key, unit_key

VERSION = "FIXED_EVIDENCE_BRIEF_V1"
NUMBERED = re.compile(r"(?m)^[ \t]*(\d{1,2})(?:(?P<punct>[.．](?!\d)|、)[ \t]*|[ \t]+)(?=\S)")


def _numbered_markers(full):
    """同一条内连续的一级编号；子括号编号、页脚单整数不另起分支。"""
    candidates = list(NUMBERED.finditer(full))
    # 沿本条实际编号分隔方式判界；“24 米”这类折行数值不混入“1．”分项。
    punctuated = bool(candidates and candidates[0]["punct"])
    markers = [marker for marker in candidates if bool(marker["punct"]) == punctuated]
    # 另一种分隔方式若仍像本列表编号，结构不确定；不把末项误并到前项。
    if any(bool(marker["punct"]) != punctuated and 1 <= int(marker[1]) <= len(markers) + 1
           for marker in candidates):
        return []
    return markers if 2 <= len(markers) <= 16 and [int(m[1]) for m in markers] == list(range(1, len(markers) + 1)) else []


def _branch_outline(full):
    """按原编号完整分项，仅供阅读；不推断哪项适用，不裁掉子项或末尾限制。"""
    markers = _numbered_markers(full)
    if not markers or re.search(r"表\s*\d", full):
        return None
    scope = full[:markers[0].start()]
    if not re.search(r"(?:下列|以下|要求|规定|如下)[^。；:：]{0,16}[:：]$", re.sub(r"\s+", "", scope)):
        return None
    rows = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(full)
        text = full[marker.start():end]
        colon = re.search(r"[:：]", text)
        title = (text[:colon.end()] if colon and colon.end() <= 180
                 and not re.search(r"[。；;]", text[:colon.start()]) else "")
        rows.append({"number": int(marker[1]), "start": marker.start(), "end": end,
                     "text": text, "title": title})
    return {"version": "NUMBERED_ORIGINAL_V1", "scope": {
        "start": 0, "end": markers[0].start(), "text": scope}, "items": rows}


def _layer(text):
    """混合、否定或不确定的问法不选一个层型，仍提供整条原文。"""
    if any(word in text for word in ("不是", "非高层", "不清楚", "未知", "不确定")):
        return None
    found = {kind for kind, words in {
        "MULTI": ("单层", "多层"), "HIGH": ("高层",), "UNDERGROUND": ("地下", "半地下"),
    }.items() if any(word in text for word in words)}
    return next(iter(found)) if len(found) == 1 else None


def _segments(query, conditions, full):
    target = _layer(query)
    context = conditions.get("fire_context", "")
    if target is None or ("地下" in context and target != "UNDERGROUND"):
        return []
    if target == "UNDERGROUND" and "地上" in context:
        return []
    markers = _numbered_markers(full)
    # 编号断裂意味着可能漏识别某个分支；不把它并入前一段后继续裁选。
    if not markers:
        return []
    scope = full[:markers[0].start()]
    compact = re.sub(r"\s+", "", scope)
    if (not compact.endswith("下列规定：") or "防火分区" not in compact
            or "面积" not in compact
            or not any(word in compact for word in ("公共建筑", "住宅", "厂房", "仓库", "车库"))
            or re.search(r"表\s*\d", full)):
        return []
    pieces = [{"start": 0, "end": markers[0].start(), "text": scope, "role": "SCOPE"}]
    matched = False
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(full)
        text = full[marker.start():end]
        content = re.sub(r"\s+", "", full[marker.end():end])
        # 只收起主句明确限定其他层型的段。修正、例外、局部设置和无法分类
        # 的段一律保留；整段内多个耐火等级不能被裁成一个数字。
        kind = _layer(content) if content.startswith("对于") and not any(
            word in content for word in ("当", "除", "局部", "全部", "但", "增加", "折减", "特殊")) else None
        if kind is not None and kind != target:
            continue
        role = "CONDITION" if kind == target else "RESTRICTION"
        matched |= role == "CONDITION"
        pieces.append({"start": marker.start(), "end": end, "text": text, "role": role})
    return pieces if matched else []


def build_answer_brief(query, conditions, items):
    """输入只能是服务端已逐字核对的完整引用；区间按 Unicode 字符计数。

    合并身份包含书与固定快照。前端按同样的 chunk 顺序连接全文，再独立
    核对每段区间；完整引用与每段原页入口不因短答而消失。
    """
    grouped = {}
    for item in items:
        citation = item["citation"]
        # 真实空条号规则按固定阅读单元分开；同名和None均不能证明同一单元。
        key = evidence_key(citation)
        grouped.setdefault(key, []).append(item)
    groups = []
    for (source, snapshot, _unit), parts in grouped.items():
        parts.sort(key=lambda item: int(item["citation"]["chunk_id"].rsplit(":", 1)[1]))
        citation = parts[0]["citation"]
        clause = citation["clause_number"]
        # 名称直接沿用已核对的引用，保留原括号编号，绝不补造数字条号。
        named = str(unit_key(citation) or "").startswith("named:")
        unit_fields = ({key: citation[key] for key in ("reading_unit_key", "reading_unit_label")}
                       if named else {})
        full = "\n".join(item["quote"] for item in parts)
        mode, reason, segments = "FULL_CLAUSE", "条件结构未明确分开，请展开整条并对照原页。", []
        warnings = {warning for item in parts for warning in item["citation"].get("warnings", [])}
        if "UNRECOGNIZED_IMAGES" in warnings:
            mode, reason = "ORIGINAL_REQUIRED", "已取得文字层，同页图片待辨认；图片中的数值、表格和例外请对照原页。"
        elif "OCR_NUMBERED_ITEM_GAP" in warnings:
            mode, reason = "ORIGINAL_REQUIRED", "条目编号不连续，请对照原页中的缺失条目，不按片段裁选条件。"
        elif "OCR_GAP_RECOVERED_NEEDS_REVIEW" in warnings:
            mode, reason = "ORIGINAL_REQUIRED", "局部补识别已完成，请核对补识别文字；整条原文与原页保留。"
        elif "LOW_CONFIDENCE_OCR" in warnings:
            mode = "ORIGINAL_REQUIRED"
            reason = "识别文字置信度不足，尚不能据此确定等级、数字或单位；请对照原页。"
        elif fire_area_question(query):
            segments = _segments(query, conditions, full)
            if segments:
                mode = "CONDITION_EXCERPTS"
                reason = "已定位整段条件，范围与修正一并保留；未选定单一面积或计算修正。"
        # 待核识别文字也可按完整原编号导航；这不裁选条件，也不清除原页待核。
        # 图片或缺号涉及尚未取得的内容，不能把现有片段包装为完整分项。
        outline = (_branch_outline(full) if mode in {"FULL_CLAUSE", "ORIGINAL_REQUIRED"}
                   and not warnings.intersection({"UNRECOGNIZED_IMAGES", "OCR_NUMBERED_ITEM_GAP"}) else None)
        if outline:
            reason = ("已按原文编号分项，点开相关项阅读整段；完整原文和原页保留。"
                      if mode == "FULL_CLAUSE" else reason + " 可按已取得文字的编号分项阅读，不据此选定适用条件。")
        groups.append({"source_id": source, "snapshot_id": snapshot, "clause_number": clause,
            **unit_fields,
            "evidence_ids": [item["citation"]["evidence_id"] for item in parts],
            "mode": mode, "reason": reason, "segments": segments,
            **({"branch_outline": outline} if outline else {})})
    return {"version": VERSION, "conditions": dict(conditions), "groups": groups}
