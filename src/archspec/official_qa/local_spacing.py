"""地方间距计算附录的有限补读与必要条件；不推算系数或替用户选公式。"""

import re

from archspec.official_documents.named_units import read_named_contexts
from archspec.official_qa.condition_checks import UNKNOWN
from archspec.official_qa.condition_fields import group_questions
from archspec.official_qa.local_scope import is_yichang_context, sunlight_spacing_question
from archspec.standard_governance.compatibility import ContentUseDenied

PARENT_TITLE = "五、建筑间距及建筑退后距离的计算"
QUESTIONS = group_questions("spacing")


def unit_key(row):
    """数字条号和阅读单元是两套标识，不能把所有空条号混成同一条。"""
    key = row.get("reading_unit_key")
    return key if isinstance(key, str) and re.fullmatch(r"named:\d{1,6}", key) else row.get("clause_number")


def evidence_key(row):
    return row["source_id"], row["snapshot_id"], unit_key(row)


def read_spacing_supplements(documents, query, contexts):
    if not sunlight_spacing_question(query):
        return [], []
    results, gaps, attempted = [], [], set()
    for old in contexts:
        identity = old["source_id"], old["snapshot_id"]
        if identity in attempted or not is_yichang_context(old):
            continue
        attempted.add(identity)
        try:
            found, missing = read_named_contexts(documents, *identity, PARENT_TITLE)
            if any(not is_yichang_context(context) for context in found):
                raise ValueError("计算规则的地方固定来源不一致")
            results.extend(found)
            gaps.extend(missing)
        except (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied):
            gaps.append("间距计算附录的固定来源、原件或读取条件无法核对，未作为回答。")
    return results, list(dict.fromkeys(gaps))


def required_spacing_evidence(query, evidence):
    if not sunlight_spacing_question(query):
        return set()
    return {row["evidence_id"] for row in evidence if row.get("quote_ready")
            and str(unit_key(row) or "").startswith("named:") and row["role"] != "RELATED_CONTEXT"
            and is_yichang_context(row)}


def spacing_followups(conditions):
    # 这里只记录用户条件，记录齐全不等于设计范围或公式已经核准。
    return [{"key": key, "label": label, "reason": "该条件影响应核对的间距分支；未知时仍保留已有原页。"}
            for key, label in QUESTIONS.items() if not conditions.get(key)
            or any(word in conditions[key] for word in UNKNOWN)]
