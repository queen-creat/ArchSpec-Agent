"""从项目资料中识别设计主题，生成可解释的规范检索线索。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicRule:
    """一组容易解释的关键词规则，不调用大模型，也不会产生费用。"""

    key: str
    label: str
    preferred_standard_code: str
    search_query: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class DetectedTopic:
    key: str
    label: str
    preferred_standard_code: str
    search_query: str
    matched_terms: list[str]


_TOPIC_RULES = (
    TopicRule(
        "FIRE_SAFETY",
        "防火与疏散",
        "GB 55037-2022",
        "建筑防火 疏散 安全出口",
        ("安全出口", "疏散", "防火", "消防", "耐火", "避难"),
    ),
    TopicRule(
        "ACCESSIBILITY",
        "无障碍设计",
        "GB 55019-2021",
        "建筑无障碍",
        ("无障碍", "坡道", "轮椅", "盲道", "扶手"),
    ),
    TopicRule(
        "CIVIL_BUILDING",
        "民用建筑通用要求",
        "GB 55031-2022",
        "民用建筑",
        ("办公", "住宅", "商业", "旅馆", "卫生间", "层高"),
    ),
    TopicRule(
        "ENVIRONMENT",
        "建筑环境",
        "GB 55016-2021",
        "建筑环境",
        ("采光", "通风", "隔声", "噪声", "日照", "热工"),
    ),
    TopicRule(
        "ENERGY",
        "建筑节能",
        "GB 55015-2021",
        "建筑节能",
        ("节能", "保温", "可再生能源", "能耗"),
    ),
    TopicRule(
        "PLUMBING",
        "给水排水与节水",
        "GB 55020-2021",
        "给水排水 节水",
        ("给水", "排水", "节水", "雨水", "污水"),
    ),
    TopicRule(
        "ELECTRICAL",
        "建筑电气与智能化",
        "GB 55024-2022",
        "建筑电气 智能化",
        ("电气", "照明", "供电", "智能化", "弱电"),
    ),
)


class MaterialTopicDetector:
    """像目录员一样找关键词，只说明“为什么推荐”，不判断设计是否合规。"""

    def detect(self, text: str) -> list[DetectedTopic]:
        topics: list[DetectedTopic] = []
        for rule in _TOPIC_RULES:
            matched = [term for term in rule.terms if term in text]
            if matched:
                topics.append(
                    DetectedTopic(
                        key=rule.key,
                        label=rule.label,
                        preferred_standard_code=rule.preferred_standard_code,
                        search_query=rule.search_query,
                        matched_terms=matched,
                    )
                )
        return topics

