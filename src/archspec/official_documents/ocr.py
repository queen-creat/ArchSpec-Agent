"""本机图片文字识别；不把规范文件发送给第三方模型。"""

import re
from functools import lru_cache
from itertools import pairwise
from math import isfinite

import numpy as np

from archspec.documents.chunker import clause_start
from archspec.official_documents.page_text import _pixel_bounds, _same_position

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip
from archspec.official_documents.runtime_ocr import require_runtime_ocr


@lru_cache(maxsize=1)
def _load_engine():
    """真正加载本机识别模型；只由_engine统一入口调用。"""
    from rapidocr import RapidOCR

    return RapidOCR()


def _engine():
    """先判运行期开关再取引擎：关闭时不得构造模型，缓存也不能绕过该判断。"""
    require_runtime_ocr()
    return _load_engine()


def recognize_page(page, *, scale=2, max_pixels=16_000_000) -> list[dict]:
    import pymupdf

    # 两倍分辨率兼顾条文小字与单页内存；过大页面明确拒绝。
    if (not isfinite(scale) or scale <= 0 or type(max_pixels) is not int
            or not 1 <= max_pixels <= 16_000_000):
        raise ValueError("文字识别图片处理配置无效")
    if page.rect.width * page.rect.height * scale ** 2 > max_pixels:
        raise ValueError("页面过大，无法安全进行文字识别")
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    engine = _engine()
    result = engine(pixels)
    if result.txts is None:
        return []
    rows = [
        {"text": str(text), "score": float(score), "box": box.tolist()}
        for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True)
    ]
    budget = [0, max_pixels]
    rows = _recover_numbered_gaps(page, rows, engine, scale, max_pixels, budget=budget)
    rows = _recover_text_gaps(page, rows, engine, scale, budget)
    rows = _recover_table_gaps(page, rows, engine, scale, budget)
    return _recover_section_start_gaps(page, rows, engine, scale, budget)


def _recover_table_gaps(page, rows, engine, scale, budget):
    """仅补原网格漏字格；不重建行列、不替换已有行，共用两次/像素补读预算。"""
    import pymupdf

    from archspec.official_documents.page_text import merge_lines
    from archspec.official_documents.reading_layout import page_grid, table_regions
    from archspec.official_documents.table_cells import reading_grid, unread_ink_cells

    output = list(rows)
    if budget[0] >= 2 or page.rotation or page.rect.get_area() * 2.25 > 16_000_000:
        return output
    positioned, _ = merge_lines(page, [], rows, scale)
    saved = {"method": "OCR", "raw_lines": positioned}
    render_pixels = int(page.rect.get_area() * 2.25)
    if render_pixels > budget[1]:
        return output
    budget[1] -= render_pixels
    grid = page_grid(page)
    # 网格和字迹检测也计入像素预算；最多查看前两个表区，不重复整页渲染无界扫描。
    for table in table_regions(page, grid_data=grid)[:2]:
        if budget[0] >= 2:
            break
        original = reading_grid(page, saved, table, grid, legacy_scale=scale)
        if render_pixels > budget[1]:
            break
        budget[1] -= render_pixels
        missing = unread_ink_cells(page, original)
        if not missing:
            continue
        a, b, c, d = table["box"]
        clip = pymupdf.Rect(a * page.rect.width, b * page.rect.height,
                            c * page.rect.width, d * page.rect.height)
        pixels_needed = int(clip.get_area() * 16)
        if clip.is_empty or not 0 < pixels_needed <= min(4_000_000, budget[1]):
            continue
        budget[0] += 1
        budget[1] -= pixels_needed
        pix = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=clip, alpha=False)
        if pix.width * pix.height > 4_000_000:
            continue
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        result = engine(pixels)
        candidates = (_region_candidates(pix, result, clip, output, scale, 4)
                      if result.txts is not None else [])
        assigned = {}
        for candidate in candidates:
            score = candidate["score"]
            bounds = _pixel_bounds(candidate["box"], scale)
            if (not bounds or not isfinite(score) or not .9 <= score <= 1
                    or not candidate["text"].strip() or len(candidate["text"]) > 80):
                continue
            owners = []
            for index, cell in enumerate(missing):
                x0, y0, x1, y1 = cell["box"]
                if (x0 * page.rect.width <= bounds[0] < bounds[2] <= x1 * page.rect.width
                        and y0 * page.rect.height <= bounds[1] < bounds[3] <= y1 * page.rect.height):
                    owners.append(index)
            if len(owners) == 1:
                assigned.setdefault(owners[0], []).append(candidate)
        filled = set()
        for index, matches in assigned.items():
            # 多行或多候选不能拼成一个数值；跨格、已有格及低置信度均不补。
            if len(matches) == 1:
                filled.add(index)
                output.append({**matches[0], "ocr_gap_kind": "TABLE_CELL",
                               "ocr_table_cell": missing[index]})
        remaining = [cell for index, cell in enumerate(missing) if index not in filled]
        output = _recover_cell_batch(page, remaining, output, engine, scale, budget)
    return output


def _recover_cell_batch(page, missing, rows, engine, scale, budget):
    """原空格的单行字迹批读；不经检测拼行，不猜单位或替换原文。"""
    from archspec.official_documents.cell_pixels import cell_clip, crop_cell

    output = list(rows)
    recognize = getattr(engine, "recognize_txt", None)
    if budget[0] >= 2 or not callable(recognize):
        return output
    crops = []
    for cell in missing[:4]:
        try:
            clip, pixels_needed = cell_clip(page, cell["box"])
            if pixels_needed > budget[1]:
                continue
            budget[1] -= pixels_needed
            crop = crop_cell(page, clip)
        except ValueError:
            continue
        if crop["single_line"]:
            crops.append((cell, crop))
    if not crops:
        return output
    budget[0] += 1
    from rapidocr.main import RapidOCRError

    try:
        result = recognize([crop["pixels"] for _, crop in crops])
    except (RapidOCRError, ValueError, RuntimeError):
        return output  # 可选补读失败不覆盖已有行；原漏格字迹检查仍会保留缺口。
    if (result.txts is None or result.scores is None or len(result.txts) != len(crops)
            or len(result.scores) != len(crops)):
        return output
    for (cell, crop), text, score in zip(crops, result.txts, result.scores, strict=True):
        text, score = str(text), float(score)
        # 自动补入仅接受单行数字原读数；单位、上下限关系及行列含义仍另核原表。
        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", text) or not isfinite(score) or not .9 <= score <= 1:
            continue
        a,b,c,d = crop["tight_pdf_box"]
        box = [[a*scale,b*scale],[c*scale,b*scale],[c*scale,d*scale],[a*scale,d*scale]]
        output.append({"text":text,"score":score,"box":box,
            "ocr_gap_status":"RECOVERED_NEEDS_REVIEW","ocr_gap_kind":"TABLE_CELL",
            "ocr_table_cell":cell,"ocr_region_pdf_box":[a,b,c,d],
            "ocr_cell_method":"RECOGNITION_ONLY_SINGLE_LINE"})
    return output


def _number_marker(text):
    """复用正文条号判定，列表与不同章节的编号不得混为一个序列。"""
    item = re.match(r"^\s*(\d{1,2})[）)]\s*\S", text)
    if item:
        return ("list",), int(item[1])
    clause = clause_start(text.strip())
    if clause and re.fullmatch(r"\d+(?:\.\d+){2,3}", clause["number"]):
        parts = tuple(int(part) for part in clause["number"].split("."))
        return ("clause", *parts[:-1]), parts[-1]
    return None


def _region_candidates(pix, recovered, clip, rows, scale, region_scale):
    candidates = []
    for text, score, box in zip(recovered.txts, recovered.scores, recovered.boxes, strict=True):
        full_box = [[(float(x) + pix.x) * scale / region_scale,
                     (float(y) + pix.y) * scale / region_scale] for x, y in box]
        bounds = _pixel_bounds(full_box, scale)
        if (not bounds or bounds[0] < clip.x0 - 1 or bounds[2] > clip.x1 + 1
                or bounds[1] < clip.y0 - 1 or bounds[3] > clip.y1 + 1):
            continue
        if any(_same_position(bounds, _pixel_bounds(row["box"], scale)) for row in rows):
            continue
        candidates.append({"text": str(text), "score": float(score), "box": full_box,
            "ocr_gap_status": "RECOVERED_NEEDS_REVIEW", "ocr_region_pdf_box": list(clip)})
    return candidates


def _has_text_ink(pixels, expected_height):
    """深色小连通域须形成文字行；纯空白、长线与浅水印不触发识别。"""
    import cv2

    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    count, _, stats, centers = cv2.connectedComponentsWithStats((gray < 120).astype(np.uint8), connectivity=8)
    ys = [centers[index][1] for index in range(1, count)
          if (stats[index, cv2.CC_STAT_AREA] >= 6
              and .3 * expected_height <= stats[index, cv2.CC_STAT_HEIGHT] <= 1.8 * expected_height
              and 1 <= stats[index, cv2.CC_STAT_WIDTH] <= 2 * expected_height)]
    return any(sum(abs(other - y) <= .35 * expected_height for other in ys) >= 4 for y in ys)


def _paragraph_column(positioned, before, after, height, page_width):
    """至少两条其他正文行的同栏坐标才能扩展裁剪，不从缺失文字推定栏宽。"""
    left, right = min(before[0], after[0]), max(before[2], after[2])
    groups = {}
    step = max(1, 2 * height)
    for row, bounds in positioned:
        if (bounds == before or bounds == after or len(row["text"].strip()) < 8
                or bounds[2] - bounds[0] < page_width * .3
                or not .7 * height <= bounds[3] - bounds[1] <= 1.5 * height
                or abs(bounds[0] - left) > 2 * height or bounds[2] < right - height):
            continue
        key = round(bounds[0] / step), round(bounds[2] / step)
        values = groups.setdefault(key, [])
        # 同一基线的重复框不能冒充两条独立正文行；输入已按纵坐标排列。
        if not values or (bounds[1] + bounds[3] - values[-1][1] - values[-1][3]) / 2 > .7 * height:
            values.append(bounds)
    agreed = [values for values in groups.values() if len(values) >= 2]
    if not agreed:
        return None
    column = max(agreed, key=lambda values: (len(values), max(b[2] for b in values)))
    return min(left, *(b[0] for b in column)), max(right, *(b[2] for b in column))


def _recover_section_start_gaps(page, rows, engine, scale, budget):
    """同节首条缺失时，仅对真实字迹补读，不按缺号补造正文或条号。"""
    import pymupdf

    output = [dict(row) for row in rows]
    if page.rotation:
        return output
    positioned = [(index, row, box) for index, row in enumerate(output)
                  if (box := _pixel_bounds(row["box"], scale))]
    positioned.sort(key=lambda item: item[2][1])
    for position, (index, heading, bounds) in enumerate(positioned):
        if budget[0] >= 2:
            break
        title = re.fullmatch(r"\s*(\d+)\.(\d+)\s+[^。；;·.…]{2,40}\s*", heading["text"])
        if not title or not .3 * page.rect.width <= (bounds[0] + bounds[2]) / 2 <= .7 * page.rect.width:
            continue
        family = ("clause", int(title[1]), int(title[2]))
        intervening = []
        first = None
        for entry in positioned[position + 1:]:
            _next_index, row, box = entry
            if box[1] <= bounds[3]:
                continue
            if re.match(r"\s*(?:表|续表|图)\s*\d", row["text"]):
                break
            marker = _number_marker(row["text"])
            if marker:
                first = (marker, box)
                break
            # 其他章节标题或目录引导线不属于本节正文。
            if re.match(r"\s*\d+\.\d+\s+", row["text"]):
                break
            intervening.append(entry)
        if not first or first[0][0] != family or not 2 <= first[0][1] <= 4:
            continue
        after = first[1]
        if after[0] > page.rect.width * .2 or after[2] < page.rect.width * .5:
            continue
        # 只取标题后的第一个缺口，避免已有续行干扰条号识别。
        upper = min([after[1], *(entry[2][1] for entry in intervening)])
        padding = max(10, page.rect.width * .03)
        clip = pymupdf.Rect(max(page.rect.x0, after[0] - padding), bounds[3] + .5,
                            min(page.rect.x1, after[2] + padding), upper - .5) & page.rect
        pixels_needed = int(clip.get_area() * 9)
        if clip.is_empty or clip.height > page.rect.height * .2 or not 0 < pixels_needed <= budget[1]:
            continue
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=False)
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        # 遮去已有OCR框，仅在空白区域确有文字形状时才消耗补读次数。
        unseen = pixels.copy()
        for _, _, existing in intervening:
            x0 = max(0, int(existing[0] * 3) - pix.x - 3)
            y0 = max(0, int(existing[1] * 3) - pix.y - 3)
            x1 = min(pix.width, int(existing[2] * 3) - pix.x + 3)
            y1 = min(pix.height, int(existing[3] * 3) - pix.y + 3)
            unseen[y0:y1, x0:x1] = 255
        budget[1] -= pixels_needed
        if not _has_text_ink(unseen, (after[3] - after[1]) * 3):
            continue
        budget[0] += 1
        output[index]["ocr_gap_status"] = "UNRESOLVED"
        output[index]["ocr_gap_kind"] = "SECTION_FIRST_CLAUSE"
        try:
            recovered = engine(pixels)
            candidates = (_region_candidates(pix, recovered, clip, output, scale, 3)
                          if recovered.txts is not None else [])
            anchors = [row for row in candidates if _number_marker(row["text"]) == (family, 1)]
            others = [row for row in candidates if _number_marker(row["text"])
                      and _number_marker(row["text"]) != (family, 1)]
            if len(anchors) != 1 or others:
                continue
            anchor = _pixel_bounds(anchors[0]["box"], scale)
            body = [row for row in candidates if row is not anchors[0]
                    and (box := _pixel_bounds(row["box"], scale))
                    and -2 <= box[0] - anchor[2] <= 2 * (anchor[3] - anchor[1])
                    and max(box[3] - box[1], anchor[3] - anchor[1])
                    <= 2 * min(box[3] - box[1], anchor[3] - anchor[1])
                    and min(box[3], anchor[3]) - max(box[1], anchor[1])
                    >= .75 * min(box[3] - box[1], anchor[3] - anchor[1])]
            # 编号与正文须来自同一基线，孤立数字或目录页不能当有效补读。
            if not body and not clause_start(anchors[0]["text"])["body"].strip():
                continue
            output[index]["ocr_gap_status"] = "RECOVERED_NEEDS_REVIEW"
            accepted = [anchors[0], *body]
            # 有未归属识别行时不能把部分条文标为已恢复。
            if len(accepted) != len(candidates):
                output[index]["ocr_gap_status"] = "UNRESOLVED"
                continue
            for candidate in accepted:
                candidate["ocr_gap_kind"] = "SECTION_FIRST_CLAUSE"
            output.extend(accepted)
        except (ValueError, RuntimeError):
            continue
    return output


def _recover_text_gaps(page, rows, engine, scale, budget):
    """只补读相邻长正文框间有文字字迹的窄缝，与缺号补读共享两次上限。"""
    import pymupdf

    output = list(rows)
    positioned = [(row, box) for row in rows if (box := _pixel_bounds(row["box"], scale))]
    positioned.sort(key=lambda pair: pair[1][1])
    def paragraph(row, box):
        return len(row["text"].strip()) >= 8 and box[2] - box[0] >= page.rect.width * .3
    heights = [box[3] - box[1] for row, box in positioned if paragraph(row, box)]
    if len(heights) < 2:
        return output
    height = float(np.median(heights))
    for (row, before), (next_row, after) in pairwise(positioned):
        if budget[0] >= 2:
            break
        # 段尾可能只有几个字；一侧仍须是长正文，另一侧保留可定位短行。
        # 不跳过已有短行去拼更远的两行，也不把两个短表格单元作为正文锚点。
        def body_tail(item, bounds):
            return len(item["text"].strip()) >= 4 and bounds[2] - bounds[0] >= 3 * height
        if (not (paragraph(row, before) or paragraph(next_row, after))
                or not body_tail(row, before) or not body_tail(next_row, after)):
            continue
        gap = after[1] - before[3]
        if not .65 * height <= gap <= 2.5 * height:
            continue
        column = _paragraph_column(positioned, before, after, height, page.rect.width)
        indent, old_limit = abs(before[0] - after[0]), max(12, page.rect.width * .04)
        if indent > old_limit:
            overlap = min(before[2], after[2]) - max(before[0], after[0])
            # 只在已证实同栏且横向大部分重合时容许分项缩进，跨栏不拼接。
            if (column is None or indent > max(old_limit, 2 * height)
                    or overlap < .8 * min(before[2] - before[0], after[2] - after[0])):
                continue
        left, right = column or (min(before[0], after[0]), max(before[2], after[2]))
        clip = pymupdf.Rect(max(page.rect.x0, left - 1), before[3] + .5,
                            min(page.rect.x1, right + 1), after[1] - .5)
        if clip.is_empty or clip.get_area() * 9 > budget[1]:
            continue
        budget[1] -= int(clip.get_area() * 9)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=False)
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if not _has_text_ink(pixels, height * 3):
            continue
        budget[0] += 1
        index = next(i for i, item in enumerate(output) if item is row)
        row = {**row, "ocr_gap_kind": "UNNUMBERED_TEXT"}
        output[index] = row
        try:
            recovered = engine(pixels)
            candidates = (_region_candidates(pix, recovered, clip, output, scale, 3)
                          if recovered.txts is not None else [])
            row["ocr_gap_status"] = "RECOVERED_NEEDS_REVIEW" if candidates else "UNRESOLVED"
            for candidate in candidates:
                candidate["ocr_gap_kind"] = "UNNUMBERED_TEXT"
            output.extend(candidates)
        except (ValueError, RuntimeError):
            row["ocr_gap_status"] = "UNRESOLVED"
    return output


def _recover_numbered_gaps(page, rows, engine, scale, max_pixels, *, budget=None):
    """只补读同页同层级条号或括号编号的物理缺口，最多两次；不猜字、不替换已有行。

    局部框转换回整页的原像素坐标，后续仍走原页定位和处理缓存。
    """
    import pymupdf

    markers = []
    for index, row in enumerate(rows):
        marker = _number_marker(row["text"])
        bounds = _pixel_bounds(row["box"], scale)
        if marker and bounds:
            markers.append((marker, index, bounds))
    markers.sort(key=lambda item: item[2][1])
    output = [dict(row) for row in rows]
    budget = [0, max_pixels] if budget is None else budget
    region_scale = 3
    for ((family, first), index, before), ((other, last), _, after) in pairwise(markers):
        if (family != other or not 1 < last - first <= 4
                or abs(before[0] - after[0]) > max(10, page.rect.width * .03)):
            continue
        output[index]["ocr_gap_status"] = "UNRESOLVED"
        missing = {(family, value) for value in range(first + 1, last)}
        clip = pymupdf.Rect(page.rect.x0, before[3], page.rect.x1, after[1]) & page.rect
        if (budget[0] >= 2 or clip.is_empty or clip.height > page.rect.height * .35
                or clip.get_area() * region_scale ** 2 > budget[1]):
            continue
        budget[0] += 1
        budget[1] -= int(clip.get_area() * region_scale ** 2)
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(region_scale, region_scale), clip=clip, alpha=False)
            pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            recovered = engine(pixels)
            if recovered.txts is None:
                continue
            candidates = _region_candidates(pix, recovered, clip, output, scale, region_scale)
            numbers = {marker for row in candidates
                       if (marker := _number_marker(row["text"]))}
            if not missing.issubset(numbers) or numbers - missing:
                continue
            output[index]["ocr_gap_status"] = "RECOVERED_NEEDS_REVIEW"
            output.extend(candidates)
        except (ValueError, RuntimeError):
            # 二次识别失败保留整页原结果及明确缺口，不无限重试或标成自动核验。
            continue
    return output
LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
