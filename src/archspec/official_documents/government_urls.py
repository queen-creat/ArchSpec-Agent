"""政府网页与附件共用URL边界；网址通过不代表正文、效力或模型使用许可。"""
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

# 取件节点与主站使用同一组传输字节上限，不依赖网页框架。
OFFICIAL_TRANSPORT_LIMITS = frozenset({2 * 1024 * 1024, 30 * 1024 * 1024, 80 * 1024 * 1024})

GATEWAY_PATH = "/api-gateway/jpaas-web-server/front/document/download"

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def validate_government_url(url):
    """政府HTTPS只证明网址边界，不证明规范正文、版本或适用性。"""
    if (not isinstance(url, str) or not url or len(url) > 4096
            or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url)):
        raise ValueError("政府搜索链接格式无效")
    try:
        parsed = urlsplit(url)
        hostname, port = parsed.hostname or "", parsed.port
    except ValueError:
        raise ValueError("政府搜索链接格式无效") from None
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment or len(hostname) > 253
            or not hostname.endswith(".gov.cn")
            or any(not _HOST_LABEL.fullmatch(label) for label in hostname.split("."))):
        raise ValueError("政府搜索链接不属于允许的HTTPS官方域名")
    return url


def validate_government_pair(page_url, pdf_url):
    """通用正文候选只接同一政府站点的HTTPS PDF；跨站仍须原有精确审核配对。"""
    validate_government_url(page_url)
    validate_government_url(pdf_url)
    if urlsplit(page_url).hostname != urlsplit(pdf_url).hostname:
        raise ValueError("政府附件未与同站发布页对应")
    if not urlsplit(pdf_url).path.lower().endswith(".pdf"):
        validate_gateway_download(pdf_url, {urlsplit(page_url).hostname})


def validate_gateway_download(url, allowed_hosts):
    """公告、下载器与独立计算共用同一官方下载入口合同，正文仍核PDF字节。"""
    parsed = urlsplit(url)
    if parsed.path != GATEWAY_PATH:
        raise ValueError("附件不是已支持的官方 PDF 下载入口")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if len(query.get("fileUrl", [])) != 1 or not query["fileUrl"][0]:
        raise ValueError("官方下载入口缺少唯一文件标识")
    nested = unquote(query["fileUrl"][0]).strip()
    # 复用不透明文件标识；显式网址仍须属于同一组已核官网，不能借入口跨站。
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", nested) or nested.startswith("//"):
        target = urljoin(url, nested)
        validate_government_url(target)
        if urlsplit(target).hostname not in allowed_hosts:
            raise ValueError("官方下载入口指向未核对的站点")
    names = query.get("fileName", [])
    # 无后缀题名沿用既有规则；明确非PDF附件留在线索区，不进入正文获取。
    extension = re.search(r"\.[a-zA-Z0-9]{1,10}$", unquote(names[0])) if names else None
    if len(names) > 1 or (extension and extension.group().lower() != ".pdf"):
        raise ValueError("官方下载入口不是 PDF 文件")
