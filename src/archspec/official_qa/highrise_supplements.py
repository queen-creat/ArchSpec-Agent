"""有限高层同义补查；只读固定完整正文，不改变原问题或V1前五。"""

import re

from archspec.official_qa.intents import (
    height_use_category,
    highrise_definition_evidence,
    highrise_definition_question,
)
from archspec.official_qa.local_scope import is_yichang_context
from archspec.standard_governance.compatibility import ContentUseDenied

CLAUSE = re.compile(r"\d+(?:\.\d+){2,3}")
QUERY = "高层建筑高度分类"
READ_ERRORS = (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied)


def _complete_definition(context, category, *, local):
    """只看所选主条完整核心；邻接同词、同号异书和未读齐不能提供定义。"""
    if local and not is_yichang_context(context):
        return False
    coverage = context["selected_clause_coverage"]
    number = coverage["clause_number"]
    expected = coverage["expected_fragment_count"]
    if not CLAUSE.fullmatch(number or "") or type(expected) is not int or expected < 1:
        return False
    prefix = f"{context['source_id']}:{context['snapshot_id']}:"
    core = [part for part in context["fragments"] if part["clause_number"] == number
            and part["role"] != "RELATED_CONTEXT"]
    if (not coverage.get("complete") or coverage.get("included_fragment_count") != expected
            or len({part["id"] for part in core}) != expected
            or not any(part["role"] == "MATCH" and part["id"] == context["selected_chunk_id"]
                       for part in core)
            or any(not part["id"].startswith(prefix)
                   or not part["id"][len(prefix):].isdecimal() for part in core)):
        return False
    core.sort(key=lambda part: int(part["id"][len(prefix):]))
    return highrise_definition_evidence("\n".join(part["text"] for part in core), category)


def read_highrise_supplements(documents, query, conditions, contexts, source_ids):
    """一次中性V1查询、五候选、最多两条不同固定正文初读；失败也消耗次数。

    返回新的完整分类上下文、缺口和独立收据。后续仍接受总预算、引用逐字核对
    及全新读取阶段的原件/许可复核；找到出处不证明现行性或项目适用性。
    """
    category = height_use_category(conditions.get("building_use", ""))
    if not highrise_definition_question(query) or category is None:
        return [], [], None
    local = "宜昌" in query
    for context in contexts:
        try:
            if _complete_definition(context, category, local=local):
                return [], [], None
        except READ_ERRORS:
            continue
    receipt = {"query": QUERY, "method": "BM25_UNIFIED_V1", "top_k": 5,
               "hits": [], "state": "SEARCHED"}
    gaps, attempted = [], set()
    try:
        searched = documents.search_all(QUERY, 5, source_ids, ranking_version="V1")
        receipt["method"] = searched["method"]
        receipt["hits"] = [{key: hit.get(key) for key in
            ("id", "source_id", "snapshot_id", "clause_number", "page_start", "page_end")}
            for hit in searched["results"][:5]]
    except READ_ERRORS:
        receipt["state"] = "FAILED"
        return [], ["高层分类补检索的正文或使用条件无法核对，未扩大资料范围。"], receipt
    for hit in searched["results"][:5]:
        number = hit.get("clause_number")
        # 不先按单片定义判别；用途、高度和分类谓词可能分处同条主文及续文。
        if (not CLAUSE.fullmatch(number or "") or re.search(r"[.…·]{3,}\s*\d+\s*(?:\n|$)", hit["text"])
                or source_ids is not None and hit["source_id"] not in source_ids):
            continue
        key = (hit["source_id"], hit["snapshot_id"], number)
        if key in attempted:
            continue
        if len(attempted) >= 2:
            gaps.append("高层分类补查达到两条初读上限，其余须继续对照固定原页。")
            break
        attempted.add(key)
        try:
            context = documents.read_context(hit["source_id"], hit["snapshot_id"], hit["id"])
            if (context["source_id"] != hit["source_id"]
                    or context["snapshot_id"] != hit["snapshot_id"]
                    or context["selected_chunk_id"] != hit["id"]
                    or context["selected_clause_coverage"]["clause_number"] != number):
                raise ValueError("高层补查固定身份不一致")
            if _complete_definition(context, category, local=local):
                receipt["state"] = "FOUND"
                return [context], gaps, receipt
            coverage = context["selected_clause_coverage"]
            if not coverage.get("complete"):
                gaps.append("高层分类相关条文续文未读齐，未将已有片段当完整定义。")
            elif local and not is_yichang_context(context):
                gaps.append("补查正文的固定地方来源档案无法核对，未作为宜昌定义。")
        except READ_ERRORS:
            gaps.append("高层分类补查的固定出处或读取条件无法核对，未纳入回答。")
    receipt["state"] = "NO_DEFINITION"
    gaps.append("当前有限补检索未读到完整的对应用途高层分类；没有命中不等于没有要求。")
    return [], list(dict.fromkeys(gaps)), receipt
