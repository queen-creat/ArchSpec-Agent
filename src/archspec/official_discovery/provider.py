"""只查住建部公开入口；公告事实不等于最新现行性证明。"""

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from archspec.official_discovery.public_queries import CODE_YEAR_SEPARATOR, normalize_standard_code
from archspec.official_http import official_http_client
from archspec.standard_catalog.provider import validate_official_url

HOSTS = {"www.mohurd.gov.cn", "mohurd.gov.cn"}
ROOT = "https://www.mohurd.gov.cn"
SEARCH_ROOT = ROOT + "/api-gateway/jpaas-jsearch-web-server/"
SERVICE_ID = "e2f3058e2a3b4f8abc93eb76e739e3e7"
CATEGORY_ID = "cc5c27d3eb894fd79cc47e72497ef2bf"  # 官网“信息公开”栏目
CODE_PATTERN = (r"(?<![A-Za-z0-9])(?:GB|JGJ|CJJ)(?:/T)?\s*\d+(?:\.\d+)?\s*"
                + CODE_YEAR_SEPARATOR + r"\s*\d{4}(?![A-Za-z0-9.]|" + CODE_YEAR_SEPARATOR + r")")
MAX_BYTES = 2 * 1024 * 1024
MAX_CHANGE_EVIDENCE_CHARS = 32000
# 官网既有“现批准《…》”和“现批准国家/行业标准《…》”两种主批准句。
# 仅接受明确类别，不从新闻介绍、拟批准或地方标准猜国家公告身份。
APPROVAL = re.compile(r"现(?:批准|发布)\s*(?:(?:国家|行业)标准\s*)?《([^》]+)》([^。]{0,200})")


class PageParser(HTMLParser):
    """把远程 HTML 当资料，不执行脚本；保留原文、题名和链接。"""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.links = []
        self.meta = {}
        self.hidden = 0
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "meta":
            self.meta[values.get("name", "").lower()] = values.get("content", "")
        if tag == "a":
            self.anchor = {"attrs": values, "parts": []}

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        if tag == "a" and self.anchor:
            self.links.append({**self.anchor["attrs"], "text": "".join(self.anchor["parts"]).strip()})
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())
            if self.anchor:
                self.anchor["parts"].append(data)


def official_link(url: str, base: str = ROOT) -> str | None:
    candidate = urljoin(base, url)
    try:
        validate_official_url(candidate, HOSTS)
    except ValueError:
        return None
    return candidate


def _date_field(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    if not match:
        return None
    groups = match.groups()
    value = groups[0] if len(groups) == 1 else "-".join(f"{int(x):02d}" for x in groups)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("公告日期格式需要人工核对") from error


def _canonical_code(value: str) -> str:
    code = normalize_standard_code(value)
    if code is None:
        raise ValueError("公告规范编号无法完整核对")
    return code


def explicit_identity_change(text: str, from_title: str, from_code: str) -> dict | None:
    """只读紧接主批准句的明确名称/编号变更，不从其它条款或标题猜继承关系。"""
    match = re.match(
        r"标准名称修改为《([^》]{2,100})》\s*[,，]\s*标准编号修改为\s*("
        + CODE_PATTERN + r")\s*[。；;]", text)
    if match is None:
        return None
    return {"from_title": from_title, "from_code": from_code,
            "to_title": match.group(1), "to_code": _canonical_code(match.group(2)),
            "evidence": match.group()}


def renamed_attachment_title(notice: dict) -> str | None:
    """登记时重核同一份更名原字，单独塞入别名或改写目标字段不能授权附件。"""
    change = notice.get("identity_change")
    if (notice.get("notice_type") != "REVISION" or not isinstance(change, dict)
            or not isinstance(change.get("evidence"), str)
            or not isinstance(notice.get("evidence_excerpt"), str)
            or change["evidence"] not in notice["evidence_excerpt"]):
        return None
    subject = APPROVAL.search(notice["evidence_excerpt"])
    code = re.search(CODE_PATTERN, subject.group(2)) if subject else None
    if (subject is None or code is None or subject.group(1) != notice.get("standard_title")
            or _canonical_code(code.group()) != notice.get("standard_code")):
        return None
    tail = notice["evidence_excerpt"][subject.end():]
    checked = (explicit_identity_change(tail[1:].lstrip(), subject.group(1), _canonical_code(code.group()))
               if tail.startswith("。") else None)
    return checked["to_title"] if checked is not None and change == checked else None


def parse_announcement(html: str, url: str) -> dict:
    validate_official_url(url, HOSTS)
    parser = PageParser()
    parser.feed(html)
    text = unicodedata.normalize("NFKC", " ".join(parser.parts))
    text = re.sub(r"\s+", " ", text)
    # 只读批准句中主标准，不把随后废止清单里的旧编号当新编号。
    subject = APPROVAL.search(text)
    code = re.search(CODE_PATTERN, subject.group(2)) if subject else None
    if not subject or not code:
        raise ValueError("公告主标准编号无法可靠识别；请查看原公告")
    standard_code = _canonical_code(code.group())
    label_text = re.sub(r"发\s*文\s*日\s*期", "发文日期", text)
    published = _date_field(r"发文日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})", label_text)
    effective = _date_field(r"实施日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    if effective is None:
        effective = _date_field(r"自\s*(\d{4})年(\d{1,2})月(\d{1,2})日起实施", text)
    announcement = re.search(r"(?:中华人民共和国住房和城乡建设部)?公告\s*\d{4}年第\d+号", text)
    partial = re.search(r"废止[^。:：]{0,70}(?:相关强制性条文|强制性条文|部分条文)", text)
    withdrawn = list(re.finditer(r"《[^》]+》\s*\(?\s*(" + CODE_PATTERN + r")\s*\)?\s*(?:同时)?废止", text))
    change_scope = "PARTIAL_CLAUSES" if partial else "FULL_STANDARD" if withdrawn else "NOT_ESTABLISHED"
    change_start = partial.start() if partial else withdrawn[0].start() if withdrawn else None
    # 调整清单常跨多本规范，不能只存开头1200字并让后段范围无声丢失。
    # 保存有界后续原字；超过保全上限时显式标记摘录不全，绝不推定整本废止。
    change_text = text[change_start:] if change_start is not None else ""
    change_truncated = len(change_text) > MAX_CHANGE_EVIDENCE_CHARS
    evidence = change_text[:MAX_CHANGE_EVIDENCE_CHARS]
    if change_truncated:
        evidence += "\n[公告调整范围摘录未完整保留，请打开官方原公告核对后续清单。]"
    attachments = []
    for link in parser.links:
        safe = official_link(link.get("href", ""), url)
        if safe and (urlsplit(safe).path.lower().endswith(".pdf") or
                     urlsplit(safe).path == "/api-gateway/jpaas-web-server/front/document/download"):
            item = {"title": link.get("title") or link["text"] or "官方 PDF 附件", "url": safe}
            if item not in attachments:
                attachments.append(item)
    title = parser.meta.get("articletitle") or f"关于《{subject.group(1)}》的公告"
    excerpt = text[subject.start():subject.start() + 1600]
    # 文号通常在批准句之前；保全已解析字段的原字，供固定档案独立核对。
    # 仍保留正文摘录；两段以省略标记分开，不拼造连续原文或认证现行性。
    if announcement and announcement.group() not in excerpt:
        heading = text[max(0, announcement.start() - 80):announcement.end() + 80]
        excerpt = heading + "\n[…]\n" + excerpt
    # 只认批准句后的下一句；后续别的规范更名不能绑定当前附件。
    tail = text[subject.end():]
    identity_change = (explicit_identity_change(tail[1:].lstrip(), subject.group(1), standard_code)
                       if tail.startswith("。") else None)
    return {
        **({"identity_change": identity_change} if identity_change is not None else {}),
        "title": title, "url": url, "standard_title": subject.group(1),
        "standard_code": standard_code, "authority": "住房和城乡建设部",
        "scope": "NATIONAL" if standard_code.startswith("GB") else "INDUSTRY",
        "notice_type": "REVISION" if "修订" in title or "修订" in subject.group(2) else "PUBLICATION",
        "announcement_number": announcement.group() if announcement else None,
        "published_at": published, "effective_at": effective,
        "effective_state": "NOT_YET_EFFECTIVE" if effective and date.fromisoformat(effective) > datetime.now(UTC).date()
        else "DATE_PASSED" if effective else "UNKNOWN",
        "lifecycle_status": "UNKNOWN", "status": "PENDING_REVIEW",
        "version_check": "ANNOUNCEMENT_ONLY", "change_scope": change_scope,
        "change_evidence": evidence, "change_evidence_truncated": change_truncated, "withdrawn_codes": [_canonical_code(m.group(1)) for m in withdrawn],
        "evidence_excerpt": excerpt,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "attachments": attachments[:10], "full_text_available": False,
        "warnings": ["只核对公告发布事实，未确认截至今天仍现行或适用于当前项目",
                     "官网附件链接不等于正文已获取、使用许可已核验"],
    }


class MohurdDiscoveryProvider:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def _read(self, url: str) -> bytes:
        validate_official_url(url, HOSTS)
        # 禁止重定向，以免官方链接跳到未审核的数据源或内网。
        client = self.client or official_http_client()
        try:
            with client.stream("GET", url, timeout=8, follow_redirects=False,
                               headers={"User-Agent": "ArchSpec-Agent/0.1 official-discovery"}) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError("官方响应过大，已停止读取")
                return bytes(content)
        finally:
            if self.client is None:
                client.close()

    def search(self, term: str) -> dict:
        import json

        params = {"serviceId": SERVICE_ID, "cateid": CATEGORY_ID, "websiteid": "",
                  "q": term, "p": 1, "pg": 20}
        url = SEARCH_ROOT + "interface/search/info?" + urlencode(params)
        payload = json.loads(self._read(url))
        if not isinstance(payload, dict):
            raise ValueError("官方搜索根数据格式变化")  # noqa: TRY004 -- 远程文档格式错误
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("官方搜索数据格式变化")  # noqa: TRY004 -- 远程文档格式错误
        result = data.get("searchResult")
        if not isinstance(result, dict):
            raise ValueError("官方搜索结果格式变化")  # noqa: TRY004 -- 远程文档格式错误
        if payload.get("success") is not True or not isinstance(result.get("result"), list):
            raise ValueError("官方搜索返回格式变化或暂不可用，不当成零条结果")
        if not isinstance(result.get("total"), int):
            raise ValueError("官方搜索缺少总数，需重新核对接口")  # noqa: TRY004 -- 远程文档格式错误
        items = []
        for fragment in result["result"][:20]:
            if not isinstance(fragment, str):
                raise ValueError("官方搜索条目格式变化")  # noqa: TRY004 -- 远程文档格式错误
            parser = PageParser()
            parser.feed(fragment)
            for link in parser.links:
                if "textTitle" not in link.get("class", "").split():
                    continue
                title = link.get("data-title") or link["text"]
                safe = official_link(link.get("href", ""))
                if safe and "公告" in title and re.search(r"(?:国家|行业)标准", title):
                    items.append({"title": title, "url": safe})
        return {"total": result["total"], "items": items,
                "search_url": SEARCH_ROOT + "search?" + urlencode(params)}

    def inspect(self, item: dict) -> dict:
        url = item["url"]
        return parse_announcement(self._read(url).decode("utf-8"), url)
