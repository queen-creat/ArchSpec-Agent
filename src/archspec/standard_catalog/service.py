"""中央规范目录检索服务。"""

import re
from datetime import UTC, datetime

from archspec.standard_catalog.models import (
    CatalogSearchResult,
    CatalogStatus,
    CatalogVerificationStatus,
    StandardCatalogEntry,
)
from archspec.standard_catalog.repository import StandardCatalogRepository
from archspec.standards.models import StandardLifecycleStatus

_IGNORED_QUERY_WORDS = ("设计", "建筑", "要求", "规定", "怎么", "如何")
_OFFICIAL_SEARCH_URL = "https://openstd.samr.gov.cn/bzgk/gb/index"
_STARTER_CATALOG = (
    ("GB 55019-2021", "建筑与市政工程无障碍通用规范"),
    ("GB 55037-2022", "建筑防火通用规范"),
    ("GB 55031-2022", "民用建筑通用规范"),
    ("GB 55016-2021", "建筑环境通用规范"),
    ("GB 55015-2021", "建筑节能与可再生能源利用通用规范"),
    ("GB 55020-2021", "建筑给水排水与节水通用规范"),
    ("GB 55024-2022", "建筑电气与智能化通用规范"),
)
_QUERY_EXPANSIONS = {
    "建筑防火": ("消防", "防火", "疏散", "安全出口", "耐火", "避难"),
    "无障碍": ("无障碍", "坡道", "轮椅", "盲道", "扶手"),
    "民用建筑": ("办公", "住宅", "商业", "旅馆", "层高", "卫生间"),
    "建筑环境": ("采光", "通风", "隔声", "噪声", "热工", "日照"),
    "建筑节能": ("节能", "保温", "可再生能源", "能耗"),
    "给水排水节水": ("给水", "排水", "节水", "雨水", "污水"),
    "建筑电气智能化": ("电气", "照明", "供电", "智能化", "弱电"),
}
_VERIFICATION_PRIORITY = {
    CatalogVerificationStatus.VERIFIED: 0,
    CatalogVerificationStatus.PENDING: 1,
    CatalogVerificationStatus.FAILED: 2,
}
_LIFECYCLE_PRIORITY = {
    StandardLifecycleStatus.CURRENT: 0,
    StandardLifecycleStatus.UNKNOWN: 1,
    StandardLifecycleStatus.UPCOMING: 2,
    StandardLifecycleStatus.SUPERSEDED: 3,
    StandardLifecycleStatus.WITHDRAWN: 4,
}


def _terms(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    for word in _IGNORED_QUERY_WORDS:
        compact = compact.replace(word, "")
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", compact)
    terms: set[str] = set()
    for run in chinese_runs:
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
        terms.update(run)
    terms.update(re.findall(r"[a-z]+|\d+(?:-\d+)?", compact))
    return {term for term in terms if term}


def _score(query: str, entry: StandardCatalogEntry) -> float:
    expanded = query
    for category, triggers in _QUERY_EXPANSIONS.items():
        if any(trigger in query for trigger in triggers):
            expanded += f" {category}"
    query_terms = _terms(expanded)
    if not query_terms:
        return 0.0
    target_terms = _terms(f"{entry.standard_code} {entry.title}")
    overlap = query_terms & target_terms
    return len(overlap) / len(query_terms)


class StandardCatalogService:
    def __init__(self, repository: StandardCatalogRepository) -> None:
        self.repository = repository

    def search(self, query: str, limit: int = 5) -> list[CatalogSearchResult]:
        if not query.strip() or limit < 1:
            return []
        scored = [(_score(query, entry), entry) for entry in self.repository.list()]
        results = [item for item in scored if item[0] > 0]
        results.sort(
            key=lambda item: (
                -item[0],
                _VERIFICATION_PRIORITY[item[1].verification_status],
                _LIFECYCLE_PRIORITY[item[1].lifecycle_status],
                item[1].linked_document_id is None,
                item[1].standard_code,
            )
        )
        output: list[CatalogSearchResult] = []
        seen_codes: set[str] = set()
        for score, entry in results:
            if entry.standard_code in seen_codes:
                continue
            seen_codes.add(entry.standard_code)
            output.append(CatalogSearchResult(
                id=entry.id,
                standard_code=entry.standard_code,
                title=entry.title,
                authority=entry.authority,
                lifecycle_status=entry.lifecycle_status,
                official_url=entry.official_url,
                source_provider=entry.source_provider,
                full_text_available=entry.linked_document_id is not None,
                score=score,
                verification_status=entry.verification_status,
                verified_at=entry.verified_at,
                content_permission=entry.content_permission,
            ))
            if len(output) == limit:
                break
        return output

    def ensure_starter_catalog(self) -> None:
        """加入少量常用规范目录卡；状态保持未知，必须再去官方平台核验。"""

        existing = {
            (entry.source_provider, entry.standard_code)
            for entry in self.repository.list()
        }
        now = datetime.now(UTC)
        for code, title in _STARTER_CATALOG:
            if ("STARTER_CATALOG", code) in existing:
                continue
            self.repository.upsert(
                StandardCatalogEntry(
                    id="",
                    standard_code=code,
                    title=title,
                    authority="住房和城乡建设部",
                    lifecycle_status=StandardLifecycleStatus.UNKNOWN,
                    published_at=None,
                    effective_at=None,
                    official_url=_OFFICIAL_SEARCH_URL,
                    source_provider="STARTER_CATALOG",
                    last_checked_at=now,
                    linked_document_id=None,
                )
            )

    def status(self) -> CatalogStatus:
        entries = self.repository.list()
        checked = [entry.last_checked_at for entry in entries]
        return CatalogStatus(
            total=len(entries),
            with_full_text=sum(
                entry.linked_document_id is not None for entry in entries
            ),
            last_sync_at=max(checked) if checked else None,
            verified=sum(
                entry.verification_status == CatalogVerificationStatus.VERIFIED
                for entry in entries
            ),
            pending_or_failed=sum(
                entry.verification_status != CatalogVerificationStatus.VERIFIED
                for entry in entries
            ),
        )

