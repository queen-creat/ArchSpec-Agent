"""官方发现共同目标：查询词、地域路线及公告主规范匹配由一个合同约束。"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace

from archspec.official_discovery.editions import bind_editions, source_editions, without_editions
from archspec.official_discovery.local_sources import TARGET
from archspec.official_discovery.public_queries import (
    LOCAL_PLANNING_QUERY,
    LOCAL_PLANNING_TITLES,
    LOCAL_SUNLIGHT_QUERY,
    LOCAL_SUNLIGHT_TITLES,
    PUBLIC_CODE,
    PUBLIC_TITLES,
    TOPICS,
    extract_public_codes,
    extract_public_titles,
    normalize_standard_code,
    plan_queries,
    validated_public_titles,
)
from archspec.official_discovery.public_regions import REGION_NAMES, REGION_PARENTS
from archspec.official_documents.local_policy import (
    REVIEWED_PUBLICATIONS,
    local_query_entries,
    region_matches,
)
from archspec.query_scope import requested_content

LOCAL_SCOPE_WORDS = ("地方标准", "地方规范", "地方规定", "本地", "本市", "本省", "当地", "某市", "某省")
MATCH_MESSAGES = {
    "TARGET": "公告主规范与本次公共目标匹配；正文、版本及适用性仍需分别核验",
    "ASSOCIATED": "这是其他主规范的关联公告，可能仅在引用或废止清单中提到目标；未自动准备其正文",
    "UNKNOWN": "公告主规范信息不完整，不能确认对应目标；保留线索，未自动准备正文",
}


def _compact(value):
    return re.sub(r"[\s《》〈〉（）()]+", "", unicodedata.normalize("NFKC", value))


def non_rule_notice_title(title):
    """题名明确描述检查结果或事务通知时，仅作线索；不据此认证法律效力。

    规则题名中的“技术审查规定”不等于一次审查结果。发布页与缓存查阅
    共用此判断，避免正文引用了规范题名就把检查通知升级为主文件。
    """
    subject = _compact(title)
    return bool(re.search(
        r"(?:检查|抽查|评估|考核|评审|监督).{0,16}(?:结果|名单|情况|通报)"
        r"|(?:机构|企业|单位).{0,12}(?:名单|名录)"
        r"|(?:会议|培训|采购|招标).*(?:通知|公告)$", subject))


def _reference_groups(query):
    """公开引用原位置共用一次；并列分组不拆题名内部标点。"""
    mentions = [(match.start(), match.end(), ("CODE", normalize_standard_code(match.group())))
                for match in PUBLIC_CODE.finditer(query)]
    mentions.extend((match.start(), match.end(), ("TITLE", title))
                    for title in extract_public_titles(query)
                    for match in re.finditer(re.escape(title), query))
    groups, previous_end = [], 0
    for start, end, target in sorted(mentions, key=lambda item: (item[0], -item[1])):
        if start < previous_end:
            continue
        between = re.sub(r'[\s《》〈〉（）()“”「」『』"]+', "", query[previous_end:start])
        if not groups or re.fullmatch(r"(?:与|和|及|以及|[、，,；;])+", between):
            groups.append([])
        groups[-1].append((start, end, target))
        previous_end = end
    return groups


def _group_targets(group):
    targets = [item[2] for item in group]
    return tuple(dict.fromkeys([target for target in targets if target[0] == "CODE"] or targets))


def _explicit_reference_targets(query):
    """同书编号带题名仍以编号为准；不改原有多书分组合同。"""
    return tuple(dict.fromkeys(target for group in _reference_groups(query) for target in _group_targets(group)))


def _edition_spans(query):
    spans = []
    for group in _reference_groups(query):
        targets = _group_targets(group)
        for start, end, target in group:
            bound = (target,) if target in targets else targets
            spans.append((start, end, bound))
    return spans


@dataclass(frozen=True)
class DiscoveryPlan:
    """内部只存不可变公共字段；每次对外序列化返回独立列表和字典。"""

    route: str
    region: str | None
    targets: tuple[tuple[str, str], ...]
    queries: tuple[tuple[str, str], ...]
    omitted_query_count: int
    version: str = "DISCOVERY_PLAN_V1"
    requested_editions: tuple[tuple[str, str, str], ...] = ()

    def public_dict(self):
        result = {
            "version": self.version, "route": self.route, "region": self.region,
            "targets": [{"kind": kind, "value": value} for kind, value in self.targets],
            "queries": [{"term": term, "reason": reason} for term, reason in self.queries],
            "omitted_query_count": self.omitted_query_count,
        }
        if self.requested_editions:
            result["requested_editions"] = [{"kind": kind, "value": value, "year": year}
                for kind, value, year in self.requested_editions]
        return result

    def explicit_targets(self, query):
        """只标记原问明确写出的来源；主题推导的候选书名不限制用户的资料范围。"""
        # 地方路线也会把“办公建筑”等宽主题标为LOCAL_TOPIC；它们只是搜索词。
        # 只有完整登记题名/编号才收紧书目范围，地域仍由独立元数据筛选保护。
        return tuple((kind, value) for kind, value in self.targets if kind == "CODE"
                     or (kind == "TITLE" or kind == "LOCAL_TOPIC" and value in PUBLIC_TITLES)
                     and _compact(value) in _compact(query))

    def match_source(self, standard_code, title):
        """主字段先核同书，再独立核年版；未知或多版本对应不猜。"""
        if not self.requested_editions:
            return self._match_source_identity(standard_code, title)
        code, name = without_editions(standard_code), without_editions(title)
        identity = self._match_source_identity(code, name)
        if identity != "TARGET":
            return identity
        required = set()
        for kind, value, year in self.requested_editions:
            if kind == "UNRESOLVED":
                return "UNKNOWN"
            one = replace(self, targets=((kind, value),), requested_editions=())
            if one._match_source_identity(code, name) == "TARGET":
                required.add(year)
        if not required:
            return identity
        observed = source_editions(standard_code, title)
        if len(required) != 1 or len(observed) != 1:
            return "UNKNOWN"
        return "TARGET" if required == observed else "ASSOCIATED"

    def _match_source_identity(self, standard_code, title):
        """只核实际公告主字段；搜索题名、摘要和废止清单不能替代主规范身份。"""
        if not isinstance(title, str) or not title.strip() or not isinstance(standard_code, str):
            return "UNKNOWN"
        subject = _compact(title)
        if self.route == "UNSUPPORTED_LOCAL":
            # 旧路线名仅表示没有城市专用提供方；实际主文件可由政府搜索取得。
            # 地域必须出现在主文件题名；国家/行业编号不能因此变成地方规定。
            if self.region in {None, "地域待补充"} or self.region not in subject:
                return "UNKNOWN"
            if normalize_standard_code(standard_code) is not None:
                return "ASSOCIATED"
            if non_rule_notice_title(title) or re.search(r"解读|图解|问答|答疑|征求意见|草案|公示", subject):
                return "ASSOCIATED"
            known = any(_compact(value) in subject for kind, value in self.targets if kind == "LOCAL_TOPIC")
            # 地方正式文件名称各异；共用文书类别，不逐城市添加题名特例。
            planning = any(value in LOCAL_PLANNING_TITLES for kind, value in self.targets)
            normative = bool(re.search(
                r"(?:规划|建筑|建设工程).*?(?:规定|办法|细则|规则|条例)$"
                r"|(?:日照|间距).*(?:管理|计算|控制|技术要求|技术规定|标准|执行).*通知$", subject))
            return "TARGET" if known or planning and normative else "ASSOCIATED"
        if self.route == "YICHANG":
            return "TARGET" if _compact(TARGET) in subject else "ASSOCIATED"
        if self.route == "REGISTERED_LOCAL":
            return "TARGET" if any(
                entry.code == standard_code and _compact(entry.title) == subject
                and region_matches(entry, self.region) and ("TITLE", entry.title) in self.targets
                for entry in REVIEWED_PUBLICATIONS.values()) else "ASSOCIATED"
        subject_code = normalize_standard_code(standard_code)
        if subject_code is None:
            return "UNKNOWN"
        # 同编号家族明确给过年份时，只接受那些年份；并列题名和无年编号不能放宽。
        explicit_versions = {value for kind, value in self.targets if kind == "CODE"
                             and "-" in value and value.split("-")[0] == subject_code.split("-")[0]}
        if explicit_versions and subject_code not in explicit_versions:
            return "ASSOCIATED"
        for kind, value in self.targets:
            if kind == "CODE":
                # 未写年份只约束同一编号，绝不因此声明返回版本现行。
                if subject_code == value or "-" not in value and subject_code.split("-")[0] == value:
                    return "TARGET"
            elif kind == "TITLE" and subject == _compact(value):
                return "TARGET"
            elif kind == "TOPIC":
                # 宽主题只是候选路线，不允许用公告全文中的一个相似单字背书。
                words = next((keywords for term, keywords in TOPICS.values() if term == value), ())
                if _compact(value) in subject or any(word in subject for word in words):
                    return "TARGET"
        return "ASSOCIATED"


def _target_region(query):
    """先识别项目目标，再处理多地歧义；引用文件地名不按词表顺序抢占。"""
    pattern = "|".join(re.escape(name) for name in sorted(REGION_NAMES, key=len, reverse=True))

    region_pairs = {(entry.province, entry.city) for entry in REVIEWED_PUBLICATIONS.values() if entry.city}
    region_pairs.update((province, city) for city, province in REGION_PARENTS.items())

    def unique_region(names):
        names = set(names)
        if len(names) == 1:
            return next(iter(names))
        # 只沿已有审核公告和官网地市目录确认组合，不推断省规适用性。
        cities = {city for province, city in region_pairs
                  if city in names and names <= {city, province}}
        return next(iter(cities)) if len(cities) == 1 else "地域待补充"

    mentions = list(re.finditer(pattern, query))
    if not mentions:
        return None
    # 引号内“住宅项目规范”等题名不能作为项目所在地语法；保留字数以核对位置。
    outside = re.sub(r"《[^》]*》|〈[^〉]*〉|“[^”]*”|「[^」]*」|『[^』]*』",
                     lambda match: " " * len(match.group()), query)
    # 角色绑定的是同一地域短语；省市连写不能截成省级目标。
    chains = []
    for province, city in region_pairs:
        chain_pattern = (rf"{re.escape(province)}(?:省)?[\s·-]{{0,3}}{re.escape(city)}(?:市)?|"
                         rf"{re.escape(city)}(?:市)?[\s·-]{{0,3}}{re.escape(province)}(?:省)?")
        chains.extend((match.start(), match.end(), (province, city))
                      for match in re.finditer(chain_pattern, outside))

    def target_names(mention):
        return next((names for start, end, names in chains
                     if start <= mention.start() and mention.end() <= end), (mention.group(),))

    destinations, projects, requested = [], [], []
    for mention in re.finditer(pattern, outside):
        after = outside[mention.end():mention.end() + 30]
        before = outside[max(0, mention.start() - 24):mention.start()]
        # 应用目的地与引用来源是不同角色，目的地不要求重复写“项目”。
        if re.search(r"(?:用于|适用于|应用于|用在|针对)\s*$", before):
            destinations.extend(target_names(mention))
        reference = bool(re.search(
            r"(?:参考|参照|引用|只有|已有|现有|手头|借用|对照)(?:的|是)?\s*$", before))
        if not reference and re.search(r"(?:想查|查询|查找|检索|要查|查)\s*$", before):
            requested.extend(target_names(mention))
        project = re.match(r"(?:省|市|地区)?([^，,；;。！？?!\n《》]{0,18}?)(?:项目|工程)", after)
        bridge = project.group(1) if project else None
        source_project = project is not None and re.match(
            r"(?:的)?(?:文件|资料|规范|规定|条文|标准)", after[project.end():])
        # “上海文件用于杭州项目”只把紧邻项目的杭州作为目标；不跨另一个地名。
        if (not reference and not source_project and bridge is not None
                and not re.search(pattern, bridge) and not re.search(
                    r"文件|资料|规范|规定|条文|标准|参考|对照|手头|只有|已有", bridge)):
            projects.extend(target_names(mention))
        if not reference and re.search(
                r"(?:项目|工程)(?:所在地|地点|位置)?(?:是|在|位于|坐落于|设在|：|:)\s*$", before):
            projects.extend(target_names(mention))
    return unique_region(destinations or projects or requested or
                         [mention.group() for mention in mentions])



def _general_reference_candidates(query):
    """仅用已有明确原字查阅结构补固定公共候选，不从原问外发私有词。"""
    if "《" in query or "》" in query:
        return ()  # 未登记书目不能被默认通用规范悄悄代替。
    from archspec.official_qa.question_plan import LocalPlanner

    proposal = LocalPlanner().propose(query, {})
    if (proposal["topic"] == "UNKNOWN" and proposal["understanding"] == "CLEAR"
            and proposal["operation"] == "REFERENCE_LOOKUP" and proposal.get("query_focus")):
        from archspec.official_discovery.public_scope_catalog import (
            PUBLIC_SECTION_CANDIDATES,
            section_subjects,
        )

        subjects = proposal["query_focus"]["subjects"]
        candidates, seen = [], set()
        for title, headings, _url, _sha, _pages in PUBLIC_SECTION_CANDIDATES:
            if title not in seen and any(term in subject for term in section_subjects(headings)
                                         for subject in subjects):
                seen.add(title)
                candidates.append({"term": title,
                                   "reason": "对象对应已核公开章节的候选书目；不是选书、适用性或正文证明"})
        return tuple(candidates)
    return ()


def _build_discovery_plan(query, topics=(), *, region=None, public_titles=()):
    """复用既有安全公共词规划，不存原问题，不接受调用方新增规范 URL。"""
    public_titles = validated_public_titles(public_titles)
    query = query.strip()
    if len(query) > 500:
        raise ValueError("问题最长为 500 个字")
    query = requested_content(query)
    if region is not None and region not in (*REGION_NAMES, "地域待补充"):
        raise ValueError("地域不属于已登记公开范围")
    region = _target_region(query) or region
    # “当地下室……”中的“当”引出条件，“地下室”是建筑部位；
    # 不能在复合词内部截出地域代词“当地”，从而把用户已选的全国规范全过滤掉。
    local_scope = (any(word in query for word in LOCAL_SCOPE_WORDS if word != "当地")
                   or re.search(r"当地(?!下)", query) is not None)
    if region is None and local_scope:
        region = "地域待补充"
    if region == "宜昌":
        return DiscoveryPlan("YICHANG", region, (("TITLE", TARGET),),
                             ((TARGET, "宜昌地方公共文件线索；不外发问题"),), 0)
    # 明确公共编号或国家题名优先，不能被同省宽主题替换为另一本指南。
    reviewed = (local_query_entries(query, region)
                if not extract_public_codes(query) and not extract_public_titles(query) else ())
    if reviewed:
        terms = tuple(dict.fromkeys(entry.public_term for entry in reviewed))
        return DiscoveryPlan("REGISTERED_LOCAL", region,
                             tuple(("TITLE", entry.title) for entry in reviewed[:3]),
                             tuple((term, "已审核地方公开题名；不外发问题") for term in terms[:3]),
                             max(0, len(terms) - 3))
    # 未接入地方可以没有全国公共词；此分支仅输出覆盖缺口，绝不外发问题。
    if region is not None:
        try:
            queries, omitted = plan_queries(query, topics)
        except ValueError:
            queries, omitted = [], 0
        if region != "地域待补充" and not extract_public_codes(query) and any(
                word in query for word in ("日照", "间距", "规划", "退让", "容积率", "建筑密度")):
            # 三次预算内保留综合文件检索，并用一条固定公共主题寻找专项规定；不发送原问题。
            sunlight=any(word in query for word in ("日照", "间距"))
            # 日照问题保留综合规定、专项管理、间距主题三路，避免泛规划题名重复占预算。
            terms=(LOCAL_PLANNING_QUERY,LOCAL_SUNLIGHT_TITLES[0] if sunlight else LOCAL_PLANNING_TITLES[3],
                   LOCAL_SUNLIGHT_QUERY if sunlight else LOCAL_PLANNING_TITLES[1])
            queries = [{"term": title, "reason": "地方规划公开题名或主题；地域单独限定"}
                       for title in terms]
            targets=(*LOCAL_PLANNING_TITLES,*(LOCAL_SUNLIGHT_TITLES if sunlight else ()))
            omitted = 0
        else:
            targets=tuple(item["term"] for item in queries)
        return DiscoveryPlan("UNSUPPORTED_LOCAL", region,
                             tuple(("LOCAL_TOPIC", title) for title in targets),
                             tuple((item["term"], item["reason"]) for item in queries), omitted)
    hinted = False
    if public_titles:
        try:
            plan_queries(query, ())
        except ValueError:
            # 只填原问的公共词缺口；不能让项目“办公”等宽主题盖掉具体补查书名。
            hinted = True
    if hinted:
        queries = [{"term": title, "reason": "已核公开目录的候选书名；不是用户选书或规范依据"}
                   for title in public_titles]
        omitted = 0
    else:
        try:
            queries, omitted = plan_queries(query, topics)
        except ValueError:
            candidates = _general_reference_candidates(query)
            if not candidates:
                raise
            queries, omitted = list(candidates[:3]), max(0, len(candidates) - 3)
    codes = extract_public_codes(query)
    if codes:
        targets = _explicit_reference_targets(query)
    else:
        titles = extract_public_titles(query)
        targets = tuple(("TITLE", title) for title in titles) if titles else tuple(
            ("TITLE" if item["term"] in PUBLIC_TITLES else "TOPIC", item["term"]) for item in queries)
    return DiscoveryPlan("MOHURD", None, targets,
                         tuple((item["term"], item["reason"]) for item in queries), omitted)


def build_discovery_plan(query, topics=(), *, region=None, public_titles=()):
    """所有消费者共享原问年版；公开词仍有界，原问题不会进入搜索请求。"""
    plan = _build_discovery_plan(query, topics, region=region, public_titles=public_titles)
    content = requested_content(query.strip())
    spans = []
    for start, end, targets in _edition_spans(content):
        bound = []
        for kind, value in targets:
            # 同一公共题名在地方路线可有城市前缀或LOCAL_TOPIC角色；
            # 仅映射到本次已选的唯一同字题名，不新增来源/地区或猜多书对应。
            aliases = tuple(target for target in plan.targets
                            if kind == "TITLE" and target[0] in {"TITLE", "LOCAL_TOPIC"}
                            and target[1].endswith(value))
            exact = tuple(target for target in aliases if target[1] == value)
            aliases = exact or aliases
            bound.append(aliases[0] if len(aliases) == 1 else (kind, value))
        spans.append((start, end, tuple(dict.fromkeys(bound))))
    # 审核地方题名自身可能含年版；完整登记原字仍走既有题名身份合同。
    spans.extend((match.start(), match.end(), ((kind, value),))
                 for kind, value in plan.targets if kind in {"TITLE", "LOCAL_TOPIC"}
                 for match in re.finditer(re.escape(value), content))
    editions = bind_editions(content, spans)
    if not editions:
        return plan
    queries = plan.queries
    # 唯一明确书目/年版优先查找；多书或多版不猜对应、不扩大三词额度。
    if len(editions) == 1 and editions[0][0] != "UNRESOLVED":
        _, value, year = editions[0]
        term = f"{value} {year}年版"
        queries = ((term, "原问明确年版的公共引用；不是版本证明"),) + tuple(
            row for row in queries if row[0] != value and row[0] != term)
    return replace(plan, requested_editions=editions, queries=queries[:3],
                   omitted_query_count=plan.omitted_query_count + max(0, len(queries) - 3))


def resolve_query_region(query, *, project_region=None):
    """原问明确目标优先；地方主题可复用项目已登记的公开省市，不推定行政范围。

    只接受既有公开地名目录及市/省后缀。未识别的项目地点不能变成全国检索，
    也不能把项目名称、地址或模型猜测送入公共查询词。
    """
    query = requested_content(query)
    try:
        explicit = build_discovery_plan(query).region
    except ValueError:
        # 纯条号等本地阅读不一定有可外发公共词，不能因此阻断原读取入口。
        explicit = None
    # “当地/本市”只说明需要地方范围，不是另一个明确目的地。
    # 多个明确地名产生的歧义仍须补充，不能用项目城市替用户消解。
    if not isinstance(project_region, Mapping):
        return explicit
    if explicit is not None and (explicit != "地域待补充" or _target_region(query) is not None):
        return explicit
    for key in ("city", "province"):
        value = project_region.get(key)
        if isinstance(value, str):
            name = unicodedata.normalize("NFKC", value).strip().removesuffix("市").removesuffix("省")
            if not name:
                continue
            # 已登记城市是更具体的目标；不识别时先补充，不能退成省规范围。
            # 只有未填写城市才继续查看省份，不推定省市之间的适用关系。
            return name if name in REGION_NAMES else "地域待补充"
    return "地域待补充" if any(project_region.get(key) for key in ("city", "province")) else explicit
