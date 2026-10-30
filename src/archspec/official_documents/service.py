"""下载一次、处理一次，后续复用；结果不自动进入正式规范证据池。"""

import hashlib
import json
import os
import re
import threading
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pymupdf

from archspec.documents.chunker import LOADED_SOURCE_IDENTITY as CHUNKER_SOURCE_IDENTITY
from archspec.documents.chunker import DocumentChunker
from archspec.documents.parser import SourceBlock
from archspec.official_documents.cell_pixels import LOADED_SOURCE_IDENTITY as CROP_SOURCE_IDENTITY
from archspec.official_documents.downloader import download_pdf_file
from archspec.official_documents.native_text import LOADED_SOURCE_IDENTITY as MARGIN_SOURCE_IDENTITY
from archspec.official_documents.native_text import _exclude_repeated_page_numbers
from archspec.official_documents.ocr import LOADED_SOURCE_IDENTITY as OCR_SOURCE_IDENTITY
from archspec.official_documents.ocr import recognize_page
from archspec.official_documents.page_text import LOADED_SOURCE_IDENTITY as PAGE_SOURCE_IDENTITY
from archspec.official_documents.page_text import (
    blank_render,
    image_area,
    merge_lines,
    native_lines,
)
from archspec.official_documents.policy import DEFAULT_POLICY, policy_for_source
from archspec.official_documents.processing_cache import (
    ProcessingCache,
    ProcessingSpec,
    checked_digest,
    publish_json,
    stable_hash,
    validate_pages,
)
from archspec.official_documents.read_phase import ReadPhase
from archspec.official_documents.reading_layout import (
    LOADED_SOURCE_IDENTITY as LAYOUT_SOURCE_IDENTITY,
)
from archspec.official_documents.recognition_risk import fragment_recognition
from archspec.official_documents.registry import (  # noqa: F401 -- 旧调用兼容
    SOURCES,
    OfficialSourceRegistry,
)
from archspec.official_documents.runtime_identity import frozen_processor_identity
from archspec.official_documents.table_cells import LOADED_SOURCE_IDENTITY as CELL_SOURCE_IDENTITY
from archspec.official_documents.table_context import admitted_tables, table_context

_LOCK = threading.Lock()
# 独立问答各开新阶段，退出恢复原值；未来复制上下文的并行子任务也须自建阶段。
_READ_PHASE = ContextVar("official_document_read_phase", default=None)
MAX_SEARCH_SOURCES = 20
MAX_SEARCH_CHUNKS = 20_000
# 只删除完全匹配的独立识别行，不改写正文或去除原图中的水印。
WATERMARK_LINES = {
    "公开",
    "浏览专用",
    "住房和城乡建设部信息公开",
    "住房城乡建设部信息公开",
    "信息公开",
}


@contextmanager
def _short_read_scope(service):
    """页内和普通短读共用同一入口；离开本页便释放SQL，不跨页保留批准。"""
    from archspec.persistence.runtime_bindings import read_phase_scope
    active = _READ_PHASE.get()
    # 已有JSON暂存则沿用；SQL、来源历史和目录只在这次受控短事务内复用。
    # 每次仍读取原件字节及当前权限，网络和模型调用不进入本范围。
    scope = (read_phase_scope(service._runtime_bindings)
             if active is not None and active[0] is service else service.read_phase(sql=True))
    with scope:
        yield


def _short_read(method):
    """真实短读操作共用SQL连接；QA外层JSON阶段和模型调用不持数据库锁。"""
    @wraps(method)
    def read(self, *args, **kwargs):
        with _short_read_scope(self):
            return method(self, *args, **kwargs)
    return read

def canonical_snapshot_changed(indexed,full):
    from archspec.official_documents.read_views import TransientSearchSnapshot, canonical
    if isinstance(indexed, TransientSearchSnapshot):
        return not indexed.matches_full_snapshot(full)
    indexed.view.assert_projection(indexed)
    return canonical(full)!=indexed.view.canonical_snapshot


class OfficialDocumentService:
    def __init__(self, cache_dir: Path, fetch=None, ocr=recognize_page, registry=None, governance=None, *, snapshot_catalog=None, page_image_repository=None):
        self.cache_dir = cache_dir
        # 注入字节获取函数仍可用于测试；生产默认走分段落盘，不读取整本 bytes。
        self.fetch = fetch
        self.ocr = ocr
        from archspec.official_documents.source_context import resolve_registry
        self.registry = resolve_registry(cache_dir, registry)
        self.governance = governance
        from archspec.persistence.runtime_bindings import current_runtime
        # worker不会继承HTTP ContextVar；构造后保留同一已选服务端Session工厂。
        self._runtime_bindings = current_runtime()
        selected_catalog = self._runtime_bindings.snapshot_catalog if self._runtime_bindings is not None else None
        if self._runtime_bindings is not None and snapshot_catalog is not None and snapshot_catalog is not selected_catalog:
            from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
            raise SnapshotCatalogUnavailable("正文快照仓库与选定运行配置不同")
        self.snapshot_catalog = selected_catalog if self._runtime_bindings is not None else snapshot_catalog
        self._selected_snapshot_catalog()
        selected_images = self._runtime_bindings.page_image_repository if self._runtime_bindings is not None else page_image_repository
        if self._runtime_bindings is not None and page_image_repository is not None and page_image_repository is not selected_images:
            raise ValueError("固定原页仓库与选定运行配置不同")
        self.page_image_repository = selected_images
        self._selected_page_image_repository()

    def _selected_page_image_repository(self):
        # worker构造时捕获所选仓库；退出HTTP后也不能换成同路径的其它仓库。
        if self._runtime_bindings is not None:
            self._runtime_bindings.validate_selection()
            if self.page_image_repository is not self._runtime_bindings.page_image_repository:
                raise ValueError("固定原页仓库与运行范围不一致")
        if self.page_image_repository is not None:
            from archspec.official_documents.page_images import PageImageRepository
            if (not isinstance(self.page_image_repository, PageImageRepository)
                    or self.page_image_repository.cache_dir != Path(self.cache_dir).resolve()):
                raise ValueError("固定原页仓库与当前缓存范围不同")
        return self.page_image_repository

    def _selected_snapshot_catalog(self):
        """后台退出请求后仍核同仓库；选定SQL故障不能回读latest或其他数据库。"""
        if self._runtime_bindings is not None:
            self._runtime_bindings.validate_selection()
            if self.snapshot_catalog is not self._runtime_bindings.snapshot_catalog:
                from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
                raise SnapshotCatalogUnavailable("正文快照仓库与运行范围不一致")
        if self.snapshot_catalog is not None:
            from archspec.persistence.snapshot_catalog import (
                SnapshotCatalogUnavailable,
                SQLSnapshotCatalog,
            )
            from archspec.persistence.source_registry import SQLSourceRegistryBackend
            if (not isinstance(self.snapshot_catalog, SQLSnapshotCatalog)
                    or self.snapshot_catalog.cache_dir != Path(self.cache_dir).resolve()
                    or not isinstance(self.registry.backend, SQLSourceRegistryBackend)
                    or self.snapshot_catalog.engine is not self.registry.backend.engine):
                raise SnapshotCatalogUnavailable("当前正文快照仓库范围无法核对")
        return self.snapshot_catalog

    def _open_database_session(self):
        from archspec.database import open_session
        from archspec.persistence.runtime_bindings import (
            RuntimeBindingsUnavailable,
            current_runtime,
        )
        if self._runtime_bindings is not None:
            self._runtime_bindings.validate_selection()
            selected = current_runtime()
            if selected is not None and selected is not self._runtime_bindings:
                raise RuntimeBindingsUnavailable("正文工人数据库范围与当前请求不同")
            from archspec.persistence.runtime_bindings import current_read_connection
            connection = current_read_connection(self._runtime_bindings.engine)
            if connection is not None:
                from sqlalchemy.orm import Session
                return Session(bind=connection, expire_on_commit=False)
            return self._runtime_bindings.sessions()
        return open_session()

    @contextmanager
    def read_phase(self, *, max_records=128, max_bytes=32 * 1024 * 1024, sql=False):
        """初读和最终复核各用全新暂存，嵌套复核也不继承初读结论。"""
        phase = ReadPhase(max_records=max_records, max_bytes=max_bytes)
        token = _READ_PHASE.set((self, phase))
        try:
            from contextlib import nullcontext

            from archspec.persistence.runtime_bindings import read_phase_scope
            with read_phase_scope(self._runtime_bindings) if sql else nullcontext():
                yield
        finally:
            _READ_PHASE.reset(token)

    def _snapshot_view(self,path,source_id):
        runtime=self._runtime_bindings
        index=runtime.snapshot_read_views if runtime is not None else None
        if index is None:return None
        runtime.validate_selection();index.validate_binding(runtime)
        return index.get(path,source_id,self)

    def _read_search_snapshot(self,source_id):
        catalog=self._selected_snapshot_catalog()
        if catalog is None:return self._read_snapshot(source_id)
        pointer=catalog.get(source_id)
        if pointer is None:return None
        path=self.cache_dir/source_id/(pointer['snapshot_id']+'.json')
        result=self._snapshot_view(path,source_id)
        if result is None:return self._read_snapshot(source_id)
        view,_=result
        if (view.file_sha256!=pointer['snapshot_sha256']
                or view.header()['sha256']!=pointer['document_sha256']):
            from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
            raise SnapshotCatalogUnavailable('SQL选定正文与读取视图不同，未回退latest')
        return view.search_record()

    def _load_snapshot(self, path, source_id, *, copy_record=True):
        """每次读取当前字节；同阶段复用结构校验，对外仍返回独立解析。"""
        if not copy_record:
            result=self._snapshot_view(path,source_id)
            if result is not None:return result[0].header(),result[1]
        content = path.read_bytes()
        key = (source_id, str(path.resolve()), hashlib.sha256(content).hexdigest())
        active = _READ_PHASE.get()
        phase = active[1] if active is not None and active[0] is self else None
        record = phase.get(key, copy_record=copy_record) if phase is not None else None
        if record is None:
            record = json.loads(content)
            self._validate_snapshot(record, source_id)
            # 新资料沿原完整加载通过后纳入同一固定视图；当前权限/发布状态不在此签发。
            runtime=self._runtime_bindings
            if (runtime is not None and runtime.snapshot_read_views is not None
                    and path.name==record['snapshot_id']+'.json'):
                runtime.snapshot_read_views.validate_binding(runtime)
                runtime.snapshot_read_views.remember(path,source_id,content,record)
            if phase is not None:
                phase.put_validated_json(key, content)
        else:
            # 文件不变也不代表登记预算不变；资源限制仍按当前来源即时核对。
            policy = self._snapshot_policy(record, source_id)
            if not 1 <= record["size_bytes"] <= policy.max_bytes or not 1 <= record["page_count"] <= policy.max_pages:
                raise ValueError("缓存文件大小或页数超出当前登记来源预算")
        return record, content

    def _check_read(self, source_id, snapshot_id, operation):
        chain = self._permission_lineage(source_id, snapshot_id)
        if self.governance is not None:
            for identity in chain:
                self.governance.assert_snapshot_read(source_id, identity, operation)
            return
        from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
        with self._open_database_session() as session:
            adapter = LegacyAdmissionAdapter(session, self.cache_dir.parent)
            for identity in chain:
                adapter.assert_snapshot_read(source_id, identity, operation)

    def _check_reads(self, source_id, snapshot_id):
        """相邻检索/摘录只计算一次完整谱系；各门仍重新读取当前治理数据。"""
        from archspec.standard_governance.compatibility import LegacyAdmissionAdapter

        chain = self._permission_lineage(source_id, snapshot_id)
        operations = ("LOCAL_SEARCH", "LOCAL_EXCERPT")

        def check(adapter):
            joint = getattr(adapter, "assert_snapshot_reads", None)
            if callable(joint):
                joint(source_id, chain, operations)
                return
            # 旧注入适配器没有联合能力时，保持原操作顺序和逐身份拒绝语义。
            for operation in operations:
                for identity in chain:
                    adapter.assert_snapshot_read(source_id, identity, operation)

        if self.governance is not None:
            check(self.governance)
            return
        with self._open_database_session() as session:
            check(LegacyAdmissionAdapter(session, self.cache_dir.parent))

    def assert_model_send(self, source_id, snapshot_id):
        """独立外传门：本地候选阅读许可不能替代明确MODEL_SEND授权。"""
        from archspec.standard_governance.compatibility import (
            ContentUseDenied,
            LegacyAdmissionAdapter,
        )

        chain = self._permission_lineage(source_id, snapshot_id)

        def assert_allowed(adapter):
            # 同原件各历史处理身份均即时复核，重切分不能洗掉未知或拒绝。
            for identity in chain:
                if not adapter.model_snapshot_allowed(source_id, identity):
                    raise ContentUseDenied("该固定正文及其使用条件前序未明确允许 MODEL_SEND")

        if self.governance is not None:
            assert_allowed(self.governance)
            return
        with self._open_database_session() as session:
            assert_allowed(LegacyAdmissionAdapter(session, self.cache_dir.parent))

    def _permission_lineage(self, source_id, snapshot_id):
        """同文件重切分继承旧操作限制；不能换编号把已知拒绝洗掉。"""
        from archspec.standard_governance.compatibility import ContentUseDenied
        found, stack, origin_digest = set(), [(snapshot_id, frozenset())], None
        try:
            while stack:
                identity, ancestors = stack.pop()
                checked_digest(identity)
                if identity in ancestors:
                    raise ValueError("使用条件前序存在环")
                if identity in found:
                    continue
                if len(found) >= 512:
                    raise ValueError("使用条件前序超过读取预算")
                path = self.cache_dir / source_id / f"{identity}.json"
                if identity == snapshot_id and not path.exists():
                    # 最早的旧缓存只有 latest；继续允许旧式只读，不自动迁移。
                    record = self._read_snapshot(source_id)
                    if (record is None or record.get("storage_layout") is not None
                            or record.get("permission_parent_ids")):
                        raise ValueError("固定使用条件快照缺失")
                else:
                    # 这里只读身份、原件指纹和前序编号，不把整本记录交给其他调用方。
                    record, _ = self._load_snapshot(path, source_id, copy_record=False)
                if record["snapshot_id"] != identity:
                    raise ValueError("使用条件前序身份不一致")
                if origin_digest is None:
                    origin_digest = record["sha256"]
                    # 处理版本 A→B→A 时，旧 A 的不可变记录不可能预知 B。
                    # 在读时加入同来源、同原件已有历史；不修改 A，也不造 A↔B 的环。
                    stack.extend((historical, frozenset()) for historical in
                                 self._same_blob_history(source_id, origin_digest, snapshot_id))
                if record["sha256"] != origin_digest:
                    raise ValueError("使用条件前序不是同一文件")
                found.add(identity)
                stack.extend((parent, ancestors | {identity})
                             for parent in record.get("permission_parent_ids", []))
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ContentUseDenied("使用条件前序无法安全读取，保留原件，暂停正文输出") from error
        return sorted(found)

    def _same_blob_history(self, source_id, digest, snapshot_id):
        """不仅看 latest，也检查 X→Y→X 时曾存在的同源 X 快照。"""
        parents = []
        for path in (self.cache_dir / source_id).glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            if path.stem == snapshot_id:
                continue
            # 只比较历史身份及指纹；每次仍重新枚举文件和核对当前字节、资源预算。
            record, _ = self._load_snapshot(path, source_id, copy_record=False)
            if record["snapshot_id"] != path.stem:
                raise ValueError("缓存历史身份不一致，未发布新快照")
            if record["sha256"] == digest:
                parents.append(path.stem)
            if len(parents) > 511:
                raise ValueError("同文件历史快照超过使用条件读取预算")
        return sorted(parents)

    def _source(self, source_id):
        return self.registry.get(source_id)

    @_short_read
    def list_sources(self, *, include_metadata=False):
        """列表只带轻量状态，不把整本 OCR 文字和条款塞进网页请求。"""
        result = []
        for source in self.registry.list():
            cache_error = None
            try:
                snapshot = self._read_snapshot(source["source_id"])
            except (OSError, ValueError, KeyError, TypeError):
                snapshot = None
                cache_error = "缓存无法安全读取，请检查或重新准备；其他正文仍可使用"
            result.append({
                **source, "status": "PENDING_REVIEW", "acquired": snapshot is not None,
                "snapshot_id": snapshot["snapshot_id"] if snapshot else None,
                "page_count": snapshot["page_count"] if snapshot else None,
                "processed_at": snapshot["processed_at"] if snapshot else None,
                "cache_error": cache_error,
            })
        if include_metadata:
            # 同一清单按需附加已有固定档案；默认读取仍不承担逐书核权和版本视图开销。
            profiled = []
            for row in result:
                row.update(source_metadata=None, source_validity=None,
                           source_metadata_gap="未取得可核对的完整固定正文，来源档案待核")
                if row["kind"] == "FULL_TEXT" and row["acquired"] and not row["cache_error"]:
                    if len(profiled) < 500:
                        profiled.append(row)
                    else:
                        row["source_metadata_gap"] = "超过本次500份来源档案上限，尚未核对"
            self._attach_source_metadata({"results": profiled})
        return result

    @staticmethod
    def _processing_spec():
        # 身份对应本进程已加载的代码；热编辑源码须重启后才生成新处理版本。
        # 每次返回新的配置及水印列表，调用者不能污染后续任务的配置身份。
        return ProcessingSpec(
            parser_version=_LOADED_PROCESSOR_IDENTITY.parser_version,
            ocr_version=_LOADED_PROCESSOR_IDENTITY.ocr_version,
            chunker_version=_LOADED_PROCESSOR_IDENTITY.chunker_version,
            extraction_config={"watermark_lines": sorted(WATERMARK_LINES),
                               "image_area_threshold": 0, "max_ocr_pixels": 16_000_000,
                               "ocr_scale": 2, "low_confidence_threshold": .9},
            chunk_config={"max_characters": 800},
        )

    def _processor_version(self):
        return self._processing_spec().processor_version

    def raw_file_path(self, record, source_id):
        """新旧快照共用的固定原文件入口；记录不能指定任意本机路径。"""
        self._source(source_id)
        if record.get("source_id") != source_id:
            raise ValueError("原文件来源不一致")
        digest = checked_digest(record["sha256"])
        layout = record.get("storage_layout")
        if layout is None:
            return self.cache_dir / source_id / f"{digest}.pdf"
        if layout != "CONTENT_V1":
            raise ValueError("缓存存储布局无效")
        return ProcessingCache(self.cache_dir).blob_path(
            digest, "text/html" if record.get("source_format") == "HTML" else "application/pdf")

    @_short_read
    def get(self, source_id):
        self._source(source_id)
        snapshot = self._read_snapshot(source_id)
        if snapshot is not None:
            self._check_read(source_id, snapshot["snapshot_id"], "LOCAL_FULL_TEXT_READ")
        return snapshot

    def _read_snapshot(self, source_id):
        # 此方法只供已经过登记检查的来源使用，避免枚举时反复读取整个登记表。
        catalog = self._selected_snapshot_catalog()
        if catalog is not None:
            from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable
            pointer = catalog.get(source_id)
            if pointer is None:
                return None
            try:
                path = self.cache_dir / source_id / (pointer["snapshot_id"] + ".json")
                if (any(p.is_symlink() for p in (path, *path.parents))
                        or not path.resolve().is_relative_to(Path(self.cache_dir).resolve())):
                    raise ValueError
                record, content = self._load_snapshot(path, source_id)
                if (hashlib.sha256(content).hexdigest() != pointer["snapshot_sha256"]
                        or record["snapshot_id"] != pointer["snapshot_id"]
                        or record["sha256"] != pointer["document_sha256"]):
                    raise ValueError
                return record
            except (OSError, ValueError, KeyError, TypeError):
                raise SnapshotCatalogUnavailable("SQL选定正文缺失或字节身份不符，未回退latest") from None
        path = self.cache_dir / source_id / "latest.json"
        if not path.exists():
            return None
        return self._load_snapshot(path, source_id)[0]

    @_short_read
    def search(self, source_id, query, top_k=5):
        """读取缓存检索，不调用获取或图片识别流程。"""
        from archspec.official_documents.search import search_snapshot

        query = query.strip()
        if not query or len(query) > 500 or not 1 <= top_k <= 20:
            raise ValueError("问题须为1—500字，结果数量须为1—20")
        self._source(source_id)
        snapshot = self._read_snapshot(source_id)
        if snapshot is None:
            raise LookupError("这份来源尚未获取正文，请先在规范库状态页获取")
        self._check_reads(source_id, snapshot["snapshot_id"])
        result = search_snapshot(snapshot, query, top_k)
        self._attach_recognition(result, [snapshot])
        self._attach_source_metadata(result)
        return result

    def _recognition(self, record, fragment, *, only_page=None):
        # 只接受能核对的历史评分配置；未知配置不猜阈值或改旧页警告。
        try:
            threshold = .9 if self._legacy_reading_scale(record) == 2 else None
        except (KeyError, TypeError, AttributeError, ValueError):
            # 旧记录缺少处理配置属于风险范围未知，不应改变检索门槛的错误语义。
            threshold = None
        return fragment_recognition(record, fragment, threshold=threshold,
                                    ignored_lines=WATERMARK_LINES, only_page=only_page)

    def _attach_recognition(self, result, snapshots):
        from archspec.official_documents.native_text import native_completeness
        records = {(row["source_id"], row["snapshot_id"]): row for row in snapshots}
        for hit in result["results"]:
            record = records[(hit["source_id"], hit["snapshot_id"])]
            assessment = self._recognition(record, hit)
            hit.update(native_completeness(record["pages"]))
            # 排序、原字和版本警告不变，只有识别风险使用当前条文范围。
            warnings = set(hit["warnings"]) - {"LOW_CONFIDENCE_OCR", "OCR_GAP_RECOVERED_NEEDS_REVIEW"}
            hit.update(recognition=assessment["recognition"],
                       warnings=sorted(warnings | set(assessment["warnings"])))

    def _assert_snapshot_published(self, source_id, snapshot_id):
        catalog = self._selected_snapshot_catalog()
        if catalog is not None and catalog.publisher is not None:
            catalog.publisher.assert_readable(self, source_id, snapshot_id)

    @_short_read
    def source_metadata(self, source_id, snapshot_id, metadata_id=None):
        """只读指定正文的来源档案；原件及读取条件仍按每次请求复核。"""
        from archspec.official_documents.source_metadata import fixed_metadata

        self._assert_snapshot_published(source_id, snapshot_id)
        source = self._source(source_id)
        if source.get("kind") != "FULL_TEXT":
            raise ValueError("来源档案阅读只支持完整正文")
        checked_digest(snapshot_id)
        path = self.cache_dir / source_id / f"{snapshot_id}.json"
        if not path.exists():
            raise LookupError("固定正文快照尚未取得，无法建立来源档案")
        result=self._snapshot_view(path,source_id)
        record,content=(result[0].header(),result[1]) if result is not None else self._load_snapshot(path,source_id)
        if not isinstance(record, dict) or record.get("snapshot_id") != snapshot_id:
            raise ValueError("固定正文快照身份不一致")
        self._check_cached_file(record, source_id, validated=True)
        self._check_reads(source_id, snapshot_id)
        return fixed_metadata(self.cache_dir, record, content, metadata_id)

    @_short_read
    def source_validity(self, source_id, snapshot_id):
        """来源当前视图使用同一固定档案/原件/读取门，绝不借latest或改旧档案。"""
        fixed = self.source_metadata(source_id, snapshot_id)
        return self._current_source_validity(fixed)

    @_short_read
    def notice_changes(self, source_id, snapshot_id):
        """当前固定正文先核权，跨书公告线索仍是观察，不进入问答事实账本。"""
        from archspec.official_documents.notice_changes import saved_notice_changes

        fixed = self.source_metadata(source_id, snapshot_id)
        result = saved_notice_changes(self.cache_dir, fixed)
        self._check_reads(source_id, snapshot_id)
        return result

    def _current_source_validity(self, fixed):
        from archspec.official_documents.source_validity import compose_view
        from archspec.standard_governance.source_validity import unverified_view

        when = datetime.now(UTC)

        def observe(adapter):
            method = getattr(adapter, "snapshot_validity", None)
            if method is None:
                # 旧注入仅提供permission时保持未知，不把缺能力当已核验。
                return unverified_view("SOURCE_VALIDITY_PROVIDER_UNAVAILABLE")
            return method(fixed["source_id"], fixed["snapshot_id"], as_of=when)

        if self.governance is not None:
            derived = observe(self.governance)
        else:
            from archspec.standard_governance.compatibility import LegacyAdmissionAdapter

            with self._open_database_session() as session:
                derived = observe(LegacyAdmissionAdapter(session, self.cache_dir.parent))
        return compose_view(fixed, derived, evaluated_at=when)

    def _attach_source_metadata(self, result):
        """排序完成后附加身份，不改变分数、前五次序或原文片段。"""
        from archspec.standard_governance.compatibility import ContentUseDenied

        identities = {}
        for row in result["results"]:
            key = (row["source_id"], row["snapshot_id"])
            if key not in identities:
                try:
                    fixed = self.source_metadata(*key)
                    identities[key] = (fixed, self._current_source_validity(fixed), None)
                except (OSError, ValueError, LookupError, KeyError, TypeError, ContentUseDenied):
                    # 旧式只有latest的缓存仍可检索；没有固定档案时不编造字段。
                    identities[key] = (None, None, "固定来源档案尚不能核对，请查看资料详情")
            row["source_metadata"], row["source_validity"], row["source_metadata_gap"] = identities[key]

    def _validate_snapshot(self, snapshot, source_id):
        """缓存损坏不能让别的书失败，也不能把错误出处或页码带给用户。"""
        if not isinstance(snapshot, dict) or snapshot.get("source_id") != source_id:
            raise ValueError("缓存来源信息不一致")
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot["snapshot_id"]):
            raise ValueError("缓存快照编号无效")
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot["sha256"]):
            raise ValueError("缓存文件指纹无效")
        if snapshot.get("storage_layout") is not None:
            if snapshot["storage_layout"] != "CONTENT_V1":
                raise ValueError("缓存存储布局无效")
            identity = snapshot["processing_identity"]
            if (not isinstance(identity, dict) or identity.get("blob_id") != snapshot["sha256"]
                    or stable_hash(identity) != snapshot["snapshot_id"]):
                raise ValueError("缓存处理身份不一致")
            version_fields = {key: value for key, value in identity.items() if key != "blob_id"}
            expected_version = (f"{identity['parser_version']}:{identity['ocr_version']}:"
                                f"processing-{stable_hash(version_fields)}")
            if snapshot["processor_version"] != expected_version:
                raise ValueError("缓存处理版本与固定身份不一致")
            extraction_identity = {key: identity[key] for key in (
                "blob_id", "parser_version", "ocr_version", "extraction_config_hash")}
            if stable_hash(extraction_identity) != snapshot["extraction_id"]:
                raise ValueError("缓存提取身份不一致")
            if snapshot["processing_payload_sha256"] != stable_hash({
                    "pages": snapshot["pages"], "chunks": snapshot["chunks"]}):
                raise ValueError("缓存处理文字与固定快照指纹不一致")
        from archspec.official_documents.native_text import native_completeness
        for key, value in native_completeness(snapshot["pages"]).items():
            if snapshot.get(key) != value:
                raise ValueError("原生文字与待辨认图片的完成度不一致")
        if snapshot.get("source_format") == "HTML":
            from archspec.official_documents.web_body import snapshot_web_spec
            snapshot_web_spec(snapshot)
            checked_digest(snapshot["rendering_sha256"])
            if (snapshot.get("page_semantics") != "HTML_RENDERED"
                    or type(snapshot.get("rendering_size_bytes")) is not int
                    or snapshot["rendering_size_bytes"] < 1):
                raise ValueError("网页派生阅读资料身份缺失")
        parents = snapshot.get("permission_parent_ids", [])
        if (not isinstance(parents, list) or len(parents) > 511
                or not all(isinstance(parent, str) and re.fullmatch(r"[a-f0-9]{64}", parent)
                           for parent in parents) or len(parents) != len(set(parents))):
            raise ValueError("缓存使用条件前序编号无效")
        policy = self._snapshot_policy(snapshot, source_id)
        size_bytes = snapshot["size_bytes"]
        if type(size_bytes) is not int or not 1 <= size_bytes <= policy.max_bytes:
            raise ValueError("缓存文件大小超出登记来源预算")
        page_count = snapshot["page_count"]
        if type(page_count) is not int or not 1 <= page_count <= policy.max_pages:
            raise ValueError("缓存页数无效")
        for key in ("title", "page_url", "processed_at"):
            if not isinstance(snapshot[key], str) or not snapshot[key]:
                raise ValueError("缓存出处信息缺失")
        if not isinstance(snapshot["standard_code"], str):
            raise ValueError("缓存文号字段无效")  # noqa: TRY004 - 沿用统一缓存校验错误；未载文号仍允许空值。
        if not isinstance(snapshot["pages"], list) or len(snapshot["pages"]) != page_count:
            raise ValueError("缓存页面信息不完整")
        for number, page in enumerate(snapshot["pages"], start=1):
            if not isinstance(page, dict) or page.get("pdf_page") != number:
                raise ValueError("缓存页码无效")
            if not isinstance(page["warnings"], list) or not all(
                isinstance(warning, str) for warning in page["warnings"]
            ):
                raise ValueError("缓存识别风险信息无效")
        # 老快照格式保持兼容；含新空白分类的快照须遵守同一完整页面合同。
        if any(page.get("method") in {"BLANK", "UNRECOGNIZED"} for page in snapshot["pages"]):
            validate_pages(snapshot["pages"])
        if not isinstance(snapshot["chunks"], list) or not snapshot["chunks"]:
            raise ValueError("缓存片段信息缺失")
        for chunk in snapshot["chunks"]:
            if not isinstance(chunk, dict) or not isinstance(chunk["text"], str):
                raise TypeError("缓存片段文字无效")
            start, end = chunk["page_start"], chunk["page_end"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_count:
                raise ValueError("缓存片段页码无效")
            for key in ("clause_number", "heading_path"):
                if chunk[key] is not None and not isinstance(chunk[key], str):
                    raise ValueError("缓存条款信息无效")

    def _snapshot_policy(self, snapshot, source_id):
        limits = snapshot.get("processing_limits")
        # 老缓存没有本轮政策记录，仍按原30MB/100页使用，不触发重复识别。
        if limits is None:
            return DEFAULT_POLICY
        policy = policy_for_source(self._source(source_id))
        # 新来源策略可提高处理上界，但历史快照仍按其原100页身份读取，不改旧记录。
        from archspec.official_documents.policy import FIXED_POLICIES

        # 提高本机上限不改变已有快照身份；只能兼容已知且不超过当前来源额度的旧策略。
        for previous in FIXED_POLICIES:
            if (limits == asdict(previous) and policy.max_pages >= previous.max_pages
                    and policy.max_bytes >= previous.max_bytes):
                return previous
        if not isinstance(limits, dict) or limits != asdict(policy):
            raise ValueError("缓存处理预算与登记来源不一致")
        return policy

    def _metadata_filter_profile(self, snapshot, *, metadata_id=None):
        """固定正文须与本次候选完全相同；同号或同PDF不能借用别的处理文字。"""
        source_id, snapshot_id = snapshot["source_id"], snapshot["snapshot_id"]
        from archspec.official_documents.read_views import IndexedSnapshot
        if isinstance(snapshot,IndexedSnapshot):
            snapshot.view.assert_projection(snapshot)
            fresh=self._snapshot_view(self.cache_dir/source_id/(snapshot_id+'.json'),source_id)
            if fresh is None or fresh[0] is not snapshot.view:raise ValueError('固定正文已变化')
            fixed=self.source_metadata(source_id,snapshot_id,metadata_id)
            current=self._current_source_validity(fixed)
            state=snapshot.view.hash_with(metadata=fixed,validity={k:v for k,v in current.items() if k!='evaluated_at'})
            return fixed,current,state
        fixed = self.source_metadata(source_id, snapshot_id, metadata_id)
        archived, _ = self._load_snapshot(
            self.cache_dir / source_id / f"{snapshot_id}.json", source_id)
        if archived != snapshot:
            raise ValueError("固定正文与本次检索快照无法核对")
        current = self._current_source_validity(fixed)
        # 评估显示日期不充当状态身份；事件头、期限、关系及完整固定字节仍全部绑定。
        state = stable_hash({"snapshot": snapshot, "metadata": fixed,
                             "validity": {key: value for key, value in current.items()
                                          if key != "evaluated_at"}})
        return fixed, current, state

    def _recheck_metadata_filter(self, snapshots, profiles, metadata_filter):
        """排名后再核当前源、原件、档案、事件和权限；变化时整请求安全失败。"""
        from archspec.standard_governance.compatibility import ContentUseDenied

        refreshed = {}
        try:
            for snapshot in snapshots:
                key = (snapshot["source_id"], snapshot["snapshot_id"])
                from archspec.official_documents.read_views import IndexedSnapshot
                current_snapshot=self._read_search_snapshot(key[0]) if isinstance(snapshot,IndexedSnapshot) else self._read_snapshot(key[0])
                from archspec.official_documents.read_views import TransientSearchSnapshot
                if isinstance(snapshot, TransientSearchSnapshot):
                    changed = (current_snapshot is None
                               or canonical_snapshot_changed(snapshot, current_snapshot))
                else:
                    changed = (current_snapshot != snapshot
                               or isinstance(snapshot, IndexedSnapshot)
                               and (not isinstance(current_snapshot, IndexedSnapshot)
                                    or current_snapshot.view is not snapshot.view))
                if changed:
                    raise ValueError("本次固定正文已变化")
                self._check_reads(*key)
                before = profiles[key]
                fixed, current, state = self._metadata_filter_profile(
                    current_snapshot, metadata_id=before[0]["metadata_id"])
                if state != before[2] or not metadata_filter.matches(fixed, current):
                    raise ValueError("本次固定来源的筛选状态已变化")
                refreshed[key] = (fixed, current)
        except (OSError, LookupError, ValueError, KeyError, TypeError, ContentUseDenied):
            # 不返回已失效命中，不输出本机路径或原始档案内容，也不自动扩大重查。
            raise ValueError("检索期间固定来源、档案、当前状态或使用条件已变化，请重新核对") from None
        return refreshed

    @_short_read
    def search_all(self, query, top_k=10, source_ids=None, ranking_version="V1", *,
                   metadata_filter=None, question_plan=None, retrieval_mode="KEYWORD", lookup_plan=None,
                   reading_priority_titles=()):
        """无筛选沿用原行为；显式筛选使用独立读取阶段，不更换排序算法。"""
        from archspec.official_discovery.public_queries import validated_public_titles

        titles = validated_public_titles(reading_priority_titles)
        planned = {"question_plan": question_plan} if question_plan is not None else {}
        if titles:
            planned["reading_priority_titles"] = titles
        if lookup_plan is not None:
            from archspec.official_documents.lookup import LookupPlan
            if (not isinstance(lookup_plan, LookupPlan) or retrieval_mode != "KEYWORD"
                    or not (len(lookup_plan.parallel_queries) == 0 or 2 <= len(lookup_plan.parallel_queries) <= 4)
                    or not (lookup_plan.retrieval_query == query or not lookup_plan.parallel_queries
                            and lookup_plan.policy.concept_query(lookup_plan.question_plan.conditions) == query)
                    or (lookup_plan.question_plan.effective_source_ids if lookup_plan.question_plan.effective_source_ids is not None
                        else lookup_plan.question_plan.requested_source_ids) != (tuple(source_ids) if source_ids is not None else None)):
                raise ValueError("正文检索计划不属于本次问题或来源范围")
            planned["lookup_plan"] = lookup_plan
        if retrieval_mode != "KEYWORD":
            planned["retrieval_mode"] = retrieval_mode
        if metadata_filter is None:
            return self._search_all(query, top_k, source_ids, ranking_version, **planned)
        from archspec.official_documents.metadata_filter import SourceMetadataFilter

        # 复制并重新验证列表，防止调用方在排名期间突变条件、扩大筛选范围。
        request = (metadata_filter.model_dump() if isinstance(metadata_filter, SourceMetadataFilter)
                   else metadata_filter)
        criteria = SourceMetadataFilter.model_validate(request)
        with self.read_phase():
            return self._search_all(query, top_k, source_ids, ranking_version,
                                    metadata_filter=criteria, **planned)

    def _search_all(self, query, top_k=10, source_ids=None, ranking_version="V1", *,
                    metadata_filter=None, question_plan=None, retrieval_mode="KEYWORD", lookup_plan=None,
                    reading_priority_titles=()):
        """只读已准备完整正文；下载、OCR、公告发现和正式依据池均不在这里调用。"""
        from archspec.official_documents.local_semantic import configured as semantic_configured
        from archspec.official_documents.search import search_snapshots

        query = query.strip()
        if not query or len(query) > 500 or not 1 <= top_k <= 20:
            raise ValueError("问题须为1—500字，结果数量须为1—20")
        if ranking_version not in {"V1", "V2"}:
            raise ValueError("排序版本须为V1或V2")
        if retrieval_mode not in {"KEYWORD", "SEMANTIC"}:
            raise ValueError("查找方式无效")
        if lookup_plan is not None and lookup_plan.region is not None:
            from archspec.official_documents.metadata_filter import SourceMetadataFilter

            if (metadata_filter is None or metadata_filter.resolved_region
                    != SourceMetadataFilter(region=lookup_plan.region).resolved_region):
                raise ValueError("共同检索计划的地域与本次固定范围不同")
        registered = {source["source_id"]: source for source in self.registry.list()}
        if source_ids is not None:
            if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 20:
                raise ValueError("显式选择的正文来源须为1—20项")
            if not all(isinstance(value, str) and value for value in source_ids):
                raise ValueError("正文来源编号无效")
            selected = set(source_ids)
            if selected - registered.keys():
                raise LookupError("选择了未登记的官方正文来源")
            if any(registered[key]["kind"] != "FULL_TEXT" for key in selected):
                raise ValueError("统一检索只能选择完整正文，不能混入修订或废止清单等附件")
        else:
            selected = registered.keys()

        if question_plan is not None:
            from archspec.official_qa.question_plan import QuestionPlan

            # 只接受服务器共同计划；先保原选书校验，再约束读取，不能空范围回退全库。
            if not isinstance(question_plan, QuestionPlan) or question_plan.explicit_source_scope is None:
                raise ValueError("规划来源范围必须来自服务器明确来源问题计划")
            if (question_plan.requested_source_ids is not None
                    and not set(selected) <= set(question_plan.requested_source_ids)):
                raise ValueError("规划读取不能扩大原问显式选书范围")

        snapshots, coverage = [], []
        profiles = {}
        from archspec.standard_governance.compatibility import ContentUseDenied
        chunk_count = 0
        title_order = {re.sub(r"\s+", "", title): index for index, title in enumerate(reading_priority_titles)}
        def reading_order(source_id):
            title = re.sub(r"\s+", "", registered[source_id]["title"])
            return title_order.get(title, len(title_order)), source_id
        # 仍只读本轮原范围内的来源；题名建议不跳过类型、版本、地域、许可及预算门。
        for source_id in sorted(selected, key=reading_order):
            source = registered[source_id]
            item = {"source_id": source_id, "title": source["title"],
                    "standard_code": source["standard_code"]}
            if source["kind"] != "FULL_TEXT":
                coverage.append({**item, "state": "EXCLUDED_ATTACHMENT",
                                 "reason": "不是完整规范正文，未混入此次检索"})
                continue
            if question_plan is not None and not question_plan.matches_explicit_source(source):
                coverage.append({**item, "state": "EXCLUDED_FIXED_SCOPE",
                                 "reason": "与本次原问明确规范目标不同，未读取或参与排名"})
                continue
            try:
                snapshot = self._read_search_snapshot(source_id)
                if snapshot is None:
                    coverage.append({**item, "state": "NOT_ACQUIRED",
                                     "reason": "正文尚未准备，本次未下载或运行文字识别"})
                    continue
                self._check_reads(source_id, snapshot["snapshot_id"])
            except ContentUseDenied:
                coverage.append({**item, "state": "RESTRICTED",
                                 "reason": "使用条件不允许本次检索或摘录；没有删除原件/快照"})
                continue
            except (OSError, ValueError, KeyError, TypeError):
                coverage.append({**item, "state": "CACHE_ERROR",
                                 "reason": "缓存无法安全读取，请检查或重新准备；其他正文仍可检索"})
                continue
            metadata_fields = {}
            profile = None
            if metadata_filter is not None:
                try:
                    profile = self._metadata_filter_profile(snapshot)
                    exclusions = metadata_filter.exclusion_reasons(profile[0], profile[1])
                    matched = not exclusions
                except ContentUseDenied:
                    coverage.append({**item, "state": "RESTRICTED",
                                     "reason": "使用条件不允许本次筛选；没有扩大范围或删除原件"})
                    continue
                except (OSError, LookupError, ValueError, KeyError, TypeError):
                    coverage.append({**item, "state": "METADATA_ERROR",
                                     "reason": "固定来源档案无法核对，未冒用未知字段参与筛选"})
                    continue
                metadata_fields = {"metadata_filter_state": "MATCHED" if matched else "EXCLUDED",
                                   "mandatory_status": "UNKNOWN",
                                   "metadata_exclusions": exclusions}
                if not matched:
                    coverage.append({**item, **metadata_fields, "state": "EXCLUDED_METADATA",
                                     "snapshot_id": snapshot["snapshot_id"],
                                     "source_metadata": profile[0], "source_validity": profile[1],
                                     "reason": "；".join(item["message"] for item in exclusions)})
                    continue
            if (len(snapshots) >= MAX_SEARCH_SOURCES or
                    chunk_count + len(snapshot["chunks"]) > MAX_SEARCH_CHUNKS):
                coverage.append({**item, "state": "LIMIT_REACHED",
                                 "reason": "超过本次20份正文或20000片段预算，整份未检索"})
                continue
            from archspec.official_documents.read_views import (
                IndexedSnapshot,
                TransientSearchSnapshot,
            )
            # LOCAL、未纳入视图或容量回退也遵守同一排名内存合同。
            snapshots.append(snapshot if isinstance(snapshot, IndexedSnapshot)
                             else TransientSearchSnapshot(snapshot))
            if profile is not None:
                profiles[(snapshot["source_id"], snapshot["snapshot_id"])] = profile
            chunk_count += len(snapshot["chunks"])
            coverage.append({**item, **metadata_fields, "state": "SEARCHED",
                             "snapshot_id": snapshot["snapshot_id"],
                             "reason": "已检索此份完整缓存；现行性、适用性和识别文字仍待核验"})
        numbers = question_plan.bound_clause_numbers() if question_plan is not None else ()
        if len(numbers) > 1:
            # 原问前top_k个条号有限子选择；所有子选择共享本批已核许可/地域的快照。
            # 先每号首段再续段，总命中不超过原top_k，不以失败为由继续试预算外条号。
            selected = numbers[:top_k]
            groups = [search_snapshots(snapshots, number, top_k, ranking_version=ranking_version,
                        index_dir=self.cache_dir / "retrieval-index")["results"] for number in selected]
            merged = [group[index] for index in range(top_k) for group in groups if index < len(group)][:top_k]
            hits = {"method": "EXACT_CLAUSE", "results": merged,
                    "clause_selection": {"requested": list(numbers), "queried": list(selected),
                                         "omitted": list(numbers[top_k:]), "subqueries": len(selected),
                                         "located": list(dict.fromkeys(row["clause_number"] for row in merged))}}
        elif lookup_plan is not None and lookup_plan.parallel_parts:
            # 复用精确多条号的有限轮转方式；所有子要求共享同批已核来源/许可/地域。
            # 不直接比较不同问法的BM25分数，不扩大原top_k或触发联网/模型。
            from archspec.official_documents.lookup import _part_matches
            groups, raw_groups = [], []
            use = lookup_plan.question_plan.conditions.get("building_use", "")
            for part in lookup_plan.parallel_parts:
                candidates = search_snapshots(snapshots, part["retrieval_query"], top_k,
                    ranking_version=ranking_version, index_dir=self.cache_dir / "retrieval-index")["results"]
                raw_groups.append(candidates)
                groups.append([hit for hit in candidates if _part_matches(part, re.sub(r"\s+", "", hit["text"]),
                    hit.get("title", ""), use, hit.get("clause_number"))])
            merged, seen = [], set()
            for index in range(top_k):
                for group in groups:
                    if index < len(group) and group[index]["id"] not in seen and len(merged) < top_k:
                        seen.add(group[index]["id"])
                        merged.append(group[index])
            # 尚未对应的原始候选填剩余位置，仍交既有关联分区展示，不冒称某部分已找到。
            for index in range(top_k):
                for group in raw_groups:
                    if index < len(group) and group[index]["id"] not in seen and len(merged) < top_k:
                        seen.add(group[index]["id"])
                        merged.append(group[index])
            hits = {"method": "PARALLEL_BM25", "results": merged}
        elif retrieval_mode == "SEMANTIC":
            from archspec.official_documents.local_semantic import search as semantic_search
            hits = semantic_search(snapshots, query, top_k, cache_dir=self.cache_dir)
        else:
            selected_plan = {"lookup_plan": lookup_plan} if lookup_plan is not None else {}
            hits = search_snapshots(snapshots, query, top_k, ranking_version=ranking_version,
                                    index_dir=self.cache_dir / "retrieval-index", **selected_plan)
        # 只有真实命中需要完整原页识别详情；排名语料和chunks不裁剪。
        from archspec.official_documents.read_views import IndexedSnapshot
        needed={(row['source_id'],row['snapshot_id']) for row in hits['results']}
        reading=[]
        for record in snapshots:
            if (record['source_id'],record['snapshot_id']) not in needed:continue
            if isinstance(record,(IndexedSnapshot,TransientSearchSnapshot)):
                full=self._read_snapshot(record['source_id'])
                if full is None or canonical_snapshot_changed(record,full):raise ValueError('命中正文已变化')
                reading.append(full)
            else:reading.append(record)
        self._attach_recognition(hits, reading)
        if metadata_filter is None:
            self._attach_source_metadata(hits)
        else:
            refreshed = self._recheck_metadata_filter(snapshots, profiles, metadata_filter)
            for row in hits["results"]:
                fixed, current = refreshed[(row["source_id"], row["snapshot_id"])]
                row.update(source_metadata=fixed, source_validity=current, source_metadata_gap=None)
        # 覆盖是正文是否可读的证据，不能因本题无命中而丢失其固定范围。
        # 复用本轮已读且排名后复核的档案，不增加下载、模型或另一套地域判断。
        for item in coverage:
            if item["state"] != "SEARCHED":
                continue
            key = (item["source_id"], item["snapshot_id"])
            if metadata_filter is not None:
                fixed, current = refreshed[key]
                item.update(source_metadata=fixed, source_validity=current)
            else:
                matched = next((hit for hit in hits["results"]
                    if (hit["source_id"], hit["snapshot_id"]) == key), None)
                if matched is not None:
                    item.update(source_metadata=matched.get("source_metadata"),
                                source_validity=matched.get("source_validity"))
        return {
            "query": query, "status": "PENDING_REVIEW", **hits,
            **({"source_reading_priority": {"method": "PUBLIC_TITLE_HINT_V1",
                 "public_titles": list(reading_priority_titles), "max_sources": MAX_SEARCH_SOURCES,
                 "max_chunks": MAX_SEARCH_CHUNKS}} if reading_priority_titles else {}),
            "retrieval_options": {"local_semantic": semantic_configured()},
            "searched_sources": len(snapshots), "searched_chunks": chunk_count,
            "coverage": coverage,
            "notice": "只查本次可读的已准备正文，不代表规范齐全或已排除全部废止版本。"
                      "关键词相关度不是正确率；没有结果不等于没有要求。"
                      "版本、适用范围、使用许可和识别文字仍待核验，未进入正式依据池。",
        }

    def _bound_text_snapshot(self, source_id, snapshot_id):
        """固定正文读取共用身份、原件及当前许可核对，不跟随latest。"""
        source = self._source(source_id)
        if source.get("kind") != "FULL_TEXT":
            raise ValueError("修订附件和公告不能作为完整规范正文问答")
        checked_digest(snapshot_id)
        path = self.cache_dir / source_id / f"{snapshot_id}.json"
        if not path.exists():
            raise LookupError("命中对应的固定快照尚未取得")
        record, content = self._load_snapshot(path, source_id)
        if not isinstance(record, dict):
            raise TypeError("固定快照内容不是完整记录")
        if record.get("source_id") != source_id or record.get("snapshot_id") != snapshot_id:
            raise ValueError("固定快照身份不一致")
        self._check_cached_file(record, source_id, validated=True)
        self._check_reads(source_id, snapshot_id)
        return record, content

    @_short_read
    def standard_mentions(self, source_id, snapshot_id, chunk_index):
        """当前固定片段内的跨书编号导航；只读取登记元数据，不读取目标正文或获取新资料。"""
        from archspec.official_documents.standard_mentions import mentions

        if type(chunk_index) is not int or not 0 <= chunk_index <= 999999:
            raise ValueError("固定片段序号无效")
        record, content = self._bound_text_snapshot(source_id, snapshot_id)
        if chunk_index >= len(record["chunks"]):
            raise LookupError("固定片段不存在")
        sources = self.list_sources()
        rows, truncated = mentions(record["chunks"][chunk_index]["text"], record["standard_code"], sources[:500])
        self._check_reads(source_id, snapshot_id)
        return {"version": "FIXED_STANDARD_MENTIONS_V1", "source_id": source_id,
                "snapshot_id": snapshot_id, "selected_chunk_id": f"{source_id}:{snapshot_id}:{chunk_index}",
                "snapshot_fingerprint": hashlib.sha256(content).hexdigest(), "mentions": rows,
                "relation_mode": "BIBLIOGRAPHIC_MENTIONS_ONLY", "truncated": truncated,
                "catalog_truncated": len(sources) > 500,
                "notice": "仅列当前原文中的编号提及；未证明效力关系、版本现行或项目适用。目标正文须另外读取核对。"}

    @_short_read
    def book_pages(self, source_id, snapshot_id, *, chunk_index=None):
        """整书原页范围只作导航；复用固定原件与当前读取许可，不生成条文关系。"""
        record, content = self._bound_text_snapshot(source_id, snapshot_id)
        references = {}
        if chunk_index is not None:
            from archspec.official_documents.artifact_references import figure_page_references

            if type(chunk_index) is not int or not 0 <= chunk_index < len(record["chunks"]):
                raise LookupError("附图提及的固定片段不存在")
            references = {"selected_chunk_id": f"{source_id}:{snapshot_id}:{chunk_index}",
                          "reference_mode": "FIGURE_CAPTION_NAVIGATION_ONLY",
                          **figure_page_references(record["chunks"][chunk_index]["text"], record["pages"])}
        self._check_reads(source_id, snapshot_id)
        return {"version": "FIXED_BOOK_PAGES_V1", "source_id": source_id,
                "snapshot_id": snapshot_id, "snapshot_fingerprint": hashlib.sha256(content).hexdigest(),
                "title": record["title"], "standard_code": record["standard_code"],
                "source_format": record.get("source_format", "PDF"),
                "page_count": record["page_count"], "navigation_only": True, **references}

    @_short_read
    def book_index(self, source_id, snapshot_id, *, query="", offset=0, limit=50):
        """仅投影本书已识别条号的导航；不造目录层级、不返回全文或回答依据。"""
        if (not isinstance(query, str) or len(query) > 80 or type(offset) is not int
                or not 0 <= offset <= 999999 or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("条款索引筛选或分页参数超出范围")
        record, content = self._bound_text_snapshot(source_id, snapshot_id)
        needle = query.strip().casefold()
        entries = []
        for index, row in enumerate(record["chunks"]):
            clause = row.get("clause_number")
            if not isinstance(clause, str) or not clause.strip():
                continue
            preview = " ".join(row["text"].split())[:100]
            if needle and needle not in (clause + " " + preview).casefold():
                continue
            entries.append({"chunk_index": index, "clause_number": clause,
                            "page_start": row["page_start"], "page_end": row["page_end"],
                            "preview": preview})
        items = entries[offset:offset + limit]
        self._check_reads(source_id, snapshot_id)
        return {"version": "FIXED_BOOK_INDEX_V1", "source_id": source_id, "snapshot_id": snapshot_id,
                "snapshot_fingerprint": hashlib.sha256(content).hexdigest(),
                "title": record["title"], "standard_code": record["standard_code"],
                "source_format": record.get("source_format", "PDF"), "query": query.strip(),
                "offset": offset, "limit": limit, "total": len(entries), "items": items,
                "next_offset": offset + limit if offset + limit < len(entries) else None,
                "notice": "仅从当前固定快照已识别条号生成，不是原版完整目录；识别、版本与适用性待核。"}

    @_short_read
    def find_in_book(self, source_id, snapshot_id, *, query, offset=0, limit=50):
        """只查当前固定书的已有全文，包含无条号片段，不改排名、提取结果或原件。"""
        from archspec.official_documents.book_find import find_entry, folded

        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 80 or type(offset) is not int
                or not 0 <= offset <= 999999 or type(limit) is not int or not 1 <= limit <= 50):
            raise ValueError("书内查找词或分页参数超出范围")
        needle = folded(query)
        if not needle:
            raise ValueError("书内查找词不能为空白")
        record, content = self._bound_text_snapshot(source_id, snapshot_id)
        entries = []
        for index, row in enumerate(record["chunks"]):
            match = find_entry(row["text"], needle)
            if match is not None:
                entries.append({"chunk_index": index, "clause_number": row.get("clause_number"),
                                "page_start": row["page_start"], "page_end": row["page_end"], **match})
        self._check_reads(source_id, snapshot_id)
        return {"version": "FIXED_BOOK_FIND_V1", "source_id": source_id, "snapshot_id": snapshot_id,
                "snapshot_fingerprint": hashlib.sha256(content).hexdigest(),
                "title": record["title"], "standard_code": record["standard_code"],
                "source_format": record.get("source_format", "PDF"), "query": query.strip(),
                "offset": offset, "limit": limit, "total": len(entries),
                "items": entries[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(entries) else None,
                "match_mode": "IGNORES_WHITESPACE_CASEFOLD", "navigation_only": True,
                "notice": "只查当前快照已取得的文字，每片段显示首处命中；未识别图片及文字仍须核原页。"}

    @_short_read
    def read_context(self, source_id, snapshot_id, chunk_id, *,
                     max_fragments=8, max_characters=16000):
        """读取命中的固定出处及邻接上下文，不跳latest、不下载、不重新识别。

        同书同快照的连续同条片段才补作续文；远处同号不合并。
        邻接条文仅标“相关上下文”，不猜它是例外。
        达到预算时保留完整片段并报告缺口，不把截断文字冒充完整要求。
        """
        record, content = self._bound_text_snapshot(source_id, snapshot_id)
        suffix = None
        for prefix in (f"{source_id}:{snapshot_id}:", f"{snapshot_id}:"):
            if isinstance(chunk_id, str) and chunk_id.startswith(prefix):
                suffix = chunk_id[len(prefix):]
                break
        if suffix is None or not re.fullmatch(r"0|[1-9]\d{0,5}", suffix):
            raise LookupError("片段不属于这份固定快照")
        selected_index = int(suffix)
        rows = record["chunks"]
        if selected_index >= len(rows):
            raise LookupError("固定快照中没有该片段")
        if (type(max_fragments) is not int or not 1 <= max_fragments <= 8
                or type(max_characters) is not int or not 1 <= max_characters <= 16000):
            raise ValueError("上下文预算无效")
        selected = rows[selected_index]
        clause = selected["clause_number"]
        from archspec.official_documents.named_units import named_unit

        unit = named_unit(rows, selected_index) if clause is None else None
        from archspec.official_documents.clause_units import clause_indices, scanned_clause_gaps

        same_clause = (clause_indices(rows, selected_index) if clause
                       else unit["indices"] if unit else [selected_index])
        # 唯一发现器扫描固定正文一次；两种兼容入口只消费同一关系草稿。
        from archspec.official_documents.evidence_relations import discover_relations

        relations = discover_relations(source_id, snapshot_id,
            [{**row, "index": i, "selected": i in same_clause} for i, row in enumerate(rows)])
        from archspec.official_documents.evidence_relations import unconfirmed_table_parents

        foreign_tables = unconfirmed_table_parents(
            "\n".join(rows[index]["text"] for index in same_clause), clause)
        boundary_issues = [{"kind": "TABLE_PARENT_UNCONFIRMED", "table_number": number,
                            "message": "固定条号与未被正文引用的表题归属尚未核对；可能存在识别漏条号，仅供原页查阅。"}
                           for number in foreign_tables]
        boundary_issues.extend(scanned_clause_gaps(record, same_clause))
        # 优先同号完整续文，再追条文明确提到的原表，最后补相邻片段。
        table_indices, table_refs, tables_omitted = table_context(rows, record["pages"], same_clause,
            source_id=source_id, snapshot_id=snapshot_id, relations=relations)
        from archspec.official_documents.clause_links import admitted_links, clause_links

        link_indices, clause_refs, links_omitted = clause_links(rows, same_clause,
            source_id=source_id, snapshot_id=snapshot_id, relations=relations)
        neighbours = range(max(0, min(same_clause) - 1), min(len(rows), max(same_clause) + 6))
        ordered = list(dict.fromkeys([selected_index, *same_clause, *table_indices, *link_indices, *neighbours]))
        fragments, gaps, characters = [], [issue["message"] for issue in boundary_issues], 0
        for index in ordered:
            row = rows[index]
            if len(fragments) >= max_fragments or characters + len(row["text"]) > max_characters:
                gaps.append("上下文达到读取上限；尚有续文或邻接片段未纳入，请继续对照原页。")
                continue
            characters += len(row["text"])
            fragments.append({
                "id": f"{source_id}:{snapshot_id}:{index}", **row,
                "role": ("MATCH" if index == selected_index else
                         "CONTINUATION" if index in same_clause else "RELATED_CONTEXT"),
                **({key: unit[key] for key in ("reading_unit_key", "reading_unit_label")}
                   if unit and index in same_clause else {}),
                **self._recognition(record, row),
            })
        pending_fragments = [row for row in fragments if row["role"] != "RELATED_CONTEXT"
                             and "OCR_GAP_RECOVERED_NEEDS_REVIEW" in row.get("warnings", [])]
        if pending_fragments:
            issue = {"kind": "OCR_RECOVERY_PENDING_REVIEW", "clause_number": clause,
                     "fragment_ids": [row["id"] for row in pending_fragments],
                     "message": "本条含尚待原页核对的局部补识别文字；保留阅读，不作为已核对摘录。"}
            boundary_issues.append(issue)
            gaps.append(issue["message"])
        # 只核对同书同快照的所选条文是否读齐；邻接片段缺失是另一种缺口。
        included_clause_count = sum(row["role"] != "RELATED_CONTEXT" for row in fragments)
        table_refs, table_gaps = admitted_tables(table_refs,
            {int(row["id"].rsplit(":", 1)[1]) for row in fragments}, source_id, snapshot_id)
        gaps.extend(table_gaps)
        if tables_omitted:
            gaps.append("所选条文引用超过三个表号，本次仅关联前三个，其余仍需原页查阅。")
        clause_refs, link_gaps = admitted_links(clause_refs,
            {int(row["id"].rsplit(":", 1)[1]) for row in fragments}, source_id, snapshot_id)
        gaps.extend(link_gaps)
        if links_omitted:
            gaps.append("明确条文关联达到每向三条上限，其余关系仍需原页查阅。")
        fixed = self.source_metadata(source_id, snapshot_id)
        return {
            "source_id": source_id, "snapshot_id": snapshot_id,
            "selected_chunk_id": chunk_id, "title": record["title"],
            "source_format": record.get("source_format", "PDF"),
            "standard_code": record["standard_code"], "source_url": record["page_url"],
            "snapshot_fingerprint": hashlib.sha256(content).hexdigest(),
            "source_metadata": fixed,
            "source_validity": self._current_source_validity(fixed),
            "fragments": fragments, "gaps": list(dict.fromkeys(gaps)), "complete": not gaps,
            "table_references": table_refs,
            "clause_references": clause_refs,
            "selected_clause_coverage": {"clause_number": clause,
                **({key: unit[key] for key in ("reading_unit_key", "reading_unit_label")}
                   if unit else {}),
                "expected_fragment_count": len(same_clause),
                "included_fragment_count": included_clause_count,
                "complete": included_clause_count == len(same_clause) and not boundary_issues,
                **({"boundary_issues": boundary_issues} if boundary_issues else {})},
            "table_pages": sorted({page["pdf_page"] for page in record["pages"]
                if re.search(r"表\s*\d", page["text"]) and any(
                    row["page_start"] <= page["pdf_page"] <= row["page_end"]
                    for row in fragments)}),
            "evidence_mode": "CANDIDATE_ONLY", "is_citable": False,
        }

    @contextmanager
    def _fixed_page(self, source_id, snapshot_id, page_number):
        """原页图和布局共用核对；读取始终使用该快照的原件。"""
        with _short_read_scope(self):
            self._source(source_id)
            if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
                raise LookupError("处理快照编号无效")
            folder = self.cache_dir / source_id
            record_path = folder / f"{snapshot_id}.json"
            if not record_path.exists():
                raise LookupError("未找到这次处理的快照")
            record, _ = self._load_snapshot(record_path, source_id)
            if record["snapshot_id"] != snapshot_id or record["source_id"] != source_id:
                raise ValueError("处理快照信息不一致")
            self._check_read(source_id, snapshot_id, "ORIGINAL_PAGE")
            if not 1 <= page_number <= record["page_count"]:
                raise LookupError("页码不在这份快照范围内")
            digest = record["sha256"]
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("原文件指纹格式无效")
            path = self.raw_file_path(record, source_id)
            if not path.exists():
                raise LookupError("本地原文件不存在，请检查缓存")
            policy = self._snapshot_policy(record, source_id)
            actual_size = path.stat().st_size
            if actual_size > policy.max_bytes:
                raise ValueError("原文件大小超出登记预算")
            if self._file_fingerprint(path) != digest:
                raise ValueError("原文件指纹已变化，拒绝显示不匹配的原页")
            if actual_size != record["size_bytes"]:
                raise ValueError("原文件大小与快照记录不一致")
            if record.get("source_format") == "HTML":
                from archspec.official_documents.web_body import checked_rendering_path
                path = checked_rendering_path(self, record)
            with pymupdf.open(path) as document:
                if document.is_repaired:
                    raise ValueError("PDF结构损坏或不完整，拒绝显示自动修复的原页")
                if document.is_encrypted or len(document) != record["page_count"]:
                    raise ValueError("原文件页数与快照记录不一致或附件加密")
                page = document[page_number - 1]
                raster = (page.rect * pymupdf.Matrix(1.5, 1.5)).irect
                if raster.width * raster.height > 10_000_000:
                    raise ValueError("原页过大，无法安全生成预览")
                yield record, page

    @_short_read
    def original_content(self, source_id, snapshot_id):
        """完整正文也保留独立原件下载；复用固定快照、出版与即时读取许可。"""
        self._assert_snapshot_published(source_id, snapshot_id)
        with self._fixed_page(source_id, snapshot_id, 1) as (record, _):
            return record.get("source_format", "PDF"), self.raw_file_path(record, source_id).read_bytes()

    def prepare_page_image(self, source_id, snapshot_id, page_number):
        """显式准备固定原页；原件和即时许可先核对，图片及描述不覆盖旧资产。"""
        repository = self._selected_page_image_repository()
        if repository is None:
            raise ValueError("固定原页持久仓库尚未选择")
        with self._fixed_page(source_id, snapshot_id, page_number) as (record, page):
            content = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")
            return repository.save(source_id, snapshot_id, record["sha256"], page_number, content)

    @_short_read
    def page_image(self, source_id, snapshot_id, page_number, *, quality="preview"):
        """旧预览保持固定；本机阅读图是同页的临时高清派生，不替换已发布资产。"""
        if quality not in {"preview", "reading"}:
            raise ValueError("原页显示质量无效")
        self._assert_snapshot_published(source_id, snapshot_id)
        repository = self._selected_page_image_repository()
        with self._fixed_page(source_id, snapshot_id, page_number) as (record, page):
            if repository is not None:
                content = repository.read(source_id, snapshot_id, record["sha256"], page_number)
                # 原页可能刚从私有云回读；返回前再核即时许可，不沿用传输前的允许状态。
                self._check_read(source_id, snapshot_id, "ORIGINAL_PAGE")
            if quality == "reading":
                from archspec.official_documents.processing_cache import publication_compute_mode
                if publication_compute_mode() == "native":
                    # 先完成旧资产及许可核对，再按固定原件渲染；缺件/坏件不能借高清请求绕过。
                    # 不存回旧仓库；单页像素上限保持10M，留余量容纳像素边界取整。
                    scale = min(3, (9_000_000 / max(1, page.rect.get_area())) ** .5)
                    raster = (page.rect * pymupdf.Matrix(scale, scale)).irect
                    if raster.width * raster.height > 10_000_000:
                        raise ValueError("原页过大，无法安全生成阅读图")
                    content = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                    self._check_read(source_id, snapshot_id, "ORIGINAL_PAGE")
                    return content
            if repository is not None:
                return content
            # 未选择持久仓库的旧预览路径保持，不去水印、不改正文。
            return page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")

    @_short_read
    def page_layout(self, source_id, snapshot_id, page_number, chunk_id):
        """只读同快照的条文位置和原表区域，不触发下载或识别。"""
        self._assert_snapshot_published(source_id, snapshot_id)
        from archspec.official_documents.reading_layout import (
            locate_lines,
            page_grid,
            table_regions,
        )
        from archspec.official_documents.table_cells import reading_grid

        with self._fixed_page(source_id, snapshot_id, page_number) as (record, page):
            self._check_read(source_id, snapshot_id, "LOCAL_EXCERPT")
            if not isinstance(chunk_id, str):
                raise LookupError("片段编号无效")  # noqa: TRY004 -- 无效片段按未找到处理
            suffix = None
            for prefix in (f"{source_id}:{snapshot_id}:", f"{snapshot_id}:"):
                if chunk_id.startswith(prefix):
                    suffix = chunk_id[len(prefix):]
                    break
            if suffix is None or not re.fullmatch(r"0|[1-9]\d{0,5}", suffix):
                raise LookupError("片段不属于这份固定快照")
            index = int(suffix)
            if index >= len(record["chunks"]):
                raise LookupError("这份固定快照没有此片段")
            selected = record["chunks"][index]
            clause = selected["clause_number"]
            from archspec.official_documents.clause_units import CLAUSE, clause_indices

            # 与固定补读共用连续位置，附录里的同号行不能变成所选正文的原页。
            parts = [record["chunks"][i] for i in clause_indices(record["chunks"], index)]
            texts = [row["text"] for row in parts
                     if row["page_start"] <= page_number <= row["page_end"]]
            if not texts:
                raise LookupError("这页不在所选条文的原页范围内")
            saved = record["pages"][page_number - 1]
            scale = self._legacy_reading_scale(record)
            # 只能从已核实的同条固定片段派生末页提示，浏览器不能提供证明。
            # 同页、无条号与中间页仍用旧规则；没有重识别、改写文字或查latest。
            continuation_texts = ([row["text"] for row in parts
                                   if row["page_start"] < page_number == row["page_end"]]
                                  if clause and CLAUSE.fullmatch(clause) else [])
            highlights = locate_lines(page, saved, texts, legacy_scale=scale,
                                      continuation_texts=continuation_texts)
            grid_data = page_grid(page)
            tables = table_regions(page, grid_data=grid_data)
            for table in tables:
                table["reading_grid"] = reading_grid(page, saved, table, grid_data, legacy_scale=scale)
            # 当前页只展示所选同条文字的疑点，不能把另一页的框画到当前原图。
            assessment = self._recognition(record, {"text": "\n\n".join(row["text"] for row in parts),
                "page_start": min(row["page_start"] for row in parts),
                "page_end": max(row["page_end"] for row in parts), "clause_number": clause},
                only_page=page_number)
            recognition = assessment["recognition"]
            from archspec.official_documents.reading_layout import _box

            for issue in recognition["issues"]:
                issue["box"] = _box(issue.pop("layout_box"), page.rect.width, page.rect.height)
            recognition["unlocated_count"] = sum(issue["box"] is None for issue in recognition["issues"])
            return {"source_id": source_id, "snapshot_id": snapshot_id,
                    "pdf_page": page_number, "chunk_id": chunk_id,
                    "width": page.rect.width, "height": page.rect.height,
                    "highlights": highlights, "tables": tables, "recognition": recognition,
                    "location_status": "LOCATED" if highlights else "NOT_LOCATED",
                    "coordinate_basis": "LEGACY_SCALE_2" if scale else "RECORDED_OR_NATIVE"}

    @staticmethod
    def _legacy_reading_scale(record):
        """旧版本仅支持能够核实的两倍识别配置；未知配置不猜坐标。"""
        if record["processor_version"] == "official-pdf-v2:rapidocr-3.9.2":
            return 2
        config_hash = record.get("processing_identity", {}).get("extraction_config_hash")
        from archspec.official_documents.native_text import native_processing_spec
        if config_hash == stable_hash(native_processing_spec(OfficialDocumentService._processing_spec()).extraction_config):
            return 2
        # B3历史配置的指纹也可核实；任意自定义识别比例不在兼容列表。
        for threshold in (.45, 0):
            config = {"watermark_lines": sorted(WATERMARK_LINES),
                      "image_area_threshold": threshold, "max_ocr_pixels": 16_000_000,
                      "ocr_scale": 2, "low_confidence_threshold": .9}
            if config_hash == stable_hash(config):
                return 2
        return None

    def reread_issue(self, source_id, snapshot_id, page_number, chunk_id, row_index, *, recognize=None):
        """固定疑点本机放大重读；独立结果不覆盖旧正文或进入事实回答。"""
        self._assert_snapshot_published(source_id, snapshot_id)
        from archspec.official_documents.region_reread import RegionRereadBusy, reread_issue
        # 整本处理与局部处理共享本机引擎；忙时不占用线程无限等待。
        if not _LOCK.acquire(blocking=False):
            raise RegionRereadBusy("正文处理或局部重读正在运行，请稍后重试")
        try:
            return reread_issue(self, source_id, snapshot_id, page_number, chunk_id, row_index,
                                recognize=recognize)
        finally:
            _LOCK.release()

    def reread_cell(self, source_id, snapshot_id, page_number, chunk_id, table_id, row, column, *, recognize=None):
        """当前固定网格内有字迹却未读出的格，只补读成独立待核候选。"""
        self._assert_snapshot_published(source_id, snapshot_id)
        from archspec.official_documents.cell_reread import reread_cell
        from archspec.official_documents.region_reread import RegionRereadBusy
        if not _LOCK.acquire(blocking=False):
            raise RegionRereadBusy("正文处理或局部重读正在运行，请稍后重试")
        try:
            return reread_cell(self, source_id, snapshot_id, page_number, chunk_id, table_id, row, column,
                               recognize=recognize)
        finally:
            _LOCK.release()

    def _saved_extraction_spec(self, record):
        """只更换切分身份，不把旧提取产物标为本进程刚执行的新解析或OCR。"""
        if record is None:
            raise ValueError("没有已保存的完整正文，不能只重切分")
        current = self._processing_spec()
        if record.get("source_format") == "HTML":
            from dataclasses import replace

            from archspec.official_documents.web_body import snapshot_web_spec
            return replace(snapshot_web_spec(record), chunker_version=current.chunker_version,
                           chunk_config=current.chunk_config)
        identity = record.get("processing_identity", {})
        from archspec.official_documents.native_text import native_processing_spec
        configs = [current.extraction_config, native_processing_spec(current).extraction_config]
        # 兼容已核对的两种历史图片面积策略；其他配置不猜测或自动重识别。
        for threshold in (.45, 0):
            configs.append({"watermark_lines": sorted(WATERMARK_LINES), "image_area_threshold": threshold,
                "max_ocr_pixels": 16_000_000, "ocr_scale": 2, "low_confidence_threshold": .9})
        config = next((value for value in configs
                       if stable_hash(value) == identity.get("extraction_config_hash")), None)
        if config is None or not all(isinstance(identity.get(key), str) and identity[key]
                                   for key in ("parser_version", "ocr_version")):
            raise ValueError("已保存提取配置无法可靠核对，未重切分或重新识别")
        spec = ProcessingSpec(identity["parser_version"], identity["ocr_version"], current.chunker_version,
                              config, current.chunk_config)
        if record.get("extraction_id") != spec.extraction_id(record["sha256"]):
            raise ValueError("已保存提取编号与历史处理身份不一致")
        return spec

    def _snapshot_processing_spec(self, snapshot):
        """消费产物按固定快照身份，不以当前默认OCR模式重解释已取得的计算包。

        与保存提取后重切分共用配置解析；完整身份核对后，正文、向量和原页
        才能进入同一云端发布事务。历史原件和已发布记录均不改写。
        """
        spec = self._saved_extraction_spec(snapshot)
        digest = snapshot["sha256"]
        if (spec.identity(digest) != snapshot["processing_identity"]
                or spec.snapshot_id(digest) != snapshot["snapshot_id"]):
            raise ValueError("计算包与固定快照的完整处理身份不同，未发布")
        return spec

    def acquire(self, source_id, refresh=False, progress=None, *, saved_extraction_only=False):
        if type(saved_extraction_only) is not bool or (saved_extraction_only and refresh):
            raise ValueError("只重切分不能同时刷新下载，处理选择必须明确")
        source = self._source(source_id)
        policy = policy_for_source(source)
        if source["kind"] == "UNCONFIRMED":
            raise ValueError("这份附件与规范正文的对应关系尚未确认，不能自动获取")
        # 同一进程串行处理，防止重复OCR及线程同时覆盖快照。
        with _LOCK, ExitStack() as computation:
            if source.get("source_format") == "HTML":
                from archspec.official_documents.web_body import acquire_web_body
                return acquire_web_body(self, source, refresh, progress, computation,
                                        saved_extraction_only=saved_extraction_only)
            catalog = self._selected_snapshot_catalog()
            old_pointer = catalog.get(source_id) if catalog is not None else None
            old = self.get(source_id)
            spec = self._saved_extraction_spec(old) if saved_extraction_only else self._processing_spec()
            from archspec.official_documents.native_text import native_processing_spec
            if old and not refresh and not saved_extraction_only:
                native = native_processing_spec(spec)
                if old["processor_version"] == native.processor_version:
                    spec = native
            processor_version = spec.processor_version
            if old and not refresh and old["processor_version"] == processor_version:
                self._check_cached_file(old, source_id)
                return {**old, "cache_hit": True}
            folder = self.cache_dir / source_id
            folder.mkdir(parents=True, exist_ok=True)
            store = ProcessingCache(self.cache_dir)
            # 非刷新升版先验证本地原件，不因切分代码变化重新下载。
            if old and not refresh:
                self._check_cached_file(old, source_id)
                digest, size_bytes = old["sha256"], old["size_bytes"]
                raw_path = store.publish_blob(self.raw_file_path(old, source_id), digest)
            elif self.fetch is not None:
                if progress:
                    progress("DOWNLOADING", {"source_id": source_id})
                content = self.fetch(source["pdf_url"])
                if len(content) > policy.max_bytes or not content.startswith(b"%PDF-"):
                    raise ValueError("附件不是有效PDF或超过大小限制")
                digest = hashlib.sha256(content).hexdigest()
                size_bytes = len(content)
                temporary = folder / f".acquire-{uuid4().hex}.part"
                try:
                    temporary.write_bytes(content)
                    raw_path = store.publish_blob(temporary, digest)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                if progress:
                    progress("DOWNLOADING", {"source_id": source_id})
                context = ({"page_url": source["page_url"]}
                           if (source.get("source_metadata") or {}).get("provenance") == "OFFICIAL_COPY" else {})
                downloaded = download_pdf_file(source["pdf_url"], folder, max_bytes=policy.max_bytes, **context)
                digest, size_bytes = downloaded.sha256, downloaded.size_bytes
                try:
                    raw_path = store.publish_blob(downloaded.path, digest)
                finally:
                    downloaded.path.unlink(missing_ok=True)
            if source.get("expected_original_sha256") and source["expected_original_sha256"] != digest:
                raise ValueError("官方PDF与已核首页的原件身份不同，请重新核对来源")
            checked_at = datetime.now(UTC).isoformat()
            if old and old["sha256"] == digest and old["processor_version"] == processor_version:
                self._check_cached_file(old, source_id)
                # 本次检查时间在返回/任务记录里，不能改变治理绑定的历史正文。
                return {**old, "last_fetched_at": checked_at, "cache_hit": True}

            if progress:
                progress("PROCESSING", {"source_id": source_id})
            # 共享结果也必须服从本来源的完整页数和大小预算。
            from archspec.official_documents.originals import save_original
            from archspec.official_documents.processing_cache import ProcessingLimited
            try:
                actual_page_count = self._check_pdf_budget(raw_path, policy)
                try:
                    cached = store.load_snapshot(spec, digest)
                except ProcessingLimited:
                    from archspec.official_documents.remote_compute import remote_publication
                    # 预备的完整OCR结果优先；仅未命中的新资料采用明确的文字层模式。
                    if os.getenv("ARCHSPEC_PDF_NATIVE_TEXT") == "1":
                        spec = native_processing_spec(spec)
                    try:
                        computation.enter_context(remote_publication(self, source, digest, size_bytes, spec))
                    except Exception as error:
                        from archspec.persistence.acquisition_state import AcquisitionLeaseLost
                        from archspec.standard_governance.compatibility import ContentUseDenied
                        if isinstance(error, (AcquisitionLeaseLost, ContentUseDenied)):
                            raise
                        return save_original(self, source_id, raw_path, digest, size_bytes, error=error)
                    cached = store.load_snapshot(spec, digest)
                else:
                    # 完整预备OCR仍优先；本地未命中时也必须兑现明确的文字层选择。
                    # 不借云端预备包缺失异常决定本地提取方式，图片与空白页仍待辨认。
                    if cached is None and os.getenv("ARCHSPEC_PDF_NATIVE_TEXT") == "1":
                        spec = native_processing_spec(spec)
                        cached = store.load_snapshot(spec, digest)
            except (ProcessingLimited, ValueError, pymupdf.FileDataError) as error:
                return save_original(self, source_id, raw_path, digest, size_bytes, error=error)
            cache_hit = cached is not None
            extraction_cache_hit = cache_hit
            if cached is None:
                pages = store.load_extraction(spec, digest)
                extraction_cache_hit = pages is not None
                if pages is None:
                    if saved_extraction_only:
                        raise ValueError("已保存提取缓存缺失，保留旧正文，未下载或重新识别")
                    from archspec.official_documents.originals import save_original
                    from archspec.official_documents.processing_cache import (
                        publication_compute_mode,
                    )
                    if publication_compute_mode() == "prepared_only":
                        return save_original(self, source_id, raw_path, digest, size_bytes)
                    try:
                        pages = self._extract(raw_path, max_pages=policy.max_pages,
                                              progress=progress, spec=spec)
                    except Exception as error:  # 文本提取不可用即保留原件；不扩展解析兼容。
                        from archspec.persistence.acquisition_state import AcquisitionLeaseLost
                        from archspec.standard_governance.compatibility import ContentUseDenied
                        if isinstance(error, (AcquisitionLeaseLost, ContentUseDenied)):
                            raise
                        return save_original(self, source_id, raw_path, digest, size_bytes, error=error)
                    if len(pages) != actual_page_count:
                        raise ValueError("识别页数与原文件不一致，未发布完整缓存")
                    store.save_extraction(spec, digest, pages)
                if len(pages) != actual_page_count:
                    raise ValueError("缓存提取页数与原文件不一致，未发布完整正文")
                blocks = tuple(SourceBlock(p["text"], p["pdf_page"], i) for i, p in enumerate(pages))
                chunks = [asdict(chunk) for chunk in DocumentChunker(
                    max_characters=spec.chunk_config["max_characters"]).chunk(blocks)]
                cached = store.save_snapshot(spec, digest, pages, chunks)
            pages, chunks = cached["pages"], cached["chunks"]
            if len(pages) != actual_page_count:
                raise ValueError("缓存提取页数与原文件不一致，未发布完整正文")
            return self._publish_processed(source, old, old_pointer, spec, digest, size_bytes,
                raw_path, pages, chunks, checked_at, cache_hit, extraction_cache_hit,
                saved_extraction_only=saved_extraction_only)

    def _publish_processed(self, source, old, old_pointer, spec, digest, size_bytes,
                           raw_path, pages, chunks, checked_at, cache_hit, extraction_cache_hit,
                           *, saved_extraction_only=False, format_fields=None):
        """各原件格式共用同一次权限谱系、不可覆盖快照和事务发布。"""
        source_id = source["source_id"]
        folder = self.cache_dir / source_id
        catalog = self._selected_snapshot_catalog()
        policy = policy_for_source(source)
        processor_version = spec.processor_version
        snapshot_id = spec.snapshot_id(digest)
        if (old and old["sha256"] == digest and old["snapshot_id"] != snapshot_id
                and not (folder / f"{old['snapshot_id']}.json").exists()):
            raise ValueError("旧固定快照缺失，保留旧读取入口，不能跳过旧使用限制升版")
        from archspec.official_documents.native_text import native_completeness
        result = {
            **native_completeness(pages),
            "source_id": source_id,
            **source,
            "snapshot_id": snapshot_id,
            "sha256": digest,
            "storage_layout": "CONTENT_V1",
            "processing_identity": spec.identity(digest),
            "extraction_id": spec.extraction_id(digest),
            "processing_payload_sha256": stable_hash({"pages": pages, "chunks": chunks}),
            "permission_parent_ids": self._same_blob_history(source_id, digest, snapshot_id),
            "processor_version": processor_version,
            "size_bytes": size_bytes,
            "processing_limits": asdict(policy),
            "processed_at": checked_at,
            "last_fetched_at": old["last_fetched_at"] if saved_extraction_only else checked_at,
            "status": "PENDING_REVIEW",
            "cache_hit": False,
            "page_count": len(pages),
            "ocr_page_count": sum(p["method"] in {"OCR", "HYBRID"} for p in pages),
            "notice": "待核验：正文识别不是版本或授权核验；PDF页序不等于印刷页码。未进入正式证据池。",
            **(format_fields or {}),
            "pages": pages,
            "chunks": chunks,
        }
        if saved_extraction_only:
            result.update(processing_origin="SAVED_EXTRACTION_RECHUNK", rechunked_at=checked_at)
        history = folder / f"{snapshot_id}.json"
        if history.exists():
            prior = json.loads(history.read_text(encoding="utf-8"))
            self._validate_snapshot(prior, source_id)
            if any(prior.get(key) != result.get(key) for key in (
                    "sha256", "processing_identity", "pages", "chunks", "size_bytes")):
                raise ValueError("缓存编号对应内容冲突，未覆盖旧快照")
            result = prior
        else:
            publish_json(history, result)
        if catalog is None:
            self._write(folder / "latest.json", result)
        else:
            # 固定JSON先完整发布；事务失败保留历史文件但不改变旧SQL选择。
            # 这是当前选择，不代替外部对象/向量的完整代次发布。
            expected = old_pointer["event_sha256"] if old_pointer is not None else None
            from archspec.official_documents.processing_cache import ProcessingLimited
            try:
                publication = (catalog.publisher.prepare(self, result, expected_event_sha256=expected)
                    if catalog.publisher is not None else None)
            except ProcessingLimited as error:
                from archspec.official_documents.originals import save_original
                return save_original(self, source_id, raw_path, digest, size_bytes, error=error)
            catalog.publish({"source_id": source_id, "snapshot_id": result["snapshot_id"],
                "snapshot_sha256": hashlib.sha256(history.read_bytes()).hexdigest(),
                "document_sha256": result["sha256"]},
                expected_event_sha256=expected, publication=publication)
        return {**result, "cache_hit": cache_hit, "extraction_cache_hit": extraction_cache_hit}

    @staticmethod
    def _check_pdf_budget(path, policy):
        from archspec.official_documents.originals import PdfInputRejected

        if not 1 <= path.stat().st_size <= policy.max_bytes:
            raise PdfInputRejected("SIZE_LIMIT")
        try:
            with pymupdf.open(path) as document:
                if document.is_repaired:
                    raise PdfInputRejected("DAMAGED")
                if document.is_encrypted:
                    raise PdfInputRejected("ENCRYPTED")
                if not 1 <= len(document) <= policy.max_pages:
                    raise PdfInputRejected("PAGE_LIMIT", max_pages=policy.max_pages)
                return len(document)
        except pymupdf.FileDataError as error:
            raise PdfInputRejected("DAMAGED") from error

    @contextmanager
    def _verified_pdf_input(self, path, record, policy, size):
        """指纹与解码必须是同份字节；大原件在私有临时目录分块保全后检查。"""
        if size > 1024 * 1024 and record.get("source_format") != "HTML":
            # 目录0700、文件0600；原路径在校验后即使换入B，解码仍只读已核的A副本。
            # 最多原已核大小+1字节，既检测增长，也不为大PDF预分配整份Python内存。
            with TemporaryDirectory(prefix="archspec-verified-pdf-") as folder:
                verified = Path(folder) / "original.pdf"
                digest, copied = hashlib.sha256(), 0
                with path.open("rb") as raw, verified.open("xb") as target:
                    os.chmod(verified, 0o600)
                    while copied <= size:
                        block = raw.read(min(256 * 1024, size + 1 - copied))
                        if not block:
                            break
                        copied += len(block)
                        digest.update(block)
                        target.write(block)
                if (copied != record["size_bytes"] or copied > policy.max_bytes
                        or digest.hexdigest() != record["sha256"]):
                    raise ValueError("缓存原文件损坏、指纹不符或大小超限，不能标记复用")
                yield {"filename": str(verified), "filetype": "pdf"}
            return
        # stat之后原件仍可能增长或被替换；只读一份限额字节，并同时用于指纹与解码。
        # 按已核实际大小多读1字节检测增长，避免为小文件预分配30/80MiB；不再按路径打开B文件。
        with path.open("rb") as raw:
            content = raw.read(size + 1)
        if (len(content) != record["size_bytes"] or len(content) > policy.max_bytes
                or hashlib.sha256(content).hexdigest() != record["sha256"]):
            raise ValueError("缓存原文件损坏、指纹不符或大小超限，不能标记复用")
        if record.get("source_format") == "HTML":
            from archspec.official_documents.web_body import (
                checked_rendering_path,
                inspect_web_body,
            )
            inspect_web_body(content, record)
            content = checked_rendering_path(self, record).read_bytes()
        yield {"stream": content, "filetype": "pdf"}

    def _check_cached_file(self, record, source_id, *, validated=False):
        """复用只检查原件结构和指纹，不重新识别健康的旧正文。"""
        # 只有紧邻内部加载器的只读副本可省结构重验；获取/外部记录仍走默认校验。
        if not validated:
            self._validate_snapshot(record, source_id)
        policy = self._snapshot_policy(record, source_id)
        path = self.raw_file_path(record, source_id)
        if not path.exists():
            raise ValueError("缓存原文件缺失，不能标记为完整正文复用")
        size = path.stat().st_size
        if size != record["size_bytes"] or not 1 <= size <= policy.max_bytes:
            raise ValueError("缓存原文件损坏、指纹不符或大小超限，不能标记复用")
        try:
            with self._verified_pdf_input(path, record, policy, size) as pdf_input, pymupdf.open(**pdf_input) as document:
                if document.is_repaired:
                    raise ValueError("缓存PDF结构损坏或不完整，不能复用自动修复后的正文")
                if (document.is_encrypted or len(document) != record["page_count"]
                        or not 1 <= len(document) <= policy.max_pages):
                    raise ValueError("缓存原文件加密或页数与完整快照不一致")
        except pymupdf.FileDataError as error:
            raise ValueError("缓存原文件损坏，不能安全复用") from error

    def _extract(self, content, max_pages=100, progress=None, spec=None):
        from archspec.official_documents.native_text import NATIVE_OCR_VERSION, extract_native_pages
        selected = spec or self._processing_spec()
        if selected.ocr_version == NATIVE_OCR_VERSION:
            return extract_native_pages(content, selected, max_pages=max_pages, progress=progress)
        return self._process(content, max_pages=max_pages, progress=progress, spec=selected)

    def _process(self, content, max_pages=100, progress=None, spec=None):
        config = (spec or self._processing_spec()).extraction_config
        watermarks = set(config["watermark_lines"])
        scale, max_pixels = config["ocr_scale"], config["max_ocr_pixels"]
        pages, sizes = [], []
        document = (pymupdf.open(content) if isinstance(content, Path)
                    else pymupdf.open(stream=content, filetype="pdf"))
        with document:
            # 阅读器可能自动修复截断的附件；能打开不等于已取得完整正文。
            # 保守拒绝这类文件，失败时仍保留原来已成功的快照。
            if document.is_repaired:
                raise ValueError("PDF结构损坏或不完整，不能将自动修复后的文件当作完整正文保存")
            if document.is_encrypted or not 1 <= len(document) <= max_pages:
                raise ValueError(f"附件加密或页数不在1—{max_pages}页范围")
            for number, page in enumerate(document, start=1):
                sizes.append((page.rect.width, page.rect.height))
                native = native_lines(page)
                body_lines = [
                    row for row in native if row["text"].strip() not in watermarks
                ]
                # 水印行仍保留在原始记录，但不能抢占同位置的图片正文。
                for row in native:
                    row["included_in_text"] = row["text"].strip() not in watermarks
                # 小图片也可能有尺寸或表格；默认任意图片触发本机识别。
                needs_ocr = (not body_lines or
                             image_area(page) > page.rect.get_area() * config["image_area_threshold"])
                if needs_ocr and page.rect.get_area() * scale ** 2 > max_pixels:
                    raise ValueError("页面过大，无法安全进行文字识别")
                warnings = ["OCR_NEEDS_REVIEW"] if needs_ocr else []
                if needs_ocr:
                    recognized = (self.ocr(page, scale=scale, max_pixels=max_pixels)
                                 if self.ocr is recognize_page else self.ocr(page))
                    gap_states = {row.get("ocr_gap_status") for row in recognized}
                    if any(row.get("ocr_gap_status") == "UNRESOLVED"
                           and row.get("ocr_gap_kind") != "UNNUMBERED_TEXT" for row in recognized):
                        warnings.append("OCR_NUMBERED_ITEM_GAP")
                    if any(row.get("ocr_gap_status") == "UNRESOLVED"
                           and row.get("ocr_gap_kind") == "UNNUMBERED_TEXT" for row in recognized):
                        warnings.append("IMAGE_TEXT_NOT_DETECTED")
                    if "RECOVERED_NEEDS_REVIEW" in gap_states:
                        warnings.append("OCR_GAP_RECOVERED_NEEDS_REVIEW")
                    # 只有水印的原生行也保存，但前面已标记为不参与正文合并。
                    raw_lines, position_warnings = merge_lines(page, native, recognized, scale)
                    warnings.extend(position_warnings)
                    if body_lines and not any(row["origin"] == "OCR" and row["included_in_text"]
                                              and row["text"].strip() not in watermarks
                                              for row in raw_lines):
                        warnings.append("IMAGE_TEXT_NOT_DETECTED")
                else:
                    raw_lines = native
                clean_lines = [
                    r["text"].strip() for r in raw_lines
                    if r["included_in_text"] and r["text"].strip() not in watermarks
                ]
                text = "\n".join(line for line in clean_lines if line)
                # 先保全原生与识别行。仅两者都为空且逐像素纯白时标为空白，
                # 不用分类抢先删掉已有文字，也不把淡线/未识别图页当空白。
                proof = blank_render(page, scale=scale, max_pixels=max_pixels) if not raw_lines else None
                if not text and proof is None:
                    raise ValueError(f"第{number}页未识别出正文，不能保存为完整成功结果")
                if proof is not None:
                    warnings = ["VISIBLE_BLANK_PAGE"]
                if any(r["score"] is not None and r["score"] < config["low_confidence_threshold"]
                       for r in raw_lines if r["included_in_text"]):
                    warnings.append("LOW_CONFIDENCE_OCR")
                pages.append(
                    {
                        "pdf_page": number,
                        "method": ("BLANK" if proof is not None else
                                   ("HYBRID" if body_lines else "OCR") if needs_ocr else "TEXT"),
                        "text": text,
                        "raw_lines": raw_lines,
                        "warnings": warnings,
                        **({"blank_render": proof} if proof is not None else {}),
                    }
                )
                if progress:
                    progress("PROCESSING", {"page_number": number, "page_count": len(document)})
        # 同一原页的连续位置证据同时用于原生与扫描正文；原行和坐标始终保存。
        _exclude_repeated_page_numbers(pages, sizes, origins=("TEXT", "OCR"))
        if not any(page["text"].strip() for page in pages):
            raise ValueError("整份附件均为可见空白页，未识别出正文")
        return pages

    @staticmethod
    def _file_fingerprint(path):
        with path.open("rb") as raw:
            return hashlib.file_digest(raw, "sha256").hexdigest()

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


# 类定义完成时固定本进程的提取实现；依赖身份取各自加载时保存的原字节。
# 不在请求时读取源码，也不修改任何历史正文、原件或缓存记录。
_LOADED_PROCESSOR_IDENTITY = frozen_processor_identity(
    OfficialDocumentService._process,
    PAGE_SOURCE_IDENTITY,
    OCR_SOURCE_IDENTITY,
    CHUNKER_SOURCE_IDENTITY,
    parser_sources=(MARGIN_SOURCE_IDENTITY, LAYOUT_SOURCE_IDENTITY, CELL_SOURCE_IDENTITY, CROP_SOURCE_IDENTITY),
)
