"""有限补读已发现的灭火面积条文，不联网、不改排序或固定正文。"""

import re

from archspec.official_qa.fire_scope import fire_use_conflict, smoke_area_only
from archspec.official_qa.intents import fire_area_question
from archspec.standard_governance.compatibility import ContentUseDenied


def read_fire_supplements(documents, query, conditions, evidence):
    """邻接片段仅是线索；重新核对同书同快照的完整主文和续文才返回。

    每次最多尝试两条，不递归扩展新邻接，不以条号相同借用另一份书的条件。
    后续仍需接受原有总片段预算和逐字引用检查，完整初读不保证最终能引用。
    """
    if not fire_area_question(query):
        return [], []
    contexts, gaps, attempted = [], [], set()
    for row in evidence:
        if row["role"] != "RELATED_CONTEXT":
            continue
        text = re.sub(r"\s+", "", row["text"])
        if (not all(word in text for word in ("自动灭火", "防火分区", "面积"))
                or any(word in text[:180] for word in ("安全出口", "疏散出口"))
                or smoke_area_only(text)
                or fire_use_conflict(conditions.get("building_use", ""), text)):
            continue
        key = (row["source_id"], row["snapshot_id"], row["clause_number"])
        if key in attempted:
            continue
        if len(attempted) >= 2:
            gaps.append("灭火面积补充条文达到两条初读上限，其余保留原页阅读入口。")
            break
        attempted.add(key)
        try:
            context = documents.read_context(row["source_id"], row["snapshot_id"], row["chunk_id"])
            coverage = context["selected_clause_coverage"]
            core = [part for part in context["fragments"]
                    if part["clause_number"] == row["clause_number"]
                    and part["role"] != "RELATED_CONTEXT"]
            expected = coverage["expected_fragment_count"]
            if (context["source_id"] != row["source_id"]
                    or context["snapshot_id"] != row["snapshot_id"]
                    or coverage["clause_number"] != row["clause_number"]
                    or type(expected) is not int or expected < 1
                    or len({part["id"] for part in core}) != expected):
                raise ValueError("补读身份或完整片段数不一致")
            contexts.append(context)
        except (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied):
            gaps.append("某条灭火面积补充条文的固定出处或完整续文无法核对，仍仅供阅读。")
    return contexts, gaps
