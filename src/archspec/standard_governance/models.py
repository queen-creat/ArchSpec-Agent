"""新增规范治理表；只有显式迁移才创建，旧声明不自动变成核验。"""

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from archspec.database_schema import Base

POLICY_VERSION = "SOURCE_ADMISSION_V1"
OPERATIONS = frozenset({"LOCAL_SEARCH", "LOCAL_EXCERPT", "LOCAL_FULL_TEXT_READ",
                        "ORIGINAL_PAGE", "MODEL_SEND", "EXPORT", "REDISTRIBUTE"})
SCOPE_SUBJECTS = {"SOURCE_IDENTITY": "SOURCE", "VERSION_STATUS": "VERSION",
                  "FULL_TEXT_BINDING": "ARTIFACT", "CONTENT_USE": "SOURCE",
                  "VERSION_RELATION": "RELATION"}


class GovernanceError(ValueError):
    """统一可解释的业务错误，不把数据库堆栈或凭据发给网页。"""

    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def utcnow():
    return datetime.now(UTC)


class StandardRow(Base):
    __tablename__ = "governance_standards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    raw_code: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str] = mapped_column(String(200))
    standard_system: Mapped[str] = mapped_column(String(100))
    identity_note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StandardVersionRow(Base):
    __tablename__ = "governance_standard_versions"
    __table_args__ = (UniqueConstraint("standard_id", "edition_key", "revision_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    standard_id: Mapped[str] = mapped_column(ForeignKey("governance_standards.id"), index=True)
    edition_code: Mapped[str] = mapped_column(String(120))
    edition_key: Mapped[str] = mapped_column(String(120))
    revision_key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    declared_lifecycle: Mapped[str] = mapped_column(String(20))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceRecordRow(Base):
    __tablename__ = "governance_source_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    standard_version_id: Mapped[str | None] = mapped_column(ForeignKey("governance_standard_versions.id"), nullable=True)
    publisher: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(30))
    source_provider: Mapped[str] = mapped_column(String(100))
    announcement_url: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    attachment_url: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    attachment_kind: Mapped[str] = mapped_column(String(30))
    discovery_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    announcement_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    announcement_fingerprint_kind: Mapped[str] = mapped_column(String(40))
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    declared_access_mode: Mapped[str] = mapped_column(String(30))
    declared_usage_right_json: Mapped[str] = mapped_column(Text)
    declared_redistribution_policy: Mapped[str] = mapped_column(String(10))
    observation_sha256: Mapped[str] = mapped_column(String(64), unique=True)


class ArtifactBindingRow(Base):
    __tablename__ = "governance_artifact_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(ForeignKey("governance_source_records.id"), index=True)
    standard_version_id: Mapped[str] = mapped_column(ForeignKey("governance_standard_versions.id"), index=True)
    artifact_kind: Mapped[str] = mapped_column(String(30))
    legacy_document_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    legacy_source_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    legacy_snapshot_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_sha256: Mapped[str] = mapped_column(String(64))
    # 真实加工身份含解析器/适配器完整指纹，不能被VARCHAR(200)截断或拒绝。
    processing_version: Mapped[str] = mapped_column(Text)
    content_manifest_sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_level: Mapped[str] = mapped_column(String(30))
    processing_status: Mapped[str] = mapped_column(String(30))
    quality_issues_json: Mapped[str] = mapped_column(Text)
    binding_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerificationRecordRow(Base):
    __tablename__ = "governance_verification_records"
    __table_args__ = (UniqueConstraint("subject_kind", "subject_id", "scope", "sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(30))
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    scope: Mapped[str] = mapped_column(String(30))
    subject_manifest_sha256: Mapped[str] = mapped_column(String(64))
    # 对象本身没变、所属版本事实变了时，旧正文/用途核验也不能复活。
    standard_version_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str] = mapped_column(String(100))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checked_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    basis_source_record_id: Mapped[str] = mapped_column(ForeignKey("governance_source_records.id"))
    basis_note: Mapped[str] = mapped_column(Text)
    statement_json: Mapped[str] = mapped_column(Text)
    supersedes_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class LegacyBindingRow(Base):
    __tablename__ = "governance_legacy_bindings"
    __table_args__ = (UniqueConstraint("legacy_kind", "legacy_key", "legacy_manifest_sha256"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    legacy_kind: Mapped[str] = mapped_column(String(40))
    legacy_key: Mapped[str] = mapped_column(String(200))
    legacy_manifest_sha256: Mapped[str] = mapped_column(String(64))
    standard_version_id: Mapped[str | None] = mapped_column(ForeignKey("governance_standard_versions.id"), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(ForeignKey("governance_source_records.id"), nullable=True)
    artifact_binding_id: Mapped[str | None] = mapped_column(ForeignKey("governance_artifact_bindings.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VersionRelationRow(Base):
    __tablename__ = "governance_version_relations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    from_version_id: Mapped[str] = mapped_column(ForeignKey("governance_standard_versions.id"), index=True)
    to_version_id: Mapped[str | None] = mapped_column(ForeignKey("governance_standard_versions.id"), nullable=True)
    relation_type: Mapped[str] = mapped_column(String(30))
    effective_at: Mapped[date] = mapped_column(Date)
    affected_clauses_json: Mapped[str] = mapped_column(Text)
    source_record_id: Mapped[str] = mapped_column(ForeignKey("governance_source_records.id"))
    relation_manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True)


class ReviewEvidencePolicyRow(Base):
    __tablename__ = "governance_review_evidence_policies"
    job_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_mode: Mapped[str] = mapped_column(String(30))
    admission_policy_version: Mapped[str] = mapped_column(String(100))


class MigrationLedgerRow(Base):
    __tablename__ = "governance_migration_ledger"
    migration_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
