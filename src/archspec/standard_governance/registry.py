"""把受信服务端保存的声明登记成固定观察；不进行下载和可信审批。"""

import re
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import select

from archspec.standard_governance.models import (
    ArtifactBindingRow,
    GovernanceError,
    LegacyBindingRow,
    SourceRecordRow,
    StandardRow,
    StandardVersionRow,
)
from archspec.standard_governance.repository import canonical_json, fingerprint, record

HASH = re.compile(r"^[a-f0-9]{64}$")
CODE = re.compile(r"^(GB(?:/T)?|JGJ(?:/T)?|CJJ(?:/T)?|DB\d+(?:/T)?)\s*(\d+(?:\.\d+)?)-(\d{4})$")


def parse_date(value):
    if value is None:
        return None
    try:
        return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))
    except ValueError as error:
        raise GovernanceError("INVALID_VERIFICATION_REQUEST", "日期格式应为年-月-日") from error


def valid_hash(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise GovernanceError("INVALID_VERIFICATION_REQUEST", "固定对象指纹须为64位SHA-256")
    return value


def code_parts(value):
    compact = re.sub(r"\s+", "", value.upper())
    match = CODE.fullmatch(compact)
    if not match:
        return None, value.strip()
    family, number, year = match.groups()
    return f"{family} {number}", f"{family} {number}-{year}"


class SourceRegistryService:
    def __init__(self, repository):
        self.repo = repository

    def observe(self, declaration, artifact=None, *, legacy_kind=None, legacy_key=None,
                legacy_manifest=None):
        self.repo.require_available()
        if not isinstance(declaration, dict):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "来源声明须为对象")
        code = str(declaration.get("standard_code", "")).strip()
        title = str(declaration.get("title", "")).strip()
        publisher = str(declaration.get("publisher", "")).strip()
        if not code or not title or not publisher:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "编号、题名和发布机构不能为空")
        manifest = (valid_hash(legacy_manifest) if isinstance(legacy_manifest, str)
                    else fingerprint(legacy_manifest if legacy_manifest is not None else {"declaration": declaration, "artifact": artifact}))
        if legacy_kind and legacy_key:
            previous = self.repo.resolve_legacy(legacy_kind, legacy_key, manifest)
            if previous:
                return self._bound_result(previous)
        canonical_key, edition_code = code_parts(code)
        session = self.repo.session
        standard = session.scalar(select(StandardRow).where(StandardRow.canonical_key == canonical_key)) if canonical_key else None
        if standard is None:
            standard = StandardRow(id=str(uuid4()), canonical_key=canonical_key, raw_code=code,
                                   title=title, publisher=publisher,
                                   standard_system=str(declaration.get("standard_system", "UNKNOWN")),
                                   identity_note="" if canonical_key else "编号未能可靠解析，不按同名合并")
            session.add(standard)
            session.flush()
        revision = str(declaration.get("revision_key", "ORIGINAL"))
        edition_key = edition_code if canonical_key else f"UNRESOLVED:{standard.id}"
        version = session.scalar(select(StandardVersionRow).where(
            StandardVersionRow.standard_id == standard.id, StandardVersionRow.edition_key == edition_key,
            StandardVersionRow.revision_key == revision,
        ))
        facts = {"standard_id": standard.id, "edition_code": edition_code, "edition_key": edition_key,
                 "revision_key": revision, "title": title,
                 "published_at": parse_date(declaration.get("published_at")),
                 "effective_at": parse_date(declaration.get("effective_at")),
                 "declared_lifecycle": str(declaration.get("declared_lifecycle", "UNKNOWN"))}
        if version is None:
            version = StandardVersionRow(id=str(uuid4()), **facts, manifest_sha256=fingerprint(facts))
            session.add(version)
        else:
            current_facts = {name: getattr(version, name) for name in facts}
            if current_facts != facts:
                # 变化链即使 A→B→A 也不回到旧指纹，必须重新核对而不能复活旧成功。
                version.manifest_sha256 = fingerprint({"facts": facts, "previous_manifest": version.manifest_sha256})
                for name, value in facts.items():
                    setattr(version, name, value)
        session.flush()
        source_facts = {
            "standard_version_id": version.id, "publisher": publisher,
            "source_type": str(declaration.get("source_type", "LOCAL_UPLOAD")),
            "source_provider": str(declaration.get("source_provider", "LEGACY")),
            "announcement_url": declaration.get("announcement_url"),
            "attachment_url": declaration.get("attachment_url"),
            "attachment_kind": str(declaration.get("attachment_kind", "UNCONFIRMED")),
            "announcement_sha256": valid_hash(declaration.get("announcement_sha256"), True),
            "announcement_fingerprint_kind": str(declaration.get("announcement_fingerprint_kind", "UNKNOWN")),
            "file_sha256": valid_hash((artifact or {}).get("file_sha256", declaration.get("file_sha256")), True),
            "declared_access_mode": str(declaration.get("declared_access_mode", "UNKNOWN")),
            "declared_usage_right_json": canonical_json(declaration.get("declared_usage_right", {})),
            "declared_redistribution_policy": str(declaration.get("declared_redistribution_policy", "UNKNOWN")),
        }
        source_hash = fingerprint(source_facts)
        source = session.scalar(select(SourceRecordRow).where(SourceRecordRow.observation_sha256 == source_hash))
        if source is None:
            source = SourceRecordRow(id=str(uuid4()), **source_facts, observation_sha256=source_hash,
                                     discovery_run_id=declaration.get("discovery_run_id"), observed_at=datetime.now(UTC))
            session.add(source)
            session.flush()
        binding = self._artifact(source, version, artifact) if artifact else None
        legacy = None
        if legacy_kind and legacy_key:
            legacy = LegacyBindingRow(id=str(uuid4()), legacy_kind=legacy_kind, legacy_key=legacy_key,
                                      legacy_manifest_sha256=manifest, standard_version_id=version.id,
                                      source_record_id=source.id, artifact_binding_id=binding.id if binding else None,
                                      reason="旧登记仅作为声明导入，尚无显式核验")
            session.add(legacy)
            session.flush()
        return {"standard": record(standard), "version": record(version), "source": record(source),
                "artifact": record(binding), "legacy_binding": record(legacy)}

    def _artifact(self, source, version, artifact):
        kind = artifact.get("artifact_kind")
        if kind not in {"UPLOADED_DOCUMENT", "OFFICIAL_SNAPSHOT"}:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "正文绑定类型不支持")
        if kind == "UPLOADED_DOCUMENT" and not artifact.get("legacy_document_id"):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "上传正文缺少原文档编号")
        if kind == "OFFICIAL_SNAPSHOT" and not all(artifact.get(key) for key in ("legacy_source_id", "legacy_snapshot_id")):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "官方正文缺少固定来源/快照编号")
        quality = artifact.get("quality_issues", ["QUALITY_RECORD_MISSING"])
        if not isinstance(quality, list) or not all(isinstance(item, str) and item for item in quality):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "处理风险须为明确字符串列表")
        facts = {"source_record_id": source.id, "standard_version_id": version.id, "artifact_kind": kind,
                 "legacy_document_id": artifact.get("legacy_document_id"), "legacy_source_id": artifact.get("legacy_source_id"),
                 "legacy_snapshot_id": artifact.get("legacy_snapshot_id"),
                 "file_sha256": valid_hash(artifact.get("file_sha256")),
                 "processing_version": str(artifact.get("processing_version", "LEGACY")),
                 "content_manifest_sha256": valid_hash(artifact.get("content_manifest_sha256")),
                 "page_count": artifact.get("page_count"), "trace_level": str(artifact.get("trace_level", "LEGACY_UNLOCATED")),
                 "processing_status": str(artifact.get("processing_status", "UNKNOWN")),
                 "quality_issues_json": canonical_json(sorted(set(quality)))}
        if facts["page_count"] is not None and (type(facts["page_count"]) is not int or facts["page_count"] < 1):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "可靠页数须为正整数或未知")
        digest = fingerprint(facts)
        session = self.repo.session
        row = session.scalar(select(ArtifactBindingRow).where(ArtifactBindingRow.binding_sha256 == digest))
        if row is None:
            row = ArtifactBindingRow(id=str(uuid4()), **facts, binding_sha256=digest)
            session.add(row)
            session.flush()
        return row

    def _bound_result(self, legacy):
        version = self.repo.get_version(legacy["standard_version_id"]) if legacy["standard_version_id"] else None
        standard = record(self.repo.session.get(StandardRow, version["standard_id"])) if version else None
        return {"standard": standard, "version": version,
                "source": self.repo.get_source(legacy["source_record_id"]) if legacy["source_record_id"] else None,
                "artifact": self.repo.get_artifact(legacy["artifact_binding_id"]) if legacy["artifact_binding_id"] else None,
                "legacy_binding": legacy}
