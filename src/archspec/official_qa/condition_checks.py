"""检查设计条件是否单一明确，不推算等级，也不判断规范适用性。"""

import re

UNKNOWN = ("未知", "不知道", "不清楚", "不确定", "未确定", "未明确", "暂定", "待定", "随便", "不限")
USES = ("办公", "住宅", "商业", "厂房", "仓库", "车库", "公共建筑", "学校", "医疗", "文化", "旅馆")
DEVICE = r"(?:自动喷水灭火|自动灭火|喷淋)(?:系统)?"
# 否定长词放前面；同一次匹配吃掉“未设置”，不能再从其中取出“设置”。
NEGATIVE = r"(?:没有设置|未设置|不设置|未安装|不安装|未设|不设|没有|无)"
POSITIVE = r"(?:已设置|设置|设有|已安装|安装|已设|有|设)"
STATE = rf"(?:{NEGATIVE}|{POSITIVE})"
CONFIGURATION = re.compile(rf"(?P<before>{STATE})\s*{DEVICE}|{DEVICE}\s*(?P<after>{STATE})")


def use_issue(value):
    """常用用途只取一个明确类别；不把‘非住宅’简化成‘住宅’。"""
    if not value:
        return "还没有填写建筑用途。"
    if any(word in value for word in UNKNOWN):
        return "建筑用途还未确定，请选定本次查询的用途。"
    matches = {word for word in USES if word in value}
    if len(matches) > 1:
        return "填写了多个用途，请分别查询或确定本次查询哪一种。"
    if not matches or re.search(rf"(?:非|不是|不属于|不作|并非)\s*(?:{'|'.join(USES)})", value):
        return "请写明具体建筑用途，否定某用途不能确定实际用途。"
    return ""


def grade_issue(value):
    """只接受一个明确等级，范围或比较条件不能自动挑第一个等级。"""
    if not value:
        return "还没有填写耐火等级。"
    if any(word in value for word in UNKNOWN):
        return "耐火等级还未确定，请确认后再查询。"
    if re.search(r"(?:或|、|和|至|到|不低于|至少|以上|以下)", value):
        return "填写了多个等级或等级范围，请确定本次查询哪一个。"
    # 核对备注不是另一个等级；保留原输入传给整理器，不将备注变成专业确认。
    normalized = value.replace("（待人工核对）", "").replace("(待人工核对)", "").strip()
    if not re.fullmatch(r"(?:耐火等级\s*(?:为|是)?\s*)?[一二三四1234]级", normalized):
        return "请填写单一明确等级，例如一级、二级；暂不解释其他等级表达。"
    return ""


def context_issue(value):
    """层位及灭火配置分别明确；未知、局部配置或相反描述需要用户说明。"""
    if not value:
        return "请同时说明地上或地下，以及有无自动灭火系统。"
    if any(word in value for word in (*UNKNOWN, "有无", "是否", "部分", "局部", "拟设", "应设", "无需")):
        return "层位或灭火配置未明确，请说明本次查询部位的实际配置。"
    if len({word for word in ("地上", "地下") if word in value}) != 1:
        return "请确定本次查地上还是地下；不同部位请分别查询。"
    states = set()
    for match in CONFIGURATION.finditer(value):
        token = match.group("before") or match.group("after")
        states.add("ABSENT" if re.fullmatch(NEGATIVE, token) else "PRESENT")
    if len(states) > 1:
        return "灭火配置同时写了有和没有，请确认本次查询部位的配置。"
    if not states:
        return "请明确有或没有自动灭火系统；自动门等不是灭火配置。"
    return ""


def condition_issues(conditions):
    """返回有问题的字段，不让已明确的其他字段被重复追问。"""
    checks = {"building_use": use_issue, "fire_resistance": grade_issue, "fire_context": context_issue}
    return {key: reason for key, check in checks.items() if (reason := check(conditions.get(key, "")))}


def query_conditions(query):
    """保留各条件所在短句中的否定和不确定词，绝不截成肯定事实。"""
    conditions = {}
    uses = {word for word in USES if word in query}
    segments = re.split(r"[，,；;。？?\n]", query)
    if len(uses) == 1:
        word = next(iter(uses))
        clauses = [segment for segment in segments if word in segment]
        if all(not use_issue(segment) for segment in clauses):
            conditions["building_use"] = word
    # 保留等级原短句供追问解释；不能只截第一个等级，也不能把范围冒充“未填”。
    grades = re.findall(r"[一二三四1234]级", query)
    clauses = [segment for segment in segments if "耐火等级" in segment]
    if clauses:
        raw = "；".join(re.sub(r"^.*?耐火等级\s*(?:为|是)?\s*", "", clause).strip() for clause in clauses)
        if len(set(grades)) > 1 and not grade_issue(raw):
            raw = "或".join(dict.fromkeys(grades))
        # 只问“耐火等级”并未填写等级；空值保持未提供，让普通查阅继续。
        # 范围、否定和不确定的非空原句仍保留给原条件检查，不能改成肯定事实。
        if raw:
            conditions["fire_resistance"] = raw
    # 分字段检查：等级“暂定”不应让另一短句明确的灭火配置也被重复追问。
    context = "，".join(segment for segment in segments if
                       any(word in segment for word in ("地上", "地下")) or re.search(DEVICE, segment))
    if context:
        conditions["fire_context"] = context
    return conditions
