"""公共建筑面积的有限补问；只记录条件，不计算最终允许面积。"""

import re

from archspec.official_qa.condition_checks import CONFIGURATION, NEGATIVE, context_issue
from archspec.official_qa.condition_fields import condition_prompt
from archspec.official_qa.garage_conditions import SCOPES, garage_initial_issues
from archspec.official_qa.intents import fire_area_question

NORMAL_SCOPE = "非木结构且无特殊要求的公共建筑"
EXCLUDED_SCOPES = {"木结构建筑", "有特殊要求的建筑", "附建于民用建筑中的汽车库"}


def public_use(conditions):
    """识别明确用途的常见后缀，原输入保留；不把多个用途合成一个类别。"""
    match = re.fullmatch(r"(办公|学校|医疗|文化|旅馆)(?:建筑)?", conditions.get("building_use", ""))
    return match[1] if match else None


def fire_initial_issues(conditions):
    """只有原句和覆盖字段都明确为局部时，允许先查原文基础值。"""
    issues = garage_initial_issues(conditions)
    context = conditions.get("fire_context", "")
    if (public_use(conditions) and SCOPES.get(conditions.get("suppression_scope", "")) == "PARTIAL"
            and any(word in context for word in ("局部", "部分"))
            and not context_issue(re.sub(r"局部|部分", "", context))):
        issues.pop("fire_context", None)
    return issues


def public_area_question(query, conditions):
    """这里只覆盖地上单、多层的明确公共用途；其他类型继续原流程。"""
    return bool(fire_area_question(query) and public_use(conditions)
                and any(word in query for word in ("单层", "多层"))
                and not any(word in query for word in ("高层", "地下", "不是", "未知", "不确定"))
                and "地上" in conditions.get("fire_context", "")
                and "地下" not in conditions.get("fire_context", ""))


def public_fire_followups(query, conditions):
    """保留已找到的出处，只补缺项；有系统不等于全部覆盖。"""
    if not public_area_question(query, conditions):
        return []
    scope = conditions.get("public_fire_scope", "")
    if scope in EXCLUDED_SCOPES:
        return []  # 已说明特殊范围；先停用一般数值卡，不重复收集一般建筑计算条件。
    questions = []
    if scope != NORMAL_SCOPE:
        questions.append({"key": "public_fire_scope", "label": condition_prompt("public_fire_scope", public=True),
                          "reason": "请先说明结构或专用用途，再按对应原文核对范围；不能默认一般公共建筑。"})
    states = {"NONE" if re.fullmatch(NEGATIVE, m.group("before") or m.group("after")) else "PRESENT"
              for m in CONFIGURATION.finditer(conditions.get("fire_context", ""))}
    coverage = SCOPES.get(conditions.get("suppression_scope", ""))
    reason = ""
    if ((states == {"NONE"} and coverage in {"ALL", "PARTIAL"})
            or (states == {"PRESENT"} and coverage == "NONE")):
        reason = "覆盖范围与有无灭火系统的描述矛盾，请核对。"
    elif states == {"PRESENT"} and coverage is None:
        reason = "有自动灭火系统仍需区分全部或局部覆盖，不能直接套用修正。"
    elif conditions.get("suppression_scope") and coverage is None:
        reason = "覆盖范围不明确，请选择全部、局部或没有设置。"
    if reason:
        questions.append({"key": "suppression_scope", "label": condition_prompt("suppression_scope", public=True),
                          "reason": reason})
    return questions


def public_scope_located(items):
    """范围结论必须出现在已选完整引用中，不凭用户标签套用另一书的范围。"""
    prefix = ("除有特殊要求的建筑、木结构建筑和附建于民用建筑中的汽车库外，"
              "其他公共建筑中")
    # 排除范围必须完整逐字出现；面积段后续的水印乱码不妨碍读这一范围，
    # 但也绝不因此认为后续等级或面积文字已经可靠。整条引用仍保持原样。
    return any(re.match(r"^\d+(?:\.\d+){2}" + re.escape(prefix),
                       re.sub(r"\s+", "", row["quote"])) for row in items)


def public_scope_note(conditions, items):
    """明确范围不展示一般数值卡；没有原文范围依据时只报告缺口。"""
    scope = conditions.get("public_fire_scope", "")
    if scope not in EXCLUDED_SCOPES:
        return ""
    if not public_scope_located(items):
        return f"已说明{scope}，当前摘录未定位对应范围或专用要求，请继续查对应原文。"
    return f"本次为{scope}，已选条文明确将其排除；保留原文供对照，需另查对应要求。"
