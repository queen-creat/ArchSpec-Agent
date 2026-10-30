"""根据既有缓存匹配与公共地区计划，决定本轮是否需要补查官方来源。"""

import re
from collections.abc import Mapping

VERSION = "NETWORK_ROUTE_V1"
_CURRENT_REQUEST = re.compile(
    r"最新|现行|新版|当前版本|(?:是否|现在|目前)[^。！？\n]{0,30}(?:有效|废止|适用|执行)"
)
_MESSAGES = {
    "LOCAL_READING_REQUIRED": "明确条号尚需在已有固定正文中核读，本轮不因缺条号重新获取整本规范。",
    "REFERENCE_ONLY": "本次仅查看已有缓存的关联参考，不触发联网、准备正文或模型，也不作为对应或充分证据。",
    "REGION_REQUIRED": "请在问题中补充省市，或关联已登记所在地的项目，再查地方正式文件。",
    "REGION": "需要补查对应地区的官方来源，缓存版本和地方适用范围仍须核对。",
    "LATEST": "需要核对最新现行官方版本，已有缓存不替代本轮版本查证。",
    "LOCAL_GAP": "当前缓存缺少对应正文，需要补查官方来源，暂不能据相似条文作答。",
    "CACHE_MATCHED": "已找到对应缓存候选，本轮不重复联网，版本和适用条件仍须核对。",
    "UNVERIFIED": "当前缓存匹配说明尚不能核对，继续独立补查官方来源。",
    "SCOPE_REVIEW_REQUIRED": "指定来源的选书范围或读取条件需先核对，本轮未自动扩大范围或准备正文。",
    "CANDIDATE_UNCONFIRMED": "已取得本次范围的可读正文，候选与问题的对应关系待核对，可继续补查官方来源。",
}


def network_decision(query, lookup):
    """纯决策：不搜索、不调用模型，不重新识别地区或比较检索分数。"""
    # 用户明确的只读关联意图优先于地域/现行版本等准备提示，不进入自动联网。
    if isinstance(lookup, Mapping) and lookup.get("scope") == "RELATED":
        return {"version": VERSION, "required": False, "reason": "REFERENCE_ONLY",
                "message": _MESSAGES["REFERENCE_ONLY"]}
    plan = lookup.get("public_plan") if isinstance(lookup, Mapping) else None
    region = plan.get("region") if isinstance(plan, Mapping) else None
    state = lookup.get("state") if isinstance(lookup, Mapping) else None
    observations = None
    if isinstance(lookup, Mapping) and lookup.get("observations") is not None:
        try:
            from archspec.official_documents.lookup import checked_observations
            observations = checked_observations(lookup["observations"])
        except (ValueError, TypeError):
            # 畸形观察不能借旧MATCHED取消本轮补核。
            state = "UNVERIFIED"
    partial = False
    if isinstance(lookup, Mapping) and "question_parts" in lookup:
        parts, primary = lookup["question_parts"], lookup.get("primary_indices", ())
        if (not isinstance(parts, list) or not 2 <= len(parts) <= 4
                or any(not isinstance(part, Mapping) or not isinstance(part.get("query"), str)
                       or not part["query"].strip() or not isinstance(part.get("primary_indices"), list)
                       or any(type(index) is not int or index not in primary for index in part["primary_indices"])
                       for part in parts)):
            state = "UNVERIFIED"
        else:
            partial = any(not part["primary_indices"] for part in parts)
    # 未完成来源需求复用同一合同；范围/许可不明不能因另一本命中而自动准备整句。
    source_gaps, clause_gaps, blocked = [], [], False
    if isinstance(lookup, Mapping) and "evidence_requirements" in lookup:
        try:
            from archspec.official_qa.evidence_requirements import EvidenceRequirement

            rows = lookup["evidence_requirements"]
            if not isinstance(rows, list) or len(rows) > 64:
                raise ValueError("来源需求无法核对")
            public_targets = {row["value"] for row in plan.get("targets", ())
                              if isinstance(row, Mapping) and row.get("kind") in {"CODE", "TITLE", "LOCAL_TOPIC"}}
            numbers = plan.get("explicit_clause_numbers", ())
            if (not isinstance(numbers, (list, tuple)) or len(set(numbers)) != len(numbers)
                    or any(not isinstance(number, str) or not re.fullmatch(r"\d+(?:\.\d+){2,3}", number)
                           for number in numbers)):
                raise ValueError("明确条号公共目标无法核对")
            for row in rows:
                requirement = EvidenceRequirement(**row)
                if requirement.source_state is not None:
                    if requirement.subject not in public_targets:
                        raise ValueError("来源需求不属于本次公共目标")
                    if requirement.state != "FOUND_COMPLETE":
                        source_gaps.append(requirement)
                elif (requirement.kind == "EXPLICIT_REFERENCE" and requirement.trigger == "BEFORE_SEARCH"
                      and requirement.necessity == "ANSWER_REQUIRED"
                      and requirement.completeness == "FIXED_EXPLICIT_CLAUSE"
                      and requirement.subject in numbers):
                    if requirement.state != "FOUND_COMPLETE":
                        clause_gaps.append(requirement)
                else:
                    raise ValueError("需求不属于本次公共来源或条号目标")
            blocked = any(row.source_state in {"EXCLUDED_FIXED_SCOPE", "RESTRICTED", "UNVERIFIED"}
                          for row in source_gaps)
        except (ValueError, TypeError, KeyError, AttributeError):
            source_gaps, blocked = [], True
    # 这是后端当次覆盖产生的候选书目缺口，不是用户指定来源需求或回答事实。
    suggested_gaps = ()
    suggested_checked = isinstance(lookup, Mapping) and "suggested_source_gaps" in lookup
    if suggested_checked:
        try:
            from archspec.official_discovery.public_queries import validated_public_titles

            suggested_gaps = validated_public_titles(lookup["suggested_source_gaps"])
            targets = {row["value"] for row in plan.get("targets", ()) if row.get("kind") == "TITLE"}
            if not set(suggested_gaps) <= targets:
                raise ValueError("建议缺口不属于本次公共书目")
        except (ValueError, TypeError, KeyError, AttributeError):
            suggested_gaps, blocked = (), True
    # 本地阅读不需要外发词；确有缺口且共同计划没有公共词时先补书目。
    # 只消费既有解析结果，不把原问焦点或任意工程名称直接拼成外部查询。
    public_scope_missing = (isinstance(plan, Mapping)
                            and plan.get("route") == "PUBLIC_WORDS_NOT_IDENTIFIED"
                            and not plan.get("queries") and not plan.get("targets")
                            and state != "MATCHED")
    # 地方范围未确定时先补条件，不创建看似正在查对应地区的无效任务。
    if blocked:
        reason = "SCOPE_REVIEW_REQUIRED"
    elif region == "地域待补充":
        reason = "REGION_REQUIRED"
    elif isinstance(region, str) and region.strip():
        reason = "REGION"
    elif public_scope_missing:
        reason = "SCOPE_REVIEW_REQUIRED"
    elif isinstance(query, str) and _CURRENT_REQUEST.search(query):
        reason = "LATEST"
    elif suggested_checked and not suggested_gaps:
        reason = "SCOPE_REVIEW_REQUIRED"
    elif source_gaps:
        reason = "LOCAL_GAP"
    elif clause_gaps:
        reason = "LOCAL_READING_REQUIRED"
    elif suggested_gaps or partial:
        reason = "LOCAL_GAP"
    elif (observations is not None and observations["source"]["state"] == "READABLE"
          and observations["candidates"]["state"] != "MATCHED"):
        reason = "CANDIDATE_UNCONFIRMED"
    elif isinstance(state, str) and state in {"NO_MATCH", "MISSING_SOURCE", "REGION_GAP"}:
        reason = "LOCAL_GAP"
    elif state == "MATCHED" and isinstance(plan, Mapping) and "region" in plan and region is None:
        reason = "CACHE_MATCHED"
    else:
        reason = "UNVERIFIED"
    message = _MESSAGES[reason]
    if public_scope_missing and not blocked and reason == "SCOPE_REVIEW_REQUIRED":
        message = ("本地尚未找到对应正文，且未识别可公开发送的检索词。"
                   "请补充规范编号或公开规范题名，再补查官方来源；已有本地候选仍可阅读，未外发原问题。")
    if suggested_checked and not suggested_gaps and not blocked and reason == "SCOPE_REVIEW_REQUIRED":
        message = "建议规范仍需在本机核读；本轮未发现可自动补取的正文缺口。"
    if partial and reason == "LOCAL_GAP":
        message = "并列要求仍有未定位部分，需继续补查对应官方来源；已有条文保留，未认定要求已查齐。"
    if source_gaps and reason in {"SCOPE_REVIEW_REQUIRED", "LOCAL_GAP"}:
        message += " 待核对：" + "、".join(row.subject for row in source_gaps) + "。"
    if clause_gaps and reason == "LOCAL_READING_REQUIRED":
        message += " 待本地核读：" + "、".join(row.subject for row in clause_gaps) + "。"
    return {
        "version": VERSION,
        "required": reason not in {"CACHE_MATCHED", "REGION_REQUIRED", "SCOPE_REVIEW_REQUIRED",
                                   "LOCAL_READING_REQUIRED"},
        "reason": reason,
        "message": message,
    }
