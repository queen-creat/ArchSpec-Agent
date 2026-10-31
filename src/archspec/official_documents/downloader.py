"""只下载已登记的官方附件；每一次跳转都重新检查，绝不关闭 TLS 验证。"""

import hashlib
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from archspec.official_documents.download_errors import AttachmentInputRejected
from archspec.official_documents.government_urls import (
    GATEWAY_PATH,
    validate_gateway_download,
    validate_government_pair,
)
from archspec.official_documents.local_policy import ATTACHMENTS, reviewed_attachment
from archspec.official_http import official_http_client
from archspec.standard_catalog.provider import validate_official_url

MOHURD_HOSTS = {"www.mohurd.gov.cn", "mohurd.gov.cn"}
LEGACY_PDF = "https://zjj.sm.gov.cn/xxgk/fgwj/jsbz/202209/P020220909629255603704.pdf"
MAX_BYTES = 30 * 1024 * 1024


def validate_attachment_url(url: str, *, allow_legacy: bool = False, page_url: str | None = None) -> None:
    """附件只能是官网 PDF 或公告中使用的固定下载入口。"""
    if allow_legacy and url == LEGACY_PDF:
        return
    if url in ATTACHMENTS:
        return
    if len(url) > 8192 or re.search(r"[\x00-\x20\\]", url):
        raise ValueError("官方附件网址包含无效字符或过长")
    parsed = urlsplit(url)
    if parsed.hostname not in MOHURD_HOSTS and page_url is not None:
        # 新副本必须由已读取的发布页带入，单独给一个政府PDF地址不能启动请求。
        validate_government_pair(page_url, url)
        return
    validate_official_url(url, MOHURD_HOSTS)
    if parsed.fragment:
        raise ValueError("官方附件网址不能包含片段")
    if parsed.path.lower().endswith(".pdf"):
        return
    validate_gateway_download(url, MOHURD_HOSTS)


def _validate_redirect_url(url: str, allowed_hosts: set[str], *, page_url=None) -> None:
    if len(url) > 8192 or re.search(r"[\x00-\x20\\]", url):
        raise ValueError("官方附件跳转网址无效")
    validate_official_url(url, allowed_hosts)
    # 下载入口如再次出现，仍需检查其中嵌套的目标；其他官方路径最终用 PDF 签名确认。
    if urlsplit(url).path == GATEWAY_PATH:
        validate_attachment_url(url, page_url=page_url)


@contextmanager
def _pdf_response(url, client, max_bytes, *, page_url=None):
    validate_attachment_url(url, allow_legacy=True, page_url=page_url)
    # 动态政府副本仍限于初始附件同站，登记的跨站配对沿用旧规则。
    allowed_hosts = MOHURD_HOSTS if urlsplit(url).hostname in MOHURD_HOSTS else {urlsplit(url).hostname}
    local = reviewed_attachment(url)
    if local is not None:
        allowed_hosts = {urlsplit(local.pdf_url).hostname}
    owned_client = client is None
    client = client or official_http_client(timeout=30, follow_redirects=False)
    try:
        for redirect_count in range(4):
            _validate_redirect_url(url, allowed_hosts, page_url=page_url)
            from archspec.official_documents.official_fetch import open_public_response
            with open_public_response(client, url, max_bytes, timeout=30, follow_redirects=False,
                               headers={"User-Agent": "ArchSpec-Agent/0.1 official-pdf"}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if redirect_count == 3:
                        raise ValueError("官方附件跳转超过三次，已停止下载")
                    target = response.headers.get("location")
                    if not target:
                        raise ValueError("官方附件跳转缺少目标网址")
                    destination = urljoin(url, target)
                    if local is not None and destination not in local.redirect_targets:
                        raise ValueError("地方附件跳转未在精确审核配对中登记")
                    url = destination
                    _validate_redirect_url(url, allowed_hosts, page_url=page_url)
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise AttachmentInputRejected("SIZE_LIMIT", max_bytes=max_bytes)
                yield response
                return
    finally:
        if owned_client:
            client.close()
    raise ValueError("官方附件下载未完成")


def _pdf_parts(response, max_bytes):
    received = 0
    first = True
    for part in response.iter_bytes(chunk_size=64 * 1024):
        if first:
            if not part.startswith(b"%PDF-"):
                raise AttachmentInputRejected("INVALID_PDF")
            first = False
        received += len(part)
        if received > max_bytes:
            raise AttachmentInputRejected("SIZE_LIMIT", max_bytes=max_bytes)
        yield part
    if first:
        raise AttachmentInputRejected("INVALID_PDF")


def download_pdf(url: str, client: httpx.Client | None = None) -> bytes:
    """保留旧的小文件字节接口；真实正文获取改用下方文件接口。"""
    content = bytearray()
    with _pdf_response(url, client, MAX_BYTES) as response:
        for part in _pdf_parts(response, MAX_BYTES):
            content.extend(part)
    return bytes(content)


@dataclass(frozen=True)
class DownloadedPDF:
    path: Path
    size_bytes: int
    sha256: str


def download_pdf_file(url: str, directory: Path, max_bytes: int = MAX_BYTES,
                      client: httpx.Client | None = None, *, page_url: str | None = None) -> DownloadedPDF:
    """分段落盘和计算指纹，不把整本文件反复复制进内存。"""
    if max_bytes not in (MAX_BYTES, 80 * 1024 * 1024):
        raise ValueError("未支持的服务端附件预算")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with (
            _pdf_response(url, client, max_bytes, page_url=page_url) as response,
            tempfile.NamedTemporaryFile(mode="wb", prefix=".acquire-", suffix=".part",
                                        dir=directory, delete=False) as output,
        ):
            temporary = Path(output.name)
            digest = hashlib.sha256()
            size_bytes = 0
            for part in _pdf_parts(response, max_bytes):
                output.write(part)
                digest.update(part)
                size_bytes += len(part)
        return DownloadedPDF(temporary, size_bytes, digest.hexdigest())
    except BaseException:
        # 只移除本次自己创建的精确临时文件，不动旧正文或其他文件。
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
