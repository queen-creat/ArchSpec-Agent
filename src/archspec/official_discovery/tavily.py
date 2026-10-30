"""Tavily 公共政府搜索线索；不接收原问题，不将摘要当成规范正文。"""
from __future__ import annotations

import json
import math
import os
import re
from urllib.parse import urlsplit

import httpx

from archspec.official_http import official_http_client

from archspec.official_discovery.local_sources import TARGET
from archspec.official_discovery.plan import REGION_NAMES
from archspec.official_discovery.public_queries import (
    LOCAL_PLANNING_QUERY,
    LOCAL_SUNLIGHT_QUERY,
    PUBLIC_TITLES,
    TOPICS,
    normalize_standard_code,
)
from archspec.official_documents.government_urls import validate_government_url
from archspec.official_documents.local_policy import reviewed_public_terms

ENDPOINT = "https://api.tavily.com/search"
VERSION = "TAVILY_GOV_CLUES_V4"
MAX_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 5
_PUBLIC_TERMS = frozenset((LOCAL_PLANNING_QUERY, LOCAL_SUNLIGHT_QUERY, *PUBLIC_TITLES, *(item[0] for item in TOPICS.values()),
                            TARGET, *reviewed_public_terms()))


class TavilyConfigurationError(ValueError):
    """配置未明确限制为免费账户；不得自动调用或静默切换。"""


class TavilyUnavailable(ValueError):
    """搜索未完成；错误文本不携带密钥、请求头或远端响应。"""

    def __init__(self, message: str, *, stop_batch: bool = False):
        super().__init__(message)
        # 账户、频率、额度故障必须阻断同批其他公共词，不靠错误文案猜类型。
        self.stop_batch = stop_batch


def validate_public_term(term):
    """只接受登记公共词及公开地名组合，重复防止旁路发送私有问题。"""
    if not isinstance(term, str) or len(term) > 200 or term != term.strip():
        raise ValueError("Tavily只接受已登记公共查询词")
    valid = term in _PUBLIC_TERMS or normalize_standard_code(term) is not None
    if not valid:
        for region in REGION_NAMES:
            prefix = region + " "
            if term.startswith(prefix):
                rest = term[len(prefix):]
                valid = rest in _PUBLIC_TERMS or normalize_standard_code(rest) is not None
                break
    if not valid:
        raise ValueError("Tavily只接受已登记公共题名、编号、主题和地域")
    return term


def reviewed_mohurd_publication(url):
    """仅已有住建部公开发布路径交给原解析器，其他地址保持搜索线索。"""
    try:
        validate_government_url(url)
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.hostname in {"www.mohurd.gov.cn", "mohurd.gov.cn"}
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/gongkai/zc/wjk/art/\d{4}/[A-Za-z0-9_-]+\.html",
                             parsed.path) is not None)


def validate_clue(item):
    """保留线索字段，丢弃提供商答案、全文和未经核对的规范元信息。"""
    if not isinstance(item, dict):
        raise TavilyUnavailable("Tavily搜索条目格式变化")
    title, url, content, score = (item.get(name) for name in ("title", "url", "content", "score"))
    if (not isinstance(title, str) or not title.strip() or len(title) > 500
            or not isinstance(url, str) or not isinstance(content, str) or len(content) > 12000
            or isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score) or not 0 <= score <= 1):
        raise TavilyUnavailable("Tavily搜索字段格式变化")
    try:
        validate_government_url(url)
    except ValueError:
        return None
    return {"title": title, "url": url, "excerpt": content, "score": float(score),
            "kind": "SEARCH_SNIPPET", "body_status": "NOT_ACQUIRED",
            "lifecycle_status": "UNKNOWN", "provider": "TAVILY"}


class TavilyDiscoveryProvider:
    """只接公共词；真实启用需工厂配置，测试可注入假HTTP。"""
    version = VERSION

    def __init__(self, api_key, *, client=None):
        # 保留原拒绝范围，只给静态分类；不回显密钥、长度、前缀或摘要，便于定位部署配置。
        if not isinstance(api_key, str) or not api_key:
            raise TavilyConfigurationError("当前服务未配置TAVILY_API_KEY，未调用搜索")
        if len(api_key) > 500:
            raise TavilyConfigurationError("TAVILY_API_KEY长度异常，请只填写完整密钥，未调用搜索")
        if any(char.isspace() or ord(char) < 32 for char in api_key):
            raise TavilyConfigurationError("TAVILY_API_KEY含空白或控制字符，未调用搜索")
        self._api_key = api_key
        self.client = client

    def search(self, term):
        term = validate_public_term(term)
        # 只给已登记公共地名加精确短语；主题仍可语义扩展，原问题绝不进入请求。
        # Tavily exact_match约束引号内片段，不将整条公共题名变成全字匹配。
        region = next((name for name in REGION_NAMES if term.startswith(name + " ")), None)
        query = f'"{region}" {term[len(region) + 1:]}' if region else term
        client = self.client or official_http_client()
        try:
            # 使用真实接口已接受的域名形式；通配符曾返回400，结果仍逐条核验HTTPS政府域名。
            # 固定basic并禁自动参数，防提供方自动升级搜索深度增加额度消耗。
            body = {"query": query, "search_depth": "basic", "topic": "general",
                    "max_results": MAX_RESULTS, "include_domains": ["gov.cn"],
                    "include_domains_mode": "restrict", "include_answer": False,
                    "include_raw_content": False, "include_images": False,
                    "auto_parameters": False, "include_usage": True,
                    "exact_match": region is not None}
            try:
                with client.stream("POST", ENDPOINT, json=body,
                        headers={"Authorization": "Bearer " + self._api_key},
                        timeout=10, follow_redirects=False) as response:
                    if response.status_code != 200:
                        stop_batch = response.status_code in {401, 403, 429, 432, 433}
                        message = ("账户、额度或频率限制，已停止后续公共词，不自动重试或购买额度"
                                   if stop_batch else "调用未完成")
                        raise TavilyUnavailable(
                            f"Tavily HTTP {response.status_code}：{message}", stop_batch=stop_batch)
                    data = bytearray()
                    for part in response.iter_bytes():
                        if len(data) + len(part) > MAX_BYTES:
                            raise TavilyUnavailable("Tavily响应超过大小上限，未保存部分线索")
                        data.extend(part)
                payload = json.loads(data)
            except (httpx.HTTPError, OSError, UnicodeError, json.JSONDecodeError):
                raise TavilyUnavailable("Tavily连接或响应未完成，未取得可核对线索") from None
            if (not isinstance(payload, dict) or not isinstance(payload.get("results"), list)
                    or len(payload["results"]) > MAX_RESULTS):
                raise TavilyUnavailable("Tavily搜索响应格式或条数变化")
            items, seen, rejected = [], set(), 0
            for item in payload["results"]:
                clue = validate_clue(item)
                if clue is None:
                    rejected += 1
                elif clue["url"] not in seen:
                    seen.add(clue["url"])
                    items.append(clue)
            return {"items": items, "total": len(items), "rejected_result_count": rejected,
                    "search_query": query, "exact_match": region is not None}
        finally:
            # 短HTTP客户端由本次调用关闭，注入客户端仍归调用方所有。
            if self.client is None:
                client.close()


def load_tavily_provider(environ=None, *, client=None):
    """默认旧服务；启用必须确认免费账户且已关闭PAYG。"""
    env = os.environ if environ is None else environ
    enabled = env.get("ARCHSPEC_TAVILY_ENABLED", "false")
    if not isinstance(enabled, str) or enabled.strip().lower() not in {"true", "false"}:
        raise TavilyConfigurationError("ARCHSPEC_TAVILY_ENABLED只允许true或false")
    if enabled.strip().lower() == "false":
        return None
    confirmed = env.get("ARCHSPEC_TAVILY_FREE_ONLY_CONFIRMED", "")
    if not isinstance(confirmed, str) or confirmed.strip().lower() != "true":
        raise TavilyConfigurationError("Tavily尚未确认免费账户及关闭PAYG，未调用搜索")
    return TavilyDiscoveryProvider(env.get("TAVILY_API_KEY"), client=client)
