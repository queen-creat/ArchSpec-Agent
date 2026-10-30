"""只读固定识别行的条文风险，不更正文字，不把评分当成准确率。"""

import re
from math import isfinite

from archspec.documents.chunker import CLAUSE_PATTERN


def _compact(text):
    # 只忽略空白，水印字符、数字、单位及否定词仍参与对应关系。
    return re.sub(r"\s+", "", text)


def _unique_start(text, part):
    start = text.find(part)
    return start if part and start >= 0 and text.find(part, start + 1) < 0 else None


def _span(page_text, target):
    start = _unique_start(page_text, target)
    if start is not None:
        return start, start + len(target)
    # 重复同文不能退回部分匹配并猜测哪一处属于当前条文。
    if target and page_text.count(target) > 1:
        return None
    # 跨页只接受完整连续重合。很短的重复页码/单字不作为页首或页尾证明。
    if len(page_text) >= 24 and _unique_start(target, page_text) is not None:
        return 0, len(page_text)
    anchor = target[:24]
    if len(anchor) == 24 and (start := _unique_start(page_text, anchor)) is not None:
        tail = page_text[start:]
        if target.startswith(tail):
            return start, len(page_text)
    anchor = page_text[:24]
    if len(anchor) == 24 and (start := _unique_start(target, anchor)) is not None:
        tail = target[start:]
        if page_text.startswith(tail):
            return 0, len(tail)
    return None


def _page_rows(page, ignored_lines):
    rows, texts = [], []
    raw = page.get("raw_lines")
    if not isinstance(raw, list) or not raw:
        return None
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            return None
        if row.get("included_in_text") is not True or row["text"].strip() in ignored_lines:
            continue
        text = _compact(row["text"])
        if not text:
            continue
        score = row.get("score")
        if score is None and row.get("origin") != "TEXT":
            return None
        if score is not None and (type(score) not in {int, float}
                                  or not isfinite(score) or not 0 <= score <= 1):
            return None
        rows.append((index, row, text)); texts.append(text)
    joined = "".join(texts)
    if not joined or not isinstance(page.get("text"), str) or joined != _compact(page["text"]):
        return None
    return rows, joined


def _terminal_span(prepared, fragment, page_number, target):
    # 只接受固定条文真实跨页末尾。不能用浏览器指定的页号或相似短句作证明。
    clause = fragment.get("clause_number")
    anchor = CLAUSE_PATTERN.match(target)
    if (not isinstance(clause, str) or not anchor or anchor["number"] != clause
            or not fragment["page_start"] < page_number == fragment["page_end"]):
        return None
    leading = []
    for _, _, text in prepared[0]:
        if CLAUSE_PATTERN.match(text):
            break
        leading.append(text)
    tail = "".join(leading)
    # 全部页首续文必须是唯一的完整后缀；纯数字、重复尾句不能证明归属。
    if (len(tail) < 6 or not re.search(r"[A-Za-z\u3400-\u9fff]", tail)
            or not target.endswith(tail) or target.count(tail) != 1
            or prepared[1].count(tail) != 1):
        return None
    return 0, len(tail)


def fragment_recognition(record, fragment, *, threshold=.9, ignored_lines=(), only_page=None):
    """完整唯一对应才缩小低分警告；评分或原行缺失保持未知。

    layout_box沿用固定原始行的PDF位置，原页接口再校验边界并归一化。
    不在缓存写入核验标记，CURRENT_FRAGMENT仅表示风险范围已对应，不表示文字正确。
    """
    pages = [page for page in record["pages"]
             if fragment["page_start"] <= page["pdf_page"] <= fragment["page_end"]
             and (only_page is None or page["pdf_page"] == only_page)]
    warnings = {warning for page in pages for warning in page["warnings"]}
    target = _compact(fragment["text"])
    unknown = not pages or threshold is None
    issues, other_count = [], 0
    pending_rows, other_pending = [], 0
    pending_unknown = any("OCR_GAP_RECOVERED_NEEDS_REVIEW" in page["warnings"]
                          and not any(row.get("ocr_gap_status") == "RECOVERED_NEEDS_REVIEW"
                                      and row.get("included_in_text") is True
                                      for row in page.get("raw_lines", []) if isinstance(row, dict))
                          for page in pages)
    if any(row.get("ocr_gap_status") == "RECOVERED_NEEDS_REVIEW"
           for page in pages for row in page.get("raw_lines", []) if isinstance(row, dict)):
        warnings.add("OCR_GAP_RECOVERED_NEEDS_REVIEW")
    for page in pages:
        prepared = _page_rows(page, set(ignored_lines))
        span = _span(prepared[1], target) if prepared else None
        if prepared and span is None:
            span = _terminal_span(prepared, fragment, page["pdf_page"], target)
        if prepared is None or span is None or threshold is None:
            unknown = True
            continue
        offset = 0
        for index, row, text in prepared[0]:
            end = offset + len(text)
            if row.get("ocr_gap_status") == "RECOVERED_NEEDS_REVIEW":
                if offset < span[1] and end > span[0]:
                    pending_rows.append({"pdf_page": page["pdf_page"], "row_index": index,
                                         "text": row["text"], "status": "RECOVERED_NEEDS_REVIEW"})
                else:
                    other_pending += 1
            if row["score"] is not None and row["score"] < threshold:
                if offset < span[1] and end > span[0]:
                    bounds = row.get("layout_box")
                    valid = (isinstance(bounds, list) and len(bounds) == 4
                             and all(type(v) in {int, float} and isfinite(v) for v in bounds)
                             and 0 <= bounds[0] < bounds[2] and 0 <= bounds[1] < bounds[3])
                    issues.append({"pdf_page": page["pdf_page"], "row_index": index,
                                   "text": row["text"], "score": row["score"],
                                   "layout_box": list(bounds) if valid else None})
                else:
                    other_count += 1
            offset = end
    # 原页风险不改写；只调整本次派生片段里确实不相关的低分提醒。
    if not unknown and not issues:
        warnings.discard("LOW_CONFIDENCE_OCR")
    elif issues:
        warnings.add("LOW_CONFIDENCE_OCR")
    if not unknown and not pending_unknown and not pending_rows:
        warnings.discard("OCR_GAP_RECOVERED_NEEDS_REVIEW")
    elif pending_rows:
        warnings.add("OCR_GAP_RECOVERED_NEEDS_REVIEW")
    images = [{"pdf_page": page["pdf_page"], **image}
              for page in pages for image in page.get("unrecognized_images", [])]
    # 旧OCR片段保持原结构，只有明确原生页面报告尚未识别的图片。
    image_risk = ({"unrecognized_image_count": len(images), "unrecognized_images": images[:20],
                   "unrecognized_image_omitted_count": max(0, len(images) - 20)}
                  if any("unrecognized_images" in page for page in pages) else {})
    return {"warnings": sorted(warnings), "recognition": {
        "scope": "UNKNOWN" if unknown else "CURRENT_FRAGMENT", **image_risk,
        "low_count": len(issues), "other_low_count": other_count,
        "pending_recovery_scope": "UNKNOWN" if unknown or pending_unknown else "CURRENT_FRAGMENT",
        "pending_recovery_count": len(pending_rows), "other_pending_recovery_count": other_pending,
        "pending_recovery_issues": pending_rows[:20],
        "pending_recovery_omitted_count": max(0, len(pending_rows) - 20),
        "unlocated_count": sum(row["layout_box"] is None for row in issues),
        "omitted_count": max(0, len(issues) - 20), "issues": issues[:20],
    }}
