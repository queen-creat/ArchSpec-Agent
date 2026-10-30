"""地方问答范围与固定来源核对；不联网、不判断最新版本、不推算间距数字。"""

import re

from archspec.official_documents.local_policy import PUBLICATIONS


def select_yichang_sources(documents, requested):
    """只能从已登记的完整正文取证，显式选书时取交集，不偷换检索范围。"""
    registered = []
    for source in documents.list_sources():
        policy = PUBLICATIONS.get(source.get("page_url"))
        if (policy and policy["kind"] == "FULL_TEXT" and source.get("kind") == "FULL_TEXT"
                and source.get("pdf_url") == policy["pdf_url"]
                and source.get("title") == policy["title"]
                and source.get("standard_code") == policy["code"]):
            registered.append(source)
    if requested is not None:
        selected = [source for source in registered if source["source_id"] in requested]
        if registered and not selected:
            return {"state": "SCOPE_MISMATCH", "source_ids": []}
        registered = selected
    ready = [source["source_id"] for source in registered if source.get("acquired") is True]
    return {"state": "READY" if ready else "CACHE_ERROR" if any(
        source.get("cache_error") for source in registered) else "MISSING", "source_ids": ready}


def is_yichang_context(context):
    """来源档案必须属于本次固定原件；仅有下载网址或地方题名不能当身份证明。"""
    metadata = context.get("source_metadata")
    if not isinstance(metadata, dict):
        return False
    policy = PUBLICATIONS.get(context.get("source_url"))
    if not policy or policy["kind"] != "FULL_TEXT":
        return False
    if (metadata.get("attachment_url") != policy["pdf_url"]
            or context.get("title") != policy["title"]
            or context.get("standard_code") != policy["code"]):
        return False
    if not all(context.get(key) and metadata.get(key) == context[key] for key in
               ("source_id", "snapshot_id", "source_url", "snapshot_fingerprint")):
        return False
    fields = metadata.get("fields", {})
    return (fields.get("level", {}).get("value") == "LOCAL"
            and fields.get("region", {}).get("value") == "湖北·宜昌")


def sunlight_spacing_question(query):
    return "日照" in query and any(term in query for term in ("间距", "系数", "公式"))


def spacing_evidence(text, query=""):
    """日照时长不是间距公式；只做候选用途筛选，原检索前五仍完整保存。"""
    compact = re.sub(r"\s+", "", text)
    if "间距" not in compact:
        return False
    # 间距的主语须是建筑或日照；停车位、墙柱、设备的间隙不能借同一词代答。
    # 不看城市和条号，原文与未确认候选仍保留，表格行列继续要求对照原页。
    if "日照" not in compact and not re.search(r"建筑.{0,24}间距|间距.{0,24}建筑", compact):
        return False
    # 排除时长主句，不能因为后句解释“不是间距公式”而把它当间距证据。
    first = re.split(r"[，。；]", compact, maxsplit=1)[0]
    if ("日照" in first and any(term in first for term in ("时长", "小时"))
            and "间距" not in first):
        return False
    # 查阅与问答共用计算用途：许可材料清单中的“建筑间距”不是计算规则。
    # 要求同一局部表达出现系数、量法或间距约束；未匹配原文仍可关联阅读。
    calculation = (r"间距.{0,16}(?:系数|计算|按|根据|不[应宜得]?小于|不[应宜得]?大于|"
                   r"[=＝≥≤×]|[0-9]+(?:[.][0-9]+)?(?:[Hh米m倍]))"
                   r"|(?:计算|系数).{0,16}间距")
    if not re.search(calculation, compact):
        return False
    # 明问系数/公式时，普通“按现状控制”不能冒充计算依据；实际原表提示
    # 和倍率关系也可作阅读候选。只筛用途，不补数值、不计算或确认适用。
    if re.search(r"系数|公式", query):
        coefficient = (r"系数.{0,16}(?:为|取|按|应|宜|不得|[=＝]|表)"
                       r"|(?:间距|[LlDd]).{0,24}[=＝×]"
                       r"|(?:建筑|高度).{0,24}[0-9]+(?:[.][0-9]+)?倍"
                       r"|[0-9]+(?:[.][0-9]+)?倍.{0,24}(?:建筑|高度)"
                       r"|间距.{0,24}(?:计算|量取|测量)"
                       r"|(?:计算|量取|测量).{0,24}间距")
        return bool(re.search(coefficient, compact))
    return True
