"""官方题录核验和同步编排。"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from archspec.standard_catalog.models import (
    CatalogSyncRun,
    CatalogSyncTrigger,
    CatalogVerificationStatus,
    StandardCatalogEntry,
)
from archspec.standard_catalog.mohurd import MohurdAnnouncementParser
from archspec.standard_catalog.provider import OfficialPageFetcher
from archspec.standard_catalog.repository import (
    CatalogSyncRepository,
    StandardCatalogRepository,
)


class CatalogVerificationError(RuntimeError):
    """外部官方来源本次无法完成核验。"""


class CatalogVerificationValidationError(ValueError):
    """目录项缺少可安全核验的信息。"""


class CatalogEntryNotFound(CatalogVerificationValidationError):
    pass


class CatalogSyncAlreadyRunning(RuntimeError):
    pass


class EntryVerifier(Protocol):
    def verify_entry(self, entry_id: str): ...


class CatalogVerificationService:
    def __init__(
        self,
        catalog: StandardCatalogRepository,
        fetcher: OfficialPageFetcher,
    ) -> None:
        self.catalog = catalog
        self.fetcher = fetcher
        self.mohurd_parser = MohurdAnnouncementParser()

    def verify_entry(self, entry_id: str) -> StandardCatalogEntry:
        entry = self.catalog.get(entry_id)
        if entry is None:
            raise CatalogEntryNotFound("规范目录项不存在")
        if not entry.official_url:
            raise CatalogVerificationValidationError(
                "当前目录项没有可核验的具体官方公告地址"
            )
        parsed_url = urlsplit(entry.official_url)
        if parsed_url.path in {"", "/", "/bzgk/gb/index"}:
            raise CatalogVerificationValidationError(
                "当前目录项没有可核验的具体官方公告地址"
            )
        try:
            html = self.fetcher.fetch(entry.official_url, timeout_seconds=6.0)
            candidate = self.mohurd_parser.parse(html, entry.official_url)
            if self._normalize_code(candidate.standard_code) != self._normalize_code(
                entry.standard_code
            ):
                raise CatalogVerificationValidationError(
                    "官方公告中的规范编号与目录项不一致"
                )
            checked_at = datetime.now(UTC)
            self.catalog.upsert(
                replace(
                    entry,
                    title=candidate.title,
                    authority=candidate.authority,
                    lifecycle_status=candidate.lifecycle_status,
                    published_at=candidate.published_at,
                    effective_at=candidate.effective_at,
                    official_url=candidate.official_url,
                    last_checked_at=checked_at,
                )
            )
            return self.catalog.update_verification(
                entry.id,
                verification_status=CatalogVerificationStatus.VERIFIED,
                verified_at=checked_at,
                verification_message="已从住建部官方公告核验",
                announcement_number=candidate.announcement_number,
                source_updated_at=candidate.source_updated_at,
                replaced_by_code=candidate.replaced_by_code,
                content_permission=candidate.content_permission,
            )
        except CatalogVerificationValidationError:
            raise
        except Exception as error:
            self.catalog.update_verification(
                entry.id,
                verification_status=CatalogVerificationStatus.FAILED,
                verified_at=entry.verified_at,
                verification_message=f"本次核验失败：{error}",
                announcement_number=entry.announcement_number,
                source_updated_at=entry.source_updated_at,
                replaced_by_code=entry.replaced_by_code,
                content_permission=entry.content_permission,
            )
            raise CatalogVerificationError(f"官方题录核验失败：{error}") from error

    @staticmethod
    def _normalize_code(value: str) -> str:
        return " ".join(value.strip().upper().split())


class CatalogSyncService:
    """小批量顺序核验已有目录，不负责自动发现全部新规范。"""

    def __init__(
        self,
        catalog: StandardCatalogRepository,
        syncs: CatalogSyncRepository,
        verifier: EntryVerifier,
    ) -> None:
        self.catalog = catalog
        self.syncs = syncs
        self.verifier = verifier

    def run_existing_entries(
        self, provider_name: str, limit: int = 20
    ) -> CatalogSyncRun:
        provider = provider_name.strip().upper()
        if self.syncs.has_running(provider):
            raise CatalogSyncAlreadyRunning("该官方来源已有同步任务正在运行")
        bounded_limit = max(1, min(limit, 20))
        entries = [
            entry
            for entry in self.catalog.list()
            if entry.source_provider.upper() == provider
            and self._has_specific_official_page(entry)
        ][:bounded_limit]
        run = self.syncs.create(provider, len(entries), CatalogSyncTrigger.MANUAL)
        successes = 0
        errors: list[str] = []
        for entry in entries:
            try:
                self.verifier.verify_entry(entry.id)
                successes += 1
            except (CatalogVerificationError, CatalogVerificationValidationError) as error:
                message = f"{entry.standard_code}：{error}"[:300]
                errors.append(message)
        return self.syncs.finish(
            run.id,
            success_count=successes,
            failure_count=len(errors),
            errors=errors,
        )

    @staticmethod
    def _has_specific_official_page(entry: StandardCatalogEntry) -> bool:
        if not entry.official_url:
            return False
        path = urlsplit(entry.official_url).path.rstrip("/")
        return bool(path and path not in {"/bzgk/gb/index", "/bzgk/gb"})
