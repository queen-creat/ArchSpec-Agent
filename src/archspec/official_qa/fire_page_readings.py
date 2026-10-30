"""有限的公共建筑面积原页读数；不把 AI 核读变成项目面积或专业审核。"""

import re

from archspec.official_qa.condition_checks import CONFIGURATION, NEGATIVE
from archspec.official_qa.intents import fire_area_question
from archspec.official_qa.page_transcriptions import load_transcription
from archspec.official_qa.public_fire_conditions import (
    EXCLUDED_SCOPES,
    fire_initial_issues,
    public_use,
)

VALUE = r"(?P<{name}>[1-9]\d{{0,5}}(?:\.\d{{1,2}})?)m²"
PATTERN = re.compile(
    r"(?P<scope>\d+(?:\.\d+){2}除有特殊要求的建筑、木结构建筑和附建于民用建筑中的汽车库外，"
    r"其他公共建筑中每个防火分区的最大允许建筑面积应符合下列规定：)"
    + r"1对于高层建筑，不应大于" + VALUE.format(name="high") + r"。"
    + r"2(?P<grade12>对于一、二级耐火等级的单、多层建筑，不应大于"
    + VALUE.format(name="area12") + r"；)"
    + r"(?P<grade3>对于三级耐火等级的单、多层建筑，不应大于"
    + VALUE.format(name="area3") + r"；)"
    + r"(?P<grade4>对于四级耐火等级的单、多层建筑，不应大于"
    + VALUE.format(name="area4") + r"。)"
    + r"3对于地下设备房，不应大于" + VALUE.format(name="equipment")
    + r"；对于地下其他区域，不应大于" + VALUE.format(name="underground") + r"。"
    + r"4(?P<modifier>当防火分区全部设置自动灭火系统时，上述面积可以增加1\.0倍；"
    r"当局部设置自动灭火系统时，可按该局部区域建筑面积的1/2计入所在防火分区的总建筑面积。)"
)


def build_page_readings(documents, query, conditions, evidence):
    """只为已选证据候选准备补充；服务在最终核对后再按模型选择过滤。

    解析完整结构而不是搜索邻近数字。未覆盖结构和用途继续原文阅读；所有
    数值取自对应记录全文，不硬编码面积，不计算全部或局部灭火修正。
    """
    if (not fire_area_question(query) or fire_initial_issues(conditions)
            or not public_use(conditions)
            or conditions.get("public_fire_scope") in EXCLUDED_SCOPES
            or not any(word in query for word in ("单层", "多层"))
            or any(word in query for word in ("高层", "地下", "不是", "未知", "不确定"))
            or "地下" in conditions["fire_context"]):
        return []
    keys = {}
    for row in evidence:
        if row["role"] != "RELATED_CONTEXT":
            key = tuple(row[field] for field in ("source_id", "snapshot_id", "clause_number"))
            keys.setdefault(key, []).append(row)
    readings = []
    grade = re.search(r"([一二三四1234])级", conditions["fire_resistance"])[1]
    branch = {"一": "12", "二": "12", "1": "12", "2": "12", "三": "3", "3": "3", "四": "4", "4": "4"}[grade]
    for parts in keys.values():
        # 本版本不凭单页记录补齐跨页条文；多片段仍走既有续页阅读。
        if len(parts) != 1 or not parts[0].get("quote_ready"):
            continue
        row = parts[0]
        if row["page_start"] != row["page_end"]:
            continue
        record = load_transcription(documents, row)
        if record is None:
            continue
        compact = re.sub(r"\s+", "", record["transcription"])
        match = PATTERN.fullmatch(compact)
        if not match or not compact.startswith(row["clause_number"] + "除"):
            continue
        context = conditions["fire_context"]
        # 与条件门共享前后置否定语义，不能把“系统未安装”标成有系统。
        tokens = [m.group("before") or m.group("after") for m in CONFIGURATION.finditer(context)]
        absent = all(re.fullmatch(NEGATIVE, token) for token in tokens)
        readings.append({**record, "baseline_value": match[f"area{branch}"], "unit": "m²",
            "scope_text": match["scope"], "condition_text": match[f"grade{branch}"],
            "modifier_text": match["modifier"], "conditions": dict(conditions),
            "extinguishing_state": "ABSENT" if absent else "PRESENT_SCOPE_UNCONFIRMED",
            "is_project_value": False, "reading_kind": "PUBLIC_MULTISTOREY_BASELINE"})
    return readings
