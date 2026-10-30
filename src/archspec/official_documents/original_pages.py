"""仅原件的按页阅读：验证同一PDF后渲染，不识别、不补写正文。"""

import hashlib
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

from archspec.official_documents.policy import policy_for_source

NAVIGATION_VERSION = "ORIGINAL_PDF_PAGES_V1"
MAX_READING_PIXELS = 4_000_000


def navigation(documents, record):
    """只公布服务端已登记的有界页数；旧原件记录与前三页预览不改。"""
    source = documents._source(record["source_id"])
    policy = policy_for_source(source)
    count, size = record.get("page_count"), record.get("size_bytes")
    digest = record.get("sha256")
    if (record.get("source_format") == "HTML" or source.get("source_format") == "HTML"
            or record.get("body_status") != "ORIGINAL_ONLY" or record.get("chunks") != []
            or type(count) is not int or not 1 <= count <= min(500, policy.max_pages)
            or type(size) is not int or not 0 < size <= policy.max_bytes
            or not isinstance(digest, str) or len(digest) != 64):
        return None
    if record.get("preview_pages") != list(range(1, min(3, count) + 1)):
        return None
    pin = hashlib.sha256(f"ORIGINAL_ONLY_V1\n{record['source_id']}\n{digest}".encode()).hexdigest()
    if pin != record.get("snapshot_id"):
        return None
    return {"version": NAVIGATION_VERSION, "page_count": count}


def reading_page(documents, source_id, snapshot_id, page_number):
    """按需验证原件分块和完整哈希；缓存图片也不能绕当前权限或损坏原件。"""
    from archspec.official_documents.originals import (
        _folder,
        _put,
        _read,
        get_original,
        original_parts,
    )

    record = get_original(documents, source_id, snapshot_id)
    limits = navigation(documents, record)
    if limits is None or type(page_number) is not int or not 1 <= page_number <= limits["page_count"]:
        raise LookupError("该原页不在读取预算内，请打开完整原件")
    folder = _folder(documents, source_id, snapshot_id)
    fixed = _read(documents, folder / "record.json")
    parts = record.get("parts")
    if (not isinstance(parts, list) or not 1 <= len(parts) <= 20
            or any(part.get("file") != f"original-{index:04d}.part" for index, part in enumerate(parts))):
        raise ValueError("原件分块身份不完整")
    cached = folder / f"reading-page-{page_number}-v1.png"
    # 临时文件与用户原件分离；只从已核对分块写入，解析器不再打开可被替换的原件路径。
    with TemporaryDirectory(prefix="archspec-original-page-") as temporary:
        path = Path(temporary) / "checked.pdf"
        whole, size = hashlib.sha256(), 0
        with path.open("wb") as target:
            for content in original_parts(documents, record):
                size += len(content)
                if size > record["size_bytes"]:
                    raise ValueError("原件长度与固定记录不一致")
                whole.update(content)
                target.write(content)
        if size != record["size_bytes"] or whole.hexdigest() != record["sha256"]:
            raise ValueError("原件身份与固定记录不一致")
        if cached.exists():
            image = _read(documents, cached)
        else:
            with pymupdf.open(path) as pdf:
                if pdf.is_encrypted or pdf.is_repaired or len(pdf) != limits["page_count"]:
                    raise ValueError("原件无法完整解析，请下载查阅")
                page = pdf[page_number - 1]
                area = page.rect.get_area()
                if not math.isfinite(area) or area <= 0:
                    raise ValueError("原页尺寸无效")
                scale = min(2, math.sqrt(MAX_READING_PIXELS / area) * .99)
                pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                if pixels.width * pixels.height > MAX_READING_PIXELS:
                    raise ValueError("原页图像超出读取预算")
                image = pixels.tobytes("png")
        # 渲染/缓存读取后再次核当前许可和不可变记录，撤销或替换时不发布图片。
        latest = get_original(documents, source_id, snapshot_id)
        if latest != record or _read(documents, folder / "record.json") != fixed:
            raise ValueError("原件读取条件已变化")
        if not cached.exists():
            _put(documents, cached, image)
        # 私有发布器也可能经历长事务；返回前仍需复核许可。
        get_original(documents, source_id, snapshot_id)
        if _read(documents, folder / "record.json") != fixed:
            raise ValueError("原件读取条件已变化")
        return image
