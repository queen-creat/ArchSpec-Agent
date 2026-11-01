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
from archspec.official_discovery.public_regions import (
    GUANGDONG_EXPLICIT_CITY_NAMES,
    REGION_NAMES,
    REGION_PARENTS,
)
from archspec.official_documents.local_policy import (
    REVIEWED_PUBLICATIONS,
    local_query_entries,
    region_matches,
)
from archspec.query_scope import requested_content

# 无公开词时只选择固定的公开综合文件候选；不截取原问或项目资料外发。
LOCAL_BUILDING_FALLBACK = "LOCAL_BUILDING_REFERENCE_V1"
_LOCAL_BUILDING_SUBJECT = re.compile(r"建筑|建设工程|用地|地块|住宅|房屋|楼宇")
_LOCAL_RULE_REQUEST = re.compile(r"规范|规定|标准|要求|限制|条件|控制|范围|规则|办法|条例|细则|多远|多少|几米|多大|多高|能否|能不能|可以|如何|怎样|怎么|查")
LOCAL_SCOPE_WORDS = ("地方标准", "地方规范", "地方规定", "本地", "本市", "本省", "当地", "某市", "某省")
MATCH_MESSAGES = {
    "TARGET": "公告主规范与本次公共目标匹配；正文、版本及适用性仍需分别核验",
    "ASSOCIATED": "这是其他主规范的关联公告，可能仅在引用或废止清单中提到目标；未自动准备其正文",
    "UNKNOWN": "公告主规范信息不完整，不能确认对应目标；保留线索，未自动准备正文",
}


def _compact(value):
    return re.sub(r"[\s《》〈〉（）()]+", "", unicodedata.normalize("NFKC", value))


# 标题中在目标地域之后仍出现显式下级行政名称时，不能以包含上级名称当作同一地域。
# 后接技术文书主题用于约束名称边界，不从名称推断省市隶属或法规适用性。
_ADMIN_SUFFIX = re.compile(r"^(?:特别行政区|(?:壮族|回族|维吾尔)?自治区|自治州|省|市|县|区|旗)")
_NESTED_ADMIN = re.compile(
    r"^(?:[\u4e00-\u9fff]{2,12}?(?:自治州|自治县|市)|[\u4e00-\u9fff]{1,12}?县)"
    r"(?=城乡|城市|城镇|建设|建筑|规划|国土|土地|住宅|日照|间距|绿色|工程|管理|技术)")


def _ambiguous_local_jurisdiction(subject, region):
    """仅按标题可见行政后缀排除下级/异地；无法确认时仍保留线索。"""
    position = subject.find(region)
    if position < 0:
        return False
    before = subject[:position]
    # 省名前缀与市名同时出现时，只接受现有目录或已审阅文件确认的组合。
    # 不推断未知省市隶属，也不因后续出现目标名覆盖前面的其他市县。
    names = "|".join(re.escape(name) for name in sorted(REGION_NAMES, key=len, reverse=True))
    province = re.fullmatch(rf"({names})(?:省|(?:壮族|回族|维吾尔)?自治区)", before)
    if province:
        pairs = {(entry.province, entry.city) for entry in REVIEWED_PUBLICATIONS.values() if entry.city}
        pairs.update((parent, city) for city, parent in REGION_PARENTS.items())
        if (province.group(1), region) not in pairs:
            return True
    elif before:
        # 标题还带“关于落实”等外层词时不是独立主文件；公告解析器会单独提取书名。
        return True
    tail = subject[position + len(region):]
    suffix = _ADMIN_SUFFIX.match(tail)
    if suffix:
        tail = tail[suffix.end():]
    # 兼容“某市某区”和“某地高新区”，也不把明确下级登记成整个上级。
    return bool(_NESTED_ADMIN.match(tail))


def _local_title_identity(title):
    """书名身份比较可去尾部括号年份；该年份不代表已核年版或现行性。"""
    identity = without_editions(title)
    return re.sub(r"[（(]\s*(?:19|20)\d{2}\s*年\s*[）)]\s*$", "", identity).strip()


def _local_primary_name_matches(title, region, names):
    """只比较已核本地前缀和完整主书名，不借尾部说明或下级名称。"""
    primary = _compact(_local_title_identity(title))
    for value in names:
        name = _compact(value)
        if not name or not primary.endswith(name):
            continue
        prefix = primary[:-len(name)]
        location = prefix.find(region)
        if location < 0:
            continue
        suffix = prefix[location + len(region):]
        if not suffix or _ADMIN_SUFFIX.fullmatch(suffix):
            return True
    return False


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


def procedural_notice_title(title):
    """明确行政办理/核发/核实程序不替代技术规范；不判断该文书法律效力。

    技术审查规定并非一次许可办理流程，不能仅凭“审查”二字排除。
    此角色只限制技术目标准入，原官方链接仍保留为关联线索。
    """
    subject = _compact(title)
    return bool(re.search(
        r"(?:(?:竣工(?:规划)?(?:条件)?|规划条件)(?:核实|核验|验收)"
        r"|(?:规划|施工)(?:许可证?|审批|备案)(?:申请|办理|核发|程序|流程))"
        r".{0,12}(?:办法|规定|细则|规则|规程|程序)(?:试行|暂行)?(?:的?通知)?$", subject))


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
        if any(kind == "LOCAL_FALLBACK" for kind, _ in self.targets):
            result["fallback"] = LOCAL_BUILDING_FALLBACK
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
        if (self.route in {"UNSUPPORTED_LOCAL", "YICHANG", "REGISTERED_LOCAL"}
                and self.region not in {None, "地域待补充"}
                and _ambiguous_local_jurisdiction(subject, self.region)):
            return "ASSOCIATED"
        if self.route == "UNSUPPORTED_LOCAL":
            # 旧路线名仅表示没有城市专用提供方；实际主文件可由政府搜索取得。
            # 地域必须出现在主文件题名；国家/行业编号不能因此变成地方规定。
            if self.region in {None, "地域待补充"} or self.region not in subject:
                return "UNKNOWN"
            if normalize_standard_code(standard_code) is not None:
                return "ASSOCIATED"
            if (non_rule_notice_title(title) or procedural_notice_title(title)
                    or re.search(r"解读|图解|问答|答疑|征求意见|草案|公示", subject)):
                return "ASSOCIATED"
            # 通用兜底只核已登记的综合文件身份；不把本地所有规划/建筑办法当目标。
            # 这是获取候选原件的身份判定，不证明该文件能回答原问题或适用于项目。
            fallback_titles = tuple(value for kind, value in self.targets if kind == "LOCAL_FALLBACK")
            if fallback_titles:
                return ("TARGET" if _local_primary_name_matches(title, self.region, fallback_titles)
                        else "ASSOCIATED")
            # 已登记的目标题名必须为主标题结尾；含题名的修正、附件或说明
            # 仍可关联查阅，不能靠子串成为同一本完整主文件。
            named = tuple(_compact(value) for kind, value in self.targets
                          if kind == "LOCAL_TOPIC" and _compact(value) in subject)
            if named:
                return "TARGET" if _local_primary_name_matches(title, self.region, named) else "ASSOCIATED"
            # 地方正式文件名称各异；共用文书类别，不逐城市添加题名特例。
            planning = any(value in LOCAL_PLANNING_TITLES for kind, value in self.targets)
            # 未登记的独立规范不再只凭文末有“办法/规定”或全文含“规划”判断；
            # 已核地域名称后的主标题必须直接以专业对象开头，不能跳过未知下级名称。
            local_title = subject[subject.find(self.region) + len(self.region):]
            marker = _ADMIN_SUFFIX.match(local_title)
            if marker:
                local_title = local_title[marker.end():]
            normative = bool(re.match(
                r"(?:城乡规划|城市规划|国土空间规划|规划|建筑|建设工程|日照|间距|国土|土地)"
                r".*?(?:规定|办法|细则|规则|条例)$"
                r"|(?:日照|间距).*(?:管理|计算|控制|技术要求|技术规定|标准|执行).*通知$", local_title))
            return "TARGET" if planning and normative else "ASSOCIATED"
        if self.route == "YICHANG":
            return "TARGET" if _compact(without_editions(title)).endswith(_compact(TARGET)) else "ASSOCIATED"
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
    # 新地名只接受显式“市”，或广东省级上下文后的项目/地点边界；
    # 不能把“中山大学/医院”中的机构名当作项目所在地。
    def region_pattern(name):
        escaped = re.escape(name)
        if name not in GUANGDONG_EXPLICIT_CITY_NAMES:
            return escaped
        end = r"(?=市|项目|地区|地点|所在地|[，,；;。！？?!\s]|$)"
        return rf"(?:{escaped}(?=市)|(?<=广东省){escaped}{end}|(?<=广东){escaped}{end})"

    pattern = "|".join(region_pattern(name) for name in sorted(REGION_NAMES, key=len, reverse=True))

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
        city_end = (r"(?=市|项目|地区|地点|所在地|[，,；;。！？?!\s]|$)"
                    if city in GUANGDONG_EXPLICIT_CITY_NAMES else "")
        chain_pattern = (rf"{re.escape(province)}(?:省)?[\s·-]{{0,3}}{re.escape(city)}{city_end}(?:市)?|"
                         rf"{re.escape(city)}{city_end}(?:市)?[\s·-]{{0,3}}{re.escape(province)}(?:省)?")
        chains.extend((match.start(), match.end(), (province, city))
                      for match in re.finditer(chain_pattern, outside))

    def target_names(mention):
        return next((names for start, end, names in chains
                     if start <= mention.start() and mention.end() <= end), (mention.group(),))

    # 参考资料的整个短语共用来源角色，包括省市连写和多个并列城市。
    reference_spans = []
    for source in re.finditer(r"(?:参考|参照|引用|只有|已有|现有|手头|借用|对照|根据|依据|按照|按)[^，,；;。！？?!\n]*", outside):
        boundary = re.search(r"用于|适用于|应用于|用在|针对|但|而(?:项目|工程)|帮我找|查询|查找|检索|想查|要查|查|找", source.group())
        if (re.match(r"(?:根据|依据|按照|按)", source.group())
                and not re.search(r"本地|本市|本省|当地(?!下)|项目|工程", outside)):
            continue
        reference_spans.append((source.start(), source.start() + boundary.start() if boundary else source.end()))

    destinations, projects, requested, unassigned = [], [], [], []
    # 用户明确要求查找书名号内的资料时，书名地名可以作为指定查询目标。
    for title in re.finditer(r"《([^》]*)》|〈([^〉]*)〉", query):
        before_title = query[max(0, title.start() - 16):title.start()]
        if re.search(r"(?:想查|查询|查找|检索|要查|查)\s*$", before_title):
            requested.extend(m.group() for m in re.finditer(pattern, title.group()))
    for mention in re.finditer(pattern, outside):
        after = outside[mention.end():mention.end() + 30]
        before = outside[max(0, mention.start() - 24):mention.start()]
        # 应用目的地与引用来源是不同角色，目的地不要求重复写“项目”。
        if re.search(r"(?:用于|适用于|应用于|用在|针对)\s*$", before):
            destinations.extend(target_names(mention))
        reference = any(start <= mention.start() < end for start, end in reference_spans)
        # “按照武汉市本地规范”明确指定本地城市；前面已有本市项目则是引用别地资料。
        if (reference and re.match(r"(?:市|省)?(?:本地|当地)(?:规范|规定|标准)", after)
                and not re.search(r"本地|本市|本省|当地(?!下)", before)):
            reference = False
        if not reference:
            unassigned.extend(target_names(mention))
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
    chosen = list(destinations + projects if destinations else projects or requested or unassigned)
    # “武汉与上海项目”中并列地域共同修饰项目，不能只保留后一地。
    for pair in re.finditer(rf"({pattern})(?:省|市)?\s*(?:和|与|及|、)\s*({pattern})", outside):
        if (not any(start <= pair.start() < end for start, end in reference_spans)
                and (pair.group(1) in chosen or pair.group(2) in chosen)):
            chosen.extend((pair.group(1), pair.group(2)))
    return unique_region(chosen) if chosen else None


def _explicit_unknown_location(query):
    """当前问题明确没有所在地，不能自动套用已保存的项目地点。"""
    return bool(re.search(r"(?:项目|工程)?(?:所在地|地点|位置)(?:是|为)?(?:未知|不详|未定|尚未确定|不确定)", query))


def _explicit_national_lookup(query):
    """用户主动查国家或行业来源，与把国标作为参考资料区分。"""
    return bool(re.search(r"(?:查找|查询|检索|查看|查|只查)\s*(?:国家|全国|国标|GB(?:/T)?\s*\d|JGJ\s*\d|CJJ\s*\d)", query, re.I)
                or re.match(r"\s*(?:请问|请)?(?:国家规范|国家标准|国标|全国)", query))


def _national_scope_requested(query):
    """明确国家范围或国家行业编号不应由项目登记城市隐式改写。"""
    codes = extract_public_codes(query)
    return bool(re.search(r"国家|全国|国标", query) or any(
        re.match(r"(?:GB|JGJ|CJJ|CECS)(?:/T)?\s*", code, re.I) for code in codes))



def _general_reference_candidates(query):
    """仅用已有明确原字查阅结构补固定公共候选，不从原问外发私有词。"""
    if "《" in query or "》" in query:
        return ()  # 未登记书目不能被默认通用规范悄悄代替。
    from archspec.official_discovery.public_scope_catalog import (
        PUBLIC_SECTION_CANDIDATES,
        section_subjects,
    )
    # No directory object means no candidate; do not invoke another planner for
    # unrelated established topics (e.g. garage fire compartments).
    if not any(term in query for _title, headings, _url, _sha, _pages in PUBLIC_SECTION_CANDIDATES
               for term in section_subjects(headings)):
        return ()
    from archspec.official_qa.question_plan import LocalPlanner

    proposal = LocalPlanner().propose(query, {})
    if (proposal["topic"] == "UNKNOWN" and proposal["understanding"] == "CLEAR"
            and proposal["operation"] == "REFERENCE_LOOKUP" and proposal.get("query_focus")):
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
    target_region = _target_region(query)
    region = target_region or region
    # “当地下室……”中的“当”引出条件，“地下室”是建筑部位；
    # 不能在复合词内部截出地域代词“当地”，从而把用户已选的全国规范全过滤掉。
    local_scope = (any(word in query for word in LOCAL_SCOPE_WORDS if word != "当地")
                   or re.search(r"当地(?!下)", query) is not None)
    deictic_scope = bool(re.search(r"本地|本市|本省|某市|某省|当地(?!下)", query))
    # “已有某地地方规范，查国家规定”中的地方仅为来源，不是查找范围。
    national_lookup = _explicit_national_lookup(query)
    requested_scope = re.sub(r"(?:国家标准|国家规范|国标)(?:可|仅供|供|作为)?参考", "", query)
    requested_scope = re.sub(r"(?:参考|参照|引用|只有|已有|现有|手头|借用|对照)[^，,；;。！？?!\n]*", "", requested_scope)
    explicitly_local = bool(re.search(r"当地(?!下)|本地|本市|地方", requested_scope))
    if explicitly_local and _national_scope_requested(requested_scope):
        # 单地域计划无法同时表达两类来源；保留城市并要求分开查询。
        known_region = region if region != "地域待补充" else None
        return DiscoveryPlan("SPLIT_SCOPE_REQUIRED", known_region, (), (), 0)
    elif national_lookup:
        # 同时要求地方与国家时单地域合同无法表达，不能静默丢掉其一。
        region = "地域待补充" if explicitly_local else None
        local_scope = explicitly_local
    elif target_region is None and _national_scope_requested(query) and not deictic_scope:
        region = None
        local_scope = False
    elif target_region is None and _explicit_unknown_location(query):
        region = "地域待补充"
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
        # “间距离”中的“间”属于前一名词或方位，“距离”是独立词；
        # 不跨词把它截成“间距”，例如“卫生间距离”或“房间距离”。
        spacing = bool(re.search(r"间距(?!离)", query))
        if region != "地域待补充" and not extract_public_codes(query) and (spacing or any(
                word in query for word in ("日照", "规划", "退让", "容积率", "建筑密度"))):
            # 三次预算内保留综合文件检索，并用一条固定公共主题寻找专项规定；不发送原问题。
            sunlight = "日照" in query or spacing
            # 日照问题保留综合规定、专项管理、间距主题三路，避免泛规划题名重复占预算。
            terms=(LOCAL_PLANNING_QUERY,LOCAL_SUNLIGHT_TITLES[0] if sunlight else LOCAL_PLANNING_TITLES[3],
                   LOCAL_SUNLIGHT_QUERY if sunlight else LOCAL_PLANNING_TITLES[1])
            queries = [{"term": title, "reason": "地方规划公开题名或主题；地域单独限定"}
                       for title in terms]
            targets=(*LOCAL_PLANNING_TITLES,*(LOCAL_SUNLIGHT_TITLES if sunlight else ()))
            omitted = 0
        else:
            # 已核公开题名是共同计划的候选提示，地方路线不能将它静默丢弃。
            # 只在原问未形成公共查询时补入；不覆盖编号/已识别主题或推定地域。
            if not queries and public_titles and region != "地域待补充":
                queries = [{"term": title, "reason": "已核公开目录的地方候选书名；不是用户选书或正文证明"}
                           for title in public_titles]
            if (not queries and region != "地域待补充" and _LOCAL_BUILDING_SUBJECT.search(query)
                    and _LOCAL_RULE_REQUEST.search(query)):
                # 无法识别具体主题时检索固定公共规划候选，不把用户文本切片传给政府网站。
                # 条目必须匹配综合文件名称；对问题的相关性仍由原有检索/证据流程判定。
                return DiscoveryPlan("UNSUPPORTED_LOCAL", region,
                    tuple(("LOCAL_FALLBACK", title) for title in LOCAL_PLANNING_TITLES),
                    tuple((term, "已知地区建筑问题的公开综合文件候选；并非答案")
                          for term in (LOCAL_PLANNING_QUERY, LOCAL_PLANNING_TITLES[1])), 0)
            targets=tuple(item["term"] for item in queries)
        return DiscoveryPlan("UNSUPPORTED_LOCAL", region,
                             tuple(("LOCAL_TOPIC", title) for title in targets),
                             tuple((item["term"], item["reason"]) for item in queries), omitted)
    hinted = bool(public_titles and not extract_public_codes(query) and not extract_public_titles(query))
    directory_fallback = False
    if hinted:
        queries = [{"term": title, "reason": "已核公开目录的候选书名；不是用户选书或规范依据"}
                   for title in public_titles]
        # Keep raw-query public topics as complementary clues, while project-only
        # labels cannot displace or widen a specific registered book suggestion.
        try:
            fallback, omitted = plan_queries(query, ())
        except ValueError:
            fallback, omitted = [], 0
        seen = {item["term"] for item in queries}
        combined = [*queries, *(item for item in fallback if item["term"] not in seen)]
        omitted += max(0, len(combined) - 3)
        queries = combined[:3]
    else:
        try:
            queries, omitted = plan_queries(query, topics)
        except ValueError:
            candidates = _general_reference_candidates(query)
            if not candidates:
                raise
            queries, omitted = list(candidates[:3]), max(0, len(candidates) - 3)
            directory_fallback = True
    # Specialist topics and verified directory scopes are complementary discovery clues.
    # Preserve explicit books/codes and model handoff; never expand the three-query cap.
    if (not directory_fallback and not extract_public_codes(query)
            and not extract_public_titles(query) and not public_titles):
        seen = {item["term"] for item in queries}
        additions = [item for item in _general_reference_candidates(query) if item["term"] not in seen]
        combined = [*queries, *additions]
        omitted += max(0, len(combined) - 3)
        queries = combined[:3]
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
    if plan.route == "SPLIT_SCOPE_REQUIRED":
        return plan
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
    # 原问明确所在地未知时，不用保存的项目地点代替用户当前输入。
    if _explicit_national_lookup(query):
        return explicit
    if _target_region(query) is None and _explicit_unknown_location(query):
        return "地域待补充"
    # “当地/本市”只说明需要地方范围，不是另一个明确目的地。
    # 多个明确地名产生的歧义仍须补充，不能用项目城市替用户消解。
    if (explicit is None and _national_scope_requested(query)
            and _target_region(query) is None):
        return None
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
