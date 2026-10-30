"""原页闭合网格＋保存文字位置恢复阅读行列，不把矩阵直接当规范答案。"""

import cv2
import numpy as np
import pymupdf

from archspec.official_documents.page_text import _pixel_bounds, native_lines
from archspec.official_documents.reading_layout import _box


def _axis(values):
    """粗线两侧的边缘合成一个分界，不把线的厚度当成新列。"""
    groups = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= 4:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def reading_grid(page, saved, table, grid_data, *, legacy_scale=None):
    """每个闭合面是一格；文字框必须完整落在唯一格内，不拆一行猜两列。"""
    lines = saved.get("raw_lines", [])
    if saved.get("method") == "TEXT" and not any(line.get("layout_box") for line in lines):
        lines = native_lines(page)  # 旧文字PDF可再读同一原件文字位置，不是OCR。
    positioned = []
    for index, line in enumerate(lines):
        text = line.get("text")
        if not line.get("included_in_text", True) or not isinstance(text, str) or not text:
            continue
        bounds = line.get("layout_box")
        if bounds is None and saved.get("method") == "OCR" and legacy_scale is not None:
            bounds = _pixel_bounds(line.get("box"), legacy_scale)
        positioned.append({"line_index": index, "text": text,
                           "box": _box(bounds, page.rect.width, page.rect.height),
                           "origin": line.get("origin", "UNKNOWN"), "score": line.get("score")})
    mask = grid_data[0].copy()
    pixel_height, pixel_width = mask.shape
    # 数字1的笔画可能与上下横线接触，形成假单元格。只在派生掩码中清掉
    # 已知文字框内的短竖笔画；横线、长于两倍文字高度的贯穿竖线保留。
    # 原图和缓存文字均不修改，真实短分隔线在文字框外也仍然保留。
    heights = [(line["box"][3] - line["box"][1]) * pixel_height
               for line in positioned if line["box"]]
    threshold = max(30, 2 * float(np.median(heights))) if heights else 30
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grid_data[2], connectivity=8)
    long_vertical = np.zeros(mask.shape, dtype=np.uint8)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_HEIGHT] >= threshold:
            long_vertical[labels == index] = 255
    protected = grid_data[1] | long_vertical
    for line in positioned:
        if not line["box"]:
            continue
        a, b, c, d = line["box"]
        left, top = max(0, int(a * pixel_width) - 2), max(0, int(b * pixel_height) - 2)
        right, bottom = min(pixel_width, int(c * pixel_width) + 3), min(pixel_height, int(d * pixel_height) + 3)
        mask[top:bottom, left:right] = protected[top:bottom, left:right]
    x0, y0, x1, y1 = table["box"]
    left, top = round(x0 * pixel_width), round(y0 * pixel_height)
    right, bottom = round(x1 * pixel_width), round(y1 * pixel_height)
    region = mask[top:bottom, left:right]
    count, _, stats, _ = cv2.connectedComponentsWithStats(255 - region, connectivity=4)
    faces = []
    for x, y, width, height, area in stats[1:count]:
        # 接触裁剪边缘的背景不属于单元格；短小孔洞和不规则流程图不重建。
        if (x <= 0 or y <= 0 or x + width >= region.shape[1] or y + height >= region.shape[0]
                or width < 8 or height < 8 or area < width * height * .88):
            continue
        faces.append([int(left + x), int(top + y), int(left + x + width), int(top + y + height)])
    if not 4 <= len(faces) <= 200:
        return None
    xs, ys = _axis([v for f in faces for v in (f[0], f[2])]), _axis([v for f in faces for v in (f[1], f[3])])
    if not 3 <= len(xs) <= 41 or not 3 <= len(ys) <= 41:
        return None
    cells = []
    for face in faces:
        a, b = [min(range(len(xs)), key=lambda i: abs(xs[i] - value)) for value in (face[0], face[2])]
        c, d = [min(range(len(ys)), key=lambda i: abs(ys[i] - value)) for value in (face[1], face[3])]
        if a >= b or c >= d:
            return None
        cells.append({"row": c + 1, "column": a + 1, "row_span": d - c, "column_span": b - a,
                      "box": [face[0] / pixel_width, face[1] / pixel_height,
                              face[2] / pixel_width, face[3] / pixel_height], "lines": []})
    cells.sort(key=lambda cell: (cell["row"], cell["column"]))
    unassigned, characters = [], 0
    for original in positioned:
        text, box = original["text"], original["box"]
        if box is not None and not (box[0] < x1 and box[2] > x0 and box[1] < y1 and box[3] > y0):
            continue  # 表题、表注和其他表有位置时不会混入网格。
        characters += len(text)
        if characters > 16000:
            return None  # 不给部分矩阵套上“全表”外观；原图仍完整可读。
        owners = []
        if box is not None:
            dx, dy = 2 / page.rect.width, 2 / page.rect.height
            owners = [cell for cell in cells if cell["box"][0] - dx <= box[0]
                      and cell["box"][1] - dy <= box[1] and cell["box"][2] + dx >= box[2]
                      and cell["box"][3] + dy >= box[3]]
        if len(owners) == 1:
            owners[0]["lines"].append(original)
        else:
            unassigned.append(original)
    for cell in cells:
        cell["lines"].sort(key=lambda line: (line["box"][1], line["box"][0], line["line_index"]))
    return {"status": "GEOMETRY_CANDIDATE", "row_count": len(ys) - 1,
            "column_count": len(xs) - 1, "cells": cells, "unassigned_lines": unassigned}


def unread_ink_cells(page, grid):
    """只查已定位空识别格中的深色字迹，不识字、不补值，也不修改原图。

    真正空白格不报漏项；浅色水印和格线不计入。无报告不等于字符全部正确，
    不完整网格仍是未知，不能由此签发整张表的真实性。
    """
    if not grid or grid.get("status") != "GEOMETRY_CANDIDATE":
        return []
    cells = grid.get("cells", [])
    empty = [cell for cell in cells if not cell.get("lines")]
    if not empty or len(cells) > 200:
        return []
    width, height, scale = page.rect.width, page.rect.height, 1.5
    # 与reading_grid同用整页渲染坐标，避免旋转页/裁剪原点造成格位偏移。
    if width * height * scale ** 2 > 16_000_000:
        return []
    pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                             colorspace=pymupdf.csGRAY, alpha=False)
    image = np.frombuffer(pixels.samples, dtype=np.uint8).reshape(pixels.height, pixels.width)
    missing = []
    for cell in empty:
        a, b, c, d = cell["box"]
        left, top = round(a * pixels.width), round(b * pixels.height)
        right, bottom = round(c * pixels.width), round(d * pixels.height)
        padding = max(3, min(8, int(min(right - left, bottom - top) * .1)))
        crop = image[max(0, top + padding):min(pixels.height, bottom - padding),
                     max(0, left + padding):min(pixels.width, right - padding)]
        if not crop.size:
            continue
        dark = cv2.threshold(crop, 120, 255, cv2.THRESH_BINARY_INV)[1]
        count, _, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
        # 小噪点和跨整格的残留横线不作为字；保留数字、符号等紧凑连通笔画。
        ink = sum(int(area) for x, y, w, h, area in stats[1:count]
                  if area >= 4 and w < crop.shape[1] * .7 and h >= 2)
        if ink >= 12:
            missing.append({key: cell[key] for key in ("row", "column", "row_span", "column_span", "box")})
    return missing


from archspec.official_documents.runtime_identity import frozen_source_identity

LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
