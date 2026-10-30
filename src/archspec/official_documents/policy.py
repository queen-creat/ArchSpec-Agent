"""正文资源预算由服务端决定，浏览器不能提交更大的大小或页数限制。"""

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass

from archspec.official_documents.downloader import MOHURD_HOSTS, validate_attachment_url
from archspec.standard_catalog.provider import validate_official_url


@dataclass(frozen=True)
class AcquisitionPolicy:
    policy_id: str
    max_bytes: int
    max_pages: int


DEFAULT_POLICY = AcquisitionPolicy("OFFICIAL_DEFAULT_V1", 30 * 1024 * 1024, 100)
LARGE_POLICY = AcquisitionPolicy("REGISTERED_LARGE_FULL_TEXT_V1", 80 * 1024 * 1024, 200)
# 已实际核过发布页与完整附件的政府原文沿用200页上界，仍保持普通30MiB字节上界。
GOVERNMENT_FULL_TEXT_POLICY = AcquisitionPolicy("VERIFIED_GOVERNMENT_FULL_TEXT_V1", 30 * 1024 * 1024, 200)
# 只由本机启动入口选择；不增加云端默认额度，也不允许请求自带上限。
LOCAL_POLICY = AcquisitionPolicy("LOCAL_REGISTERED_PDF_V1", 80 * 1024 * 1024, 500)
FIXED_POLICIES = (DEFAULT_POLICY, GOVERNMENT_FULL_TEXT_POLICY, LARGE_POLICY, LOCAL_POLICY)
LARGE_STANDARDS = {
    "GB55037-2022": "建筑防火通用规范",
    "GB50067-2014": "汽车库、修车库、停车场设计防火规范",
    "JGJ100-2015": "车库建筑设计规范",
}


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _verified_government_full_text(source):
    """只有既有登记链核过的完整主文件可选200页，不按城市、题名或URL加白名单。"""
    from archspec.official_documents.government_urls import validate_government_pair

    if (source.get("kind") != "FULL_TEXT" or source.get("source_format") == "HTML"
            or (source.get("source_metadata") or {}).get("provenance") != "OFFICIAL_COPY"
            or not isinstance(source.get("title"), str) or not source["title"].strip()
            or not isinstance(source.get("standard_code"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source.get("announcement_hash") or "")):
        return False
    identity = f"{source['standard_code']}\n{source.get('pdf_url')}\nFULL_TEXT"
    expected = "official-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    if source.get("source_id") != expected:
        return False
    try:
        validate_government_pair(source.get("page_url", ""), source.get("pdf_url", ""))
    except ValueError:
        return False
    return True


def _local_registered_pdf(source):
    """大预算只绑定服务端已保存的公告/正文或修订附件，不按题名或编号白名单放行。"""
    if (os.environ.get("ARCHSPEC_OFFICIAL_RESOURCE_PROFILE") != "LOCAL"
            or source.get("kind") not in {"FULL_TEXT", "REVISION"} or source.get("source_format") == "HTML"
            or not isinstance(source.get("title"), str) or not source["title"].strip()
            or not isinstance(source.get("standard_code"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", source.get("announcement_hash") or "")):
        return False
    identity = f"{source['standard_code']}\n{source.get('pdf_url')}\n{source['kind']}"
    if source.get("source_id") != "official-" + hashlib.sha256(identity.encode()).hexdigest()[:32]:
        return False
    from archspec.official_documents.registry import validate_source_pair

    role = "OFFICIAL_COPY" if (source.get("source_metadata") or {}).get("provenance") == "OFFICIAL_COPY" else None
    try:
        validate_source_pair(source.get("page_url", ""), source.get("pdf_url", ""), publication_kind=role)
    except ValueError:
        return False
    return True


def policy_for_source(source: dict) -> AcquisitionPolicy:
    """按运行入口和已登记来源选择固定额度，资料角色和正文效力不随额度改变。"""
    from archspec.official_documents.local_policy import reviewed_publication

    if _local_registered_pdf(source):
        return LOCAL_POLICY

    # 复用现有80MiB/200页策略；仅这一已审完整地方原件，普通来源仍为30MiB/100页。
    notice = "https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202408/t20240821_5311048.shtml"
    local = reviewed_publication(source.get("page_url")) if source.get("page_url") == notice else None
    if (local is not None and local.kind == source.get("kind") == "FULL_TEXT"
            and source.get("title") == local.title and source.get("standard_code") == local.code
            and source.get("pdf_url") == local.pdf_url):
        identity = f"{local.code}\n{local.pdf_url}\nFULL_TEXT"
        expected_id = "official-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if source.get("source_id") == expected_id:
            return LARGE_POLICY
    fallback = GOVERNMENT_FULL_TEXT_POLICY if _verified_government_full_text(source) else DEFAULT_POLICY
    expected_title = LARGE_STANDARDS.get(_normalized(source.get("standard_code", "")))
    if (source.get("kind") != "FULL_TEXT" or expected_title is None or
            _normalized(source.get("title", "")) != _normalized(expected_title) or
            not re.fullmatch(r"official-[a-f0-9]{32}", source.get("source_id", ""))):
        return fallback
    try:
        validate_official_url(source.get("page_url", ""), MOHURD_HOSTS)
        validate_attachment_url(source.get("pdf_url", ""))
    except ValueError:
        return fallback
    return LARGE_POLICY
