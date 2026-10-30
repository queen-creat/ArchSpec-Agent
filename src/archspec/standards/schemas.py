"""规范题录接口格式。"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from archspec.standards.models import (
    StandardLifecycleStatus,
    StandardMetadataRecord,
    StandardSourceType,
)

SOURCE_LABELS = {
    StandardSourceType.OFFICIAL: "官方来源",
    StandardSourceType.LOCAL_UNVERIFIED: "本地资料（未核验）",
}
STATUS_LABELS = {
    StandardLifecycleStatus.CURRENT: "现行",
    StandardLifecycleStatus.UPCOMING: "即将实施",
    StandardLifecycleStatus.SUPERSEDED: "已被替代",
    StandardLifecycleStatus.WITHDRAWN: "已废止",
    StandardLifecycleStatus.UNKNOWN: "状态未知",
}


class SaveStandardMetadataRequest(BaseModel):
    standard_code: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    authority: str = Field(min_length=1, max_length=200)
    source_url: str | None = Field(default=None, max_length=1000)
    source_type: StandardSourceType
    lifecycle_status: StandardLifecycleStatus
    published_at: date | None = None
    effective_at: date | None = None
    replaced_by: str | None = Field(default=None, max_length=100)


class StandardMetadataResponse(BaseModel):
    id: str
    document_id: str
    standard_code: str
    title: str
    authority: str
    source_url: str | None
    source_type: StandardSourceType
    source_type_label: str
    lifecycle_status: StandardLifecycleStatus
    lifecycle_status_label: str
    published_at: date | None
    effective_at: date | None
    replaced_by: str | None
    content_sha256: str
    checked_at: datetime

    @classmethod
    def from_record(cls, record: StandardMetadataRecord) -> "StandardMetadataResponse":
        return cls(
            **record.__dict__,
            source_type_label=SOURCE_LABELS[record.source_type],
            lifecycle_status_label=STATUS_LABELS[record.lifecycle_status],
        )
