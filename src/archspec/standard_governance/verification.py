"""维护者核验只追加事件，检查对象指纹、范围、依据和并发事件头。"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, OperationalError

from archspec.standard_governance.models import (
    OPERATIONS,
    SCOPE_SUBJECTS,
    GovernanceError,
    VerificationRecordRow,
)
from archspec.standard_governance.registry import code_parts, parse_date
from archspec.standard_governance.repository import canonical_json

REQUEST_FIELDS = {"subject_kind", "subject_id", "scope", "decision", "expected_manifest_sha256",
                  "expected_head_id", "basis_source_record_id", "basis_note", "checked_as_of",
                  "valid_until", "statement"}
LOGGER = logging.getLogger(__name__)


def log_failed_attempt(operation, error):
    # 尝试日志不含请求、凭据、SQL、来源正文或异常原文，且不推进实质事件头。
    LOGGER.warning("verification_attempt_failed operation=%s error_type=%s", operation, type(error).__name__)


def rollback_failed_attempt(repository, operation):
    """清理异常不可覆盖提交结果未知或既有业务拒绝，也不记录驱动原文。"""
    try:
        repository.session.rollback()
    except Exception as error:  # noqa: BLE001 - 清理失败仅记录类型，保留原安全错误
        log_failed_attempt(f"{operation}_ROLLBACK", error)


def parse_time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError) as error:
        raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验时间须包含时区") from error


def active_event(event, as_of):
    """不从历史成功回退；实质拒绝/撤销不因期限到期而复活。"""
    return bool(event and event["decision"] == "VERIFY" and event.get("valid_until")
                and parse_time(event["checked_as_of"]) <= as_of < parse_time(event["valid_until"]))


def version_context_manifest(repository, kind, subject):
    """兼容旧调用方；版本绑定统一由仓库读取，不在服务中复制。"""
    return repository.version_context_manifest(kind, subject)


def matches_current_event(repository, kind, subject, event):
    return bool(event and event["subject_manifest_sha256"] == subject["manifest_sha256"]
                and event.get("standard_version_manifest_sha256")
                == version_context_manifest(repository, kind, subject))


class VerificationService:
    def __init__(self, repository):
        self.repo = repository

    def verify(self, actor_id, payload):
        now = datetime.now(UTC)
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 100:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "缺少可信维护者标识")
        if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验字段不完整或包含不允许字段")
        kind, scope, decision = payload["subject_kind"], payload["scope"], payload["decision"]
        if (not all(isinstance(item, str) for item in (kind, scope, decision))
                or SCOPE_SUBJECTS.get(scope) != kind or decision not in {"VERIFY", "REJECT"}):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验范围、对象或决定不匹配")
        if not isinstance(payload["basis_note"], str) or not payload["basis_note"].strip():
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验必须记录具体依据")
        checked = parse_time(payload["checked_as_of"])
        valid_until = parse_time(payload["valid_until"])
        if checked > now or valid_until <= now or valid_until <= checked:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验截至时间/有效期不能虚构或已过期")
        try:
            self.repo.lock_write(kind, payload["subject_id"])
            subject = self.repo.get_subject(kind, payload["subject_id"])
            self._compare(subject, kind, scope, payload)
            basis = self.repo.get_source(payload["basis_source_record_id"])
            self._basis_matches(kind, subject, basis)
            statement = self._statement(scope, subject, payload["statement"], decision)
            result = self._append(actor_id.strip(), subject, kind, scope, decision, statement,
                                  checked, valid_until, basis["id"], payload["basis_note"].strip())
            self.repo.commit_write()
            return result
        except (IntegrityError, OperationalError) as error:
            rollback_failed_attempt(self.repo, "VERIFY")
            log_failed_attempt("VERIFY", error)
            raise GovernanceError("VERIFICATION_HEAD_CHANGED", "并发核验状态已变化，请重新读取事件头", 409) from None
        except Exception as error:
            rollback_failed_attempt(self.repo, "VERIFY")
            if not isinstance(error, GovernanceError):
                log_failed_attempt("VERIFY", error)
                raise GovernanceError("GOVERNANCE_TECHNICAL_FAILURE", "核验暂未完成，旧事件与原有效期保留", 503) from None
            raise

    def _compare(self, subject, kind, scope, payload):
        if subject["manifest_sha256"] != payload["expected_manifest_sha256"]:
            raise GovernanceError("MANIFEST_CHANGED", "固定对象已变化，旧核验请求不再适用", 409)
        head = self.repo.head(kind, subject["id"], scope)
        if (head["id"] if head else None) != payload["expected_head_id"]:
            raise GovernanceError("VERIFICATION_HEAD_CHANGED", "核验事件头已变化，请重新确认", 409)

    def _basis_matches(self, kind, subject, basis):
        version_id = subject.get("standard_version_id", subject["id"] if kind == "VERSION" else None)
        allowed = {version_id}
        if kind == "RELATION":
            allowed = {subject["from_version_id"], subject["to_version_id"]}
        if basis["standard_version_id"] not in allowed:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验依据与指定版本不对应")

    def _statement(self, scope, subject, value, decision):
        if not isinstance(value, dict):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "核验声明须为对象")
        if decision == "REJECT":
            if value:
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "拒绝核验不夹带允许声明")
            return {}
        fields = {
            "SOURCE_IDENTITY": ({"standard_code", "title", "publisher"}, set()),
            "VERSION_STATUS": ({"lifecycle_status", "effective_at"}, {"published_at"}),
            "FULL_TEXT_BINDING": ({"artifact_binding_id", "file_sha256", "processing_version",
                                   "content_manifest_sha256", "page_count", "complete_full_text", "review_note"}, set()),
            "CONTENT_USE": ({"access_mode", "operations", "redistribution_policy"}, set()),
            "VERSION_RELATION": ({"relation_type", "from_version_id", "to_version_id", "effective_at", "affected_clauses"}, set()),
        }
        required, optional = fields[scope]
        if not required <= value.keys() or set(value) - required - optional:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "本范围核验声明字段不完整或越界")
        if scope == "SOURCE_IDENTITY":
            version = self.repo.get_version(subject["standard_version_id"])
            if (code_parts(str(value["standard_code"]))[1] != version["edition_code"]
                    or value["title"] != version["title"] or value["publisher"] != subject["publisher"]):
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "来源身份核验与登记版本不对应")
        elif scope == "VERSION_STATUS":
            if value["lifecycle_status"] not in {"UNKNOWN", "UPCOMING", "CURRENT", "SUPERSEDED", "WITHDRAWN"}:
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "生命周期状态不支持")
            if value["lifecycle_status"] in {"CURRENT", "UPCOMING"} and value["effective_at"] is None:
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "现行/待实施核验必须有实施日期")
            value = {**value, "effective_at": (parsed.isoformat() if (parsed := parse_date(value["effective_at"])) else None)}
            if "published_at" in value:
                value["published_at"] = (parsed.isoformat() if (parsed := parse_date(value["published_at"])) else None)
        elif scope == "FULL_TEXT_BINDING":
            pairs = {"artifact_binding_id": "id", "file_sha256": "file_sha256",
                     "processing_version": "processing_version", "content_manifest_sha256": "content_manifest_sha256"}
            if (any(value[key] != subject[target] for key, target in pairs.items())
                    or value["complete_full_text"] is not True or type(value["page_count"]) is not int
                    or value["page_count"] < 1 or not isinstance(value["review_note"], str) or not value["review_note"].strip()
                    or (subject["page_count"] is not None and value["page_count"] != subject["page_count"])):
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "正文核验与文件/处理/页码绑定不一致")
        elif scope == "CONTENT_USE":
            ops = value["operations"]
            if (value["access_mode"] not in {"UNKNOWN", "PUBLIC_DOWNLOAD", "ONLINE_ONLY", "AUTHORIZED_LOCAL", "RESTRICTED"}
                    or value["redistribution_policy"] not in {"ALLOW", "DENY", "UNKNOWN"}
                    or not isinstance(ops, dict) or set(ops) - OPERATIONS
                    or any(item not in {"ALLOW", "DENY", "UNKNOWN"} for item in ops.values())):
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "访问/用途/再分发声明不合法")
        elif scope == "VERSION_RELATION":
            if any(value[key] != subject[key] for key in required):
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "关系核验与固定关系声明不一致")
        return value

    def _append(self, actor, subject, kind, scope, decision, statement, checked, until, basis, note):
        head = self.repo.head(kind, subject["id"], scope)
        row = VerificationRecordRow(id=str(uuid4()), subject_kind=kind, subject_id=subject["id"], scope=scope,
                                    subject_manifest_sha256=subject["manifest_sha256"], sequence=head["sequence"] + 1 if head else 1,
                                    standard_version_manifest_sha256=version_context_manifest(self.repo, kind, subject),
                                    decision=decision, actor_id=actor, checked_as_of=checked, valid_until=until,
                                    basis_source_record_id=basis, basis_note=note, statement_json=canonical_json(statement),
                                    supersedes_event_id=head["id"] if head else None)
        return self.repo.append_event(row, expected_head_id=head["id"] if head else None)

    def revoke(self, actor_id, event_id, payload):
        if not isinstance(actor_id, str) or not actor_id.strip() or not isinstance(payload, dict):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "撤销需要可信维护者及明确原因")
        if set(payload) != {"expected_head_id", "expected_manifest_sha256", "reason"} or not isinstance(payload["reason"], str) or not payload["reason"].strip():
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "撤销字段不完整或包含伪造字段")
        try:
            self.repo.require_available()
            # 锁前只读不可变对象身份用于选锁；实质事件、指纹和头须在锁内重新读。
            identity = self.repo.get_event(event_id)
            self.repo.lock_write(identity["subject_kind"], identity["subject_id"])
            event = self.repo.get_event(event_id)
            if any(event[key] != identity[key] for key in ("subject_kind", "subject_id", "scope")):
                raise GovernanceError("VERIFICATION_HEAD_CHANGED", "核验事件身份已变化，请重新读取", 409)
            subject = self.repo.get_subject(event["subject_kind"], event["subject_id"])
            self._compare(subject, event["subject_kind"], event["scope"], payload)
            head = self.repo.head(event["subject_kind"], event["subject_id"], event["scope"])
            if (head["id"] != event_id or event["decision"] != "VERIFY"
                    or event["subject_manifest_sha256"] != subject["manifest_sha256"]):
                raise GovernanceError("VERIFICATION_HEAD_CHANGED", "只能撤销当前固定对象的核验头", 409)
            result = self._append(actor_id.strip(), subject, event["subject_kind"], event["scope"], "REVOKE", {},
                                  datetime.now(UTC), None, event["basis_source_record_id"], payload["reason"].strip())
            self.repo.commit_write()
            return result
        except (IntegrityError, OperationalError) as error:
            rollback_failed_attempt(self.repo, "REVOKE")
            log_failed_attempt("REVOKE", error)
            raise GovernanceError("VERIFICATION_HEAD_CHANGED", "并发撤销已变化", 409) from None
        except Exception as error:
            rollback_failed_attempt(self.repo, "REVOKE")
            if not isinstance(error, GovernanceError):
                log_failed_attempt("REVOKE", error)
                raise GovernanceError("GOVERNANCE_TECHNICAL_FAILURE", "撤销暂未完成，请重新读取当前状态", 503) from None
            raise
