"""旧 ID 的只读适配：按固定指纹关联，而不是沿用旧 OFFICIAL/CURRENT 授信。"""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect

from archspec.config import settings
from archspec.standard_governance.imports import GovernanceImportService


class ContentUseDenied(Exception):
    """使用限制不是输入格式错误；由应用统一返回 403，避免旧路由误捕获。"""
    code = "CONTENT_USE_NOT_AUTHORIZED"
    status_code = 403


def unresolved(reason, *, manifest=None):
    return {
        "policy_version": "SOURCE_ADMISSION_V1", "evidence_mode": "CANDIDATE_ONLY",
        "is_citable": False, "standard_version_id": None, "source_record_id": None,
        "artifact_binding_id": None, "legacy_snapshot_id": None,
        "verification_record_ids": [], "evaluated_at": datetime.now(UTC).isoformat(),
        "reasons": [reason], "applicability_status": "NOT_REVIEWED", "manifest_sha256": manifest,
    }


class LegacyAdmissionAdapter:
    def __init__(self, session, data_dir=None):
        self.session = session
        self.data_dir = Path(data_dir or session.info.get("archspec_data_dir") or settings.data_dir)
        self.importer = GovernanceImportService(session, self.data_dir)

    @property
    def repository(self):
        from archspec.standard_governance.repository import GovernanceRepository
        return GovernanceRepository(self.session)

    @property
    def admission(self):
        from archspec.standard_governance.admission import AdmissionService
        return AdmissionService(self.repository)

    def document_fingerprint(self, document_id):
        try:
            observation = self.importer.describe_document(document_id)
            # 文件实际内容不能只信 DB 的 sha256；替换原件后本轮重发必须停止。
            return observation["legacy_manifest"] + ":" + ",".join(observation["runtime_issues"])
        except (LookupError, OSError, ValueError):
            return None

    def _evaluate(self, description, operations):
        manifest = description["legacy_manifest"]
        repository = self.repository
        context = repository.read_legacy_context(description["legacy_kind"], description["legacy_key"], manifest)
        if not context["available"]:
            result = unresolved("GOVERNANCE_NOT_MIGRATED", manifest=manifest)
        else:
            binding = context["current"]
            if binding is None or not binding.get("artifact_binding_id"):
                previous = context["bindings"]
                result = unresolved("MANIFEST_CHANGED" if previous else "LEGACY_METADATA_UNVERIFIED",
                                    manifest=manifest)
            else:
                result = self.admission.evaluate(binding["artifact_binding_id"],
                                                 operations=operations,
                                                 runtime_issues=description["runtime_issues"])
                result["manifest_sha256"] = manifest
            # 页面上的“可引用”必须与实际读取门一致，不能挑新绑定绕过旧来源的拒绝。
            for operation in operations:
                try:
                    self._assert_read(description["legacy_kind"], description["legacy_key"], operation)
                except ContentUseDenied:
                    if "CONTENT_USE_CONTEXT_UNRESOLVED" not in result["reasons"]:
                        result["reasons"].append("CONTENT_USE_CONTEXT_UNRESOLVED")
                    result["is_citable"] = False
                    result["evidence_mode"] = "CANDIDATE_ONLY"
        for issue in description["runtime_issues"]:
            if issue not in result["reasons"]:
                result["reasons"].append(issue)
                result["is_citable"] = False
                result["evidence_mode"] = "CANDIDATE_ONLY"
        return result

    def for_document(self, document_id, operations=("LOCAL_SEARCH", "LOCAL_EXCERPT")):
        try:
            description = self.importer.describe_document(document_id)
        except LookupError:
            return unresolved("DOCUMENT_NOT_FOUND")
        except (OSError, ValueError):
            return unresolved("FULL_TEXT_TRACE_UNRELIABLE")
        return self._evaluate(description, operations)

    def for_snapshot(self, source_id, snapshot_id,
                     operations=("LOCAL_SEARCH", "LOCAL_EXCERPT")):
        try:
            description = self.importer.describe_snapshot(source_id, snapshot_id)
        except (LookupError, OSError, ValueError, KeyError, TypeError):
            return unresolved("FULL_TEXT_TRACE_UNRELIABLE")
        if description["artifact"] is None:
            return unresolved("FULL_TEXT_NOT_ACQUIRED")
        return self._evaluate(description, operations)

    def snapshot_validity(self, source_id, snapshot_id, *, as_of=None):
        """仅精确当前固定观察可使用版本事件；不写绑定或扩大任何操作许可。"""
        from archspec.standard_governance.source_validity import (
            SourceValidityService,
            unverified_view,
        )

        try:
            description = self.importer.describe_snapshot(source_id, snapshot_id)
        except (LookupError, OSError, ValueError, KeyError, TypeError):
            return unverified_view("FULL_TEXT_TRACE_UNRELIABLE")
        expected = description["artifact"]
        if expected is None:
            return unverified_view("FULL_TEXT_NOT_ACQUIRED")
        if description["runtime_issues"]:
            return unverified_view(description["runtime_issues"][0])
        repository = self.repository
        context = repository.read_legacy_context(description["legacy_kind"], description["legacy_key"],
                                                 description["legacy_manifest"])
        if not context["available"]:
            return unverified_view("GOVERNANCE_NOT_MIGRATED")
        binding = context["current"]
        if binding is None or not binding.get("artifact_binding_id"):
            previous = context["bindings"]
            return unverified_view("MANIFEST_CHANGED" if previous else
                                   "LEGACY_METADATA_UNVERIFIED")
        artifact = repository.get_artifact(binding["artifact_binding_id"])
        # 相同原件或版本不能代替此来源/处理绑定；不接受错连的历史artifact。
        fields = ("artifact_kind", "legacy_source_id", "legacy_snapshot_id",
                  "file_sha256", "processing_version", "content_manifest_sha256",
                  "page_count")
        if (any(artifact.get(key) != expected.get(key) for key in fields)
                or artifact["source_record_id"] != binding["source_record_id"]
                or artifact["standard_version_id"] != binding["standard_version_id"]):
            return unverified_view("ARTIFACT_BINDING_MISMATCH")
        from archspec.standard_governance.registry import code_parts

        version = repository.get_version(artifact["standard_version_id"])
        fixed = description["fixed_identity"]
        if (version["title"] != fixed["title"]
                or version["edition_code"] != code_parts(fixed["standard_code"])[1]):
            return unverified_view("VERSION_IDENTITY_MISMATCH", state="MANIFEST_CHANGED")
        derived = SourceValidityService(repository).evaluate(artifact["id"], as_of=as_of)
        # 仅内部组合核对使用，不进入公开视图或模型消息。
        return {**derived, "_fixed_identity": fixed}

    def allowed_document_ids(self, document_ids):
        return [document_id for document_id in document_ids
                if self.for_document(document_id)["is_citable"]]

    def _known_bindings(self, kind, key):
        # 绑定读取入口已经完整核合同；每个后续permission仍实时读取事件头。
        return [item for item in self.repository.bindings_for_legacy(kind, key)
                if item.get("artifact_binding_id")]

    def _assert_read(self, kind, key, operation):
        bindings = self._known_bindings(kind, key)
        permissions = {self.admission.permission(item["artifact_binding_id"], operation)
                       for item in bindings}
        self._assert_permissions(permissions, operation)

    @staticmethod
    def _assert_permissions(permissions, operation):
        # 已知拒绝不能因改角色/更新登记/进入旧页面而消失。多来源冲突保守拒绝。
        if "DENY" in permissions or "CONTEXT_UNRESOLVED" in permissions:
            raise ContentUseDenied(f"该资料登记的使用条件不允许 {operation}；只可查看题录和状态")
        if len(permissions) > 1 and "ALLOW" in permissions:
            raise ContentUseDenied("CONTENT_USE_CONTEXT_UNRESOLVED：多个来源使用条件尚未确定")

    def assert_snapshot_reads(self, source_id, snapshot_ids, operations):
        """联合本地检索/摘录门；全部处理谱系与全部来源在同一payload快照归约。"""
        from archspec.standard_governance.models import GovernanceError

        operations = tuple(operations)
        if operations != ("LOCAL_SEARCH", "LOCAL_EXCERPT"):
            raise GovernanceError("INVALID_VERIFICATION_REQUEST", "联合门仅支持本地检索和摘录")
        keys = [f"{source_id}:{identity}" for identity in snapshot_ids]
        context = self.repository.read_legacy_permissions("OFFICIAL_SNAPSHOT", keys)
        if not context["available"]:
            return
        admission = self.admission
        for operation in operations:
            for key in keys:
                permissions = {admission.permission_from_values(
                    item["source"], item["event"],
                    item["version"]["manifest_sha256"] if item["version"] is not None else None,
                    operation) for item in context["contexts"][key]}
                self._assert_permissions(permissions, operation)

    def assert_document_read(self, document_id, operation):
        self._assert_read("DOCUMENT", document_id, operation)

    def assert_snapshot_read(self, source_id, snapshot_id, operation):
        self._assert_read("OFFICIAL_SNAPSHOT", f"{source_id}:{snapshot_id}", operation)

    def model_snapshot_allowed(self, source_id, snapshot_id):
        """官方正文外传须绑定当前固定观察，并由所有已知来源明确允许。"""
        try:
            description = self.importer.describe_snapshot(source_id, snapshot_id)
        except (LookupError, OSError, ValueError, KeyError, TypeError):
            return False
        artifact = description["artifact"]
        if artifact is None or artifact.get("legacy_snapshot_id") != snapshot_id:
            return False
        # 外传许可不等于“可正式引用”；OCR质量/现行性仍由原有证据门判断。
        # 但实际原件与固定观察若已不对应，旧ALLOW不能替被更换的正文背书。
        if any(issue in description["runtime_issues"] for issue in (
                "RAW_FILE_CHANGED", "RAW_FILE_MISSING_OR_INVALID", "RAW_FILE_INVALID",
                "FULL_TEXT_TRACE_UNRELIABLE", "PROCESSING_INCOMPLETE")):
            return False
        repository = self.repository
        kind, key = description["legacy_kind"], description["legacy_key"]
        context = repository.read_legacy_context(kind, key, description["legacy_manifest"])
        if not context["available"]:
            return False
        current, bindings = context["current"], context["bindings"]
        if (current is None or not current.get("artifact_binding_id") or not bindings
                or any(not item.get("artifact_binding_id") for item in bindings)):
            return False
        permissions = {self.admission.permission(item["artifact_binding_id"], "MODEL_SEND")
                       for item in bindings}
        return permissions == {"ALLOW"}

    def model_document_allowed(self, document_id):
        bindings = self._known_bindings("DOCUMENT", document_id)
        if not bindings:
            tables = inspect(self.session.get_bind()).get_table_names()
            if "standard_metadata" in tables:
                from archspec.standards.repository import StandardMetadataRepository
                if StandardMetadataRepository(self.session).get_for_document(document_id) is not None:
                    return False
            # 未登记治理的普通项目材料仍需原有用户分享同意；不承诺识别任意粘贴规范。
            return True
        decision = self.for_document(document_id)
        if decision.get("artifact_binding_id") is None:
            return False
        if any(issue in decision["reasons"] for issue in (
                "RAW_FILE_CHANGED", "RAW_FILE_MISSING_OR_INVALID", "MANIFEST_CHANGED")):
            return False
        permissions = {self.admission.permission(item["artifact_binding_id"], "MODEL_SEND")
                       for item in bindings}
        return permissions == {"ALLOW"}
