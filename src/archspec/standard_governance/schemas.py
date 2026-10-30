"""显式核验的输入合同：未知字段拒绝，声明与真正批准分开。"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Permission = Literal["ALLOW", "DENY", "UNKNOWN"]
Operation = Literal["LOCAL_SEARCH", "LOCAL_EXCERPT", "LOCAL_FULL_TEXT_READ", "ORIGINAL_PAGE",
                    "MODEL_SEND", "EXPORT", "REDISTRIBUTE"]
SubjectKind = Literal["SOURCE", "VERSION", "ARTIFACT", "RELATION"]
Scope = Literal["SOURCE_IDENTITY", "VERSION_STATUS", "FULL_TEXT_BINDING", "CONTENT_USE",
                "VERSION_RELATION"]
RelationType = Literal["WHOLE_SUPERSEDES", "AMENDS", "PARTIAL_WITHDRAWAL"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceFromRecordRequest(StrictRequest):
    kind: Literal["DOCUMENT", "CATALOG", "OFFICIAL_SOURCE", "OFFICIAL_SNAPSHOT"]
    key: Identifier
    snapshot_id: Identifier | None = None

    @field_validator("key", "snapshot_id")
    @classmethod
    def only_record_identifiers(cls, value):
        if value is not None and ("/" in value or "\\" in value or value in {".", ".."}):
            raise ValueError("只能指定服务端已保存的记录标识，不能提交网址或文件路径")
        return value


class SourceIdentityStatement(StrictRequest):
    standard_code: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    publisher: Annotated[str, Field(min_length=1, max_length=200)]


class VersionStatusStatement(StrictRequest):
    lifecycle_status: Annotated[str, Field(min_length=1, max_length=30)]
    effective_at: date | None
    published_at: date | None = None


class FullTextBindingStatement(StrictRequest):
    artifact_binding_id: Identifier
    file_sha256: Fingerprint
    # 与完整处理身份一致；保留明确请求上限，不截短原件/快照绑定字段。
    processing_version: Annotated[str, Field(min_length=1, max_length=8192)]
    content_manifest_sha256: Fingerprint
    page_count: Annotated[int, Field(gt=0, strict=True)]
    complete_full_text: Literal[True]
    review_note: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("complete_full_text", mode="before")
    @classmethod
    def explicit_true(cls, value):
        if value is not True:
            raise ValueError("完整正文必须明确确认，不能以数字或字符串代替")
        return value


class ContentUseStatement(StrictRequest):
    access_mode: Annotated[str, Field(min_length=1, max_length=30)]
    operations: dict[Operation, Permission]
    redistribution_policy: Permission


class VersionRelationStatement(StrictRequest):
    relation_type: RelationType
    from_version_id: Identifier
    to_version_id: Identifier | None = None
    effective_at: date
    affected_clauses: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=1000
    )


_SCOPE_SUBJECTS = {"SOURCE_IDENTITY": "SOURCE", "VERSION_STATUS": "VERSION",
                   "FULL_TEXT_BINDING": "ARTIFACT", "CONTENT_USE": "SOURCE",
                   "VERSION_RELATION": "RELATION"}
_STATEMENTS = {"SOURCE_IDENTITY": SourceIdentityStatement, "VERSION_STATUS": VersionStatusStatement,
               "FULL_TEXT_BINDING": FullTextBindingStatement, "CONTENT_USE": ContentUseStatement,
               "VERSION_RELATION": VersionRelationStatement}


class VerificationRequest(StrictRequest):
    subject_kind: SubjectKind
    subject_id: Identifier
    scope: Scope
    decision: Literal["VERIFY", "REJECT"]
    expected_manifest_sha256: Fingerprint
    # 第一次核验也必须明确提交 null；缺省不能偷偷忽略并发检查。
    expected_head_id: Identifier | None
    basis_source_record_id: Identifier
    basis_note: Annotated[str, Field(min_length=1, max_length=4000)]
    checked_as_of: datetime
    valid_until: datetime
    statement: dict

    @field_validator("checked_as_of", "valid_until")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("日期必须明确时区")
        return value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.subject_kind != _SCOPE_SUBJECTS[self.scope]:
            raise ValueError("核验范围与对象类型不一致")
        if self.valid_until <= self.checked_as_of:
            raise ValueError("核验有效期必须晚于核验截至时间")
        if self.decision == "REJECT" and not self.statement:
            return self
        self.statement = _STATEMENTS[self.scope].model_validate(self.statement).model_dump(mode="json")
        return self


class RevokeVerificationRequest(StrictRequest):
    expected_head_id: Identifier
    expected_manifest_sha256: Fingerprint
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


class VersionRelationRequest(VersionRelationStatement):
    source_record_id: Identifier
