"""正式引用与具体操作许可分别判断；全部未知状态失败关闭。"""

from datetime import UTC, datetime

from archspec.standard_governance.models import (
    OPERATIONS,
    POLICY_VERSION,
    GovernanceError,
    StandardRow,
)
from archspec.standard_governance.registry import parse_date
from archspec.standard_governance.verification import (
    active_event,
    matches_current_event,
    parse_time,
)


class AdmissionService:
    def __init__(self, repository):
        self.repo = repository

    def _event(self, kind, subject, scope):
        event = self.repo.head(kind, subject["id"], scope)
        return event if matches_current_event(self.repo, kind, subject, event) else None

    @staticmethod
    def _declared_denial(source, operation):
        declared = source.get("declared_usage_right", {})
        return (source["declared_access_mode"] == "RESTRICTED" or declared.get(operation) == "DENY"
                or operation == "REDISTRIBUTE" and source["declared_redistribution_policy"] == "DENY")

    @classmethod
    def _permission_before_version(cls, source, event, operation):
        """单操作和联合门共用纯值判据；已知拒绝优先于期限或版本未知。"""
        if cls._declared_denial(source, operation):
            return "DENY"
        if event is None or event["subject_manifest_sha256"] != source["manifest_sha256"]:
            return "UNKNOWN"
        if event["decision"] in {"REJECT", "REVOKE"}:
            return "DENY"
        statement = event["statement"]
        value = statement.get("operations", {}).get(operation, "UNKNOWN")
        if (statement.get("access_mode") == "RESTRICTED" or value == "DENY"
                or operation == "REDISTRIBUTE" and statement.get("redistribution_policy") == "DENY"):
            return "DENY"
        return None

    @classmethod
    def permission_from_values(cls, source, event, version_manifest, operation, as_of=None):
        """只消费一条语句所得固定值；每个操作取当前时间，不沿用上一门的判断。"""
        if operation not in OPERATIONS:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "正文操作不支持")
        early = cls._permission_before_version(source, event, operation)
        if early is not None:
            return early
        if event.get("standard_version_manifest_sha256") != version_manifest:
            return "UNKNOWN"
        when = parse_time(as_of) if as_of is not None else datetime.now(UTC)
        if not active_event(event, when):
            return "UNKNOWN"
        statement = event["statement"]
        if operation == "REDISTRIBUTE" and statement.get("redistribution_policy") != "ALLOW":
            return "UNKNOWN"
        return statement.get("operations", {}).get(operation, "UNKNOWN")

    def permission(self, artifact_id, operation, as_of=None):
        if operation not in OPERATIONS:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "正文操作不支持")
        if not self.repo.available():
            return "UNKNOWN"
        artifact = self.repo.get_artifact(artifact_id)
        source = self.repo.get_source(artifact["source_record_id"])
        if self._declared_denial(source, operation):
            return "DENY"
        event = self.repo.head("SOURCE", source["id"], "CONTENT_USE")
        early = self._permission_before_version(source, event, operation)
        if early is not None:
            return early
        return self.permission_from_values(
            source, event, self.repo.version_context_manifest("SOURCE", source), operation, as_of)

    def evaluate(self, artifact_id, operations=("LOCAL_SEARCH", "LOCAL_EXCERPT"), as_of=None,
                 runtime_manifest_sha256=None, runtime_issues=()):
        when = parse_time(as_of) if as_of is not None else datetime.now(UTC)
        result = {"policy_version": POLICY_VERSION, "is_citable": False, "evidence_mode": "CANDIDATE_ONLY",
                  "standard_version_id": None, "source_record_id": None, "artifact_binding_id": None,
                  "legacy_snapshot_id": None, "verification_record_ids": [], "evaluated_at": when.isoformat(),
                  "reasons": [], "applicability_status": "NOT_REVIEWED"}
        if not self.repo.available():
            result["reasons"] = ["GOVERNANCE_NOT_MIGRATED"]
            return result
        artifact = self.repo.get_artifact(artifact_id)
        version = self.repo.get_version(artifact["standard_version_id"])
        source = self.repo.get_source(artifact["source_record_id"])
        result.update({"standard_version_id": version["id"], "source_record_id": source["id"],
                       "artifact_binding_id": artifact["id"], "legacy_snapshot_id": artifact["legacy_snapshot_id"]})
        reasons, events = [], []
        standard = self.repo.session.get(StandardRow, version["standard_id"])
        if standard is None or standard.canonical_key is None:
            reasons.append("VERSION_IDENTITY_UNRESOLVED")
        checks = (("SOURCE", source, "SOURCE_IDENTITY"), ("VERSION", version, "VERSION_STATUS"),
                  ("ARTIFACT", artifact, "FULL_TEXT_BINDING"), ("SOURCE", source, "CONTENT_USE"))
        accepted = {}
        for kind, subject, scope in checks:
            event = self._event(kind, subject, scope)
            if not active_event(event, when):
                reasons.append(scope + "_UNVERIFIED")
            else:
                accepted[scope] = event
                events.append(event["id"])
        lifecycle = accepted.get("VERSION_STATUS", {}).get("statement", {})
        if lifecycle:
            if lifecycle["lifecycle_status"] != "CURRENT":
                reasons.append("VERSION_NOT_CURRENT")
            effective = parse_date(lifecycle.get("effective_at"))
            if effective is None:
                reasons.append("VERSION_EFFECTIVE_DATE_UNKNOWN")
            elif effective > when.date():
                reasons.append("VERSION_NOT_YET_EFFECTIVE")
        explicitly_confirmed_upload = (
            source["attachment_kind"] == "UNCONFIRMED"
            and artifact["artifact_kind"] == "UPLOADED_DOCUMENT"
            and accepted.get("FULL_TEXT_BINDING", {}).get("statement", {}).get("complete_full_text") is True
        )
        if source["attachment_kind"] != "FULL_TEXT" and not explicitly_confirmed_upload:
            reasons.append("NOT_COMPLETE_STANDARD_ATTACHMENT")
        if source["standard_version_id"] != version["id"] or source["file_sha256"] != artifact["file_sha256"]:
            reasons.append("FULL_TEXT_BINDING_MISMATCH")
        if artifact["trace_level"] != "PDF_PAGE" or not artifact["page_count"]:
            reasons.append("FULL_TEXT_TRACE_UNRELIABLE")
        if artifact["processing_status"] != "READY" or artifact["quality_issues"]:
            reasons.append("PROCESSING_QUALITY_UNVERIFIED")
        if runtime_manifest_sha256 is not None and runtime_manifest_sha256 != artifact["content_manifest_sha256"]:
            reasons.append("ARTIFACT_MANIFEST_CHANGED")
        reasons.extend(runtime_issues)
        if not isinstance(operations, (list, tuple)) or not operations or any(op not in OPERATIONS for op in operations):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "正式引用须指明本次实际操作")
        for operation in operations:
            value = self.permission(artifact_id, operation, when)
            if value != "ALLOW":
                reasons.append("CONTENT_USE_DENIED:" + operation if value == "DENY" else "CONTENT_USE_UNKNOWN:" + operation)
        for relation in self.repo.list_relations(version["id"]):
            if parse_date(relation["effective_at"]) > when.date():
                continue
            event = self._event("RELATION", relation, "VERSION_RELATION")
            if relation["relation_type"] == "WHOLE_SUPERSEDES" and active_event(event, when):
                reasons.append("VERSION_SUPERSEDED")
                events.append(event["id"])
            else:
                # 当前尚不能生成修订后的完整正文，已知关系范围不能被忽略。
                reasons.append("VERSION_RELATION_UNRESOLVED")
        result["verification_record_ids"] = events
        result["reasons"] = list(dict.fromkeys(reasons))
        if not reasons:
            result.update({"is_citable": True, "evidence_mode": "AUTHORITATIVE"})
        return result
