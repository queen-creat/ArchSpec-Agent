"""只排除明确用途或主题冲突，不推断耐火等级、数字或规范适用性。"""

import re

FAMILIES = {
    "PUBLIC": ("公共建筑", "办公", "商业", "学校", "医疗", "文化", "旅馆",
               "商店营业厅", "商场", "购物中心"),
    "RESIDENTIAL": ("住宅",),
    "FACTORY": ("厂房",),
    "WAREHOUSE": ("仓库",),
    "GARAGE": ("车库",),
}


def _family(text):
    found = {family for family, words in FAMILIES.items()
             if any(word in text for word in words)}
    return next(iter(found)) if len(found) == 1 else None


def _garage_kind(text):
    """只辨认明确单一车库用途；通用、并列、否定或未知表述不猜类别。"""
    normalized = re.sub(r"\s+", "", text)
    if any(word in normalized for word in ("未知", "不清楚", "不知道", "不确定", "未确定")):
        return None
    if re.search(r"(?:非|不是)(?:汽车库|修车库)", normalized):
        return None
    kinds = {word for word in ("汽车库", "修车库") if word in normalized}
    return next(iter(kinds)) if len(kinds) == 1 else None


def smoke_area_only(text):
    """主句仅规定防烟面积时，不能因后文提到防火分区而用于防火面积回答。

    只排除明确主语；并列主题、未知表述和防火条文中的防烟关联仍保留。
    完整条文先合并再调用，避免将跨页的后半句误当作独立主语。
    """
    normalized = re.sub(r"\s+", "", text)
    introduction = re.split(r"[。；;：:]|不宜|不应|不得|应|宜", normalized, maxsplit=1)[0][:180]
    return ("防烟分区" in introduction and "面积" in introduction
            and "防火分区" not in introduction)


def fire_use_conflict(use, text):
    """仅看条文主句的明确用途；例外、邻接条文不能冒充主语。

    这是回答筛选，不是专业适用性判定。混合范围或无法辨认的主语不猜分类，
    原文仍可阅读；完整引用另由原有固定证据检查保证。
    """
    expected = _family(use)
    if expected is None:
        return False
    normalized = re.sub(r"\s+", "", text)
    # ‘除……汽车库外，其他公共建筑……’应看后面的公共建筑主语；
    # 只在条文开头处理这一明确句型，正文后部的例外不能改变整条分类。
    excluded = normalized.find("除")
    if 0 <= excluded < 40:
        boundary = re.search(r"外[，,]", normalized[excluded:220])
        if boundary:
            normalized = normalized[excluded + boundary.end():]
    # 不扫完整正文里的表注、引用和并列要求，避免把邻接用途当成主语。
    introduction = re.split(r"[。；;：:]|应符合|应满足|应按|不应|不得", normalized, maxsplit=1)[0][:180]
    actual = _family(introduction)
    if actual is not None and actual != expected:
        return True
    # 汽车库和修车库同属车库，但专用面积条文不能互相代答。
    # 仅看已确定的主句，不能把正文后面的引用或共同范围误判为冲突。
    if actual == expected == "GARAGE":
        wanted, subject = _garage_kind(use), _garage_kind(introduction)
        if wanted is not None and subject is not None and wanted != subject:
            return True
    # 都属于公共建筑并不等于用途相同。只排除明确的零售专用主句；
    # 混合用途、后部邻接说明和‘除商店外’不能当成零售专用范围。
    other_public_uses = ("办公", "学校", "医疗", "文化", "旅馆")
    return (actual == expected == "PUBLIC"
            and any(word in use for word in other_public_uses)
            and any(word in introduction for word in ("商店营业厅", "商场", "购物中心"))
            and "公共建筑" not in introduction
            and not any(word in introduction for word in other_public_uses))
