"""车库明确条件关联完整条文；不产生设计数值、不确认规范现行或适用。"""

import re

from archspec.official_qa.fire_scope import fire_use_conflict, smoke_area_only
from archspec.official_qa.garage_conditions import SCOPES
from archspec.standard_governance.compatibility import ContentUseDenied


def garage_rule_topics(conditions):
    """主题只来自明确填写的汽车库条件，题名与另一用途不能替用户补条件。"""
    if conditions.get("building_use") != "汽车库":
        return []
    topics = []
    form = conditions.get("garage_form", "")
    if "机械式" in form or form in {"敞开式", "敞开式汽车库", "错层式", "错层式汽车库",
                                  "斜楼板式", "斜楼板式汽车库"}:
        topics.append({"kind": "FORM", "label": "车库形式相关条文", "condition": form})
    storage = conditions.get("garage_storage", "")
    if storage in {"存放危险物品", "甲、乙类物品运输车", "甲乙类物品运输车"}:
        topics.append({"kind": "STORAGE", "label": "运输或存放内容相关条文", "condition": storage})
    scope = SCOPES.get(conditions.get("suppression_scope", ""))
    if scope in {"ALL", "PARTIAL"}:
        topics.append({"kind": "SUPPRESSION", "label": "自动灭火面积条件", "condition": scope})
    return topics


def topic_matches(topic, text):
    """匹配完整文字的主题，不凭固定条号或另一书同号推定联系。

    机械式的车道和人员是不同分支；明确子型须在原文里出现同样的完整描述。
    危险物品只提供可能相关的正式条文，其分类仍需用户确认。
    """
    text = re.sub(r"\s+", "", text)
    if "汽车库" not in text or smoke_area_only(text) or fire_use_conflict("汽车库", text):
        return False
    if topic["kind"] == "FORM":
        value = topic["condition"].removesuffix("汽车库")
        return value in text and any(word in text for word in ("面积", "分隔", "应符合"))
    if topic["kind"] == "STORAGE":
        return ("防火分区" in text and "面积" in text
                and any(word in text for word in ("危险物品", "甲、乙类物品运输车", "甲乙类物品运输车")))
    return all(word in text for word in ("自动灭火", "防火分区", "面积"))


def read_garage_supplements(documents, conditions, evidence):
    """最多补读两条特殊条件，不递归邻接、不改初始前五；失败保留原阅读。

    与既有灭火补读分开限制，总新增初读最多四条。只有同书同快照的全部主文
    和续文核对成功才返回，后续仍受总片段预算及逐字引用检查约束。
    """
    topics = [topic for topic in garage_rule_topics(conditions) if topic["kind"] != "SUPPRESSION"]
    contexts, gaps, attempted = [], [], set()
    for row in evidence:
        if row["role"] != "RELATED_CONTEXT" or not any(topic_matches(topic, row["text"]) for topic in topics):
            continue
        key = (row["source_id"], row["snapshot_id"], row["clause_number"])
        if key in attempted:
            continue
        if len(attempted) >= 2:
            gaps.append("特殊车库条件达到两条补读上限，其余保留原页入口。")
            break
        attempted.add(key)
        try:
            context = documents.read_context(row["source_id"], row["snapshot_id"], row["chunk_id"])
            coverage = context["selected_clause_coverage"]
            core = [part for part in context["fragments"] if part["clause_number"] == row["clause_number"]
                    and part["role"] != "RELATED_CONTEXT"]
            expected = coverage["expected_fragment_count"]
            if (context["source_id"] != row["source_id"] or context["snapshot_id"] != row["snapshot_id"]
                    or coverage["clause_number"] != row["clause_number"]
                    or type(expected) is not int or expected < 1
                    or len({part["id"] for part in core}) != expected):
                raise ValueError("特殊条件条文身份或完整续文不一致")
            contexts.append(context)
        except (ValueError, OSError, LookupError, TypeError, KeyError, ContentUseDenied):
            gaps.append("特殊车库条件的完整条文或续文无法核对，仍仅供原页阅读。")
    return contexts, gaps


def required_garage_evidence(conditions, evidence):
    """已完整读到的明确特殊条件不能被模型或本地整理器悄悄漏选。"""
    topics = [topic for topic in garage_rule_topics(conditions) if topic["kind"] != "SUPPRESSION"]
    groups = _complete_groups(evidence)
    return {row["evidence_id"] for rows in groups.values()
            if any(topic_matches(topic, "\n".join(row["text"] for row in rows)) for topic in topics)
            for row in rows}


def _complete_groups(evidence):
    groups = {}
    for row in evidence:
        if not row.get("quote_ready") or row["role"] == "RELATED_CONTEXT":
            continue
        key = (row["source_id"], row["snapshot_id"], row["clause_number"])
        groups.setdefault(key, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: int(row["chunk_id"].rsplit(":", 1)[1]))
    return groups


def build_garage_rule_checks(conditions, evidence):
    """输出真实完整出处及具体缺口；LOCATED仅表示找到原文，不是适用审批。"""
    checks = []
    groups = _complete_groups(evidence)
    for topic in garage_rule_topics(conditions):
        references = [{"source_id": key[0], "snapshot_id": key[1], "clause_number": key[2],
                       "evidence_ids": [row["evidence_id"] for row in rows]}
                      for key, rows in groups.items() if topic_matches(topic, "\n".join(row["text"] for row in rows))]
        state = "LOCATED" if references else "NOT_LOCATED"
        message = ("已找到完整相关原文，按原页核对适用条件。" if references else
                   f"当前有限证据未读到“{topic['condition']}”的完整相关条文，请继续查对应正式正文。")
        if topic["kind"] == "STORAGE" and topic["condition"] == "存放危险物品":
            state = "DETAILS_NEEDED"
            message = "请说明运输或存放物品类别，不能把所有危险物品套用甲、乙类物品运输车要求。"
        elif topic["kind"] == "SUPPRESSION" and topic["condition"] == "PARTIAL":
            state = "DETAILS_NEEDED"
            message = "已记录局部覆盖；尚未找到局部配置的完整处理依据，不按全部覆盖增加面积。"
        checks.append({"kind": topic["kind"], "label": topic["label"], "state": state,
                       "message": message, "references": references})
    return checks
