"""共同回答充分性：只呈现最终证据包的覆盖，不查资料或证明工程适用性。"""

_LABELS = {
    "CORE_BODY": "核心正文", "TABLE": "原表", "EXPLICIT_REFERENCE": "相关条文",
    "CONDITION_EXCEPTION": "适用条件及例外", "NAMED_RULE": "地方计算规则",
}
_MESSAGES = {
    "BUDGET_TRUNCATED": "未完整纳入本次证据预算，需继续对照固定原文。",
    "BUDGET_EXCLUDED": "未纳入本次证据预算，需继续对照固定原文。",
    "PARTIAL": "正文或相关续文不完整，不能据此作充分回答。",
    "NOT_LOCATED": "本固定来源中未定位到所需原文，不能借同号其他来源代替。",
    "NOT_RETRIEVED": "本次检索未取得所需完整正文；不代表要求不存在。",
    "MISSING_SOURCE": "尚未取得所需正式正文，线索不能作为规范事实。",
    "DENIED": "当前使用条件不允许读取所需正文。",
    "ORIGINAL_REQUIRED": "需核对固定原页，识别文字不足以确认要求。",
    "LIMIT_REACHED": "已达到本次有限读取上限，仍需核对相关原文。",
    "UNRESOLVED": "所需证据尚未完成核对。",
}
_PUBLIC_SOURCE_FIELDS = ("source_id", "snapshot_id", "title", "standard_code")


def _sources(requirement, bundle):
    # 元数据只取本轮确已读context；即使整本预算未纳入，也不删除其真实缺口。
    identities = {(target["source_id"], target["snapshot_id"])
                  for target in requirement.fixed_targets
                  if "source_id" in target and "snapshot_id" in target}
    wanted = {identity for target in requirement.fixed_targets
              for identity in target.get("required_evidence_ids", ())}
    identities.update((row["source_id"], row["snapshot_id"]) for row in bundle.evidence
                      if row["evidence_id"] in wanted)
    rows, seen = [], set()
    for context in bundle.contexts:
        identity = context["source_id"], context["snapshot_id"]
        if identity in identities and identity not in seen:
            seen.add(identity)
            rows.append({key: context[key] for key in _PUBLIC_SOURCE_FIELDS})
    return rows


def describe_answer_sufficiency(bundle, *, status, condition_followups=(), condition_state=None):
    """输出无正文/新链接的缺口详情；最终守卫失败后不展示旧来源或已记录状态。"""
    failed = status == "EVIDENCE_CHECK_FAILED"
    unresolved = ([row for row in bundle.requirements if row.state != "FOUND_COMPLETE"]
                  if bundle is not None and not failed else [])
    if failed:
        evidence_state, conditions_state = "CHECK_FAILED", "CHECK_FAILED"
    else:
        evidence_state = ("NOT_EVALUATED" if bundle is None else
                          "INCOMPLETE" if unresolved else "COMPLETE")
        conditions_state = ("NEEDS_DETAILS" if condition_followups else
                            "DETAILS_RECORDED" if condition_state == "DETAILS_RECORDED"
                            else "NOT_REQUESTED")
    summary = {
        "version": "ANSWER_SUFFICIENCY_V1", "evidence_state": evidence_state,
        "condition_state": conditions_state, "applicability_state": "NOT_VERIFIED",
        "unresolved_requirement_ids": [row.requirement_id for row in unresolved],
    }
    details = []
    if status in {"REFERENCE_BRIEF", "READING_ONLY", "NO_EVIDENCE"} and bundle is not None:
        for row in unresolved:
            if status == "NO_EVIDENCE" and row.completeness != "FIXED_EXPLICIT_CLAUSE":
                continue
            label = (f"指定规范正文 · {row.subject}" if row.source_state is not None
                     else _LABELS[row.kind])
            if row.completeness == "PARALLEL_CORE_UNITS":
                label = f"并列要求 · {row.subject}"
            elif row.source_state is None and row.kind in {"TABLE", "EXPLICIT_REFERENCE"} and row.subject:
                label += row.subject
            elif row.kind == "NAMED_RULE" and row.subject:
                label = row.subject
            if row.completeness == "FIXED_ARTIFACT_REFERENCE":
                label = f"原图表 · {row.subject}"
            details.append({
                "requirement_id": row.requirement_id, "kind": row.kind,
                "necessity": row.necessity, "state": row.state, "subject": row.subject,
                "label": label, "message": (row.reason if row.source_state is not None
                    or row.completeness in {"FIXED_EXPLICIT_CLAUSE", "FIXED_ARTIFACT_REFERENCE"}
                    else _MESSAGES.get(row.state, "所需证据尚未完成核对。")),
                "sources": _sources(row, bundle),
            })
    return details, summary
