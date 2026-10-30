"""当前来源版本的只读派生视图，不审批声明或扩大正文使用许可。"""

import re
from datetime import UTC, datetime

from archspec.standard_governance.registry import parse_date
from archspec.standard_governance.verification import (
    active_event,
    matches_current_event,
    parse_time,
)

UNKNOWN_STATES = frozenset({"UNVERIFIED", "EXPIRED", "REJECTED", "REVOKED", "MANIFEST_CHANGED", "CONFLICT"})


def unverified_view(reason: str, *, state="UNVERIFIED") -> dict:
    """缺精确治理绑定时复用完整未知合同，不读取或创建治理数据。"""
    if (not isinstance(reason, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", reason) is None
            or not isinstance(state, str) or state not in UNKNOWN_STATES):
        raise ValueError("未知来源状态只能使用有限状态及简短原因码")
    return {"standard_version_id": None, "artifact_binding_id": None,
            "evaluated_at": datetime.now(UTC).isoformat(),
            "version_status": {"value": "UNKNOWN", "verification_state": state,
                               "event_id": None, "checked_as_of": None,
                               "valid_until": None, "effective_at": None},
            "relations": [], "reasons": [reason], "applicability_status": "NOT_REVIEWED"}


class SourceValidityService:
    """按当前事件头投影版本与调整范围，正式引用仍由 AdmissionService 判断。"""

    def __init__(self, repository):
        self.repo = repository

    def _verification(self, kind, subject, scope, when):
        # 不按旧 manifest 筛选事件头，也不从旧的成功记录回退。
        event = self.repo.head(kind, subject["id"], scope)
        matches = matches_current_event(self.repo, kind, subject, event)
        if event is None:
            return "UNVERIFIED", None, False
        if event["decision"] == "REJECT":
            state = "REJECTED"
        elif event["decision"] == "REVOKE":
            state = "REVOKED"
        elif not matches:
            state = "MANIFEST_CHANGED"
        elif active_event(event, when):
            state = "VERIFIED"
        elif event.get("valid_until") and when >= parse_time(event["valid_until"]):
            state = "EXPIRED"
        else:
            state = "UNVERIFIED"
        return state, event, not matches

    @staticmethod
    def _event_fields(event):
        return {"event_id": event["id"] if event else None,
                "checked_as_of": event["checked_as_of"] if event else None,
                "valid_until": event.get("valid_until") if event else None}

    def evaluate(self, artifact_id, *, as_of=None):
        when = parse_time(as_of) if as_of is not None else datetime.now(UTC)
        artifact = self.repo.get_artifact(artifact_id)
        version = self.repo.get_version(artifact["standard_version_id"])
        state, event, changed = self._verification("VERSION", version, "VERSION_STATUS", when)
        statement = event.get("statement", {}) if event else {}
        effective = statement.get("effective_at")
        value = statement.get("lifecycle_status", "UNKNOWN") if state == "VERIFIED" else "UNKNOWN"
        reasons = []
        if state != "VERIFIED":
            reasons.append("VERSION_STATUS_" + state)
        if changed:
            reasons.append("VERSION_STATUS_MANIFEST_CHANGED")
        # 日期只约束已核 CURRENT，不把已到日期的 UPCOMING 自行升级。
        effective_date = parse_date(effective)
        if (state == "VERIFIED" and value in {"CURRENT", "UPCOMING"}
                and effective_date is not None and effective_date > when.date()):

            value = "UPCOMING"
            reasons.append("VERSION_NOT_YET_EFFECTIVE")
        status = {"value": value, "verification_state": state,
                  **self._event_fields(event), "effective_at": effective}

        relations = self.repo.list_relations(version["id"])
        # 正常登记已拒绝多整版目标；遗留异常数据也不能产生任一确定替代结论。
        whole_targets = {relation["to_version_id"] for relation in relations
                         if relation["relation_type"] == "WHOLE_SUPERSEDES"}
        conflict = len(whole_targets) > 1
        views = []
        for relation in relations:
            relation_state, proof, relation_changed = self._verification(
                "RELATION", relation, "VERSION_RELATION", when)
            if conflict and relation["relation_type"] == "WHOLE_SUPERSEDES":
                relation_state = "CONFLICT"
            effect = "UNRESOLVED"
            if relation_state == "VERIFIED":
                effect = ("NOT_EFFECTIVE" if parse_date(relation["effective_at"]) > when.date()
                          else "IN_EFFECT")
            views.append({"relation_id": relation["id"], "relation_type": relation["relation_type"],
                          "from_version_id": relation["from_version_id"],
                          "to_version_id": relation["to_version_id"],
                          "effective_at": relation["effective_at"],
                          # 原范围字符串逐项保留，不能把子款前缀当整条或整本。
                          "affected_clauses": list(relation["affected_clauses"]),
                          "verification_state": relation_state, **self._event_fields(proof),
                          "effect_state": effect})
            if relation_changed:
                reasons.append("VERSION_RELATION_MANIFEST_CHANGED")
            if effect == "UNRESOLVED" or (effect == "IN_EFFECT"
                                          and relation["relation_type"] != "WHOLE_SUPERSEDES"):
                # 已知部分调整不能从原版快照自动生成调整后的完整正文。
                reasons.append("VERSION_RELATION_UNRESOLVED")
            elif effect == "IN_EFFECT" and state == "VERIFIED" and value != "WITHDRAWN":
                status["value"] = "SUPERSEDED"
                reasons.append("VERSION_SUPERSEDED")
        if conflict:
            reasons.append("VERSION_RELATION_CONFLICT")
            if state not in {"REJECTED", "REVOKED"} and value != "WITHDRAWN":
                status.update(value="UNKNOWN", verification_state="CONFLICT")
        return {"standard_version_id": version["id"], "artifact_binding_id": artifact["id"],
                "evaluated_at": when.isoformat(), "version_status": status, "relations": views,
                "reasons": list(dict.fromkeys(reasons)), "applicability_status": "NOT_REVIEWED"}


