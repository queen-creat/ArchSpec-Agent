"""采光具体取值的必要条件；不推断等级、城市气候区或原表数字。"""

import re

from archspec.official_qa.condition_checks import UNKNOWN
from archspec.official_qa.condition_fields import group_questions

QUESTIONS = group_questions("daylight")
ROOMS = ("普通办公室", "办公室", "阅览室", "绘图室", "教室", "病房", "卧室", "起居室")
NEGATED = re.compile(r"不是|并非|不属于|未确认|未明确|不详|非(?:普通)?办公室")
CLIMATE = re.compile(r"(?:[ⅠⅡⅢⅣⅤ]|I{1,3}|IV|V|[一二三四五1-5])类光气候区")


def daylight_value_question(query):
    compact = re.sub(r"\s+", "", query)
    return "采光系数" in compact and any(word in compact for word in (
        "多少", "取值", "取几", "最低值", "应为", "具体数值"))


def unclear(value):
    return not value or any(word in value for word in UNKNOWN) or bool(NEGATED.search(value))


def daylight_query_conditions(query):
    """只取原问句中的明确单项；否定/多种房间不裁剪成某个肯定选择。"""
    if "采光" not in query:
        return {}
    result = {}
    rooms = [room for room in ROOMS if room in query]
    # 普通办公室中包含办公室，只保留实际更长的明确表述。
    rooms = [room for room in rooms if not any(room != other and room in other for other in rooms)]
    segments = re.split(r"[，,；;。]", query)
    if len(rooms) == 1 and all(not unclear(part) for part in segments if rooms[0] in part):
        result["daylight_space"] = rooms[0]
    modes = [mode for mode in ("侧面", "顶部") if mode in query]
    if len(modes) == 1:
        segment = next((part for part in segments if modes[0] in part), "")
        if not unclear(segment) and not re.search(r"不(?:是|用|采用)?(?:侧面|顶部)", segment):
            result["daylight_mode"] = modes[0]
    climates = set(CLIMATE.findall(query))
    if len(climates) == 1:
        region = next(iter(climates))
        if all(not unclear(part) for part in segments if region in part):
            result["daylight_region"] = region
    return result


def daylight_condition_issues(conditions):
    """城市值只是用户输入，不会被转换成已确认的光气候区。"""
    issues = {}
    space = conditions.get("daylight_space", "")
    if unclear(space) or re.search(r"或|、|和|及", space):
        issues["daylight_space"] = "请确定本次查询的具体房间功能，不同空间请分别查。"
    mode = conditions.get("daylight_mode", "")
    if mode not in {"侧面", "顶部", "侧面采光", "顶部采光"}:
        issues["daylight_mode"] = "采光方式尚未明确，请选择侧面或顶部采光。"
    region = conditions.get("daylight_region", "")
    if unclear(region) or re.search(r"或|、", region):
        issues["daylight_region"] = "请给出一个地点或已确认的光气候区；系统不会根据城市猜出气候分区。"
    return issues


def daylight_room_conflict(conditions, text):
    """窄用途排除：普通办公室不采用医疗、教育等专用空间表，原候选不删除。"""
    if conditions.get("daylight_space") not in {"普通办公室", "办公室"}:
        return False
    # 只核对完整同条主文开头的明确用途，不因例外/引用中出现其他用途误排。
    heading = re.sub(r"\s+", "", text)[:80]
    return any(subject in heading for subject in ("医疗建筑", "教育建筑", "图书馆建筑", "旅馆建筑", "博物馆建筑"))


def daylight_text_matches(conditions, text):
    """采光主题和用途共用同一入口；同房间的噪声、人工照明不是采光依据。"""
    compact = re.sub(r"\s+", "", text)
    return "采光" in compact and not daylight_room_conflict(conditions, text)
