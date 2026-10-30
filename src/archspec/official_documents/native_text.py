"""提取已有文字层并保留未知图片；不把图片存在等同于整份正文不可读。"""
import hashlib
import math
import re
from dataclasses import replace
from importlib.metadata import version

import pymupdf

from archspec.official_documents.page_text import LOADED_SOURCE_IDENTITY as PAGE_IDENTITY
from archspec.official_documents.page_text import native_lines

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip

NATIVE_OCR_VERSION = "unrecognized-images-native-v1"


def native_processing_spec(base):
    """独立真实提取身份；原OCR模式、旧快照和固定嵌入模型保持原身份。"""
    digest = hashlib.sha256(LOADED_SOURCE_IDENTITY.source_bytes + PAGE_IDENTITY.source_bytes).hexdigest()
    return replace(base, parser_version=f"official-native-pdf-v1:pymupdf-{version('pymupdf')}:parser-{digest}",
                   ocr_version=NATIVE_OCR_VERSION,
                   extraction_config={**base.extraction_config, "content_mode": "NATIVE_TEXT_IMAGES_UNRECOGNIZED_V1"})


def _bounded_rows(page, width, height):
    """边缘判断只消费同一页可核对的文字行框；任一框无效就保守保留整页。"""
    rows = [(index, row) for index, row in enumerate(page["raw_lines"])
            if row.get("included_in_text") and row["text"].strip()]
    boxes = {}
    for index, row in rows:
        box = row.get("layout_box")
        if (not isinstance(box, list) or len(box) != 4
                or not all(type(value) in {int, float} and math.isfinite(value) for value in box)
                or not 0 <= box[0] < box[2] <= width + .5
                or not 0 <= box[1] < box[3] <= height + .5):
            return None
        boxes[index] = box
    return rows, boxes


def _margin_number(page, width, height, *, origins=("TEXT",), clearance=.6):
    """仅核有可靠位置和评分的独立底部页码候选；普通正文数字和表格行不能靠数字匹配排除。"""
    bounded = _bounded_rows(page, width, height)
    if bounded is None:
        return None
    rows, boxes = bounded
    candidates = []
    for index, row in rows:
        text, box = row["text"].strip(), boxes[index]
        score = row.get("score")
        if (row.get("origin") not in origins
                or row.get("origin") == "OCR" and (type(score) not in {int, float}
                    or not math.isfinite(score) or not .9 <= score <= 1)
                or not re.fullmatch(r"[0-9]{1,4}", text)
                or not .85 <= box[1] / height < box[3] / height <= .98
                or not .04 <= (box[0] + box[2]) / (2 * width) <= .96
                or box[2] - box[0] > width * .12):
            continue
        others = [value for other, value in boxes.items() if other != index]
        # 表格同一行的另一列、正文末行都不得被当作页码；须有独立的页脚空隙。
        gap = box[1] - max((value[3] for value in others), default=box[1])
        if not others or gap < max(4, (box[3] - box[1]) * clearance):
            continue
        candidates.append({"page": page, "index": index, "number": int(text),
                           "x": (box[0] + box[2]) / (2 * width), "y": box[1] / height,
                           "isolated": gap >= max(4, (box[3] - box[1]) * .6)})
    return candidates[0] if len(candidates) == 1 else None


def _exclude_repeated_page_numbers(pages, sizes, *, origins=("TEXT",)):
    """连续同位置、同递增页码带须有三个独立空隙锚点；排满正文不再切断整段证据。"""
    run = []
    candidates = [_margin_number(page, *size, origins=origins, clearance=.2)
                  for page, size in zip(pages, sizes, strict=True)]
    for candidate in [*candidates, None]:
        if (candidate is not None and run
                and candidate["page"]["pdf_page"] == run[-1]["page"]["pdf_page"] + 1
                and candidate["number"] == run[-1]["number"] + 1
                and (abs(candidate["x"] - run[-1]["x"]) <= .03
                     or ((candidate["x"] < .25 and run[-1]["x"] > .75
                          or candidate["x"] > .75 and run[-1]["x"] < .25)
                         and abs(candidate["x"] + run[-1]["x"] - 1) <= .1))
                and (len(run) < 2 or abs(candidate["x"] - run[-2]["x"]) <= .03)
                and abs(candidate["y"] - run[0]["y"]) <= .02):
            run.append(candidate)
            continue
        anchors = [item["page"]["pdf_page"] for item in run if item["isolated"]]
        # 只放宽已被整段独立页脚证明的间隙；单纯连续整数不能排除表格或正文。
        # 各行仍须满足四点净空、不重叠、同位置和同页码偏移，不能跨越缺证据页面。
        if len(anchors) >= 3:
            _exclude_margin_run(run, "REPEATED_PAGE_NUMBER_MARGIN", "page_number_peers")
            for item in run:
                nearest = sorted(anchors, key=lambda page: (abs(page - item["page"]["pdf_page"]), page))[:3]
                item["page"]["raw_lines"][item["index"]]["page_number_isolation_anchors"] = sorted(nearest)
        run = [candidate] if candidate is not None else []


def _exclude_margin_run(run, reason, peer_key):
    """所有排除都须连续三页支持，只更改派生文字，原始行/坐标仍可逐页核对。"""
    if len(run) < 3:
        return
    for index, item in enumerate(run):
        row = item["page"]["raw_lines"][item["index"]]
        row.update(included_in_text=False, exclusion_reason=reason)
        start = max(0, min(index - 1, len(run) - 3))
        row[peer_key] = [peer["page"]["pdf_page"] for peer in run[start:start + 3]]
        item["page"]["text"] = "\n".join(line["text"].strip() for line in item["page"]["raw_lines"]
                                           if line["included_in_text"])


def _exclude_repeated_cover_titles(pages, sizes):
    """封面大标题+连续三页独立小页眉共同证明；普通表头和正文同名文字保留。"""
    first = _bounded_rows(pages[0], *sizes[0])
    if first is None:
        return
    width, height = sizes[0]
    titles = {}
    for index, row in first[0]:
        text, box = row["text"].strip(), first[1][index]
        # 只锚定封面中部大字题名，不把某页重复表头或单个数字当书名。
        if (row.get("origin") == "TEXT" and 6 <= len(text) <= 80
                and len(re.findall(r"[\u4e00-\u9fff]", text)) >= 6
                and .1 <= box[1] / height < box[3] / height <= .65
                and .25 <= (box[0] + box[2]) / (2 * width) <= .75
                and box[2] - box[0] >= width * .25 and box[3] - box[1] >= height * .025):
            titles[text] = (box[3] - box[1]) / height
    run = []
    for page, (width, height) in [*zip(pages, sizes, strict=True), (None, (0, 0))]:
        candidates = []
        bounded = _bounded_rows(page, width, height) if page is not None else None
        if bounded is not None:
            rows, boxes = bounded
            for index, row in rows:
                text, box = row["text"].strip(), boxes[index]
                if (row.get("origin") != "TEXT" or text not in titles
                        or not 0 <= box[1] < box[3] <= height * .08
                        or (box[3] - box[1]) / height > titles[text] * .7):
                    continue
                others = [value for other, value in boxes.items() if other != index]
                if not others or min(value[1] for value in others) < box[3] + max(6, box[3] - box[1]):
                    continue
                candidates.append({"page": page, "index": index, "title": text,
                                   "x": (box[0] + box[2]) / (2 * width), "y": box[1] / height})
        candidate = candidates[0] if len(candidates) == 1 else None
        if (candidate is not None and run and candidate["title"] == run[0]["title"]
                and candidate["page"]["pdf_page"] == run[-1]["page"]["pdf_page"] + 1
                and abs(candidate["x"] - run[0]["x"]) <= .02
                and abs(candidate["y"] - run[0]["y"]) <= .015):
            run.append(candidate)
            continue
        _exclude_margin_run(run, "REPEATED_COVER_TITLE_MARGIN", "running_title_peers")
        run = [candidate] if candidate is not None else []


def _exclude_repeated_publisher_bands(pages, sizes):
    """连续同位且独立的出版标识才退出派生正文；不按城市/整页词串删原文。"""
    publisher = re.compile(r"[\u4e00-\u9fff]{2,30}(?:政府|委员会|部|厅|局|院)"
                           r"(?:规章|公报|发布|印发|编印|出版)")
    groups = {}
    for page, (width, height) in zip(pages, sizes, strict=True):
        bounded = _bounded_rows(page, width, height)
        if bounded is None:
            continue
        rows, boxes = bounded
        found = {"HEADER": [], "FOOTER": []}
        for index, row in rows:
            text, box = row["text"].strip(), boxes[index]
            if (row.get("origin") != "TEXT" or not publisher.fullmatch(text)
                    or re.search(r"应|须|不|可|按|根据|依据|^本|^由|^经|^注|^第|^表|^图|^附", text)):
                continue
            edge = ("HEADER" if box[3] <= height * .12 else
                    "FOOTER" if box[1] >= height * .88 else None)
            if edge is None:
                continue
            others = [value for other, value in boxes.items() if other != index]
            if not others:
                continue
            gap = (min(value[1] for value in others) - box[3] if edge == "HEADER"
                   else box[1] - max(value[3] for value in others))
            # 与正文或同一表行重叠时不排除，不能把重复表头/说明当出版页边。
            if gap < max(6, box[3] - box[1]):
                continue
            found[edge].append({"page": page, "index": index, "text": text,
                "x": (box[0] + box[2]) / (2 * width), "y": box[1] / height,
                "width": (box[2] - box[0]) / width, "height": (box[3] - box[1]) / height})
        for edge, candidates in found.items():
            if len(candidates) == 1:
                item = candidates[0]
                groups.setdefault((edge, item["text"]), []).append(item)
    for candidates in groups.values():
        run = []
        for candidate in [*candidates, None]:
            if (candidate is not None and run
                    and candidate["page"]["pdf_page"] == run[-1]["page"]["pdf_page"] + 1
                    and abs(candidate["x"] - run[0]["x"]) <= .02
                    and abs(candidate["y"] - run[0]["y"]) <= .015
                    and abs(candidate["width"] - run[0]["width"]) <= .02
                    and abs(candidate["height"] - run[0]["height"]) <= .01):
                run.append(candidate)
                continue
            _exclude_margin_run(run, "REPEATED_PUBLISHER_MARGIN", "publisher_margin_peers")
            run = [candidate] if candidate is not None else []


def extract_native_pages(path, spec, *, max_pages=100, progress=None):
    """逐页保留真实文字和待辨认区域；图片封面不阻断后续可读正文。"""
    pages, sizes = [], []
    with pymupdf.open(path) as document:
        if document.is_repaired or document.is_encrypted or not 1 <= len(document) <= max_pages:
            raise ValueError("原件暂不能建立可核对的文字层，请查看原件")
        ignored = set(spec.extraction_config["watermark_lines"])
        for number, page in enumerate(document, 1):
            rows = native_lines(page)
            sizes.append((page.rect.width, page.rect.height))
            for row in rows:
                row["included_in_text"] = row["text"].strip() not in ignored
            text = "\n".join(row["text"].strip() for row in rows if row["included_in_text"])
            images = []
            visible = page.rect * page.derotation_matrix
            for image in page.get_image_info():
                bounds = pymupdf.Rect(image["bbox"]) & visible
                if not bounds.is_empty:
                    images.append({"pdf_box": list(bounds), "layout_box": list(bounds * page.rotation_matrix),
                                   "coordinate_system": "PDF_POINTS", "status": "UNRECOGNIZED"})
            # 无文字页保留原页位置，不猜作空白；正文只来自其余真正有文字的页面。
            if not text.strip() and not images:
                images.append({"pdf_box": list(visible), "layout_box": list(visible * page.rotation_matrix),
                               "coordinate_system": "PDF_POINTS", "status": "UNRECOGNIZED"})
            warnings = ["UNRECOGNIZED_IMAGES"] if images else []
            if not text.strip():
                warnings.append("UNRECOGNIZED_PAGE")
            pages.append({"pdf_page": number, "method": "TEXT" if text.strip() else "UNRECOGNIZED",
                          "text": text, "raw_lines": rows, "warnings": warnings, "unrecognized_images": images})
            if progress:
                progress("PROCESSING", {"page_number": number, "page_count": len(document)})
    _exclude_repeated_page_numbers(pages, sizes)
    _exclude_repeated_cover_titles(pages, sizes)
    _exclude_repeated_publisher_bands(pages, sizes)
    if not any(page["text"].strip() for page in pages):
        raise ValueError("整份原件未取得可读文字层，请直接查看已保存原件")
    return pages


def native_completeness(pages):
    """文字可用与图片完成度分开，不用完整页数冒称所有内容已识别。"""
    if not pages or not all("unrecognized_images" in page for page in pages):
        return {}
    count = sum(len(page["unrecognized_images"]) for page in pages)
    completeness = {"text_layer": "AVAILABLE", "image_content": "PENDING_RECOGNITION" if count else "NOT_PRESENT",
                    "unrecognized_image_count": count}
    missing_pages = sum(page.get("method") == "UNRECOGNIZED" for page in pages)
    if missing_pages:
        completeness.update(text_layer="PARTIAL", unrecognized_page_count=missing_pages)
    return {"body_status": "NATIVE_TEXT_WITH_IMAGE_GAPS" if count else "NATIVE_TEXT",
            "processing_completeness": completeness}


LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
