"""高度量法的人类条件入口；只记录明确字面，不推断设备性质、比例或适用结论。"""

import re

UNKNOWN = re.compile(r"未知|不清楚|不知道|不确定|未确定|未明确|未确认|不详|不明|未定|待定|待确认|待测")
ALTERNATIVE = re.compile(r"或|还是|抑或|二选一")
NEGATIVE = re.compile(r"(?:不是|并非|不按|不采用|不属于|不涉及|未处于|不在|没有|无|是否|是不是|非)(?:一个|的|采用|这种|是)?$")
OTHER_SUBJECT = re.compile(r"邻楼|邻居|隔壁|另一栋|其他建筑|别的建筑|消防车道|车道")
SEPARATORS = re.compile(r"[，,。；;！？!?\n]")
ROOF = re.compile(
    r"平屋[顶面](?:和|与|及|、)坡屋[顶面]混合|坡屋[顶面](?:和|与|及|、)平屋[顶面]混合"
    r"|混合屋[顶面]|平坡(?:混合)?屋[顶面]|[平坡]屋[顶面]")
PURPOSE = re.compile(r"消防|规划|一般民用|民用建筑|一般建筑|一般口径|建筑通用")
QUERY_PURPOSE = re.compile(
    r"(消防|规划|一般民用)(?:建筑)?高度|(消防|规划|一般民用)(?:的)?口径|按(消防|规划)(?:规范|要求)")
NO_CONTROL = re.compile(r"(?:无|没有|不涉及|未处于|不在)特殊(?:高度)?控制(?:区|范围|要求)")
CONTROL = re.compile(
    r"机场(?:净空)?(?:控制区|管制区|净空区)|机场净空(?:控制)?区|历史(?:文化)?(?:保护区|街区)"
    r"|风景名胜区|文物保护(?:区|范围)|特殊(?:高度)?控制区")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _parts(text):
    return [part.strip() for part in SEPARATORS.split(text) if part.strip()]


def _positive(part, start, end):
    """否定或疑问只作用于本字段token之前，不借另一句的肯定词覆盖。"""
    before = re.sub(r"\s+", "", part[:start])
    after = re.sub(r"\s+", "", part[end:])
    after = re.sub(r"^(?:建筑)?(?:高度|口径|的形式)", "", after, count=1)
    denied = re.match(r"^(?:不是|并非|不属于|不适用|不采用|未采用)", after)
    return not NEGATIVE.search(before) and not denied


def _roof_values(text):
    values = []
    for part in _parts(text):
        if UNKNOWN.search(part):
            continue
        for match in ROOF.finditer(part):
            if not _positive(part, match.start(), match.end()):
                continue
            literal = match[0]
            kind = "MIXED" if "混合" in literal or "平坡" in literal else (
                "FLAT" if literal.startswith("平") else "PITCHED")
            values.append((literal, kind))
    return values


def _purpose_values(text, *, query=False):
    values = []
    pattern = QUERY_PURPOSE if query else PURPOSE
    for part in _parts(text):
        if UNKNOWN.search(part) or OTHER_SUBJECT.search(part):
            continue
        for match in pattern.finditer(part):
            if not _positive(part, match.start(), match.end()):
                continue
            literal = next((value for value in match.groups() if value), match[0])
            kind = literal if literal in {"消防", "规划"} else "一般民用"
            values.append(kind)
    return values


def _control_values(text):
    values = []
    for part in _parts(text):
        if UNKNOWN.search(part):
            continue
        values.extend((match[0], "NONE") for match in NO_CONTROL.finditer(part))
        for match in CONTROL.finditer(part):
            if _positive(part, match.start(), match.end()):
                values.append((match[0], "SPECIAL"))
    return values


def height_query_conditions(original_query):
    """服务器只解析原问一次；保留屋面原字，裸机房/数字不能自动填设备两字段。"""
    text = _text(original_query)
    if not text:
        return {}
    parts = [part for part in _parts(text) if not OTHER_SUBJECT.search(part)]
    eligible = "，".join(parts)
    result = {}
    purposes = set(_purpose_values(eligible, query=True))
    if len(purposes) == 1:
        result["height_purpose"] = next(iter(purposes))
    roofs = _roof_values(eligible)
    if len({kind for _, kind in roofs}) == 1:
        result["roof_form"] = roofs[0][0]
    controls = list(dict.fromkeys(_control_values(eligible)))
    if len(controls) == 1:
        result["height_control_scope"] = controls[0][0]
    return result


def _basic_known(key, value):
    text = _text(value)
    if not text or UNKNOWN.search(text) or ALTERNATIVE.search(text):
        return False
    if key == "height_purpose":
        return len(set(_purpose_values(text))) == 1
    if key == "roof_form":
        return len({kind for _, kind in _roof_values(text)}) == 1
    controls = _control_values(text)
    return bool(controls) and len({kind for _, kind in controls}) == 1


def _description_known(value):
    text = _text(value)
    normalized = re.sub(r"\s+", "", text)
    return bool(text and not UNKNOWN.search(text) and not ALTERNATIVE.search(text)
                and not re.fullmatch(r"(?:屋顶|屋面|顶部)?的?机房", normalized)
                and not NEGATIVE.search(normalized)
                and not re.match(r"^(?:不是|并非|非|不属于|是否|是不是)", normalized))


def _area_known(value):
    text = _text(value)
    # 单位/占比说明仅确认用户确实给了面积描述；保留原字，不解析数字或计算比例。
    return bool(text and not UNKNOWN.search(text) and not ALTERNATIVE.search(text)
                and any(character.isnumeric() or character == "半" for character in text)
                and re.search(r"㎡|m²|m2|平方米|平方|%|％|百分|分之|面积", text))


def height_condition_known(key, value):
    """项目明确赋值和手填补问共享已知性判断，不计算面积或规范结论。"""
    if key in {"height_purpose", "roof_form", "height_control_scope"}:
        return _basic_known(key, value)
    if key == "roof_equipment_description":
        return _description_known(value)
    if key == "roof_equipment_area":
        return _area_known(value)
    return False


def height_condition_issues(conditions, *, equipment_rule_observed=False):
    """先基本三项，再按真实已读例外问设备；返回新dict，最多三项，不更改输入。"""
    issues = {}
    for key, reason in (
        ("height_purpose", "高度口径尚未明确或存在歧义，需要说明本次按哪种口径核对。"),
        ("roof_form", "屋面形式尚未明确、被否定或存在冲突，需要说明所问建筑的实际形式。"),
        ("height_control_scope", "特殊控制范围尚未明确；明确无特殊控制区也可作为回答。"),
    ):
        if not height_condition_known(key, conditions.get(key)):
            issues[key] = reason
    if issues or equipment_rule_observed is not True:
        return issues
    if not height_condition_known("roof_equipment_description", conditions.get("roof_equipment_description")):
        issues["roof_equipment_description"] = "需要说明屋面突出用房的实际用途和设备性质，机房名称本身不足。"
    if not height_condition_known("roof_equipment_area", conditions.get("roof_equipment_area")):
        issues["roof_equipment_area"] = "需要用户给出面积、单位或占屋面的原说明；不据此自动计算或判定不计入。"
    return issues
