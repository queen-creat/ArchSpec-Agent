"""官方原件兜底：复用已有私有对象与业务账本，不伪造正文或向量。"""
import hashlib
import json
from datetime import UTC, datetime
from typing import ClassVar

import pymupdf

from archspec.official_documents.page_images import PageImageRepository
from archspec.official_documents.processing_cache import checked_digest
from archspec.persistence import business_files

MESSAGE = "已获取官方 PDF 原件，但系统暂无法自动提取文本，请直接查看原页对照。"
PART_BYTES = 4 * 1024 * 1024


class PdfInputRejected(ValueError):
    """仅发布服务端确认的PDF状态和登记预算，不透传解析异常原文。"""
    MESSAGES: ClassVar[dict[str, str]] = {
        "SIZE_LIMIT": "附件大小不在本次处理预算内",
        "ENCRYPTED": "附件已加密，当前无法提取可检索正文",
        "DAMAGED": "PDF结构损坏或不完整，不能作为完整正文",
    }

    def __init__(self, code, *, max_pages=None):
        if code == "PAGE_LIMIT" and type(max_pages) is int and max_pages > 0:
            detail = f"附件页数不在本次1—{max_pages}页处理预算内"
        elif code in self.MESSAGES:
            detail = self.MESSAGES[code]
        else:
            raise ValueError("PDF处理原因未登记")
        self.public_message = detail + "；官方原件已保留，可下载查阅，未发布可检索正文。"
        super().__init__(self.public_message)


def _publisher(documents):
    runtime = documents._runtime_bindings
    return runtime.business_files if runtime is not None else business_files.selected_files()


def _folder(documents, source_id, snapshot_id):
    documents._source(source_id)
    return documents.cache_dir / "original-only" / source_id / checked_digest(snapshot_id)


def _put(documents, path, content):
    publisher = _publisher(documents)
    if publisher is not None:
        publisher.write(path, content)  # 原协议完成私有上传、回读、SQL签收和本机落盘。
    else:
        PageImageRepository._publish(path, content)


def _read(documents, path):
    publisher = _publisher(documents)
    if publisher is not None:
        publisher.assert_readable(path)
    return path.read_bytes()


def _check_read(documents, record):
    from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
    source_id, pin = record["source_id"], record["snapshot_id"]
    documents._source(source_id)
    # 原件不能绕过同来源同文件已有正文的读取限制。
    for parent in documents._same_blob_history(source_id, record["sha256"], pin):
        documents._check_read(source_id, parent, "LOCAL_FULL_TEXT_READ")
    if documents.governance is not None:
        documents.governance.assert_snapshot_read(source_id, pin, "LOCAL_FULL_TEXT_READ")
    else:
        with documents._open_database_session() as session:
            LegacyAdmissionAdapter(session, documents.cache_dir.parent).assert_snapshot_read(
                source_id, pin, "LOCAL_FULL_TEXT_READ")


def save_original(documents, source_id, raw_path, digest, size_bytes, *, error=None):
    """失败原因归当前任务；既有原件快照保持不可覆盖。"""
    from archspec.official_documents.remote_compute import ComputeUnavailable
    from archspec.official_documents.runtime_ocr import RuntimeOcrDisabled
    if error is None:
        import sys
        error = sys.exception()  # 旧HTML调用在except中进入；保留当前原因，不覆盖旧原件。
    import logging

    from archspec.persistence.migration_operator import safe_diagnostic
    logging.getLogger(__name__).warning("OFFICIAL_ORIGINAL_ONLY source=%s reason=%s code=%s diagnostic=%s",
        source_id, type(error).__name__ if error is not None else "UNKNOWN",
        error.code if isinstance(error, ComputeUnavailable) else "PROCESSING_UNAVAILABLE",
        safe_diagnostic(error) if error is not None else None)
    record = _save_original(documents, source_id, raw_path, digest, size_bytes)
    if isinstance(error, (ComputeUnavailable, PdfInputRejected)):
        message = error.public_message
    elif isinstance(error, RuntimeOcrDisabled):
        message = "本实例未开启运行期文字识别；官方原件已保留，可下载查阅，未发布可检索正文。"
    else:
        # 未知异常只说明当前处理缺口，不编造独立计算故障或外传底层详情。
        message = "正文暂无法自动处理；官方原件已保留，可下载查阅，未发布可检索正文。"
    return {**record, "processing_error": message}


def _save_original(documents, source_id, raw_path, digest, size_bytes):
    source = documents._source(source_id)
    pin = hashlib.sha256(f"ORIGINAL_ONLY_V1\n{source_id}\n{digest}".encode()).hexdigest()
    folder = _folder(documents, source_id, pin)
    existing = folder / "record.json"
    if existing.exists():
        return get_original(documents, source_id, pin)
    parts, whole = [], hashlib.sha256()
    with raw_path.open("rb") as original:
        while content := original.read(PART_BYTES):
            name = f"original-{len(parts):04d}.part"
            whole.update(content)
            _put(documents, folder / name, content)
            parts.append({"file": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    if whole.hexdigest() != digest or sum(p["size_bytes"] for p in parts) != size_bytes:
        raise OSError("原件尚未完整保存")
    previews, page_count = [], None
    try:
        if source.get("source_format") == "HTML":
            raise ValueError("网页原件不冒称PDF预览；独立计算不可用时保留下载")
        with pymupdf.open(raw_path) as pdf:
            page_count = len(pdf)
            for index in range(min(3, page_count)):
                page = pdf[index]
                scale = min(.8, (1_000_000 / max(1, page.rect.get_area())) ** .5)
                previews.append(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png"))
    except Exception:  # noqa: BLE001 - 预览失败不能否决已保全原件；不尝试修复解析器。
        import logging
        logging.getLogger(__name__).debug("官方原件预览尚不可生成，已保全的原件仍可下载")
    for number, content in enumerate(previews, 1):
        _put(documents, folder / f"page-{number}.png", content)
    from archspec.official_documents.local_policy import reviewed_publication
    local = reviewed_publication(source.get("page_url", ""))
    metadata = dict(source.get("source_metadata") or {})
    if local:
        metadata.update(level="LOCAL", region=local.region, publisher=local.authority)
    record = {**source, "source_metadata": metadata, "source_id": source_id, "snapshot_id": pin, "sha256": digest,
        "size_bytes": size_bytes, "page_count": page_count, "preview_pages": list(range(1, len(previews)+1)),
        "body_status": "ORIGINAL_ONLY", "status": "PENDING_RECOGNITION", "chunks": [],
        "message": ("已获取官方网页原件，但系统暂无法处理正文，请下载原件或查看官方页面。"
                    if source.get("source_format") == "HTML" else MESSAGE), "processed_at": datetime.now(UTC).isoformat(), "parts": parts}
    # 完成标记最后签收，前面的分块和预览均已进入同一云端业务账本。
    _put(documents, existing, json.dumps(record, ensure_ascii=False, sort_keys=True).encode())
    return record


def get_original(documents, source_id, snapshot_id):
    record = json.loads(_read(documents, _folder(documents, source_id, snapshot_id) / "record.json"))
    if record["source_id"] != source_id or record["snapshot_id"] != snapshot_id:
        raise ValueError("原件快照身份不一致")
    _check_read(documents, record)
    from archspec.official_documents.source_metadata import code_level
    # 只补读取响应中的编号类别，已登记地域优先；不改写旧原件快照。
    fields = {"level": code_level(record["standard_code"]), **(record.get("source_metadata") or {})}
    return {**record, "source_metadata": fields}


def original_parts(documents, record):
    folder = _folder(documents, record["source_id"], record["snapshot_id"])
    for part in record["parts"]:
        content = _read(documents, folder / part["file"])
        if len(content) != part["size_bytes"] or hashlib.sha256(content).hexdigest() != part["sha256"]:
            raise OSError("原件分块暂不可读")
        yield content


def preview_image(documents, record, page):
    if page not in record["preview_pages"]:
        raise LookupError("该页尚无预览，请下载官方原件查看")
    return _read(documents, _folder(documents, record["source_id"], record["snapshot_id"]) / f"page-{page}.png")
