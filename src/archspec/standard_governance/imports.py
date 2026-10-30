"""只从服务端已有登记/固定快照建立观察，不下载、不识别、更不自动核验。"""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import date, datetime
from enum import Enum
from pathlib import Path

import pymupdf
from sqlalchemy import inspect, select

from archspec.config import settings
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.official_documents.registry import OfficialSourceRegistry
from archspec.official_documents.service import OfficialDocumentService
from archspec.standards.repository import StandardMetadataRepository


def stable_hash(value) -> str:
    """固定事实指纹排除观察时间；日期/枚举有统一表示，便于兼容重读。"""
    def encode(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"不能生成固定指纹的类型：{type(item).__name__}")
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=encode)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def safe_child(folder: Path, filename: str) -> Path:
    resolved = (folder / filename).resolve()
    if resolved.parent != folder.resolve():
        raise ValueError("登记的原件路径不在本项目资料目录")
    return resolved


class GovernanceImportService:
    def __init__(self, session, data_dir=None):
        self.session = session
        self.data_dir = Path(data_dir or session.info.get("archspec_data_dir") or settings.data_dir)

    def describe_document(self, document_id):
        doc = DocumentRepository(self.session).get(document_id)
        if doc is None:
            raise LookupError("服务端没有这份文档登记")
        tables = inspect(self.session.get_bind()).get_table_names()
        metadata = (StandardMetadataRepository(self.session).get_for_document(document_id)
                    if "standard_metadata" in tables else None)
        chunks = [asdict(item) for item in
                  DocumentChunkRepository(self.session).list_for_document(document_id)]
        facts = {key: value for key, value in asdict(metadata).items()
                 if key not in {"id", "checked_at"}} if metadata else {}
        content_manifest = stable_hash({"text": doc.extracted_text, "chunks": chunks})
        declaration = {
            "standard_code": metadata.standard_code if metadata else f"LOCAL-UNRESOLVED:{doc.id}",
            "title": metadata.title if metadata else doc.original_name,
            "publisher": metadata.authority if metadata else "未登记发布机构",
            "published_at": metadata.published_at if metadata else None,
            "effective_at": metadata.effective_at if metadata else None,
            "declared_lifecycle": str(metadata.lifecycle_status) if metadata else "UNKNOWN",
            "source_type": ("OFFICIAL_DECLARED" if metadata and
                            str(metadata.source_type) == "OFFICIAL" else "LOCAL_UPLOAD"),
            "source_provider": "STANDARD_METADATA" if metadata else "LOCAL_UPLOAD",
            "announcement_url": metadata.source_url if metadata else None,
            "attachment_kind": "UNCONFIRMED",
            "file_sha256": doc.sha256,
        }
        issues, pages, trace = [], None, "LEGACY_UNLOCATED"
        path = safe_child(self.data_dir / "uploads", doc.stored_name)
        try:
            if file_hash(path) != doc.sha256:
                issues.append("RAW_FILE_CHANGED")
            if doc.media_type == "application/pdf":
                with pymupdf.open(path) as pdf:
                    pages = len(pdf)
                    if pdf.is_repaired or pdf.is_encrypted or pages < 1:
                        issues.append("RAW_FILE_INVALID")
                    else:
                        trace = "PDF_PAGE"
                        if any(not page.get_text().strip() for page in pdf):
                            issues.append("PROCESSING_QUALITY_UNVERIFIED")
                        if any(not isinstance(chunk["page_start"], int) or
                               not isinstance(chunk["page_end"], int) or
                               not 1 <= chunk["page_start"] <= chunk["page_end"] <= pages
                               for chunk in chunks):
                            issues.append("FULL_TEXT_TRACE_UNRELIABLE")
            else:
                issues.append("FULL_TEXT_TRACE_UNRELIABLE")
        except (OSError, ValueError, RuntimeError):
            issues.append("RAW_FILE_MISSING_OR_INVALID")
        if str(doc.status) != "PARSED" or not chunks:
            issues.append("PROCESSING_INCOMPLETE")
        return {
            "declaration": declaration,
            "artifact": {
                "artifact_kind": "UPLOADED_DOCUMENT", "legacy_document_id": doc.id,
                "file_sha256": doc.sha256, "processing_version": "LEGACY_NATIVE_PARSER",
                "content_manifest_sha256": content_manifest,
                "page_count": pages, "trace_level": trace,
                "processing_status": "READY" if str(doc.status) == "PARSED" else "FAILED",
                "quality_issues": sorted(set(issues)),
            },
            "legacy_kind": "DOCUMENT", "legacy_key": doc.id,
            "legacy_manifest": stable_hash({"document_id": doc.id, "file_sha256": doc.sha256,
                                            "metadata": facts, "content": content_manifest}),
            "runtime_issues": issues,
        }

    def describe_snapshot(self, source_id, snapshot_id=None):
        cache = self.data_dir / "official-cache"
        source = OfficialSourceRegistry(cache).get(source_id)
        folder = cache / source_id
        if snapshot_id is not None and not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
            raise ValueError("固定快照编号无效")
        path = folder / (f"{snapshot_id}.json" if snapshot_id else "latest.json")
        if not path.exists():
            return self.describe_official_source(source_id)
        # 同一实际字节同时支持固定头和指纹，避免两次读取夹入新的快照。
        documents=OfficialDocumentService(cache)
        bound=documents._snapshot_view(path,source_id)
        if bound is None:
            snapshot_content=path.read_bytes()
            snapshot=json.loads(snapshot_content)
            documents._validate_snapshot(snapshot,source_id)
            view=None
        else:
            view,snapshot_content=bound
            snapshot=view.header()
        if snapshot_id is not None and snapshot["snapshot_id"] != snapshot_id:
            raise ValueError("固定快照内容与请求身份不一致")
        declaration = self._source_declaration(source)
        declaration["file_sha256"] = snapshot["sha256"]
        issues = []
        try:
            # 新共享原件与旧来源目录使用同一个固定解析入口；不调用带授权的 get。
            raw = OfficialDocumentService(cache).raw_file_path(snapshot, source_id)
            if file_hash(raw) != snapshot["sha256"]:
                issues.append("RAW_FILE_CHANGED")
            with pymupdf.open(raw) as pdf:
                if pdf.is_repaired or pdf.is_encrypted or len(pdf) != snapshot["page_count"]:
                    issues.append("RAW_FILE_INVALID")
        except (OSError, ValueError, RuntimeError):
            issues.append("RAW_FILE_MISSING_OR_INVALID")
        if view is not None:
            if view.quality_unverified:issues.append('PROCESSING_QUALITY_UNVERIFIED')
        else:
            for page in snapshot["pages"]:
                if page.get("warnings") or str(page.get("method", "")).upper() == "OCR":
                    issues.append("PROCESSING_QUALITY_UNVERIFIED")
        declaration["attachment_kind"] = source["kind"]
        content=(view.content_manifest_sha256 if view is not None else stable_hash({
            'pages':snapshot['pages'],'chunks':snapshot['chunks'],
            'processing_version':snapshot.get('processor_version','LEGACY')}))
        manifest=view.hash_with(source=source) if view is not None else stable_hash({'source':source,'snapshot':snapshot})
        return {
            "declaration": declaration,
            "artifact": {
                "artifact_kind": "OFFICIAL_SNAPSHOT", "legacy_source_id": source_id,
                "legacy_snapshot_id": snapshot["snapshot_id"], "file_sha256": snapshot["sha256"],
                "processing_version": snapshot.get("processor_version", "LEGACY"),
                "content_manifest_sha256": content, "page_count": snapshot["page_count"],
                "trace_level": "PDF_PAGE", "processing_status": "READY",
                "quality_issues": sorted(set(issues)),
            },
            # 当前登记声明和固定正文出生头是两个层次，生命周期不得借给别的头。
            "fixed_identity": {
                "source_id": snapshot["source_id"], "snapshot_id": snapshot["snapshot_id"],
                "title": snapshot["title"], "standard_code": snapshot["standard_code"],
                "file_sha256": snapshot["sha256"],
                "snapshot_fingerprint": hashlib.sha256(snapshot_content).hexdigest(),
            },
            "legacy_kind": "OFFICIAL_SNAPSHOT",
            "legacy_key": f"{source_id}:{snapshot['snapshot_id']}",
            "legacy_manifest": manifest, "runtime_issues": sorted(set(issues)),
        }

    @staticmethod
    def _source_declaration(source):
        return {
            "standard_code": source["standard_code"], "title": source["title"],
            "publisher": "住房和城乡建设部（来源登记声明，待核验）",
            "source_type": "OFFICIAL_DISCOVERED", "source_provider": "MOHURD",
            "announcement_url": source["page_url"], "attachment_url": source["pdf_url"],
            "attachment_kind": source["kind"], "discovery_run_id": source.get("discovery_run_id"),
            "announcement_sha256": source.get("announcement_hash"),
            "announcement_fingerprint_kind": "RAW_TEXT" if source.get("announcement_hash") else "UNKNOWN",
        }

    def describe_official_source(self, source_id):
        source = OfficialSourceRegistry(self.data_dir / "official-cache").get(source_id)
        return {"declaration": self._source_declaration(source), "artifact": None,
                "legacy_kind": "OFFICIAL_SOURCE", "legacy_key": source_id,
                "legacy_manifest": stable_hash(source), "runtime_issues": []}

    def describe_catalog(self, catalog_id):
        from archspec.standard_catalog.models import StandardCatalogRow

        row = self.session.get(StandardCatalogRow, catalog_id)
        if row is None:
            raise LookupError("服务端没有这条题录登记")
        declaration = {"standard_code": row.standard_code, "title": row.title,
                       "publisher": row.authority, "source_type": "OFFICIAL_DECLARED",
                       "source_provider": row.source_provider, "announcement_url": row.official_url,
                       "published_at": row.published_at, "effective_at": row.effective_at,
                       "declared_lifecycle": str(row.lifecycle_status), "attachment_kind": "UNCONFIRMED"}
        return {"declaration": declaration, "artifact": None, "legacy_kind": "CATALOG",
                "legacy_key": catalog_id, "legacy_manifest": stable_hash(declaration),
                "runtime_issues": []}

    def describe(self, kind, key, snapshot_id=None):
        if kind == "DOCUMENT":
            return self.describe_document(key)
        if kind == "CATALOG":
            return self.describe_catalog(key)
        if kind == "OFFICIAL_SOURCE":
            return self.describe_official_source(key)
        if kind == "OFFICIAL_SNAPSHOT":
            if ":" in key and snapshot_id is None:
                key, snapshot_id = key.rsplit(":", 1)
            return self.describe_snapshot(key, snapshot_id)
        raise ValueError("只支持服务端已保存的文档、题录、官方来源和固定快照")

    def from_record(self, kind, key, snapshot_id=None, *, commit=True):
        from archspec.standard_governance.service import GovernanceService

        GovernanceService(self.session).repo.require_available()
        description = self.describe(kind, key, snapshot_id)
        return GovernanceService(self.session).observe_source(
            description["declaration"], description["artifact"],
            legacy_kind=description["legacy_kind"], legacy_key=description["legacy_key"],
            legacy_manifest=description["legacy_manifest"], commit=commit)

    def record_keys(self):
        """迁移枚举已有登记；不递归扫描未知目录，也不依赖网页提供路径。"""
        from archspec.standard_catalog.models import StandardCatalogRow
        from archspec.standards.models import StandardMetadataRow

        tables = inspect(self.session.get_bind()).get_table_names()
        result = []
        if "standard_metadata" in tables:
            result.extend(("DOCUMENT", item, None) for item in
                          self.session.scalars(select(StandardMetadataRow.document_id)))
        if "standard_catalog" in tables:
            result.extend(("CATALOG", item, None) for item in
                          self.session.scalars(select(StandardCatalogRow.id)))
        for source in OfficialSourceRegistry(self.data_dir / "official-cache").list():
            source_id = source["source_id"]
            result.append(("OFFICIAL_SOURCE", source_id, None))
            folder = self.data_dir / "official-cache" / source_id
            for path in sorted(folder.glob("*.json")):
                if re.fullmatch(r"[a-f0-9]{64}", path.stem):
                    result.append(("OFFICIAL_SNAPSHOT", source_id, path.stem))
        return result
