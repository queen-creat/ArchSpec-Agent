"""恢复后重建的固定快照视图；只复用不可变内容，不保存当前权限或状态。"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf8")


def composite_hash(snapshot_bytes, **fields):
    """对原组合JSON流式求SHA；不能用摘要替代嵌套原文，旧指纹保持。"""
    pieces = {key: canonical(value) for key, value in fields.items()}
    if "snapshot" in pieces:
        raise ValueError("固定快照字段不能被覆盖")
    pieces["snapshot"] = snapshot_bytes
    digest = hashlib.sha256()
    digest.update(b"{")
    for index, key in enumerate(sorted(pieces)):
        if index:
            digest.update(b",")
        digest.update(canonical(key))
        digest.update(b":")
        digest.update(pieces[key])
    digest.update(b"}")
    return digest.hexdigest()


def search_projection(record):
    """排名仅消费完整条文和页警告，不携带整页坐标图。"""
    return {
        **{key: value for key, value in record.items() if key != "pages"},
        "pages": [
            {"pdf_page": page["pdf_page"], "warnings": page["warnings"]} for page in record["pages"]
        ],
    }


class TransientSearchSnapshot(dict):
    """无绑定视图时的一次查询投影；完整身份只存摘要，命中再读原档。"""

    def __init__(self, record):
        super().__init__(search_projection(record))
        self.snapshot_sha256 = hashlib.sha256(canonical(record)).hexdigest()
        from archspec.official_documents.document_sections import commentary_boundary

        self._commentary_boundary_page = commentary_boundary(record)

    @property
    def commentary_boundary_page(self):
        return self._commentary_boundary_page

    def matches_full_snapshot(self, record):
        # 原页即使不参与排名也属于固定身份；不能仅比条文而忽略其变化。
        return (
            self == search_projection(record)
            and hashlib.sha256(canonical(record)).hexdigest() == self.snapshot_sha256
        )


class IndexedSnapshot(dict):
    """每轮独占的小型检索记录；原页细节只从完整快照物化。"""

    def __init__(self, view):
        super().__init__(view.header())
        self["chunks"] = json.loads(view.chunks_json)
        self["pages"] = json.loads(view.page_summary_json)
        self.view = view

    @property
    def commentary_boundary_page(self):
        return self.view.commentary_boundary_page


@dataclass(frozen=True, slots=True)
class SnapshotReadView:
    path: Path
    source_id: str
    snapshot_id: str
    file_sha256: str
    file_size: int
    header_json: bytes = field(repr=False)
    chunks_json: bytes = field(repr=False)
    page_summary_json: bytes = field(repr=False)
    canonical_snapshot: bytes = field(repr=False)
    content_manifest_sha256: str
    quality_unverified: bool
    commentary_boundary_page: int | None = None

    def header(self):
        return json.loads(self.header_json)

    def search_record(self):
        return IndexedSnapshot(self)

    def assert_projection(self, record):
        if (
            not isinstance(record, IndexedSnapshot)
            or record.view is not self
            or record != self.search_record()
        ):
            raise ValueError("检索投影已改变，未借用固定快照身份")

    def hash_with(self, **fields):
        return composite_hash(self.canonical_snapshot, **fields)


class SnapshotReadViews:
    """只属于一个runtime的128条/32MiB固定字节；容量不足沿原完整读取。"""

    def __init__(self, engine, cache_dir, instance_uuid):
        self.engine = engine
        self.cache_dir = Path(cache_dir).resolve()
        self.instance_uuid = instance_uuid
        self._views = {}
        self.byte_count = 0
        self._publication_lock = Lock()

    def validate_binding(self, runtime):
        if (
            runtime.engine is not self.engine
            or runtime.instance_uuid != self.instance_uuid
            or Path(runtime.cache_dir).resolve() != self.cache_dir
        ):
            raise ValueError("固定读取视图不属于当前运行范围")

    def remember(self, path, source_id, content, record):
        # 发行者必须已执行原完整_validate_snapshot，调用方不能通过HTTP发出此操作。
        path = Path(path).absolute()
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(self.cache_dir)
            or path.parent.name != source_id
            or path.stem != record["snapshot_id"]
        ):
            raise ValueError("固定读取视图路径无效")
        if path in self._views or len(self._views) >= 128:
            return
        raw_hash = hashlib.sha256(content).hexdigest()
        header = canonical({k: v for k, v in record.items() if k not in {"pages", "chunks"}})
        chunks = canonical(record["chunks"])
        snapshot = canonical(record)
        page_summary = canonical(
            [{"pdf_page": p["pdf_page"], "warnings": p["warnings"]} for p in record["pages"]]
        )
        size = len(header) + len(chunks) + len(snapshot) + len(page_summary)
        if (
            path in self._views
            or len(self._views) >= 128
            or self.byte_count + size > 32 * 1024 * 1024
        ):
            return
        from archspec.official_documents.document_sections import commentary_boundary

        view = SnapshotReadView(
            path,
            source_id,
            record["snapshot_id"],
            raw_hash,
            len(content),
            header,
            chunks,
            page_summary,
            snapshot,
            hashlib.sha256(
                canonical(
                    {
                        "pages": record["pages"],
                        "chunks": record["chunks"],
                        "processing_version": record.get("processor_version", "LEGACY"),
                    }
                )
            ).hexdigest(),
            any(
                page.get("warnings") or str(page.get("method", "")).upper() == "OCR"
                for page in record["pages"]
            ),
            commentary_boundary_page=commentary_boundary(record),
        )
        # 哈希与序列化在锁外执行；纳入身份和容量结算必须原子完成，不能替换在读视图。
        with self._publication_lock:
            if (
                path in self._views
                or len(self._views) >= 128
                or self.byte_count + size > 32 * 1024 * 1024
            ):
                return
            self._views[path] = view
            self.byte_count += size

    def get(self, path, source_id, service):
        path = Path(path).absolute()
        view = self._views.get(path)
        if view is None:
            return None
        # 仍核当前实际字节和当前资源政策；缓存不保存一次成功的原件/目录核验。
        if (
            source_id != view.source_id
            or any(p.is_symlink() for p in (path, *path.parents))
            or not path.resolve().is_relative_to(self.cache_dir)
        ):
            raise ValueError("固定读取视图身份或路径改变")
        content = path.read_bytes()
        if (
            len(content) != view.file_size
            or hashlib.sha256(content).hexdigest() != view.file_sha256
        ):
            return None  # 新/改变字节必须回原完整加载及原catalog/档案守卫。
        header = view.header()
        policy = service._snapshot_policy(header, source_id)
        if (
            not 1 <= header["size_bytes"] <= policy.max_bytes
            or not 1 <= header["page_count"] <= policy.max_pages
        ):
            raise ValueError("固定读取视图超过当前来源预算")
        return view, content


def build_read_views(runtime, generation):
    """只从本次已完整恢复manifest发行；旧原件和固定记录不增写或搬迁。"""
    from archspec.official_documents.service import OfficialDocumentService
    from archspec.persistence.runtime_bindings import runtime_scope

    index = runtime.snapshot_read_views
    with runtime_scope(runtime):
        documents = OfficialDocumentService(runtime.cache_dir)
        for entry in json.loads(generation._read_view_assets_json)["entries"]:
            match = re.fullmatch(
                r"official-cache/(gb55031|official-[a-f0-9]{32})/([a-f0-9]{64})[.]json",
                entry["path"],
            )
            if match is None:
                continue
            path = generation.data_dir / entry["path"]
            try:
                content = path.read_bytes()
                if (
                    len(content) != entry["size_bytes"]
                    or hashlib.sha256(content).hexdigest() != entry["sha256"]
                ):
                    continue
                record = json.loads(content)
                documents._validate_snapshot(record, match[1])
                index.remember(path, match[1], content, record)
            except (OSError, ValueError, KeyError, TypeError):
                # 非可用视图仍走原逐来源完整读取/报错；不把坏记录当已核或阻塞其它书。
                continue
    return index
