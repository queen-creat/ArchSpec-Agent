"""读取住房和城乡建设部公告中的公开题录字段。"""

import re
from datetime import date
from html.parser import HTMLParser

from archspec.standard_catalog.models import CatalogContentPermission
from archspec.standard_catalog.provider import (
    OfficialCatalogCandidate,
    OfficialPageFormatError,
    validate_official_url,
)
from archspec.standards.models import StandardLifecycleStatus

_MOHURD_HOSTS = {"mohurd.gov.cn", "www.mohurd.gov.cn"}


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def _match(pattern: str, text: str, label: str) -> str:
    result = re.search(pattern, text)
    if result is None:
        raise OfficialPageFormatError(f"官方公告缺少{label}")
    return result.group(1).strip()


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise OfficialPageFormatError(f"官方公告中的{label}格式无法识别") from error


class MohurdAnnouncementParser:
    """只提取公告题录，不读取或保存附件正文。"""

    def parse(self, html: str, source_url: str) -> OfficialCatalogCandidate:
        validate_official_url(source_url, _MOHURD_HOSTS)
        parser = _VisibleTextParser()
        parser.feed(html[: 2 * 1024 * 1024])
        text = " ".join(parser.parts)
        text = re.sub(r"\s+", " ", text)

        standard_code = _match(
            r"(?:编号为|编号[：:]?)\s*((?:GB|JGJ|CJJ)(?:/T)?\s*\d+(?:\.\d+)?-\d{4})",
            text,
            "规范编号",
        )
        standard_code = " ".join(standard_code.upper().split())
        title = _match(r"《([^》]+)》", text, "规范名称")
        announcement_number = _match(
            r"文号[：:]\s*(.+?)(?=\s+发文日期[：:]|\s+实施日期[：:]|$)",
            text,
            "公告编号",
        )
        published_at = _parse_date(
            _match(r"发文日期[：:]\s*(\d{4}-\d{2}-\d{2})", text, "发文日期"),
            "发文日期",
        )
        effective_at = _parse_date(
            _match(r"实施日期[：:]\s*(\d{4}-\d{2}-\d{2})", text, "实施日期"),
            "实施日期",
        )
        return OfficialCatalogCandidate(
            standard_code=standard_code,
            title=title,
            authority="住房和城乡建设部",
            # 公告只说明一次发布行为；现行性需要独立、带截至日期的核验。
            lifecycle_status=StandardLifecycleStatus.UNKNOWN,
            published_at=published_at,
            effective_at=effective_at,
            official_url=source_url,
            announcement_number=announcement_number,
            source_updated_at=published_at,
            replaced_by_code=None,
            content_permission=CatalogContentPermission.METADATA_ONLY,
        )
