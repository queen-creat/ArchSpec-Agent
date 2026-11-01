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

VERSION = "OFFICIAL_PORTAL_DISCOVERY_V5"
_API = "https://api.so-gov.cn/"
# 已核的公开工具合同，记录官方页面指纹；不含规范地址、正文或答案。
# 在线query/site仍逐次核对站点身份及全域栏目，避免每题重复依赖门户首页。
VERIFIED_SEARCH_CONTRACTS = {
    "https://www.xm.gov.cn/": {"entry": "https://www.xm.gov.cn/so/s",
        "site_code": "3502000001", "sha256": "0f2b038b9875872256f812181b48f2a31709f1aa9423b822ce4e8b5632142226"},
}


# 仅登记真实核验的官网合同；不同地区复用同一适配器，不根据用户问题拼地址。
VERIFIED_IGS_CONTRACTS = {
    "https://www.wuhan.gov.cn/": {
        "entry": "/search/index.shtml",
        "endpoint": "/igs/front/search.jhtml",
        "config": "/material/2025_search/js/config.js",
        "site_id": "10",
        "code": "ffd32baa9d7a4b0db3d2c7a55d2e8959",
        "sha256": "e93e213438dd2b575fcc0a429e61fd6752c3b8d7ed05e49e458c03192008efbc",
    },
}


# 广东省政府主页表单绑定的公开索引；这是省级跨城搜索，不代表城市官网。
_GD_INDEX_ENTRY = 'https://search.gd.gov.cn/search/all/2'
_GD_INDEX_ENDPOINT = 'https://search.gd.gov.cn/api/search/all'
_GD_INDEX_HOME = 'https://www.gd.gov.cn/'
_GD_INDEX_HOME_SHA = '575041cbd24df850b9d046469b0ecb4c43fa7bb5900e929c266ab7e866cb1c4d'
_GD_INDEX_SCRIPT = 'https://search.gd.gov.cn/static/pc/dist/app-5e6dbe7c.js'
_GD_INDEX_SCRIPT_SHA = 'a80c979d05d76f8ea72892abef7a20382b79ac798afe92d8a7cf3e20619475fa'


def _guangdong_index_region(region):
    from archspec.official_discovery.public_regions import REGION_PARENTS

    return region == '广东' or REGION_PARENTS.get(region) == '广东'


def supports_portal(region, roots):
    """只声明有已登记官网或已审阅的省级跨城搜索，不伪称某市官网。"""
    return bool(region and (region in roots or _guangdong_index_region(region)))


def _discover_guangdong_index(plan, client, result):
    """政府索引线索须先核目标地域及文件名，再由下游独立取公告和正文。"""
    import json

    from archspec.official_discovery.public_queries import extract_public_codes

    validate_government_url(_GD_INDEX_HOME)
    validate_government_url(_GD_INDEX_ENTRY)
    validate_government_url(_GD_INDEX_ENDPOINT)
    result['search_contract'] = {'source_url': _GD_INDEX_SCRIPT, 'sha256': _GD_INDEX_SCRIPT_SHA,
                                 'owner_url': _GD_INDEX_HOME, 'owner_sha256': _GD_INDEX_HOME_SHA}
    for term, _reason in plan.queries[:3]:
        public = term if plan.region in term else f'{plan.region} {term}'
        try:
            # 搜索内容仅含登记地区和固定公共词；不读取浏览器凭据或项目原问。
            # HTTP 客户端可能在本轮保留官网设置的临时 cookie，不持久化为用户配置。
            validate_public_term(public)
            payload = json.loads(_read(client, 'POST', _GD_INDEX_ENDPOINT,
                headers={'Origin': 'https://search.gd.gov.cn', 'Referer': _GD_INDEX_ENTRY},
                json={'keywords': public, 'sort': 'smart', 'site_id': '2', 'range': 'province',
                      'position': 'title', 'page': 1, 'recommand': 1}))
            news = payload.get('data', {}).get('news')
            if payload.get('errcode') != 0 or not isinstance(news, dict) or not isinstance(news.get('list'), list):
                raise ValueError('广东官网搜索响应结构无法核对')
            items, seen = [], set()
            for row in news['list'][:20]:
                if not isinstance(row, dict):
                    continue
                title = _igs_plain_text(row.get('title'), 500)
                observed = row.get('url')
                if not title or not isinstance(observed, str):
                    continue
                # 只保留被当前问题的地域及主文件合同接受的线索；标题本身仍非正文。
                # 只有明确印发/发布的公告标题可把书名作为候选；废止/修改壳不能据此取件。
                publication = re.match(r'^(?:[\u4e00-\u9fff]{0,32}关于|关于)?(?:印发|发布|公布|颁布)', title)
                names = (title, *re.findall(r'《([^》]+)》', title)) if publication else (title,)
                if not any(plan.match_source(code, name) == 'TARGET'
                           for name in names for code in (extract_public_codes(name) or ('',))):
                    continue
                try:
                    parts = urlsplit(observed)
                    candidate = (urlunsplit(('https', parts.netloc, parts.path, parts.query, parts.fragment))
                                 if parts.scheme == 'http' else observed)
                    url = validate_government_url(candidate)
                except ValueError:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                items.append({'title': title, 'url': url, 'excerpt': _igs_plain_text(row.get('content'), 4000),
                              'score': 0.0, 'score_kind': 'NOT_CALIBRATED', 'kind': 'SEARCH_SNIPPET',
                              'body_status': 'NOT_ACQUIRED', 'lifecycle_status': 'UNKNOWN',
                              'provider': 'NATIVE_PORTAL', 'matched_queries': [public],
                              'requested_region': plan.region, 'observed_url': observed})
            from archspec.official_discovery.government_copies import candidate_priority
            items = sorted(items, key=lambda item: candidate_priority(item, plan))[:5]
            result['items'].extend(items)
            result['queues'].append([item['url'] for item in items])
            result['searches'].append({'term': public, 'provider': 'NATIVE_PORTAL', 'status': 'COMPLETE',
                'result_count': len(items), 'search_query': public, 'site_code': '2',
                'source_url': _GD_INDEX_ENTRY, 'scope_label': '广东省政府全省索引',
                'search_url': _GD_INDEX_ENTRY + '?' + urlencode({'keywords': public})})
        except Exception:  # noqa: BLE001 -- 合同或服务失败即停止本批，不重试或扩大查询。
            result['searches'].append({'term': public, 'provider': 'NATIVE_PORTAL', 'status': 'FAILED',
                                      'result_count': 0, 'message': '广东政府索引暂不可用'})
            result['errors'].append({'term': public, 'message': '广东政府索引暂不可用，本批已停止'})
            break
    return result


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


def _igs_plain_text(value, limit):
    """官网索引字段仅作纯文本线索，不能作为规范正文。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return html.fragment_fromstring(value, create_parent=True).text_content().strip()[:limit]
    except (ValueError, TypeError):
        return ""


def _discover_reviewed_igs(plan, root, contract, client, result):
    """使用已核同源 IGS 合同；不执行脚本，不接受动态端点。"""
    import json

    root_url = urlsplit(validate_government_url(root))
    endpoints = []
    for field in ("entry", "endpoint", "config"):
        path = contract[field]
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("官方搜索合同路径无效")
        url = validate_government_url(urljoin(root, path))
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != root_url.hostname
                or parsed.port != root_url.port or parsed.query or parsed.fragment):
            raise ValueError("官方搜索合同必须同源")
        endpoints.append(url)
    entry, endpoint, config_url = endpoints
    site, code = contract["site_id"], contract["code"]
    if (not isinstance(site, str) or not re.fullmatch(r"[0-9]{1,10}", site)
            or not isinstance(code, str) or not re.fullmatch(r"[a-f0-9]{32}", code)
            or not re.fullmatch(r"[a-f0-9]{64}", contract["sha256"])):
        raise ValueError("官方搜索合同身份无效")
    result["search_contract"] = {"source_url": config_url, "sha256": contract["sha256"]}
    for term, _reason in plan.queries[:3]:
        public = f"{plan.region} {term}" if plan.region not in term else term
        validate_public_term(public)
        # 官方站点身份约束地域；只发送已登记公共词，不发送用户原问。
        query = term[len(plan.region) + 1:] if term.startswith(plan.region + " ") else term
        try:
            payload = json.loads(_read(client, "GET", endpoint, headers={"Referer": entry}, params={
                "code": code, "siteId": site, "searchWord": query,
                "pageNumber": 1, "pageSize": 10, "queryAll": "true"}))
            page = payload["page"]
            if (not isinstance(page, dict) or page.get("code") != code
                    or page.get("searchWord") != query or not isinstance(page.get("content"), list)):
                raise ValueError("官方搜索响应身份或查询词不一致")
            items = []
            for row in page["content"][:10]:
                if not isinstance(row, dict):
                    continue
                observed_url = row.get("DOCPUBURL") or row.get("url")
                if not isinstance(observed_url, str):
                    continue
                try:
                    parsed = urlsplit(observed_url)
                    # 和已有门户一致：仅把政府站的明文地址升级到同域 HTTPS。
                    candidate = (urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
                                 if parsed.scheme == "http" else observed_url)
                    url = validate_government_url(candidate)
                except ValueError:
                    continue
                title = _igs_plain_text(row.get("FileName") or row.get("DOCTITLE") or row.get("title"), 500)
                if not title:
                    continue
                excerpt = _igs_plain_text(row.get("DOCCONTENT") or row.get("content"), 4000)
                items.append({"title": title, "url": url, "excerpt": excerpt,
                              "score": 0.0, "score_kind": "NOT_CALIBRATED",
                              "kind": "SEARCH_SNIPPET", "body_status": "NOT_ACQUIRED",
                              "lifecycle_status": "UNKNOWN", "provider": "NATIVE_PORTAL",
                              "matched_queries": [public], "requested_region": plan.region,
                              "observed_url": observed_url})
            from archspec.official_discovery.government_copies import candidate_priority
            items = sorted(items, key=lambda clue: candidate_priority(clue, plan))[:5]
            result["items"].extend(items)
            result["queues"].append([item["url"] for item in items])
            result["searches"].append({"term": public, "provider": "NATIVE_PORTAL",
                "status": "COMPLETE", "result_count": len(items), "search_query": query,
                "site_code": site, "source_url": entry, "scope_label": "官网全部栏目",
                "search_url": entry + "?" + urlencode({"searchWord": query})})
        except Exception:  # noqa: BLE001 -- 单词失败不重试，也不采用无法绑定的摘要。
            result["searches"].append({"term": public, "provider": "NATIVE_PORTAL",
                "status": "FAILED", "result_count": 0, "message": "属地官方搜索暂不可用，本词未取得线索"})
            result["errors"].append({"term": public, "message": "属地官方搜索暂不可用，本词未取得线索"})
    return result


def discover_portal(plan, roots, *, client=None):
    """在已有三个公共词内按真实机关站点检索，返回线索给共同五页正文读取器。"""
    import json
    result = {"searches": [], "items": [], "queues": [], "errors": [], "metadata_read_count": 0}
    root = roots.get(plan.region)
    guangdong = root is None and _guangdong_index_region(plan.region)
    if root is None and not guangdong:
        return result
    owned = client is None
    client = client or official_http_client()
    try:
        if guangdong:
            return _discover_guangdong_index(plan, client, result)
        validate_government_url(root)
        igs = VERIFIED_IGS_CONTRACTS.get(root)
        if igs:
            return _discover_reviewed_igs(plan, root, igs, client, result)
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
