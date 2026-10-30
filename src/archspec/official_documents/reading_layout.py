"""只读原页定位：找网格和已有文字的位置，不识别或补写表格数值。"""

import re
from collections import Counter
from math import isfinite

import cv2
import numpy as np
import pymupdf

from archspec.documents.chunker import clause_start
from archspec.official_documents.page_text import _pixel_bounds, native_lines


def _normal_text(text):
    # 只去空白，不把全角小数点、漏字或相似数字偷偷改成另一份正文。
    return re.sub(r"\s+", "", text)


def _box(bounds, width, height):
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 4
            or any(type(value) not in {int, float} or not isfinite(value) for value in bounds)):
        return None
    x0, y0, x1, y1 = bounds
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return None
    return [x0 / width, y0 / height, x1 / width, y1 / height]


def locate_lines(page, saved_page, texts, *, legacy_scale=None, continuation_texts=()):
    """只画能与所选文字对应的独立行；相同短数字不因到处出现就画框。"""
    targets = [_normal_text(text) for text in texts]
    lines = list(saved_page.get("raw_lines", []))
    # 老的可选文字记录没存位置，可从同一固定原件再读取可选文字；不是OCR。
    if saved_page.get("method") == "TEXT" and not any(row.get("layout_box") for row in lines):
        lines = native_lines(page)
    counts = Counter(_normal_text(row.get("text", "")) for row in lines
                     if row.get("included_in_text", True))
    target_clauses = {match["number"] for target in targets
                      if (match := clause_start(target))}
    normalized_lines = [_normal_text(row.get("text", ""))
                        if row.get("included_in_text", True) else "" for row in lines]
    full_page = "".join(normalized_lines)
    offsets, end = [], 0
    for text in normalized_lines:
        offsets.append((end, end + len(text)))
        end += len(text)
    exact_indices = set()
    for target in targets:
        start = full_page.find(target)
        if target and start >= 0 and full_page.find(target, start + 1) < 0:
            exact_indices.update(i for i, (a, b) in enumerate(offsets)
                                 if a < start + len(target) and b > start)
    has_target_anchor = any(match and match["number"] in target_clauses
                            for text in normalized_lines
                            if (match := clause_start(text)))
    # 跨页无编号时，只定位开头连续、唯一对应的续文；旧缓存可能把后面的
    # 表格单元格拆走，所以不要求整段页首（含表格）都包含在同一文字片段中。
    leading = []
    for i, text in enumerate(normalized_lines):
        if clause_start(text):
            break
        if text:
            leading.append(i)
    continuation = ""
    continuation_indices = []
    if not has_target_anchor:
        for i in leading:
            candidate = continuation + normalized_lines[i]
            if not any(target.count(candidate) == 1 for target in targets):
                break
            continuation = candidate
            continuation_indices.append(i)
    leading_supported = (len(continuation_indices) >= 2 and len(continuation) >= 30)
    # 仅服务端核实的跨页末尾可支持短单行，旧普通调用不放宽门槛。
    # 提示必须属于当前完整目标；文字在所有目标中唯一并且为完整后缀，
    # 不能把页首相似词、重复数字或另一条款的尾句当成当前续文。
    confirmed_tails = {_normal_text(text) for text in continuation_texts
                       if _normal_text(text) in targets}
    short_tail_supported = (
        len(continuation_indices) == 1 and bool(continuation)
        and any(target.endswith(continuation) for target in confirmed_tails)
        and sum(target.count(continuation) for target in targets) == 1
    )
    current_clause = None
    result = []
    for i, row in enumerate(lines):
        if not row.get("included_in_text", True):
            continue
        text = _normal_text(row.get("text", ""))
        if match := clause_start(text):
            current_clause = match["number"]
        # 下一条款开始后，相似续句不能仅因包含相同词语被画到当前条款上。
        supported = (i in exact_indices or
                     (current_clause in target_clauses and counts[text] == 1) or
                     ((leading_supported or short_tail_supported)
                      and i in continuation_indices and counts[text] == 1))
        if not supported:
            continue
        if (len(text) < 6 or not re.search(r"[A-Za-z\u3400-\u9fff]", text)
                or not any(text in target for target in targets)):
            continue
        bounds = row.get("layout_box")
        if bounds is None and legacy_scale is not None and saved_page.get("method") == "OCR":
            bounds = _pixel_bounds(row.get("box"), legacy_scale)
        normalized = _box(bounds, page.rect.width, page.rect.height)
        if normalized is not None:
            result.append({"box": normalized})
    return result


def _runs(values):
    """多像素粗线算一条；没有连续线的孤点不算网格。"""
    indices = np.flatnonzero(values)
    return 0 if len(indices) == 0 else int(1 + np.count_nonzero(np.diff(indices) > 3))


def page_grid(page):
    """一页只生成一次网格掩码，区域定位与单元格恢复共用它。"""
    # 与原页预览相同的1.5倍；预算在服务共用读取入口核对。
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False, colorspace=pymupdf.csGRAY)
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    binary = cv2.threshold(pixels, 200, 255, cv2.THRESH_BINARY_INV)[1]
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                 np.ones((1, max(30, pixmap.width // 20)), np.uint8))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                               np.ones((max(20, pixmap.height // 60), 1), np.uint8))
    grid = cv2.morphologyEx(horizontal | vertical, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return grid, horizontal, vertical


def table_regions(page, *, grid_data=None):
    """从原图中找长横竖网格。输出原图区域，不把缺识别值当成空单元格。"""
    grid, horizontal, vertical = grid_data if grid_data is not None else page_grid(page)
    pixel_height, pixel_width = grid.shape
    contours = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    regions = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < max(80, pixel_width * .12) or height < 30:
            continue
        h = horizontal[y:y + height, x:x + width]
        v = vertical[y:y + height, x:x + width]
        # 水印/正文的短线、矩形外框不满足至少3条长横线和3条长竖线。
        if _runs(np.count_nonzero(h, axis=1) >= width * .45) < 3:
            continue
        if _runs(np.count_nonzero(v, axis=0) >= height * .45) < 3:
            continue
        # 合并表头会让内列只占部分高度。要求一条连续竖线真正连接至少三条
        # 横线，而非把流程框之间的两个分离连接线加起来冒充一列。
        columns = cv2.findContours(v, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
        junctions = []
        for column in columns:
            cx, cy, cw, ch = cv2.boundingRect(column)
            if cx <= 3 or cx + cw >= width - 3 or ch < height * .3:
                continue
            support = np.any(h[cy:cy + ch, max(0, cx - 1):min(width, cx + cw + 1)], axis=1)
            junctions.append(_runs(support))
        if not any(count >= 3 for count in junctions):
            continue
        # 四周扩少量边距，保留外框线，不裁掉首末列。
        margin = 6
        bounds = [max(0, x - margin) / pixel_width,
                  max(0, y - margin) / pixel_height,
                  min(pixel_width, x + width + margin) / pixel_width,
                  min(pixel_height, y + height + margin) / pixel_height]
        regions.append(bounds)
    regions.sort(key=lambda box: (box[1], box[0]))
    return [{"id": f"table-{i}", "label": f"本页表格 {i}", "box": box}
            for i, box in enumerate(regions[:20], start=1)]


from archspec.official_documents.runtime_identity import frozen_source_identity

LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
