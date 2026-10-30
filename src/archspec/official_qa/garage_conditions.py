"""原表基础读数后的车库补问；记录用户条件，不推断规范值或工程适用性。"""

import re

from archspec.official_qa.condition_checks import (
    CONFIGURATION,
    NEGATIVE,
    condition_issues,
    context_issue,
)
from archspec.official_qa.condition_fields import group_questions
from archspec.official_qa.intents import fire_area_question

QUESTIONS = group_questions("garage", keys=("suppression_scope",))
FORMS = {"普通封闭式", "普通封闭式汽车库", "敞开式", "敞开式汽车库",
         "错层式", "错层式汽车库", "斜楼板式", "斜楼板式汽车库",
         "室内有车道且有人员停留的机械式汽车库",
         "室内有车道但无人员停留的机械式汽车库",
         "室内无车道但有人员停留的机械式汽车库",
         "室内无车道且无人员停留的机械式汽车库"}
STORAGE = {"普通车辆", "只停放普通车辆", "普通车辆（不存放危险物品）", "存放危险物品",
           "甲、乙类物品运输车", "甲乙类物品运输车"}
SCOPES = {"全部": "ALL", "全部覆盖": "ALL", "局部": "PARTIAL", "局部覆盖": "PARTIAL",
          "无": "NONE", "没有": "NONE", "没有设置": "NONE"}


def garage_query_conditions(query):
    """只取用户原句的明确字段；题名、否定、问句和多个候选不变成工程事实。"""
    if not fire_area_question(query):
        return {}
    parts = re.split(r"[，,；;。？?\n]", query)
    found = {}
    # 原有通用提取将‘汽车库’缩成‘车库’，会丢掉与修车库的区别。
    kinds = {kind for kind in ("汽车库", "修车库") if kind in query}
    if len(kinds) == 1:
        kind = next(iter(kinds))
        clauses = [part for part in parts if kind in part]
        if all(not re.search(r"未知|不清楚|不确定|未确定|(?:非|不是|并非|不属于)(?:汽车库|修车库)", part)
               for part in clauses) and "规范》" not in query and "《" not in query:
            found["building_use"] = kind
    for key, values in (("garage_form", FORMS), ("garage_storage", STORAGE)):
        values_found = {part.strip() for part in parts if part.strip() in values}
        if len(values_found) == 1:
            found[key] = next(iter(values_found))
    # 仅明确的‘全部覆盖/局部覆盖’赋值，不把‘应该全部’或‘是否全部’当实际配置。
    scopes = {match[1] for part in parts for match in [
        re.fullmatch(r"(?:设置自动灭火系统且|自动灭火系统)?(全部|局部)覆盖", part.strip())] if match}
    if len(scopes) == 1:
        found["suppression_scope"] = next(iter(scopes))
    return found


def garage_use_followup(conditions):
    """首轮与原文后的补问共用用途歧义判定，避免把能同时回答的条件拆成多轮。"""
    use = conditions.get("building_use", "")
    if "车库" in use and use not in {"汽车库", "修车库"}:
        return {"key": "building_use", "label": "本次查汽车库还是修车库？",
                "reason": "两种用途的面积条文不同，请说明具体用途。"}
    return None


def garage_initial_issues(conditions):
    """局部配置可先读原表，但仅在覆盖字段明确且原句也明确时通过。

    普通建筑旧条件门不变；这里既不删除原始条件，也不把局部配置归成全部覆盖。
    未知、矛盾或仅说‘局部可能设置’仍由原检查拒绝。
    """
    issues = condition_issues(conditions)
    question = garage_use_followup(conditions)
    if question is not None and issues and "building_use" not in issues:
        # 有其他必要补问时一起问用途；条件已齐仍保留先读原文再补问的原行为。
        # 保留原未知、否定及混合用途拒绝原因。
        issues = {"building_use": question["label"]+question["reason"], **issues}
    context = conditions.get("fire_context", "")
    if (conditions.get("building_use") == "汽车库"
            and SCOPES.get(conditions.get("suppression_scope", "")) == "PARTIAL"
            and any(word in context for word in ("局部", "部分"))):
        check_text = re.sub(r"局部|部分", "", context)
        if not context_issue(check_text):
            issues.pop("fire_context", None)
    return issues


def garage_followups(query, conditions):
    """已有原文后继续补问，不阻止先看基础表；未齐绝不宣布已获得最终面积。"""
    if not fire_area_question(query):
        return []
    use = conditions.get("building_use", "")
    question = garage_use_followup(conditions)
    if question is not None:
        return [question]
    if use != "汽车库":
        return []
    context = conditions.get("fire_context", "")
    kinds = set(re.findall(r"半地下|(?<!半)地下|单层|多层|高层", context))
    if (len(kinds) != 1 or re.search(r"(?:非|不是|并非|不属于)(?:单层|多层|高层|地下|半地下)", context)):
        return [{"key": "fire_context", "label": "是单层、多层、半地下、地下还是高层汽车库？同时保留灭火配置。",
                 "reason": "只有‘地上’不能定位原表类型列；多个类型请分开查询。"}]
    reasons = {}
    if conditions.get("garage_form", "") not in FORMS:
        reasons["garage_form"] = "请明确一种形式；机械式还需说明是否室内有车道且有人员停留，不能默认按普通车库处理。"
    if conditions.get("garage_storage", "") not in STORAGE:
        reasons["garage_storage"] = "是否存放危险物品尚未明确，不默认普通车辆。"
    elif conditions.get("garage_storage") == "存放危险物品":
        reasons["garage_storage"] = "请明确运输或存放物品类别；不能把所有危险物品当成甲、乙类物品运输车。"
    scope = SCOPES.get(conditions.get("suppression_scope", ""))
    states = set()
    for match in CONFIGURATION.finditer(context):
        token = match.group("before") or match.group("after")
        states.add("NONE" if re.fullmatch(NEGATIVE, token) else "PRESENT")
    if scope is None:
        reasons["suppression_scope"] = "设置了系统不等于整个防火分区都覆盖，请说明覆盖范围。"
    elif (states == {"NONE"} and scope != "NONE") or (states == {"PRESENT"} and scope == "NONE"):
        reasons["suppression_scope"] = "覆盖范围与已有的有无灭火系统描述矛盾，请核对。"
    return [{"key": key, "label": QUESTIONS[key], "reason": reason} for key, reason in reasons.items()]

