"""有限地方官网线索：共用精确审核配对，保留宜昌旧入口，观察不当问答正文。"""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from archspec.official_http import official_http_client

from archspec.official_discovery.provider import MAX_BYTES, PageParser
from archspec.official_documents.local_policy import (
    PUBLICATIONS,
    canonical_local_page,
    permitted_notice_redirect,
    reviewed_publication,
)
from archspec.official_documents.processing_cache import publish_json, stable_hash
from archspec.standard_catalog.provider import validate_official_url

ROOT = "https://zrzy.yichang.gov.cn/"
HOSTS = {"zrzy.yichang.gov.cn"}
TARGET = "宜昌市城乡规划管理技术规定"


def yichang_hints():
    """仅提供固定官方入口和可能题名，不把题名线索说成已核对版本。"""
    return [{"name": "宜昌市自然资源和城乡建设局官网", "url": ROOT,
             "target_title": TARGET, "region": "湖北·宜昌", "scope": "LOCAL_CLUE",
             "kind": "OFFICIAL_ENTRY", "body_status": "NOT_ACQUIRED",
             "message": "可在政策法规栏目查找正式发布文件；版本、文号及正文尚未取得。"}]


def article_url(href):
    candidate = urljoin(ROOT, href)
    known = canonical_local_page(candidate)
    if known and known != ROOT:
        return known
    try:
        validate_official_url(candidate, HOSTS)
        parts = urlsplit(candidate)
        if (parts.query or parts.fragment or "\\" in candidate
                or re.search(r"[\x00-\x20]", candidate)
                or not re.fullmatch(r"/content-\d+-\d+-1\.html", parts.path)):
            return None
    except ValueError:
        return None
    return candidate


class _LocalPageParser(PageParser):
    """保留共同HTML解析，并单独记录主标题，正文中的引用不能充当主文件。"""

    def __init__(self):
        super().__init__()
        self.subject_titles = []
        self._heading_parts = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if not self.hidden and tag == "h1":
            self._heading_parts = []

    def handle_data(self, data):
        super().handle_data(data)
        if not self.hidden and self._heading_parts is not None:
            self._heading_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self._heading_parts is not None:
            self.subject_titles.append("".join(self._heading_parts).strip())
            self._heading_parts = None
        super().handle_endtag(tag)


class YichangClueProvider:
    def __init__(self, cache_dir, *, client=None, registered_pages=None):
        # 服务端仅可选择已登记页面的子集；默认两篇均独立读取，不依赖首页。
        pages = tuple(PUBLICATIONS) if registered_pages is None else registered_pages
        if not isinstance(pages, (list, tuple)) or any(
                not isinstance(url, str) or url not in PUBLICATIONS for url in pages):
            raise ValueError("登记页面只能是现有PUBLICATIONS精确HTTPS网址的子集")
        self.registered_pages = tuple(dict.fromkeys(pages))
        self.cache_dir = cache_dir
        self.client = client

    def _read(self, url):
        # 所有URL由固定登记或已读首页链接产生，不接受浏览器网址。
        if url != ROOT and article_url(url) != url and reviewed_publication(url) is None:
            raise ValueError("地方页面不属于已审核的入口或文章路径")
        client = self.client or official_http_client()
        try:
            origin = url
            for attempt in range(2):
                with client.stream("GET", url, timeout=8, follow_redirects=False,
                                   headers={"User-Agent": "ArchSpec-Agent/0.1 local-clue"}) as response:
                    if 300 <= response.status_code < 400:
                        target = urljoin(url, response.headers.get("location", ""))
                        if attempt == 0 and permitted_notice_redirect(origin, target):
                            url = target
                            continue
                        raise ValueError(f"官方页面返回 {response.status_code} 跳转，本次未跟随")
                    response.raise_for_status()
                    content = bytearray()
                    for part in response.iter_bytes():
                        if len(content) + len(part) > MAX_BYTES:
                            raise ValueError("官方页面响应过大，未保存部分页面")
                        content.extend(part)
                    raw = bytes(content)
                    page = _LocalPageParser()
                    page.feed(raw.decode("utf-8"))
                    return raw, page, url
        finally:
            if self.client is None:
                client.close()

    def _save_observation(self, url, title, raw, page, now, observed_url):
        registered = reviewed_publication(url)
        value = {"schema_version": "LOCAL_CLUE_V1", "url": url, "title": title,
                 "site_name": registered.site_name if registered else "宜昌市自然资源和城乡建设局官网", "publisher": None,
                 "target_title": registered.title if registered else TARGET,
                 "region": registered.region if registered else "湖北·宜昌", "scope": "LOCAL_CLUE",
                 "fetched_at": now, "page_sha256": hashlib.sha256(raw).hexdigest(),
                 "body_status": "NOT_ACQUIRED", "attachment_status": "NOT_INSPECTED",
                 "edition": None, "effective_at": None, "announcement_number": None,
                 "lifecycle_status": "UNKNOWN", "kind": "OFFICIAL_PAGE_CLUE",
                 "observed_url": observed_url,
                 "transport_status": "HTTP_OBSERVED" if observed_url.startswith("http:") else "HTTPS_OBSERVED",
                 "visible_text": "\n".join(page.parts), "html": raw.decode("utf-8")}
        observation = stable_hash(value)
        relative = f"local-clues/observations/{observation}.json"
        record = {**value, "observation_id": observation}
        path = self.cache_dir / relative
        from archspec.persistence.business_files import assert_runtime_file, write_runtime_file
        # REMOTE 必须先核旧签收，再经同一私有业务账本完整回读签收；失败不能落成本地成功。
        assert_runtime_file(path, missing_ok=True)
        content = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if not write_runtime_file(path, content):
            publish_json(path, record)
        # 完整文字留在观察档案，不放进普通正文结果或简短回答。
        return {key: val for key, val in {**value, "observation_id": observation,
                "snapshot_path": relative}.items() if key not in {"visible_text", "html"}}

    @staticmethod
    def _publication(url, page, observation):
        """仅公告实际包含已登记题名和精确附件时，向原有正文队列交接。"""
        registered = reviewed_publication(url)
        if registered is None:
            return None
        text = "\n".join(page.parts)
        def compact(value):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
        # 官网的命名锚点/按钮可没有地址；保留所有原文，只筛选可核对的附件地址。
        links = [link for link in page.links
                 if isinstance(href := link.get("href"), str) and href.strip()
                 and urljoin(url, href) == registered.pdf_url]
        if compact(registered.title) not in compact(text) or not links:
            return None
        if registered.attachment_titles:
            # 优先公告的ArticleTitle，否则用可见主标题；不能从关联/引用段猜主文件。
            declared = page.meta.get("articletitle")
            titles = [declared] if declared else getattr(page, "subject_titles", ())
            expected_subject = compact(registered.notice_title)
            # 新增的题名角色是已审完整公告主体，不能仅在无关主标题中被引用；旧来源合同保持。
            exact_subject = "notice_title" in dict(registered.review_evidence)
            if not any((expected_subject == compact(title) if exact_subject else
                        expected_subject in compact(title)) for title in titles):
                return None
            # 只有已审阅的精确别名+真实主标题+精确href三者同时成立才交给正文队列。
            aliases = {compact(title) for title in registered.attachment_titles}
            links = [link for link in links if compact(link.get("text", "")) in aliases]
            if not links or registered.authority not in text:
                return None
        effective = re.search(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起实施", text)
        published = re.search(r"日期：\s*(\d{4}-\d{2}-\d{2})", text)
        return {
            **observation, "standard_title": registered.title,
            # 地方规定未见标准编号：这是本系统资料标签，不伪造GB编号或文号。
            "standard_code": registered.code, "code_type": "INTERNAL_LABEL",
            "scope": "LOCAL", "region": registered.region, "authority": registered.authority,
            "region_components": {"province": registered.province, "city": registered.city},
            "document_type": registered.document_type, "review_id": registered.review_id,
            "use_permission": registered.use_permission,
            "notice_type": "REVISION" if registered.kind == "REVISION" else "PUBLICATION",
            "version_check": "ANNOUNCEMENT_ONLY", "status": "PENDING_REVIEW",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "published_at": published[1] if published else None,
            "effective_at": "-".join([effective[1], effective[2].zfill(2), effective[3].zfill(2)]) if effective else None,
            "evidence_excerpt": text[:20_000], "change_scope": "UNKNOWN",
            "matched_queries": [registered.public_term], "full_text_available": True,
            "attachments": [{"title": links[0].get("text") or registered.title,
                             "url": registered.pdf_url}],
        }

    @staticmethod
    def _error(url, error):
        if isinstance(error, httpx.HTTPStatusError):
            message = f"宜昌官方页面返回 HTTP {error.response.status_code}，未取得页面或正文"
        elif isinstance(error, httpx.TimeoutException):
            message = "宜昌官方页面访问超时，未取得页面或正文"
        elif isinstance(error, ValueError):
            message = str(error)
        else:
            # 不回显代理配置、内部地址或完整异常串。
            message = "宜昌官方页面访问未完成，未取得页面或正文"
        return {"url": url, "message": message}

    def inspect(self, item):
        """复用同一HTML解析器读取精确审核公告；不依靠城市parser或搜索摘要。"""
        url = item.get("url") if isinstance(item, dict) else None
        registered = reviewed_publication(url)
        if registered is None:
            raise ValueError("未登记的地方正式公告，未请求页面")
        raw, page, observed_url = self._read(url)
        observation = self._save_observation(url, registered.notice_title, raw, page,
                                            datetime.now(UTC).isoformat(), observed_url)
        result = self._publication(url, page, observation)
        if result is None:
            raise ValueError("地方公告主标题、机关或精确附件未核对；仅保留页面观察")
        return result

    def discover(self, *, notice_budget=None):
        now = datetime.now(UTC).isoformat()
        result = {"results": [], "local_clues": [], "source_hints": yichang_hints(),
                  "status": "FAILED", "local_coverage": "ENTRY_ONLY", "errors": [],
                  "searches": [], "omitted_notice_count": 0,
                  "limitations": ["读取宜昌登记页面并用官网首页补充新线索，文章合计最多三条，不保证文件齐全",
                                  "地方页面与题名不等于正式正文；未核验版本、日期、适用范围或使用许可",
                                  "已登记附件可进入独立正文准备；公告与题名本身不用于系数、公式回答",
                                  "精确登记页面可出现一次同路径HTTP跳转，记录传输情况；PDF仍仅HTTPS",
                                  "联网访问固定公共入口，不发送问题或项目资料，也不调用模型"]}
        homepage = None
        result["searches"] = [{"term": TARGET, "reason": "读取固定登记及官网入口，不外发问题",
                              "search_url": ROOT, "total": None}]
        try:
            raw, homepage, observed_url = self._read(ROOT)
            result["entry_observation"] = self._save_observation(
                ROOT, "宜昌官方入口页面观察", raw, homepage, now, observed_url)
        except (httpx.HTTPError, OSError, ValueError) as error:
            result["errors"].append(self._error(ROOT, error))
        # 首页错误保留为错误，但不阻断已登记文章；统一去重并共享总请求上限。
        candidates = {url: PUBLICATIONS[url]["title"] for url in self.registered_pages}
        for link in homepage.links if homepage is not None else ():
            title = link.get("title") or link["text"]
            url = article_url(link.get("href", ""))
            if TARGET in title and url:
                candidates.setdefault(url, title)
        result["omitted_notice_count"] = max(0, len(candidates) - 3)
        for url, title in list(candidates.items())[:3]:
            if notice_budget is not None and not notice_budget.reserve(url):
                continue
            try:
                raw, page, observed_url = self._read(url)
                observation = self._save_observation(url, title, raw, page, now, observed_url)
                result["local_clues"].append(observation)
                publication = self._publication(url, page, observation)
                if publication:
                    result["results"].append(publication)
            except (httpx.HTTPError, OSError, ValueError) as error:
                result["errors"].append(self._error(url, error))
        observed = "entry_observation" in result or bool(result["local_clues"])
        result["status"] = ("PARTIAL" if observed else "FAILED") if result["errors"] else "COMPLETE"
        if result["local_clues"]:
            result["local_coverage"] = "CLUES_ONLY"
        if result["results"]:
            result["local_coverage"] = "ATTACHMENTS_DISCOVERED"
        return result
