"""高度计量主题与有限完整补读；只定位原文，不推算量法或数值。"""

import re

from archspec.official_qa.intents import height_measurement_question
from archspec.standard_governance.compatibility import ContentUseDenied


def height_topic(query):
    """口语机房只作为设备用房检索线索，不据此判断实际用房性质。"""
    if any(term in query for term in ("机房", "设备用房", "局部突出", "突出屋面")):
        return "ROOF_EQUIPMENT"
    return "GENERAL"


def measurement_text(text):
    """动词必须讨论高度本身，不能让‘按高度计算面宽’代答高度量法。

    只忽略空白。原文和识别风险不改写；命中主题仍不是完整引用证明。
    """
    text = re.sub(r"\s+", "", text)
    if re.search(r"(?:计入|计作)(?:其)?(?:建筑|檐口|屋脊)高度", text):
        return True
    for match in re.finditer(r"(?:建筑|檐口|屋脊)高度([^。；！？]{0,65}?)(?:计算|计量|测量|量取)", text):
        # “有关高度计算的规定”只要求查另一标准，不能把分类注释当成实际量法。
        # 按每个语句匹配判断，真正量法后附带引用注释时，原条文仍整体保留。
        if re.match(r"(?:的)?规定", text[match.end():]):
            continue
        if not any(term in match[1] for term in ("面宽", "面积", "间距", "停机坪", "疏散", "净高")):
            return True
    return False


def height_clause_matches(query, text, *, focus=None):
    if not measurement_text(text):
        return False
    compact = re.sub(r"\s+", "", text)
    return (focus if focus is not None else height_topic(query)) != "ROOF_EQUIPMENT" or any(
        term in compact for term in ("设备用房", "机房", "局部突出屋面", "突出屋面用房"))


def read_height_supplements(documents, query, evidence, *, focus=None):
    """邻接只是线索；最多两条同书同快照完整初读，不递归补新的邻接。

    初读后仍经过总预算、完整续文计数、逐字引用和输出前原件核对。
    """
    # 一般量法已有章节阅读组，不把所有邻接提升为新命中。
    # 只有明确机房/设备问题才需要将目标条文的跨页续文完整补读。
    if (focus != "ROOF_EQUIPMENT" if focus is not None
            else not height_measurement_question(query) or height_topic(query) != "ROOF_EQUIPMENT"):
        return [], []
    contexts, gaps, attempted = [], [], set()
    for row in evidence:
        if row["role"] != "RELATED_CONTEXT" or not height_clause_matches(query, row["text"], focus=focus):
            continue
        number = row["clause_number"]
        if not re.fullmatch(r"\d+(?:\.\d+){2,3}", number or ""):
            continue
        key = (row["source_id"], row["snapshot_id"], number)
        if key in attempted:
            continue
        if len(attempted) >= 2:
            gaps.append("高度计量补读达到两条初读上限，其余保留原页阅读。")
            break
        attempted.add(key)
        try:
            context = documents.read_context(row["source_id"], row["snapshot_id"], row["chunk_id"])
            coverage = context["selected_clause_coverage"]
            core = [part for part in context["fragments"] if part["clause_number"] == number
                    and part["role"] != "RELATED_CONTEXT"]
            expected = coverage["expected_fragment_count"]
            if (context["source_id"] != row["source_id"] or context["snapshot_id"] != row["snapshot_id"]
                    or coverage["clause_number"] != number
                    or type(expected) is not int or expected < 1
                    or len({part["id"] for part in core}) != expected):
                raise ValueError("高度补读身份或续文数量不一致")
            core.sort(key=lambda part: int(part["id"].rsplit(":", 1)[1]))
            if height_clause_matches(query, "\n".join(part["text"] for part in core), focus=focus):
                contexts.append(context)
        except (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied):
            gaps.append("高度计量相关条文的完整出处或续文无法核对，仍仅供原页阅读。")
    return contexts, gaps
