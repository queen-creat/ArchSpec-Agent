"""免密钥的政府网页搜索线索；不执行页面脚本，不把摘要当正文。"""
import os
from urllib.parse import parse_qs, urlsplit

import httpx
from lxml import html

from archspec.official_discovery.tavily import (
    TavilyConfigurationError,
    TavilyUnavailable,
    validate_public_term,
)
from archspec.official_documents.government_urls import validate_government_url
from archspec.official_http import official_http_client

ENDPOINT = 'https://html.duckduckgo.com/html/'
MAX_BYTES = 1024 * 1024


def validate_search_proxy(value):
    """只允许管理员明确配置的本机代理；不探测端口或读取浏览器/系统凭据。"""
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise TavilyConfigurationError('免费搜索代理配置无效')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and not parsed.username and not parsed.password
                 and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise TavilyConfigurationError('免费搜索代理只接受明确的本机HTTP入口')
    return value


def _target(href):
    """仅解开搜索页明确给出的目标参数；不访问跳转器、不猜官网URL。"""
    parsed = urlsplit(href)
    if parsed.hostname in {'duckduckgo.com', 'html.duckduckgo.com'} and parsed.path == '/l/':
        targets = parse_qs(parsed.query).get('uddg', [])
        if len(targets) != 1:
            return None
        href = targets[0]
    try:
        return validate_government_url(href)
    except ValueError:
        return None


class DuckDuckGoDiscoveryProvider:
    name = 'DUCKDUCKGO'
    version = 'DUCKDUCKGO_GOV_CLUES_V3'

    def __init__(self, *, client=None, proxy_url=None):
        self.client = client
        self.proxy_url = validate_search_proxy(proxy_url)

    def search(self, term):
        # 沿用唯一公共词校验，原问、项目名、电话和任务书不能进入外部请求。
        validate_public_term(term)
        query = term + ' site:gov.cn'
        options = {'proxy': self.proxy_url} if self.proxy_url else {}
        client = self.client or official_http_client(**options)
        try:
            try:
                with client.stream('GET', ENDPOINT, params={'q': query}, timeout=10,
                                   follow_redirects=False,
                                   headers={'User-Agent': 'ArchSpec-Agent/0.1 public-official-search'}) as response:
                    if response.status_code != 200:
                        raise TavilyUnavailable(
                            f'免费搜索HTTP {response.status_code}，未取得可核对线索',
                            stop_batch=response.status_code in {202, 403, 429})
                    data = bytearray()
                    for part in response.iter_bytes():
                        if len(data) + len(part) > MAX_BYTES:
                            raise TavilyUnavailable('免费搜索响应超过上限，未保存部分结果')
                        data.extend(part)
                # 此搜索入口为UTF-8；按明确合同解码，避免无meta时HTML库默认成西文。
                # 解码失败保持错误，不用替换字符或猜编码把损坏页冒充有效线索。
                tree = html.fromstring(bytes(data).decode('utf-8'))
            except (httpx.HTTPError, OSError):
                raise TavilyUnavailable('免费搜索连接未完成，未取得可核对线索') from None
            except (ValueError, TypeError) as error:
                if isinstance(error, TavilyUnavailable):
                    raise
                raise TavilyUnavailable('免费搜索响应暂无法核对') from None
            if tree.xpath('//form[contains(@action,"anomaly") or contains(@id,"challenge")]'):
                # 不提交验证、不把验证页当零结果，也不换词继续撞同一限制。
                raise TavilyUnavailable('免费搜索需要网页验证，本批公共词已停止', stop_batch=True)
            anchors = tree.xpath('//a[contains(concat(" ",normalize-space(@class)," ")," result__a ")]')
            if not anchors and not tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," no-results ")]'):
                raise TavilyUnavailable('免费搜索结果结构尚无法核对，未当作零结果')
            items, seen, rejected = [], set(), 0
            for anchor in anchors[:20]:
                title = anchor.text_content().strip()
                target = _target(anchor.get('href', ''))
                if not target or not title or len(title) > 500:
                    rejected += 1
                    continue
                if target in seen:
                    continue
                seen.add(target)
                parents = anchor.xpath('ancestor::div[contains(concat(" ",normalize-space(@class)," ")," result ")][1]')
                snippets = parents[0].xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," result__snippet ")]') if parents else []
                excerpt = snippets[0].text_content().strip()[:12000] if snippets else ''
                items.append({'title': title, 'url': target, 'excerpt': excerpt, 'score': 0.0,
                              'score_kind': 'NOT_CALIBRATED', 'kind': 'SEARCH_SNIPPET',
                              'body_status': 'NOT_ACQUIRED', 'lifecycle_status': 'UNKNOWN',
                              'provider': self.name})
            from archspec.official_discovery.government_copies import inspection_priority
            from archspec.official_discovery.plan import build_discovery_plan

            # 同一有界响应先复用主文件优先规则，再取五条；不增加请求或正文额度。
            # 这里只用已校验公共词决定候选读取顺序，不把搜索题名/摘要当正文身份。
            plan = build_discovery_plan(term)
            items = sorted(items, key=lambda clue: inspection_priority(clue, plan))
            return {'items': items[:5], 'total': len(items[:5]),
                    'rejected_result_count': rejected, 'search_query': query, 'exact_match': False}
        finally:
            if self.client is None:
                client.close()


def load_free_search_provider(environ=None, *, client=None):
    """显式选择免费路线；Tavily仍由旧工厂优先选择，不能同时增加提供方。"""
    env = os.environ if environ is None else environ
    enabled = env.get('ARCHSPEC_FREE_SEARCH_ENABLED', 'false')
    if not isinstance(enabled, str) or enabled.strip().lower() not in {'true', 'false'}:
        raise TavilyConfigurationError('ARCHSPEC_FREE_SEARCH_ENABLED只允许true或false')
    if enabled.strip().lower() == 'false':
        return None
    return DuckDuckGoDiscoveryProvider(client=client, proxy_url=env.get('ARCHSPEC_FREE_SEARCH_PROXY'))
