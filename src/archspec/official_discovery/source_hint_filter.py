"""根据未修改的原问过滤模型建议的公开题名。

这里只删除可选的模型建议，不判断明确提及的来源是否为目标，也不修改原问。
"""
from archspec.official_discovery.public_queries import validated_public_titles
from archspec.query_scope import requested_content


def filter_public_title_hints(raw_query, public_titles):
    """按有限的来源交接合同返回通过验证的可选题名。

    既有取舍规则删减原问时，不推定题名、编号或别名属于同一来源，
    直接丢弃全部可选建议。其他情况下，去除原问逐字出现的题名；
    这些题名由显式来源路径处理，不由模型建议路径提升为目标。
    这一过滤不解决任意排除措辞的语义识别。
    """
    hints = validated_public_titles(public_titles)
    if not isinstance(raw_query, str):
        raise ValueError("原始问题必须为文本")
    if requested_content(raw_query) != raw_query:
        return ()
    return tuple(title for title in hints if title not in raw_query)


def active_public_title_hints(plan, raw_query, public_titles):
    """只让本轮实际搜索和目标共同采纳的建议影响有限读取。"""
    hints = filter_public_title_hints(raw_query, public_titles)
    if plan is None or plan.explicit_targets(raw_query):
        return ()
    terms = {term for term, _ in plan.queries}
    targets = {value for kind, value in plan.targets if kind in {"TITLE", "LOCAL_TOPIC"}}
    return tuple(title for title in hints if title in terms and title in targets)
