"""固定正文的旁挂来源档案；不修改 PDF、正文快照或专业核验记录。"""

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from uuid import UUID

from archspec.official_documents.downloader import validate_attachment_url
from archspec.official_documents.local_policy import reviewed_publication
from archspec.official_documents.processing_cache import checked_digest, publish_json, stable_hash
from archspec.official_documents.registry import SOURCES, validate_source_pair


def code_level(code):
    """正文与原件共用编号类别线索；不据此认证现行性或适用范围。"""
    return "NATIONAL" if re.match(r"^GB(?:/T)?\s", code) else (
        "INDUSTRY" if re.match(r"^(?:JGJ|CJJ)(?:/T)?\s", code) else None)


def _field(value=None, state="UNKNOWN", basis="尚无对应固定资料支持"):
    return {"value": value, "state": state, "basis": basis}


def _notice(cache_dir, record):
    """只读正文当时登记的公告；不拿最新登记表补旧快照。"""
    try:
        run_id = record.get("discovery_run_id")
        if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
            return None
        expected_hash = checked_digest(record.get("announcement_hash"))
        path = cache_dir.parent / "official-discovery" / "runs" / f"{run_id}.json"
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        content = path.read_bytes()
        run = json.loads(content)
        if not isinstance(run, dict) or run.get("id") != run_id:
            return None
        matches = []
        for item in run.get("results", []):
            if not isinstance(item, dict):
                continue
            # 同号跨书、不同附件、相似网址均不能给该正文的来源字段背书。
            if (item.get("text_sha256") == expected_hash
                    and item.get("standard_code") == record["standard_code"]
                    and item.get("standard_title") == record["title"]
                    and item.get("url") == record["page_url"]
                    and any(isinstance(a, dict) and a.get("url") == record["pdf_url"]
                            for a in item.get("attachments", []))):
                matches.append(item)
        if len(matches) != 1:
            return None
        # 只存公告观察，不复制本次检索问题、任务书或整个运行日志。
        keys = ("title", "url", "standard_title", "standard_code", "text_sha256",
                "authority", "scope", "announcement_number", "published_at", "effective_at",
                "notice_type", "change_scope", "change_evidence", "change_evidence_truncated", "withdrawn_codes",
                "evidence_excerpt", "attachments", "region", "transport_status", "observed_url",
                "review_id", "region_components", "document_type")
        return {"run_id": run_id, "run_sha256": hashlib.sha256(content).hexdigest(),
                "notice": {k: matches[0][k] for k in keys if k in matches[0]}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # 缺少旧公告不等于没有正文；字段保持未知，不联网补成“已核实”。
        return None


def _build(cache_dir, record, fingerprint):
    source_url, attachment_url = record["page_url"], record["pdf_url"]
    try:
        if source_url != SOURCES["gb55031"]["page_url"]:
            validate_source_pair(source_url, attachment_url,
                publication_kind=(record.get("source_metadata") or {}).get("provenance"))
    except (TypeError, ValueError):
        source_url = None
    try:
        context = ({"page_url": source_url}
                   if (record.get("source_metadata") or {}).get("provenance") == "OFFICIAL_COPY" else {})
        if record.get("source_format") == "HTML":
            validate_source_pair(source_url, attachment_url, publication_kind="OFFICIAL_WEB_BODY")
        else:
            validate_attachment_url(attachment_url, allow_legacy=True, **context)
    except (TypeError, ValueError):
        attachment_url = None
    fields = {key: _field() for key in (
        "level", "region", "publisher", "announcement_number", "edition", "effective_at", "withdrawal")}
    code = record["standard_code"]
    level = code_level(code)
    if level:
        fields["level"] = _field(level, "CODE_CLUE", "固定正文编号类别线索，不认证法律效力或现行性")
    if code:
        fields["edition"] = _field(code, "SNAPSHOT_RECORDED", "固定正文登记编号，版本仍待核验")
    observation = _notice(cache_dir, record) if source_url and attachment_url else None
    if observation:
        item = observation["notice"]
        local = reviewed_publication(source_url)
        if local is not None:
            fields["level"] = _field("LOCAL", "NOTICE_OBSERVED", "精确登记的地方公告来源，不认证现行性或适用性")
            fields["region"] = _field(local.region, "NOTICE_OBSERVED", "精确登记公告的适用地域线索，未专业核验")
            if item.get("review_id") == local.review_id and item.get("document_type") == local.document_type:
                fields["document_type"] = _field(local.document_type, "NOTICE_OBSERVED", "审阅资料类别，不认证法律效力或现行性")
            fields["edition"] = _field(item["standard_title"], "NOTICE_OBSERVED", "已保存公告题名，不等于截至今日最新版本")
        elif (item.get("notice_type") == "OFFICIAL_COPY" and item.get("scope") == "LOCAL"
              and isinstance(item.get("region"), str) and item["region"] in item["standard_title"]):
            # 同一保存公告、附件与原件身份下的地方观察；不认证现行性或法律效力。
            fields["level"] = _field("LOCAL", "NOTICE_OBSERVED", "已保存政府发布页的地方主文件题名")
            fields["region"] = _field(item["region"], "NOTICE_OBSERVED", "对应地方主文件题名的地域，不推断行政包含关系")
        excerpt = str(item.get("evidence_excerpt") or "")
        for key, notice_key in (("publisher", "authority"), ("announcement_number", "announcement_number")):
            value = item.get(notice_key)
            # 解析器的占位机关不能变成事实；必须在保存的可见文字中找到支持。
            if isinstance(value, str) and value.strip() and value in excerpt:
                fields[key] = _field(value, "NOTICE_OBSERVED", "对应公告的已保存可见文字，未专业核验")
        value = item.get("effective_at")
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
                chinese = f"{parsed.year}年{parsed.month}月{parsed.day}日"
                if value in excerpt or chinese in excerpt:
                    fields["effective_at"] = _field(value, "NOTICE_OBSERVED", "对应公告实施日期声明，不等于现行性已核验")
            except ValueError:
                pass
        # 部分废止清单不证明该本规范已废止；本轮不自动推断整本生命周期。
    return {
        "schema_version": "FIXED_SOURCE_V1", "source_id": record["source_id"],
        "snapshot_id": record["snapshot_id"], "snapshot_fingerprint": fingerprint,
        "file_sha256": record["sha256"], "title": record["title"], "standard_code": code,
        "source_url": source_url, "attachment_url": attachment_url,
        "snapshot_path": f"{record['source_id']}/{record['snapshot_id']}.json",
        "fetched_at": record.get("last_fetched_at") or record["processed_at"],
        "fields": fields, "notice_observation": observation,
    }


def fixed_metadata(cache_dir: Path, record: dict, content: bytes, expected_id=None):
    """首次追加冻结档案，后续仅核对。调用者须先检查正文及读取条件。"""
    source, snapshot = record["source_id"], checked_digest(record["snapshot_id"])
    if not re.fullmatch(r"gb55031|official-[a-f0-9]{32}", source):
        raise ValueError("来源档案身份无效")
    if expected_id is not None:
        checked_digest(expected_id)
    fingerprint = hashlib.sha256(content).hexdigest()
    root = cache_dir / "source-metadata"
    binding_path = root / "bindings" / source / f"{snapshot}.json"
    identity = {"source_id": source, "snapshot_id": snapshot,
                "snapshot_fingerprint": fingerprint, "file_sha256": record["sha256"]}
    if not binding_path.exists():
        # 带期望编号的读取不能悄悄创建另一份观察来匹配用户给出的编号。
        if expected_id is not None:
            raise ValueError("该固定正文尚无对应来源档案绑定")
        value = _build(cache_dir, record, fingerprint)
        metadata_id = stable_hash(value)
        publish_json(root / "observations" / f"{metadata_id}.json", {**value, "metadata_id": metadata_id})
        publish_json(binding_path, {**identity, "metadata_id": metadata_id})
    try:
        binding = json.loads(binding_path.read_bytes())
        if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in identity.items()):
            raise ValueError("正文身份与来源档案绑定不一致")
        metadata_id = checked_digest(binding["metadata_id"])
        if expected_id is not None and expected_id != metadata_id:
            raise ValueError("元数据编号不属于该固定正文")
        value = json.loads((root / "observations" / f"{metadata_id}.json").read_bytes())
        if (not isinstance(value, dict) or value.get("metadata_id") != metadata_id
                or stable_hash({k: v for k, v in value.items() if k != "metadata_id"}) != metadata_id
                or any(value.get(k) != v for k, v in identity.items())):
            raise ValueError("来源档案内容指纹或正文身份不一致")
        return value
    except (OSError, KeyError, TypeError, ValueError) as error:
        # 不覆盖或“修复”坏档案；保留问题供核对，也不改正文原件。
        raise ValueError("来源档案或指纹无法核对，未覆盖旧记录") from error
