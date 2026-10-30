"""固定来源的候选范围合同；不核定法律效力、工程适用性或强条身份。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["NATIONAL", "INDUSTRY", "LOCAL", "UNKNOWN"]
Lifecycle = Literal["CURRENT", "UPCOMING", "SUPERSEDED", "WITHDRAWN", "UNKNOWN"]
Mandatory = Literal["MANDATORY", "NON_MANDATORY", "UNKNOWN"]
_VERIFICATION_STATES = frozenset(
    {
        "VERIFIED",
        "UNVERIFIED",
        "EXPIRED",
        "REJECTED",
        "REVOKED",
        "MANIFEST_CHANGED",
        "CONFLICT",
    }
)
_IDENTITY_KEYS = (
    "source_id",
    "snapshot_id",
    "metadata_id",
    "snapshot_fingerprint",
    "file_sha256",
    "title",
    "standard_code",
)


def _fixed_field(fixed, name, known_states):
    field = fixed.get("fields", {}).get(name)
    if not isinstance(field, dict) or field.get("state") not in {"UNKNOWN", *known_states}:
        raise ValueError("固定来源的筛选字段无法核对")
    value = field.get("value")
    if field["state"] == "UNKNOWN":
        if value is not None:
            raise ValueError("固定来源未知字段的内容不一致")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("固定来源的筛选字段内容无效")
    return value


class SourceMetadataFilter(BaseModel):
    """字段内并集、字段间交集；只有已存在的固定观察可以支持匹配。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, frozen=True)
    region: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    levels: Annotated[list[Level], Field(min_length=1, max_length=20)] | None = None
    lifecycle_statuses: Annotated[list[Lifecycle], Field(min_length=1, max_length=20)] | None = None
    mandatory_statuses: Annotated[list[Mandatory], Field(min_length=1, max_length=20)] | None = None

    @property
    def resolved_region(self):
        # 仅复用已有宜昌别名；不推断上下级行政范围，序列化仍保留原请求。
        return "湖北·宜昌" if self.region in {"宜昌", "宜昌市"} else self.region

    def matches(self, fixed_metadata: dict, current_validity: dict) -> bool:
        """匹配与拒绝原因共用一次规则，不维护两套筛选条件。"""
        return not self.exclusion_reasons(fixed_metadata, current_validity)

    def exclusion_reasons(self, fixed_metadata: dict, current_validity: dict) -> list[dict]:
        """只消费固定观察和当前绑定视图；未知与不符分开，不推断工程事实。"""
        if (
            not isinstance(fixed_metadata, dict)
            or not isinstance(current_validity, dict)
            or not isinstance(fixed_metadata.get("fields"), dict)
            # 地方原件可以未载文号；空编号不替代来源、文件及快照的完整身份绑定。
            or not isinstance(fixed_metadata.get("standard_code"), str)
            or any(
                (key != "standard_code" and not fixed_metadata.get(key))
                or current_validity.get(key) != fixed_metadata.get(key)
                for key in _IDENTITY_KEYS
            )
        ):
            raise ValueError("筛选所用来源档案与当前固定身份不一致")
        level = _fixed_field(fixed_metadata, "level", {"CODE_CLUE", "NOTICE_OBSERVED"})
        region = _fixed_field(fixed_metadata, "region", {"NOTICE_OBSERVED"})
        if level is not None and level not in {"NATIONAL", "INDUSTRY", "LOCAL"}:
            raise ValueError("固定来源的类别无法核对")
        status = current_validity.get("version_status")
        if (
            not isinstance(status, dict)
            or status.get("verification_state") not in _VERIFICATION_STATES
            or status.get("value")
            not in {"CURRENT", "UPCOMING", "SUPERSEDED", "WITHDRAWN", "UNKNOWN"}
        ):
            raise ValueError("当前来源生命周期字段无法核对")
        lifecycle = status["value"] if status["verification_state"] == "VERIFIED" else "UNKNOWN"
        # 仅支持当前已有的两个宜昌别名，不据此推断行政范围包含关系。
        requested_region = self.resolved_region
        reasons = []
        def rejected(field, unknown, message):
            reasons.append({"field": field, "state": "UNKNOWN" if unknown else "MISMATCH",
                            "message": message})
        if requested_region is not None and region != requested_region:
            rejected("region", region is None,
                "尚无地方来源档案证明该正文属于本次地域；未凭下载网址或题名推定地域。"
                if region is None else "地方来源档案的地域与本次筛选不同，未扩大范围。")
        if self.levels is not None and (level or "UNKNOWN") not in self.levels:
            rejected("level", level is None, "来源类别未知或不符合本次筛选，未猜测类别。")
        if self.lifecycle_statuses is not None and lifecycle not in self.lifecycle_statuses:
            rejected("lifecycle", lifecycle == "UNKNOWN",
                "尚未取得经核验的版本状态，未凭编号或实施日期推定现行。"
                if lifecycle == "UNKNOWN" else "经核验的版本状态与本次筛选不同。")
        # 没有强条核验合同；现行、GB编号或“应”都不能给强条背书。
        if self.mandatory_statuses is not None and "UNKNOWN" not in self.mandatory_statuses:
            rejected("mandatory", True, "尚无强条核验依据，未凭编号或措辞推定强制性。")
        return reasons
