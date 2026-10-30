"""保留行的来处和位置，合并可选文字与本机识别文字，不猜改正文。"""

from math import isfinite

import pymupdf

from archspec.documents.chunker import clause_start

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip

def native_lines(page):
    """读取真正的文字行；不把图片字节复制进解析字典。坐标单位是 PDF 点。"""
    rows = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text:
                continue
            bounds = list(line["bbox"])
            rows.append({"text": text, "score": None, "box": bounds,
                         "coordinate_system": "PDF_POINTS", "origin": "TEXT",
                         "pdf_box": bounds,
                         "layout_box": list(pymupdf.Rect(bounds) * page.rotation_matrix),
                         "included_in_text": True})
    return rows


def image_area(page):
    # get_image_info 也包括直接嵌入的图片，不只检查有对象编号的附件图片。
    boxes = {tuple(info["bbox"]) for info in page.get_image_info()}
    original_page = page.rect * page.derotation_matrix
    return sum((pymupdf.Rect(box) & original_page).get_area() for box in boxes)


def blank_render(page, *, scale, max_pixels):
    """仅接受有界RGB渲染中每个像素都纯白的页面；识别失败不能冒充空白。"""
    if page.rect.get_area() * scale ** 2 > max_pixels:
        raise ValueError("页面过大，无法安全进行文字识别")
    pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                             colorspace=pymupdf.csRGB, alpha=False)
    if pixels.width * pixels.height > max_pixels:
        raise ValueError("页面过大，无法安全进行文字识别")
    samples = pixels.samples
    if samples.count(b"\xff") != len(samples):
        return None
    return {"kind": "ALL_WHITE_RGB_V1", "width": pixels.width,
            "height": pixels.height, "scale": scale}


def _pixel_bounds(box, scale):
    """引擎给出四点像素框；缺失或非法框不冒充可定位内容。"""
    if (not isinstance(box, list) or len(box) != 4 or
            any(not isinstance(point, (list, tuple)) or len(point) != 2 for point in box)):
        return None
    if any(type(value) not in {int, float} or not isfinite(value)
           for point in box for value in point):
        return None
    xs, ys = [point[0] / scale for point in box], [point[1] / scale for point in box]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    return bounds if bounds[0] < bounds[2] and bounds[1] < bounds[3] else None


def _same_position(first, second):
    if first is None or second is None:
        return False
    a, b = pymupdf.Rect(first), pymupdf.Rect(second)
    overlap = (a & b).get_area()
    # 两个框都至少重叠一半，避免把一大行与旁边的小数字当成同一行。
    return overlap > 0 and overlap >= .5 * a.get_area() and overlap >= .5 * b.get_area()


def _native_covers_ocr(native, recognized):
    # 只排除原生文字框内的副本。整行 OCR 可能还包括旁边的小图片尺寸，
    # 仅“重叠过半”不能整行删除；允许两点的识别框边缘误差。
    return (native[0] - 2 <= recognized[0] and native[1] - 2 <= recognized[1]
            and native[2] + 2 >= recognized[2] and native[3] + 2 >= recognized[3])


def _order_separate_anchors(rows):
    """独占识别框的条号先于紧邻同一基线的正文；不改文字或原始坐标。"""
    for anchor in list(rows):
        match = clause_start(anchor["text"].strip())
        box = anchor["layout_box"]
        if (not match or match["body"].strip() or not box
                or not anchor["included_in_text"]):
            continue
        height = box[3] - box[1]
        neighbors = []
        for row in rows:
            other = row["layout_box"]
            if (row is anchor or not other or not row["included_in_text"]
                    or clause_start(row["text"].strip())):
                continue
            other_height = other[3] - other[1]
            overlap = min(box[3], other[3]) - max(box[1], other[1])
            if (0 <= other[0] - box[2] <= 2 * height
                    and max(height, other_height) <= 2 * min(height, other_height)
                    and overlap >= .75 * min(height, other_height)
                    and abs((box[1] + box[3] - other[1] - other[3]) / 2)
                    <= .3 * max(height, other_height)):
                neighbors.append(row)
        if neighbors:
            neighbor = min(neighbors, key=lambda row: row["layout_box"][0])
            # 只修正反序，不将别行文字移动到条号之后或拼接成新识别文字。
            if rows.index(anchor) > rows.index(neighbor):
                rows.remove(anchor)
                rows.insert(rows.index(neighbor), anchor)
    return rows


def merge_lines(page, native, recognized, scale):
    """原始行全部保留，included_in_text 标明哪些行参与合并后的阅读文字。"""
    rows = [dict(line) for line in native]
    warnings = []
    for line in recognized:
        layout = _pixel_bounds(line["box"], scale)
        row = {**line, "origin": "OCR", "coordinate_system": "RENDER_PIXELS",
               "layout_box": layout,
               "pdf_box": list(pymupdf.Rect(layout) * page.derotation_matrix) if layout else None,
               "included_in_text": True}
        if layout is None:
            warnings.append("OCR_POSITION_MISSING")
        else:
            for prior in rows:
                if not prior["included_in_text"] or not _same_position(
                        prior["layout_box"], layout):
                    continue
                if (prior["text"].strip() == row["text"].strip() or
                        (prior["origin"] == "TEXT" and
                         _native_covers_ocr(prior["layout_box"], layout))):
                    # 相同位置可直接读出的文字比识别副本优先；原副本仍保存在 rows。
                    row["included_in_text"] = False
                    break
                warnings.append("OCR_OVERLAP_CONFLICT")
        rows.append(row)
    # 缺坐标行保留在末尾并标记，不制造它在页面里的具体位置。
    rows.sort(key=lambda row: (row["layout_box"][1], row["layout_box"][0])
              if row["layout_box"] else (float("inf"), float("inf")))
    return _order_separate_anchors(rows), sorted(set(warnings))
LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
