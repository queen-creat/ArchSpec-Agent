"""固定档案与当前治理视图组合；不改出生档案或推断规范现行性。"""

from datetime import UTC, datetime

IDENTITY_FIELDS = ("source_id", "snapshot_id", "metadata_id", "snapshot_fingerprint",
                   "file_sha256", "title", "standard_code")
VIEW_FIELDS = ("standard_version_id", "artifact_binding_id", "version_status",
               "relations", "reasons", "applicability_status")


def compose_view(fixed, derived, *, evaluated_at=None):
    """公开核对日期稳定；有效期和当前事件仍由真实UTC时刻即时判断。"""
    from archspec.standard_governance.source_validity import unverified_view

    when = evaluated_at or datetime.now(UTC)
    identity = derived.get("_fixed_identity")
    if identity is not None and any(identity.get(key) != fixed[key] for key in (
            "source_id", "snapshot_id", "title", "standard_code",
            "file_sha256", "snapshot_fingerprint")):
        derived = unverified_view("FIXED_SOURCE_IDENTITY_MISMATCH", state="MANIFEST_CHANGED")
    elif derived["version_status"]["verification_state"] == "VERIFIED" and identity is None:
        # 没有本次真实固定字节绑定，不让第三方旧provider只给一个成功标记。
        derived = unverified_view("FIXED_SOURCE_IDENTITY_UNPROVEN")
    return {"schema_version": "SOURCE_VALIDITY_V1",
            **{key: fixed[key] for key in IDENTITY_FIELDS},
            # 日期只用于显示，不是授权缓存或版本核验有效期。
            "evaluated_at": when.astimezone(UTC).date().isoformat(),
            **{key: derived[key] for key in VIEW_FIELDS}}
