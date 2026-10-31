"""已审核公告寻找入口；国家与地方共用目标合同，不预填取得结果或效力。"""
import hashlib
import json
from dataclasses import dataclass, replace

from archspec.official_discovery.editions import source_editions, without_editions
from archspec.official_discovery.plan import DiscoveryPlan
from archspec.official_discovery.public_queries import PUBLIC_TITLES, normalize_standard_code
from archspec.official_discovery.tavily import reviewed_mohurd_publication
from archspec.official_documents.local_policy import LOCAL_POLICY_VERSION, REVIEWED_PUBLICATIONS

# 仅登记已经审阅的公开公告路径。实际主规范仍由原公告解析器重新读取核对。
_DEFAULT_ENTRIES = (
    ("GB 55036-2022", "消防设施通用规范",
     "https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2022/art_17339_767704.html"),
    ("GB 50352-2019", "民用建筑设计统一标准",
     "https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2019/art_17339_240715.html"),
    ("GB 50067-2014", "汽车库、修车库、停车场设计防火规范",
     "https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2015/art_17339_224265.html"),
    ("JGJ 100-2015", "车库建筑设计规范",
     "https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2015/art_17339_224048.html"),
)
# 已实际核过首页的公共寻找入口；仍须每次读取确认，不附带正文或现行性结论。
_DEFAULT_COPY_ENTRIES = (
    ("GB 50099-2011", "中小学校设计规范",
     "https://edu.foshan.gov.cn/attachment/0/196/196510/4866032.pdf"),
    ("GB 50016-2014", "建筑设计防火规范（2018年版）",
     "https://js.119.gov.cn/202111/715323810e37465891fc6a5f66b9589d_c_3d35986ea8ea4ce781db.html"),
)
_MAX_ENTRIES = 128


@dataclass(frozen=True)
class _Publication:
    code: str
    title: str
    url: str


def _public_target(term):
    """只有公开编号或服务端登记题名可以选择入口，宽主题不产生确定编号。"""
    term = without_editions(term)
    code = normalize_standard_code(term)
    if code is not None:
        return "CODE", code
    if isinstance(term, str) and term in PUBLIC_TITLES:
        return "TITLE", term
    return None


class PublicationCatalog:
    """只存不可变公共字段；内容指纹用于隔离旧查询缓存，输出每次独立复制。"""

    __slots__ = ("_copy_entries", "_entries", "_version")

    def __init__(self, entries=None, *, copy_entries=None):
        source = (
            ({"code": code, "title": title, "url": url} for code, title, url in _DEFAULT_ENTRIES)
            if entries is None else entries
        )
        by_url = {}
        try:
            if isinstance(source, (str, bytes, bytearray, dict)):
                raise TypeError
            for index, row in enumerate(source):
                if index >= _MAX_ENTRIES:
                    raise ValueError
                if not isinstance(row, dict) or set(row) != {"code", "title", "url"}:
                    raise ValueError
                code = normalize_standard_code(row["code"])
                title, url = row["title"], row["url"]
                if (code is None or not isinstance(title, str) or title not in PUBLIC_TITLES
                        or not reviewed_mohurd_publication(url)):
                    raise ValueError
                entry = _Publication(code, title, url)
                if url in by_url and by_url[url] != entry:
                    # 同一公告不能静默绑定互相矛盾的主规范。完全相同的入口只保留一次。
                    raise ValueError
                by_url[url] = entry
        except Exception:  # noqa: BLE001 - 服务端输入及生成器错误统一拒绝，不回显原输入
            raise ValueError("已审核公告目录含无效入口、重复身份或超限输入") from None
        self._entries = tuple(sorted(by_url.values(), key=lambda entry: (
            entry.code, entry.title, entry.url)))
        from archspec.official_documents.government_urls import validate_government_url

        copies = (_DEFAULT_COPY_ENTRIES if entries is None else ()) if copy_entries is None else copy_entries
        by_copy_url = {}
        try:
            if isinstance(copies, (str, bytes, bytearray, dict)):
                raise TypeError
            for index, row in enumerate(copies):
                if index >= _MAX_ENTRIES:
                    raise ValueError
                if copy_entries is None:
                    row = dict(zip(("code", "title", "url"), row, strict=True))
                if not isinstance(row, dict) or set(row) != {"code", "title", "url"}:
                    raise ValueError
                code = normalize_standard_code(row["code"])
                # 年版只装饰已登记公开题名；目录仍是寻找提示，后续实际主页面必须重核。
                if (code is None or without_editions(row["title"]) not in PUBLIC_TITLES
                        or len(source_editions(None, row["title"])) > 1):
                    raise ValueError
                url = validate_government_url(row["url"])
                entry = _Publication(code, row["title"], url)
                if url in by_copy_url and by_copy_url[url] != entry:
                    raise ValueError
                by_copy_url[url] = entry
        except (KeyError, TypeError, ValueError):
            raise ValueError("已审核政府全文入口含无效地址、身份或超限输入") from None
        self._copy_entries = tuple(sorted(by_copy_url.values(),
            key=lambda entry: (entry.code, entry.title, entry.url)))
        payload = json.dumps(
            {"national": [(entry.code, entry.title, entry.url) for entry in self._entries],
             "local_policy": LOCAL_POLICY_VERSION,
             "copies": [(entry.code, entry.title, entry.url) for entry in self._copy_entries]},
            ensure_ascii=False, separators=(",", ":"))
        self._version = "REGISTERED_PUBLICATIONS_V2:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def version(self):
        """只读内容身份；不会把目录存在说成公告已读取或规范仍现行。"""
        return self._version

    def for_query(self, plan, term):
        """同时绑定共同计划的精确目标和当前公共词，地域与宽主题不借国家入口。"""
        if not isinstance(plan, DiscoveryPlan):
            raise TypeError("公告入口选择需要共同发现计划")
        if plan.route == "REGISTERED_LOCAL":
            if term not in {public for public, _reason in plan.queries}:
                return []
            return [{"title": entry.title, "url": entry.notice_url}
                    for entry in REVIEWED_PUBLICATIONS.values()
                    if entry.public_term == term
                    and plan.match_source(entry.code, entry.title) == "TARGET"]
        return self._for_entries(self._entries, plan, term)

    def copies_for_query(self, plan, term):
        """政府全文只在精确目标中选入口；旧显式空目录仍完全隔离默认入口。"""
        return self._for_entries(self._copy_entries, plan, term)

    @staticmethod
    def _for_entries(entries, plan, term):
        if not isinstance(plan, DiscoveryPlan):
            raise TypeError("官方入口选择需要共同发现计划")
        if plan.route != "MOHURD" or plan.region is not None:
            return []
        target = _public_target(term)
        if target is None or target not in {
                _public_target(public) for public, _reason in plan.queries}:
            return []
        precise = tuple((kind, value) for kind, value in plan.targets if kind in {"CODE", "TITLE"})
        if not precise:
            return []
        # 保留唯一的编号/年份/题名匹配规则；只过滤TOPIC，不另写同义词或问题判断。
        target_plan = replace(plan, targets=precise)
        query_plan = replace(plan, targets=(target,))
        return [
            {"title": entry.title, "url": entry.url}
            for entry in entries
            if (not source_editions(None, entry.title) or plan.requested_editions)
            and target_plan.match_source(entry.code, entry.title) == "TARGET"
            and query_plan.match_source(entry.code, entry.title) == "TARGET"
        ]
