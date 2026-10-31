"""已存历史页文字的独立派生身份；不推定旧解析器或OCR运行配置。"""

import hashlib

from archspec.official_documents.processing_cache import ProcessingSpec, stable_hash, validate_pages

PARSER = "saved-fixed-snapshot-pages-v1"
OCR = "historical-page-text-not-rerun"
SCHEMA = "FIXED_SNAPSHOT_PAGE_TEXT_V1"


def saved_pages_spec(parent, content, current):
    """只消费已核固定父记录；调用方负责原件、当前权限和固定文件读取。"""
    if (parent.get("storage_layout") is not None
            or "processing_identity" in parent or "extraction_id" in parent
            or parent.get("source_format", "PDF") != "PDF"
            or not isinstance(parent.get("processor_version"), str)
            or not parent["processor_version"]):
        raise ValueError("仅缺少历史提取身份的完整PDF旧记录可复用已存页文字")
    validate_pages(parent["pages"])
    config = {"schema": SCHEMA, "source_id": parent["source_id"],
              "parent_snapshot_id": parent["snapshot_id"],
              "parent_snapshot_sha256": hashlib.sha256(content).hexdigest(),
              "pages_sha256": stable_hash(parent["pages"]),
              "legacy_processor_version": parent["processor_version"]}
    return ProcessingSpec(PARSER, OCR, current.chunker_version, config, current.chunk_config)


def validate_saved_pages(record):
    """派生出处必须与处理身份及原页文字同时绑定，不能只改一个展示字段。"""
    provenance = record.get("saved_page_provenance")
    if provenance is None:
        return
    identity = record.get("processing_identity", {})
    fields = {"schema", "source_id", "parent_snapshot_id", "parent_snapshot_sha256",
              "pages_sha256", "legacy_processor_version"}
    if (not isinstance(provenance, dict) or set(provenance) != fields
            or provenance["schema"] != SCHEMA or provenance["source_id"] != record["source_id"]
            or identity.get("parser_version") != PARSER or identity.get("ocr_version") != OCR
            or stable_hash(provenance) != identity.get("extraction_config_hash")
            or provenance["pages_sha256"] != stable_hash(record["pages"])
            or provenance["parent_snapshot_id"] not in record.get("permission_parent_ids", [])):
        raise ValueError("已存页文字的派生出处与固定处理身份不同")
