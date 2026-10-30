"""正文、原页、固定档案与向量共同发布；现有目录事务消费内部核验收据。"""
from __future__ import annotations

import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Column, MetaData, String, Table, Text, func, insert, select

from archspec.persistence import control_image, evidence_manifest, sql_namespace
from archspec.persistence.control_migration import _decode, installation_scope
from archspec.persistence.migration_flow import _canonical, _sha
from archspec.persistence.snapshot_catalog import SnapshotCatalogUnavailable

_SCHEMA = "OFFICIAL_RUNTIME_PUBLICATION_V1"
_SAFE = "正文、原页与向量尚未完整发布，旧选择及历史保留"
_META = MetaData()
_RECORDS = Table("official_snapshot_publications", _META,
    Column("event_sha256", String(64), primary_key=True),
    Column("body_sha256", String(64), nullable=False),
    Column("body_json", Text, nullable=False))
_MAX_HISTORY_BYTES = 32 * 1024 * 1024


def _table(connection, *, check=True, _schema_review=None):
    table = sql_namespace.bind_tables(connection, [_RECORDS])[0]
    if check:
        if _schema_review is None:control_image._physical_schema(connection,table)
        else:control_image._assert_schema_review(_schema_review,connection,[table])
    return table


def install_schema(engine, *, connection):
    """随原控制迁移显式安装；启动和读取不建空表或补历史。"""
    from archspec.persistence.snapshot_catalog import _LOCK, _transaction
    with installation_scope(engine, connection, _transaction, pg_lock=_LOCK) as selected:
        table = _table(selected, check=False)
        table.create(selected, checkfirst=True)
        _table(selected)


def _body(row, control, event, previous, model_fingerprint=None):
    content = row["body_json"].encode("utf-8")
    if hashlib.sha256(content).hexdigest() != row["body_sha256"]:
        raise SnapshotCatalogUnavailable(_SAFE)
    body = _decode(content)
    identity = body["identity"]
    pointer = identity["pointer"]
    if (body["schema_version"] != _SCHEMA or _sha(identity) != body["publication_sha256"]
            or identity["source_cut_sha256"] != control["source_cut_sha256"]
            or identity["migration_id"] != control["migration_id"]
            or identity["expected_event_sha256"] != previous
            or row["event_sha256"] != event["event_sha256"]
            or any(pointer[k] != event[k] for k in
                ("source_id", "snapshot_id", "snapshot_sha256", "document_sha256"))
            or body["asset_manifest"]["manifest_id"] != identity["asset_manifest_id"]
            or body["asset_manifest"]["schema_version"] != evidence_manifest.SCHEMA
            or body["vector_index"]["model_fingerprint"] != identity["model_fingerprint"]
            or model_fingerprint is not None and identity["model_fingerprint"] != model_fingerprint):
        raise SnapshotCatalogUnavailable(_SAFE)
    evidence_manifest._check_manifest(body["asset_manifest"])
    entries = {e["path"]: e for e in body["asset_manifest"]["entries"]}
    pin = "official-cache/" + pointer["source_id"] + "/" + pointer["snapshot_id"] + ".json"
    if pin not in entries or entries[pin]["sha256"] != pointer["snapshot_sha256"]:
        raise SnapshotCatalogUnavailable(_SAFE)
    return body


def read_publications(connection, *, model_fingerprint=None, _schema_review=None, _catalog=None):
    """完整核对新增快照事件与收据一一对应；不能用空表掩盖已选择的新正文。"""
    from archspec.persistence.snapshot_catalog import _state
    if _catalog is None:
        control, events, _, _ = _state(connection)
    else:
        from archspec.persistence.snapshot_catalog import SQLSnapshotCatalog
        if type(_catalog) is not SQLSnapshotCatalog or _catalog.engine is not connection.engine:
            raise SnapshotCatalogUnavailable(_SAFE)
        control, events, _, _ = _catalog._publication_state(connection)
    table = _table(connection,_schema_review=_schema_review)
    count, size = connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(table.c.body_json)), 0)).select_from(table)).one()
    if count > 50_000 or size > _MAX_HISTORY_BYTES:
        raise SnapshotCatalogUnavailable(_SAFE)
    rows = {r["event_sha256"]: dict(r) for r in connection.execute(select(table)).mappings()}
    initial = len(json.loads(control["initial_json"]))
    required = {e["event_sha256"] for e in events[initial:]}
    if set(rows) != required:
        raise SnapshotCatalogUnavailable(_SAFE)
    result = []
    heads = {}
    for number, event in enumerate(events):
        if number >= initial:
            result.append(_body(rows[event["event_sha256"]], control, event,
                heads.get(event["source_id"]), model_fingerprint))
        heads[event["source_id"]] = event["event_sha256"]
    return result


def merge_assets(base, publications):
    """不可变路径只准相同字节重用，恢复原件及所有历史，不覆盖旧快照。"""
    entries = {e["path"]: e for e in evidence_manifest._check_manifest(base)}
    for body in publications:
        for entry in evidence_manifest._check_manifest(body["asset_manifest"]):
            previous = entries.get(entry["path"])
            if previous is not None and previous != entry:
                raise SnapshotCatalogUnavailable(_SAFE)
            entries[entry["path"]] = entry
    manifest = {"schema_version": base["schema_version"],
        "entries": [entries[p] for p in sorted(entries)]}
    manifest["manifest_id"] = _sha(manifest)
    evidence_manifest._check_manifest(manifest)
    return manifest


@dataclass(frozen=True)
class VerifiedSnapshotPublication:
    """只在实际对象/向量回读完成后签发；不从HTTP接受此类型或目标地址。"""
    owner: object = field(repr=False)
    documents: object = field(repr=False)
    body_json: str = field(repr=False)


class RuntimeSnapshotPublisher:
    def __init__(self, generation, objects, vectors):
        self.generation, self.objects, self.vectors = generation, objects, vectors
        self.engine = generation.engine
        self.cache_dir = generation.data_dir / "official-cache"
        self._provider = None
        self._preparing = ContextVar("archspec_snapshot_internal_read", default=None)

    def validate_binding(self, engine, cache_dir):
        from archspec.persistence.remote_startup import _ISSUER, RestoredCloudGeneration
        owner = self.generation
        if (type(owner) is not RestoredCloudGeneration or owner._issuer is not _ISSUER
                or owner.closed or owner.closing or engine is not self.engine
                or owner.engine is not engine or Path(cache_dir).resolve() != self.cache_dir
                or self.objects is None or self.vectors is None):
            raise SnapshotCatalogUnavailable(_SAFE)

    def _model(self):
        if self._provider is None:
            from archspec.embeddings.provider import PinnedFastEmbedProvider
            directory = os.getenv("ARCHSPEC_EMBEDDING_MODEL_DIR",
                str(self.generation.data_dir.parent / "models" / "bge-small-zh-v1.5"))
            self._provider = PinnedFastEmbedProvider(Path(directory))
        if self._provider.model_fingerprint != self.generation.metadata["vector_index"]["model_fingerprint"]:
            raise SnapshotCatalogUnavailable("固定嵌入模型与已发布索引身份不同，旧正文选择保留")
        return self._provider

    def _profile(self, documents, snapshot):
        source, pin = snapshot["source_id"], snapshot["snapshot_id"]
        token = self._preparing.set((documents, source, pin))
        try:
            documents._check_read(source, pin, "LOCAL_FULL_TEXT_READ")
            registration = documents._source(source)
            registry_sha = _sha(registration)
            if registration["kind"] != "FULL_TEXT":
                return {}, registry_sha
            documents._check_read(source, pin, "LOCAL_SEARCH")
            fixed, current, profile_sha = documents._metadata_filter_profile(snapshot)
            return {source: (fixed, {k: v for k, v in current.items() if k != "evaluated_at"}, profile_sha)}, registry_sha
        finally:
            self._preparing.reset(token)

    def assert_readable(self, documents, source_id, snapshot_id):
        """固定阅读只接受已提交资产；内部档案准备仅对本对象/本身份短时放行。"""
        self.validate_binding(self.engine, documents.cache_dir)
        catalog = documents._selected_snapshot_catalog()
        if catalog is None or catalog.publisher is not self or catalog.engine is not self.engine:
            raise SnapshotCatalogUnavailable(_SAFE)
        allowed = self._preparing.get()
        if allowed is not None and allowed[0] is documents and allowed[1:] == (source_id, snapshot_id):
            return
        relative = "official-cache/" + source_id + "/" + snapshot_id + ".json"
        if any(entry["path"] == relative for entry in self.generation.metadata["asset_manifest"]["entries"]):
            return
        from archspec.persistence.runtime_bindings import publication_read_scope
        fingerprint = self.generation.metadata["vector_index"]["model_fingerprint"]
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise SnapshotCatalogUnavailable(_SAFE)
        with publication_read_scope(self.engine) as connection:
            scope = catalog._publication_read_scope(connection, self)
            key = (self, fingerprint)
            identities = None if scope is None else scope[1].get(key)
            if identities is None:
                bodies = read_publications(connection, model_fingerprint=fingerprint,
                    **({"_catalog": catalog} if scope is not None else {}))
                identities = frozenset((body["identity"]["pointer"]["source_id"],
                    body["identity"]["pointer"]["snapshot_id"]) for body in bodies)
                if scope is not None:
                    scope[1][key] = identities
            else:
                # 只省同受锁目录阶段的收据/历史重读；完整约束、触发器、TEMP仍即时核验。
                _table(connection)
        if (source_id, snapshot_id) not in identities:
            raise LookupError("该固定正文尚未完整发布，原选择及历史保留")

    def prepare(self, documents, snapshot, *, expected_event_sha256):
        """网络与嵌入在SQL事务外；所有字节核齐后才返回内部收据。"""
        from archspec.official_documents.semantic_experiment import prepare_semantic_corpus
        from archspec.official_documents.service import OfficialDocumentService
        from archspec.persistence import migration_flow
        try:
            self.validate_binding(self.engine, documents.cache_dir)
            if (not isinstance(documents, OfficialDocumentService)
                    or documents.snapshot_catalog.publisher is not self):
                raise SnapshotCatalogUnavailable(_SAFE)
            source, pin = snapshot["source_id"], snapshot["snapshot_id"]
            documents._validate_snapshot(snapshot, source)
            documents._check_cached_file(snapshot, source, validated=True)
            profiles, registry_sha = self._profile(documents, snapshot)
            from archspec.official_documents.processing_cache import (
                ProcessingCache,
                ProcessingLimited,
                publication_compute_mode,
            )
            mode = publication_compute_mode()
            provider = self._model()  # 只核模型五件套和运行指纹；命中时绝不加载ONNX。
            # 获取与出版消费同一固定处理身份；默认OCR配置不代表这份已完成的产物。
            spec = documents._snapshot_processing_spec(snapshot)
            prepared = ProcessingCache(documents.cache_dir).load_publication(
                spec, snapshot["sha256"], snapshot["chunks"],
                provider.model_fingerprint, page_count=snapshot["page_count"])
            if prepared is None and mode == "prepared_only":
                raise ProcessingLimited()
            corpus = prepare_semantic_corpus(
                [snapshot] if profiles else [], provider=provider,
                prepared_vectors=prepared.vectors if prepared is not None and profiles else None)
            paths = {"official-cache/" + source + "/" + pin + ".json",
                documents.raw_file_path(snapshot, source).relative_to(self.generation.data_dir).as_posix()}
            if snapshot.get("source_format") == "HTML":
                from archspec.official_documents.web_body import checked_rendering_path
                paths.add(checked_rendering_path(documents, snapshot).relative_to(
                    self.generation.data_dir).as_posix())
            for layer, identity in (("extractions", snapshot["extraction_id"]), ("snapshots", pin)):
                relative = "official-cache/content-store/" + layer + "/" + identity + ".json"
                if snapshot.get("storage_layout") == "CONTENT_V1":
                    paths.add(relative)
            for page in range(1, snapshot["page_count"] + 1):
                if prepared is None:
                    result = documents.prepare_page_image(source, pin, page)
                else:
                    # 仍逐页核当前原件/权限；只复用内容PNG，当前描述由原仓库生成。
                    with documents._fixed_page(source, pin, page) as (record, _):
                        repository = documents._selected_page_image_repository()
                        if repository is None:
                            raise SnapshotCatalogUnavailable(_SAFE)
                        result = repository.save(source, pin, record["sha256"], page,
                                                 prepared.page(page))
                paths.update(result["asset_paths"])
            if profiles:
                fixed = profiles[source][0]
                paths.add("official-cache/source-metadata/bindings/" + source + "/" + pin + ".json")
                paths.add("official-cache/source-metadata/observations/" + fixed["metadata_id"] + ".json")
            # 同原件重切分的许可前序也是证据闭包；不能让失败暂存前序在重启后消失。
            for parent in documents._permission_lineage(source, pin):
                paths.add("official-cache/" + source + "/" + parent + ".json")
            manifest = evidence_manifest.build_manifest(self.generation.data_dir, sorted(paths))
            from archspec.persistence.business_files import merged_assets, read_state
            with control_image.image_transaction(self.engine) as connection:
                history = read_publications(connection, model_fingerprint=provider.model_fingerprint)
                merged_assets(self.generation.metadata, history + [{"asset_manifest": manifest}], read_state(connection))
            pointer = {"source_id": source, "snapshot_id": pin,
                "snapshot_sha256": evidence_manifest._sha(self.cache_dir / source / (pin + ".json")),
                "document_sha256": snapshot["sha256"]}
            identity = {"migration_id": self.generation.metadata["migration_id"],
                "source_cut_sha256": self.generation.metadata["source_cut_sha256"],
                "pointer": pointer, "expected_event_sha256": expected_event_sha256,
                "asset_manifest_id": manifest["manifest_id"], "model_fingerprint": provider.model_fingerprint,
                "registry_sha256": registry_sha, "profile_sha256": profiles[source][2] if profiles else None}
            publication_sha = _sha(identity)
            points = migration_flow._points_for_corpus(corpus, profiles, publication_sha)
            self.objects.store.health_check()
            self.vectors.health_check()
            work = self.generation.scratch_dir / ("publication-" + uuid4().hex)
            work.mkdir()
            evidence_manifest.publish_manifest(self.generation.data_dir, manifest, self.objects)
            evidence_manifest.restore_manifest(manifest, work / "readback", self.objects)
            migration_flow._write_verify_vectors(points, self.vectors, publication_sha)
            index = migration_flow._vector_artifacts(points, corpus, self.objects, work, self.vectors.collection)
            body = {"schema_version": _SCHEMA, "identity": identity, "publication_sha256": publication_sha,
                "asset_manifest": manifest, "vector_index": index,
                "retrieval_eligibility": {source: {"snapshot_id": pin,
                    "state": "ALLOWED" if profiles else "NOT_FULL_TEXT", "operation": "LOCAL_SEARCH"}}}
            receipt = VerifiedSnapshotPublication(self, documents, _canonical(body).decode("utf-8"))
            self.verify(receipt, pointer, expected_event_sha256)
            return receipt
        except SnapshotCatalogUnavailable:
            raise
        except Exception as error:
            from archspec.official_documents.processing_cache import ProcessingLimited
            from archspec.standard_governance.compatibility import ContentUseDenied
            if isinstance(error, (ContentUseDenied, ProcessingLimited)):
                raise
            raise SnapshotCatalogUnavailable(_SAFE) from None

    def verify(self, receipt, pointer, expected_event_sha256):
        if type(receipt) is not VerifiedSnapshotPublication or receipt.owner is not self:
            raise SnapshotCatalogUnavailable(_SAFE)
        self.validate_binding(self.engine, receipt.documents.cache_dir)
        body = _decode(receipt.body_json.encode("utf-8"))
        identity = body["identity"]
        if identity["pointer"] != pointer or identity["expected_event_sha256"] != expected_event_sha256:
            raise SnapshotCatalogUnavailable(_SAFE)
        snapshot, content = receipt.documents._load_snapshot(
            self.cache_dir / pointer["source_id"] / (pointer["snapshot_id"] + ".json"), pointer["source_id"])
        if hashlib.sha256(content).hexdigest() != pointer["snapshot_sha256"]:
            raise SnapshotCatalogUnavailable(_SAFE)
        profiles, registry_sha = self._profile(receipt.documents, snapshot)
        if (registry_sha != identity["registry_sha256"]
                or (profiles[pointer["source_id"]][2] if profiles else None) != identity["profile_sha256"]):
            raise SnapshotCatalogUnavailable(_SAFE)
        for entry in evidence_manifest._check_manifest(body["asset_manifest"]):
            path = evidence_manifest._file(self.generation.data_dir, entry["path"])
            if path.stat().st_size != entry["size_bytes"] or evidence_manifest._sha(path) != entry["sha256"]:
                raise SnapshotCatalogUnavailable(_SAFE)
        return body

    def commit(self, connection, event, receipt, *, expected_event_sha256):
        """只借用原目录写事务，权限/工人fence复核与收据、指针一起提交。"""
        if connection.engine is not self.engine:
            raise SnapshotCatalogUnavailable(_SAFE)
        pointer = {k: event[k] for k in ("source_id", "snapshot_id", "snapshot_sha256", "document_sha256")}
        from archspec.persistence.runtime_bindings import borrowed_read_scope
        with borrowed_read_scope(receipt.documents._runtime_bindings, connection):
            body = self.verify(receipt, pointer, expected_event_sha256)
        table = _table(connection)
        content = _canonical(body)
        row = {"event_sha256": event["event_sha256"], "body_json": content.decode("utf-8"),
            "body_sha256": hashlib.sha256(content).hexdigest()}
        previous = connection.execute(select(table).where(table.c.event_sha256 == event["event_sha256"])).mappings().one_or_none()
        if previous is None:
            connection.execute(insert(table).values(**row))
        elif dict(previous) != row:
            raise SnapshotCatalogUnavailable(_SAFE)
        from archspec.persistence.business_files import merged_assets, read_state
        merged_assets(self.generation.metadata,
            read_publications(connection, model_fingerprint=self.generation.metadata["vector_index"]["model_fingerprint"]),
            read_state(connection))
