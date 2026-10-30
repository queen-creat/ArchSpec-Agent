"""规范治理公开服务：声明登记、只读查询和可信维护操作。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from archspec.standard_governance.admission import AdmissionService
from archspec.standard_governance.models import POLICY_VERSION, GovernanceError, VersionRelationRow
from archspec.standard_governance.registry import SourceRegistryService, parse_date
from archspec.standard_governance.repository import (
    GovernanceRepository,
    canonical_json,
    fingerprint,
    record,
)
from archspec.standard_governance.verification import (
    VerificationService,
    active_event,
    matches_current_event,
)


class GovernanceService:
    def __init__(self, session):
        self.repo = GovernanceRepository(session)
        self.repository = self.repo
        self.admission = AdmissionService(self.repo)
        self.registry = SourceRegistryService(self.repo)
        self.verification = VerificationService(self.repo)

    def observe_source(self, declaration, artifact=None, *, legacy_kind=None, legacy_key=None,
                       legacy_manifest=None, commit=True):
        try:
            self.repo.lock_write()
            result = self.registry.observe(declaration, artifact, legacy_kind=legacy_kind, legacy_key=legacy_key,
                                           legacy_manifest=legacy_manifest)
            if commit:
                self.repo.session.commit()
            result["source"] = self.get_source(result["source"]["id"])
            return result
        except IntegrityError as error:
            self.repo.session.rollback()
            raise GovernanceError("MANIFEST_CHANGED", "来源登记并发变化，请重新读取", 409) from error
        except Exception:
            self.repo.session.rollback()
            raise

    def list_standards(self):
        return self.repo.list_standards()

    def get_version(self, version_id):
        self.repo.require_available()
        result = self.repo.get_version(version_id)
        event = self.repo.head("VERSION", version_id, "VERSION_STATUS")
        valid = matches_current_event(self.repo, "VERSION", result, event) and active_event(event, datetime.now(UTC))
        status = "VERIFIED" if valid else "REVOKED" if event and event["decision"] == "REVOKE" else "FAILED" if event and event["decision"] == "REJECT" else "UNVERIFIED"
        return {**result, "verification_status": status,
                "lifecycle_status": event["statement"]["lifecycle_status"] if valid else "UNKNOWN"}

    def get_source(self, source_id):
        self.repo.require_available()
        result = self.repo.get_source(source_id)
        event = self.repo.head("SOURCE", source_id, "SOURCE_IDENTITY")
        valid = matches_current_event(self.repo, "SOURCE", result, event) and active_event(event, datetime.now(UTC))
        use = self.repo.head("SOURCE", source_id, "CONTENT_USE")
        use_valid = matches_current_event(self.repo, "SOURCE", result, use) and active_event(use, datetime.now(UTC))
        status = "VERIFIED" if valid else "REVOKED" if event and event["decision"] == "REVOKE" else "FAILED" if event and event["decision"] == "REJECT" else "UNVERIFIED"
        return {**result, "verification_status": status, "verified_by": event["actor_id"] if valid else None,
                "verified_at": event["recorded_at"] if valid else None,
                "access_mode": use["statement"]["access_mode"] if use_valid else "UNKNOWN",
                "usage_right": use["statement"]["operations"] if use_valid else {},
                "redistribution_policy": use["statement"]["redistribution_policy"] if use_valid else "UNKNOWN"}

    def get_artifact(self, artifact_id):
        self.repo.require_available()
        return self.repo.get_artifact(artifact_id)

    def status(self):
        return {"migrated": self.repo.available(), "policy_version": POLICY_VERSION,
                "scope": "LOCAL_PROTOTYPE_ONLY", "applicability_status": "NOT_REVIEWED"}

    def history(self, subject_kind, subject_id):
        return self.repo.history(subject_kind, subject_id)

    def verify(self, actor_id, payload):
        return self.verification.verify(actor_id, payload)

    def revoke(self, actor_id, event_id, payload):
        return self.verification.revoke(actor_id, event_id, payload)

    def record_relation(self, actor_id, payload):
        fields = {"from_version_id", "to_version_id", "relation_type", "effective_at", "affected_clauses", "source_record_id"}
        if not isinstance(actor_id, str) or not actor_id.strip() or not isinstance(payload, dict) or set(payload) != fields:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "关系登记字段不完整或缺可信维护者")
        if payload["relation_type"] not in {"WHOLE_SUPERSEDES", "AMENDS", "PARTIAL_WITHDRAWAL"}:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "版本关系类型不支持")
        clauses = payload["affected_clauses"]
        if not isinstance(clauses, list) or not all(isinstance(clause, str) and clause for clause in clauses):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "部分条款范围须为明确条款编号列表")
        if payload["relation_type"] == "PARTIAL_WITHDRAWAL" and not clauses:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "部分废止必须注明受影响条款")
        effective = parse_date(payload["effective_at"])
        if effective is None:
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "版本关系缺少生效日期")
        try:
            self.repo.lock_write()
            previous = self.repo.get_version(payload["from_version_id"])
            new = self.repo.get_version(payload["to_version_id"]) if payload["to_version_id"] else None
            source = self.repo.get_source(payload["source_record_id"])
            if payload["relation_type"] != "PARTIAL_WITHDRAWAL" and new is None:
                raise GovernanceError("INVALID_VERIFICATION_REQUEST", "替代/修订必须有目标版本")
            if new and (new["id"] == previous["id"] or new["standard_id"] != previous["standard_id"]):
                raise GovernanceError("VERSION_RELATION_CONFLICT", "版本关系自环或身份不一致", 409)
            if source["standard_version_id"] not in {previous["id"], new["id"] if new else None}:
                raise GovernanceError("VERSION_RELATION_CONFLICT", "关系依据不属于所涉及版本", 409)
            if payload["relation_type"] == "WHOLE_SUPERSEDES":
                graph = {}
                for item in self.repo.list_relations():
                    if item["relation_type"] == "WHOLE_SUPERSEDES":
                        graph.setdefault(item["from_version_id"], set()).add(item["to_version_id"])
                pending, seen = [new["id"]], set()
                while pending:
                    node = pending.pop()
                    if node == previous["id"]:
                        raise GovernanceError("VERSION_RELATION_CONFLICT", "整版替代不能形成循环", 409)
                    if node not in seen:
                        seen.add(node)
                        pending.extend(graph.get(node, ()))
                if graph.get(previous["id"], set()) - {new["id"]}:
                    raise GovernanceError("VERSION_RELATION_CONFLICT", "已有不同整版替代关系，须先人工解决", 409)
            facts = {**payload, "effective_at": effective.isoformat(), "affected_clauses": sorted(set(clauses))}
            digest = fingerprint(facts)
            row = self.repo.session.scalar(select(VersionRelationRow).where(VersionRelationRow.relation_manifest_sha256 == digest))
            if row is None:
                row = VersionRelationRow(id=str(uuid4()), from_version_id=previous["id"],
                                         to_version_id=new["id"] if new else None, relation_type=payload["relation_type"],
                                         effective_at=effective, affected_clauses_json=canonical_json(facts["affected_clauses"]),
                                         source_record_id=payload["source_record_id"], relation_manifest_sha256=digest)
                self.repo.session.add(row)
                self.repo.session.flush()
            result = record(row)
            self.repo.session.commit()
            return result
        except Exception:
            self.repo.session.rollback()
            raise

    def record_review_policy(self, job_id, evidence_mode="AUTHORITATIVE", admission_policy_version=POLICY_VERSION):
        result = self.repo.save_review_policy(job_id, evidence_mode, admission_policy_version)
        self.repo.session.commit()
        return result
