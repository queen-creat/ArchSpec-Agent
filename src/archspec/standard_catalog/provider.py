"""规范目录来源的统一接口，后续可替换为获授权的官方数据源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from archspec.standard_catalog.models import CatalogContentPermission, CatalogSearchResult
from archspec.standards.models import StandardLifecycleStatus

if TYPE_CHECKING:
    from archspec.standard_catalog.service import StandardCatalogService


class StandardCatalogProvider(Protocol):
    """任何目录来源只要实现这个接口，就能被检索服务调用。"""

    def search(self, query: str, limit: int) -> list[CatalogSearchResult]: ...


class LocalCatalogProvider:
    """当前使用本地已核验目录；不会擅自抓取或转载规范正文。"""

    def __init__(self, service: StandardCatalogService) -> None:
        self.service = service

    def search(self, query: str, limit: int) -> list[CatalogSearchResult]:
        return self.service.search(query, limit)


class OfficialSourceRejected(ValueError):
    """网址不是允许的官方 HTTPS 来源。"""


class OfficialPageFormatError(ValueError):
    """官方页面存在，但必要题录字段无法可靠读取。"""


@dataclass(frozen=True)
class VerificationTarget:
    standard_code: str
    official_url: str


@dataclass(frozen=True)
class OfficialCatalogCandidate:
    standard_code: str
    title: str
    authority: str
    lifecycle_status: StandardLifecycleStatus
    published_at: date | None
    effective_at: date | None
    official_url: str
    announcement_number: str | None
    source_updated_at: date | None
    replaced_by_code: str | None
    content_permission: CatalogContentPermission


@dataclass(frozen=True)
class ProviderHealth:
    available: bool
    checked_at: datetime
    message: str


class OfficialCatalogProvider(Protocol):
    name: str

    def verify(
        self, target: VerificationTarget
    ) -> OfficialCatalogCandidate | None: ...

    def health(self) -> ProviderHealth: ...


class OfficialPageFetcher(Protocol):
    def fetch(self, url: str, timeout_seconds: float) -> str: ...


def validate_official_url(url: str, allowed_hosts: set[str]) -> None:
    """只接受名单内的官方 HTTPS 地址，拒绝相似域名和内嵌账号。"""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise OfficialSourceRejected("官方来源网址格式无效") from error
    hostname = (parsed.hostname or "").lower().rstrip(".")
    normalized_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
    if parsed.scheme.lower() != "https":
        raise OfficialSourceRejected("官方来源必须使用 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise OfficialSourceRejected("官方来源网址不能包含账号信息")
    if port not in (None, 443):
        raise OfficialSourceRejected("官方来源网址不能使用非标准端口")
    if hostname not in normalized_hosts:
        raise OfficialSourceRejected(f"不受信任的官方来源域名：{hostname or '空'}")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_official_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibOfficialPageFetcher:
    """读取有限大小的官方题录页面，不下载附件或规范正文。"""

    def __init__(self, allowed_hosts: set[str], max_bytes: int = 2 * 1024 * 1024) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes

    def fetch(self, url: str, timeout_seconds: float = 6.0) -> str:
        validate_official_url(url, self.allowed_hosts)
        request = Request(
            url,
            headers={
                "User-Agent": "ArchSpec-Agent/0.1 official-metadata-verifier",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        opener = build_opener(_SafeRedirectHandler(self.allowed_hosts))
        with opener.open(request, timeout=timeout_seconds) as response:
            validate_official_url(response.geturl(), self.allowed_hosts)
            raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise OfficialPageFormatError("官方题录页面超过允许大小")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset)
            except (LookupError, UnicodeDecodeError) as error:
                raise OfficialPageFormatError("官方题录页面编码无法识别") from error

