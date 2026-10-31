"""公开查询词的唯一登记与规划；不发送原问题、工程名称或私有主题标签。"""

import re

from archspec.official_discovery.public_title_catalog import PUBLIC_CATALOG_TITLES
from archspec.official_qa.intents import (
    fire_area_question,
    height_measurement_question,
    highrise_definition_question,
    net_height_question,
)

# 这些是检索词扩展规则，不是“已找到的规范目录”，不保证主题覆盖齐全。
TOPICS = {
    "WATERPROOFING": ("建筑与市政工程防水通用规范", ("防水",)),
    "FIRE_SAFETY": ("建筑防火", ("消防", "防火", "疏散", "安全出口")),
    # 坡道也可能用于车辆等其它场景；只有明确无障碍语义才选择此公共专业主题。
    "ACCESSIBILITY": ("无障碍通用规范", ("无障碍", "轮椅", "盲道")),
    "GARAGE": ("车库", ("车库", "停车", "汽车库")),
    "OFFICE": ("办公建筑", ("办公",)),
    "CIVIL_BUILDING": ("民用建筑", ("民用", "建筑高度", "高层建筑", "高层")),
    "ENVIRONMENT": ("建筑环境", ("通风", "采光", "日照", "噪声", "隔声")),
    "ENERGY": ("建筑节能", ("节能", "保温", "能耗")),
    "PLUMBING": ("给水排水", ("排水", "给水", "节水")),
    "ELECTRICAL": ("建筑电气", ("电气", "照明", "智能化")),
}
# 这里只保留可公开发送的题名，不接受《工程内部名称》作为自动查询词。
LOCAL_PLANNING_TITLES = ("城市规划管理技术规定", "城乡规划管理技术规定", "建设工程规划管理技术规定", "国土空间规划管理技术规定")
# 专项规则和综合规划规定都是可能的正式出处；搜索词与来源题名约束分别登记。
LOCAL_PLANNING_QUERY = "规划管理 技术规定"
LOCAL_SUNLIGHT_QUERY = "建筑日照 间距 规定"
LOCAL_SUNLIGHT_TITLES = ("日照分析管理规定", "日照分析技术管理规则", "日照分析管理办法",
                         "日照分析技术规定", "建筑间距管理规定")
PUBLIC_TITLES = (*LOCAL_PLANNING_TITLES, *LOCAL_SUNLIGHT_TITLES,
    "汽车库、修车库、停车场设计防火规范", "建筑设计防火规范",
    "建筑防火通用规范", "无障碍设计规范", "建筑与市政工程无障碍通用规范",
    "民用建筑通用规范", "民用建筑设计统一标准", "办公建筑设计标准",
    "车库建筑设计规范", "建筑环境通用规范", "民用建筑热工设计规范",
    "建筑采光设计标准", "建筑日照计算参数标准", "建筑给水排水设计标准",
    "建筑电气与智能化通用规范", "建筑节能与可再生能源利用通用规范",
    "住宅项目规范", "住宅设计规范", "消防设施通用规范",
)
# 已登记地方/旧题名保留，出版目录扩展只增加公开名称，不推定编号与版本。
PUBLIC_TITLES = tuple(dict.fromkeys((*PUBLIC_TITLES, *PUBLIC_CATALOG_TITLES)))
# 排版连字符只表示已明确写出的编号分隔符；残缺年份不能退回无年份编号。
CODE_YEAR_SEPARATOR = r"[-‐‑‒–—－]"
PUBLIC_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>GB|JGJ|CJJ)\s*(?P<t>/\s*T)?\s*"
    r"(?P<number>\d{2,6}(?:\.\d{1,2})?)(?:\s*" + CODE_YEAR_SEPARATOR
    + r"\s*(?P<year>\d{4}))?(?![A-Za-z0-9.]|" + CODE_YEAR_SEPARATOR + r")",
    re.IGNORECASE,
)


def validated_public_titles(values):
    """外部只可交接最多三个已登记名称，不接受任意检索词或私有标签。"""
    if (not isinstance(values, (list, tuple)) or len(values) > 3
            or any(not isinstance(value, str) or value not in PUBLIC_TITLES for value in values)):
        raise ValueError("补查题名须为最多三个已登记的公开规范名称")
    return tuple(dict.fromkeys(values))


def normalize_query(query: str) -> str:
    """仅供本机检索与理解的同义词归一化；结果不是获准外发的公共查询词。"""
    text = query.strip()
    text = re.sub(r"\s+", " ", text)
    return text.replace("消防分区", "防火分区")


def normalize_standard_code(value) -> str | None:
    """规范编号唯一归一化规则；不从普通数字、项目代号中构造编号。"""
    if not isinstance(value, str):
        return None
    match = PUBLIC_CODE.fullmatch(value.strip())
    if not match:
        return None
    code = f"{match['prefix'].upper()}{'/T' if match['t'] else ''} {match['number']}"
    return code + (f"-{match['year']}" if match["year"] else "")


def extract_public_codes(query: str) -> tuple[str, ...]:
    """只抽取既有公共编号规则；不返回原问题或内部说明。"""
    return tuple(dict.fromkeys(normalize_standard_code(match.group())
                               for match in PUBLIC_CODE.finditer(query)))


def extract_public_titles(query: str) -> tuple[str, ...]:
    """只返回服务端登记题名，任意书名或内部资料标题不成为外发词。"""
    matches = []
    for title in PUBLIC_TITLES:
        start = query.find(title)
        while start >= 0:
            matches.append((title, start, start + len(title)))
            start = query.find(title, start + 1)
    # “坡屋面工程技术规范”内嵌“屋面工程技术规范”不代表用户另选一本书。
    # 若短题名在别处独立出现仍保留，不能按全局字符串包含直接删除它。
    visible = {title for title, start, end in matches
               if not any(left <= start and end <= right and right - left > end - start
                          for _other, left, right in matches)}
    return tuple(title for title in PUBLIC_TITLES if title in visible)


def plan_queries(query: str, topics: list) -> tuple[list[dict], int]:
    query = query.strip()
    if len(query) > 500:
        raise ValueError("问题最长为 500 个字")
    proposed = []
    if query:
        # 编号只截取公开部分，尾随的工程代号、联系方式或任务书不进入官网URL。
        for term in extract_public_codes(query):
            proposed.append({"term": term, "reason": "仅使用公开规范编号"})
        # 编号检索可能只命中关联公告或零条；保留用户已明确给出的公共题名退路。
        # 精确目标仍由共同计划里的编号限定，补查词不改变规范身份或版本。
        for title in extract_public_titles(query):
            proposed.append({"term": title, "reason": "仅使用登记的公共规范题名"})
        if extract_public_codes(query):
            # 车库专项词优先于共用的“防火”，仍只补公开主题、不更换精确目标。
            fallback_topics = ((TOPICS["GARAGE"], *TOPICS.values())
                               if any(word in query for word in TOPICS["GARAGE"][1]) else TOPICS.values())
            for term, keywords in fallback_topics:
                if any(word in query for word in keywords):
                    proposed.append({"term": term, "reason": "编号未取得正文时的公共主题补查"})
                    break
        if not proposed:
            # 自然问题只扩展为已登记的公共题名候选，不确认现行性或项目适用性。
            # 与问答层共用口语意图，避免回答认识“多大”，联网却只查宽主题。
            highrise = highrise_definition_question(query)
            if (highrise or height_measurement_question(query) or
                    "高度" in query and any(word in query for word in ("计量", "测量", "计算", "丈量"))):
                height_reason = "高层定义" if highrise else "高度计量"
                proposed.extend({"term": title, "reason": f"{height_reason}的公共题名候选；适用性待核验"}
                                for title in ("民用建筑通用规范", "民用建筑设计统一标准"))
            if fire_area_question(query):
                garage = any(word in query for word in TOPICS["GARAGE"][1])
                titles = ("汽车库、修车库、停车场设计防火规范", "车库建筑设计规范", "建筑防火通用规范") if garage else (
                    "建筑防火通用规范", "建筑设计防火规范")
                reason = "车库防火分区面积" if garage else "非车库防火分区面积"
                proposed.extend({"term": title, "reason": f"{reason}的公共题名候选；适用性待核验"}
                                for title in titles)
            if (net_height_question(query) and any(word in query for word in ("住宅", "住房"))):
                # 住宅条件必须在用户问法中明确；普通房间净高不能猜成住宅。
                proposed.extend({"term": title, "reason": "住宅净高的公共题名候选；版本和适用性待核验"}
                                for title in ("住宅项目规范", "住宅设计规范"))
            if "采光" in query and any(word in query for word in ("系数", "标准值")):
                proposed.append({"term": "建筑采光设计标准",
                                 "reason": "采光标准值的公共题名候选；原表和适用条件待核验"})
        if not proposed:
            for term, keywords in TOPICS.values():
                if any(word in query for word in keywords):
                    proposed.append({"term": term, "reason": "问题包含该主题的关键词"})
    for topic in topics:
        rule = TOPICS.get(topic.get("key"))
        if rule:
            # label可能来自工程资料，只采用服务端固定key对应的公共词。
            proposed.append({"term": rule[0], "reason": f"项目公共主题：{rule[0]}"})
    unique = []
    for candidate in proposed:
        if not any(old["term"] == candidate["term"] for old in unique):
            unique.append(candidate)
    if not unique:
        raise ValueError("未识别公共规范编号、已登记题名或主题；请补充规范编号或防火、车库等主题，不会原样发送问题")
    return unique[:3], max(0, len(unique) - 3)

