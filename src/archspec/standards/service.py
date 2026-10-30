"""规范来源校验与可用版本筛选。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from archspec.documents.repository import DocumentRepository
from archspec.standard_catalog.models import StandardCatalogEntry
from archspec.standard_catalog.repository import StandardCatalogRepository
from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
from archspec.standards.models import (
    StandardLifecycleStatus,
    StandardMetadataRecord,
    StandardSourceType,
)
from archspec.standards.repository import StandardMetadataRepository

OFFICIAL_DOMAINS = (
    "mohurd.gov.cn",
    "openstd.samr.gov.cn",
    "std.samr.gov.cn",
)
INELIGIBLE_STATUSES = {
    # “即将实施”还没有法律效力，不能提前当作现行设计依据。
    StandardLifecycleStatus.UPCOMING,
    StandardLifecycleStatus.SUPERSEDED,
    StandardLifecycleStatus.WITHDRAWN,
}


class StandardMetadataValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StandardSourceDescriptor:
    document_id: str
    source_type: StandardSourceType
    lifecycle_status: StandardLifecycleStatus
    standard_code: str | None
    title: str | None
    source_url: str | None
    warning: str | None


class StandardMetadataService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        metadata_repository: StandardMetadataRepository,
        catalog_repository: StandardCatalogRepository | None = None,
    ) -> None:
        self.document_repository = document_repository
        self.metadata_repository = metadata_repository
        self.catalog_repository = catalog_repository
        self.admission = LegacyAdmissionAdapter(document_repository.session)

    def register(
        self,
        *,
        document_id: str,
        standard_code: str,
        title: str,
        authority: str,
        source_url: str | None,
        source_type: StandardSourceType,
        lifecycle_status: StandardLifecycleStatus,
        published_at: date | None,
        effective_at: date | None,
        replaced_by: str | None,
    ) -> StandardMetadataRecord:
        document = self.document_repository.get(document_id)
        if document is None:
            raise StandardMetadataValidationError("文档不存在")
        if source_type == StandardSourceType.OFFICIAL:
            self._validate_official_url(source_url)
        if lifecycle_status == StandardLifecycleStatus.SUPERSEDED and not replaced_by:
            raise StandardMetadataValidationError("被替代规范必须填写新标准编号")
        record = self.metadata_repository.upsert(
            document_id=document_id,
            standard_code=standard_code.strip(),
            title=title.strip(),
            authority=authority.strip(),
            source_url=source_url,
            source_type=source_type,
            lifecycle_status=lifecycle_status,
            published_at=published_at,
            effective_at=effective_at,
            replaced_by=replaced_by.strip() if replaced_by else None,
            content_sha256=document.sha256,
        )
        if self.catalog_repository is not None:
            self.catalog_repository.upsert(
                StandardCatalogEntry(
                    id="",
                    standard_code=record.standard_code,
                    title=record.title,
                    authority=record.authority,
                    lifecycle_status=record.lifecycle_status,
                    published_at=record.published_at,
                    effective_at=record.effective_at,
                    official_url=record.source_url,
                    source_provider="STANDARD_METADATA",
                    last_checked_at=datetime.now(UTC),
                    linked_document_id=record.document_id,
                )
            )
        return record

    def eligible_document_ids(self, document_ids: list[str]) -> list[str]:
        metadata = self.metadata_repository.list_for_documents(document_ids)
        return [
            document_id
            for document_id in document_ids
            if metadata.get(document_id) is None
            or metadata[document_id].lifecycle_status not in INELIGIBLE_STATUSES
        ]

    def verified_current_document_ids(self, document_ids: list[str]) -> list[str]:
        """只保留来源已核验且状态为现行的规范正文。"""

        # 手填 OFFICIAL/CURRENT 是声明，不是核验；由固定正文绑定统一准入。
        return self.admission.allowed_document_ids(document_ids)

    def get_metadata(self, document_id: str) -> StandardMetadataRecord | None:
        return self.metadata_repository.get_for_document(document_id)

    def describe_source(self, document_id: str) -> StandardSourceDescriptor:
        record = self.metadata_repository.get_for_document(document_id)
        if record is None:
            return StandardSourceDescriptor(
                document_id=document_id,
                source_type=StandardSourceType.LOCAL_UNVERIFIED,
                lifecycle_status=StandardLifecycleStatus.UNKNOWN,
                standard_code=None,
                title=None,
                source_url=None,
                warning="本地上传资料，来源和版本状态尚未核验",
            )
        decision = self.admission.for_document(document_id)
        warning = (
            None if decision["is_citable"]
            else "来源与状态仅为登记声明，未通过本次正式引用核验"
        )
        if record.source_type == StandardSourceType.LOCAL_UNVERIFIED:
            warning = "本地上传资料，来源尚未核验"
        elif record.lifecycle_status == StandardLifecycleStatus.UNKNOWN:
            warning = "官方来源已记录，但标准状态尚未核验"
        return StandardSourceDescriptor(
            document_id=document_id,
            source_type=record.source_type,
            lifecycle_status=record.lifecycle_status,
            standard_code=record.standard_code,
            title=record.title,
            source_url=record.source_url,
            warning=warning,
        )

    @staticmethod
    def _validate_official_url(source_url: str | None) -> None:
        parsed = urlparse(source_url or "")
        hostname = (parsed.hostname or "").lower()
        recognized = any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in OFFICIAL_DOMAINS
        )
        if parsed.scheme != "https" or not recognized:
            raise StandardMetadataValidationError(
                "官方来源域名必须是已认可政府平台的 HTTPS 地址"
            )
