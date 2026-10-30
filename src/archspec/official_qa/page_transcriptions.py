"""独立原页核读记录：只补阅读，不改 OCR、不授予专业核验或使用许可。"""

import hashlib
import json
import re
from datetime import datetime
from itertools import islice

FIELDS = {"version", "source_id", "snapshot_id", "snapshot_fingerprint", "chunk_id",
          "clause_number", "page_start", "page_end", "file_sha256", "raw_text_sha256",
          "page_png_sha256", "transcription", "method", "status", "reviewer_role", "read_at"}
HEX = re.compile(r"[a-f0-9]{64}")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def load_transcription(documents, row):
    """只选同一完整单页片段；缺失/歧义/损坏记录不能阻止旧原文阅读。

    文件身份不代表文字正确。核读始终标记 AI 开发者、待人工确认；不接受
    浏览器提供路径，也不挑选多条冲突记录中的“最新版”。
    """
    folder = documents.cache_dir.parent / "official-page-transcriptions"
    if not folder.is_dir() or folder.is_symlink():
        return None
    prefix = digest(row["chunk_id"].encode())
    paths = list(islice(folder.glob(f"{prefix}.*.json"), 2))
    if len(paths) != 1:
        return None
    path = paths[0]
    if path.is_symlink() or path.stat().st_size > 32_000:
        return None
    try:
        content = path.read_bytes()
        record = json.loads(content)
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        # 文本编辑器可保留一个结尾换行；摘要仍绑定实际字节，任意空格不获豁免。
        if (not isinstance(record, dict) or set(record) != FIELDS or content not in (canonical, canonical + b"\n")
                or path.name != f"{prefix}.{digest(content)}.json"):
            return None
        if any(record[key] != row[key] for key in ("source_id", "snapshot_id", "snapshot_fingerprint",
                  "chunk_id", "clause_number", "page_start", "page_end")):
            return None
        if (type(record["page_start"]) is not int or type(record["page_end"]) is not int
                or record["page_start"] != record["page_end"]):
            return None
        if any(not isinstance(record[key], str) or not HEX.fullmatch(record[key]) for key in
               ("snapshot_id", "snapshot_fingerprint", "file_sha256", "raw_text_sha256", "page_png_sha256")):
            return None
        if (record["version"] != "FIXED_PAGE_TRANSCRIPTION_V1"
                or record["method"] != "AI_VISUAL_TRANSCRIPTION"
                or record["status"] != "PENDING_HUMAN_CONFIRMATION"
                or record["reviewer_role"] != "AI_DEVELOPER"):
            return None
        text = record["transcription"]
        if (not isinstance(text, str) or not 20 <= len(text) <= 6000
                or any(ord(c) < 32 and c not in "\n\t\r" for c in text)):
            return None
        if datetime.fromisoformat(record["read_at"]).tzinfo is None:
            return None
        if (record["raw_text_sha256"] != digest(row["text"].encode())
                or record["file_sha256"] != row["source_metadata"]["file_sha256"]):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    # 此处继续沿用原件完整校验和两种独立读取权限，记录不能为撤回许可解锁。
    documents._check_read(row["source_id"], row["snapshot_id"], "LOCAL_EXCERPT")
    if digest(documents.page_image(row["source_id"], row["snapshot_id"], row["page_start"])) != record["page_png_sha256"]:
        return None
    return {**record, "transcription_id": digest(content), "original_quote": row["text"],
            "evidence_id": row["evidence_id"]}
