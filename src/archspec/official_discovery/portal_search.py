"""属地官网原生发现；只读取机关公开搜索合同，不登记规范或预填答案。"""
from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from lxml import html

from archspec.official_discovery.public_regions import (
    DIRECTORY_SHA256,
    DIRECTORY_SOURCE,
    PORTAL_ROOTS,
)
from archspec.official_discovery.tavily import validate_public_term
from archspec.official_documents.government_urls import validate_government_url
from archspec.official_http import official_http_client

# 保留原模块的公开目录入口，外部消费同一不可变来源，不另维护副本。
__all__ = ["DIRECTORY_SHA256", "DIRECTORY_SOURCE", "PORTAL_ROOTS", "VERIFIED_SEARCH_CONTRACTS", "VERSION", "discover_portal"]

VERSION = "OFFICIAL_PORTAL_DISCOVERY_V3"
_API = "https://api.so-gov.cn/"
# 已核的公开工具合同，记录官方页面指纹；不含规范地址、正文或答案。
# 在线query/site仍逐次核对站点身份及全域栏目，避免每题重复依赖门户首页。
VERIFIED_SEARCH_CONTRACTS = {
    "https://www.xm.gov.cn/": {"entry": "https://www.xm.gov.cn/so/s",
        "site_code": "3502000001", "sha256": "0f2b038b9875872256f812181b48f2a31709f1aa9423b822ce4e8b5632142226"},
}


def _read(client, method, url, *, headers=None, **options):
    """沿官网正常匿名HTTP合同读取；不重试、不执行网页脚本。"""
    with client.stream(method, url, timeout=10, follow_redirects=False,
                       headers={"User-Agent": "ArchSpec-Agent/0.1 official-copy", **(headers or {})}, **options) as response:
        response.raise_for_status()
        raw = bytearray()
        for block in response.iter_bytes():
            raw.extend(block)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("官方搜索响应超过读取范围")
    return bytes(raw)


def discover_portal(plan, roots, *, client=None):
    """在已有三个公共词内按真实机关站点检索，返回线索给共同五页正文读取器。"""
    import json
    result = {"searches": [], "items": [], "queues": [], "errors": [], "metadata_read_count": 0}
    root = roots.get(plan.region)
    if root is None:
        return result
    owned = client is None
    client = client or official_http_client()
    try:
        validate_government_url(root)
        contract = VERIFIED_SEARCH_CONTRACTS.get(root)
        if contract:
            entry, site = contract["entry"], contract["site_code"]
            validate_government_url(entry)
            result["search_contract"] = {"source_url": entry, "sha256": contract["sha256"]}
        else:
            result["metadata_read_count"] += 1
            tree = html.fromstring(_read(client, "GET", root))
            forms = tree.xpath("//form[.//input[@name='qt']]")
            form = next(node for node in forms if node.get("method", "get").lower() == "get")
            entry = urljoin(root, form.get("action", ""))
            validate_government_url(entry)
            if urlsplit(entry).hostname != urlsplit(root).hostname:
                raise ValueError("官方搜索入口尚未绑定同一机关")
            result["metadata_read_count"] += 1
            page = html.fromstring(_read(client, "GET", entry).decode("utf-8"))
            meta = {node.get("name", "").lower(): node.get("content", "") for node in page.xpath("//meta[@name]")}
            site = meta["siteidcode"]
            domain = meta["sitedomain"].removeprefix("https://").removeprefix("http://").rstrip("/")
            if (domain != urlsplit(root).hostname or plan.region not in meta["sitename"]
                    or not re.fullmatch(r"[0-9]{10}", site)
                    or not re.search(r'api[.]basic\s*=\s*["\']https://api[.]so-gov[.]cn/["\']',
                                     page.text_content())):
                raise ValueError("门户未声明已支持的原生搜索合同")
        anonymous = str(uuid4())  # 仅本轮随机匿名标识，不读取浏览器用户资料。
        api_headers = {"Origin": root.rstrip("/"), "Referer": entry}
        result["metadata_read_count"] += 1
        config = json.loads(_read(client, "POST", _API + "query/site", headers=api_headers,
                                 data={"siteCode": site, "tab": "", "ie": anonymous}))
        if (config.get("ok") is not True or config["data"]["site"]["siteCode"] != site
                or (contract and plan.region not in config["data"]["site"]["name"])):
            raise ValueError("官方搜索站点配置无法核验")
        # 本站常只有批准公告；使用官网明确开放的行政全域栏目，避免漏掉部门原文。
        regional_tabs = [row for row in config["data"].get("tabs", [])
                         if row.get("chsName") in {"全市", "全省", "全区", "全县"}
                         and row.get("outside") == 0 and row.get("labelAvailableStatus") == 1]
        tab = regional_tabs[0]["tabName"] if regional_tabs else config["data"]["site"]["defaultTabsId"]
        scope_label = regional_tabs[0]["chsName"] if regional_tabs else "官网默认范围"
        for term, _reason in plan.queries[:3]:
            public = f"{plan.region} {term}" if plan.region not in term else term
            validate_public_term(public)
            # 地域由官网站点绑定，查询只用公共主题，避免城市名匹配全站每一页。
            query = term[len(plan.region) + 1:] if term.startswith(plan.region + " ") else term
            try:
                payload = json.loads(_read(client, "POST", _API + "query/s", headers=api_headers, data={
                    "siteCode": site, "tab": tab, "qt": query, "sort": "relevance",
                    "keyPlace": "0", "ie": anonymous}))
                if payload.get("ok") is not True:
                    raise ValueError("官方搜索未返回可核对结果")
                items = []
                for row in payload["resultDocs"][:20]:
                    data = row["data"]
                    try:
                        observed_url = data["url"]
                        parsed = urlsplit(observed_url)
                        # 旧官网索引仍留HTTP；只尝试同一地址的HTTPS，政府域/TLS/正文身份照常核验。
                        candidate = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)) if parsed.scheme == "http" else observed_url
                        url = validate_government_url(candidate)
                    except ValueError:
                        continue
                    title = data.get("titleO") or html.fromstring(data["title"]).text_content()
                    # 门户分数与BM25/Tavily不在同一尺度；不把它当证据置信度。
                    items.append({"title": title, "url": url, "excerpt": data.get("summary") or "",
                                  "score": 0.0, "score_kind": "NOT_CALIBRATED",
                                  "kind": "SEARCH_SNIPPET", "body_status": "NOT_ACQUIRED",
                                  "lifecycle_status": "UNKNOWN", "provider": "NATIVE_PORTAL",
                                  "matched_queries": [public], "requested_region": plan.region,
                                  "observed_url": observed_url})
                from archspec.official_discovery.government_copies import candidate_priority
                # 先按共同主文件关系排完整的第一页，再取五条；前置公告不能挤掉正文入口。
                items = sorted(items, key=lambda clue: candidate_priority(clue, plan))[:5]
                result["items"].extend(items)
                result["queues"].append([item["url"] for item in items])
                result["searches"].append({"term": public, "provider": "NATIVE_PORTAL",
                    "status": "COMPLETE", "result_count": len(items), "search_query": query,
                    "site_code": site, "source_url": entry, "scope_label": scope_label,
                    "search_url": entry + "?" + urlencode({"qt": query})})
            except Exception:  # noqa: BLE001 -- 外部格式受限即结束本词，不扩展模板兼容或重发。
                result["searches"].append({"term": public, "provider": "NATIVE_PORTAL",
                    "status": "FAILED", "result_count": 0, "message": "属地官方搜索暂不可用，本词未取得线索"})
                result["errors"].append({"term": public, "message": "属地官方搜索暂不可用，本词未取得线索"})
    except Exception as error:  # noqa: BLE001 -- 未支持门户正常降级，不泄露远端原文。
        reason = (f"HTTP_{error.response.status_code}" if isinstance(error, httpx.HTTPStatusError)
                  else "TIMEOUT" if isinstance(error, httpx.TimeoutException)
                  else "CONNECTION_FAILED" if isinstance(error, httpx.TransportError)
                  else "METADATA_UNVERIFIED")
        result["errors"].append({"code": reason,
            "message": "属地门户搜索合同暂无法核验（" + reason + "）；保留其他官方搜索线索"})
    finally:
        if owned:
            client.close()
    return result
