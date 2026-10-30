"""旧开发预算同连接捕获正式候选；保留历史，未默认接入应用。"""

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, Text, inspect
from sqlalchemy.engine import Connection

from archspec.persistence import budget, control_image

_SAFE = "旧预算源候选无法完整核验，未修改原账本、提交目标或激活模型"
_METADATA = MetaData()
_LIMIT = Table(
    "development_budget",
    _METADATA,
    # 真实旧 DDL 未显式 NOT NULL；不能借新声明改变 SQLite INTEGER PK 物理合同。
    Column("id", Integer, primary_key=True, nullable=True),
    Column("limit_cents", Integer, nullable=False),
    CheckConstraint("id = 1"),
    CheckConstraint("limit_cents BETWEEN 0 AND 1200"),
)
_CALLS = Table(
    "development_calls",
    _METADATA,
    Column("id", Integer, primary_key=True, nullable=True),
    Column("request_digest", Text, nullable=False),
    Column("reserved_cents", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("actual_model", Text),
    Column("actual_usage", Text),
    Column("request_id", Text),
    CheckConstraint("reserved_cents = 10"),
    CheckConstraint("status IN ('RESERVED', 'COMPLETED', 'FAILED')"),
)
_TABLES = (_LIMIT, _CALLS)
_NAMES = tuple(sorted(table.name for table in _TABLES))


class LegacyBudgetUnavailable(control_image.ControlImageUnavailable):
    """旧账本结构/历史或事务不完整，保持原数据并拒绝转换。"""


@dataclass(frozen=True)
class LegacyBudgetCandidate:
    """仅同一源事务取得的候选，无法单独证明冻结、授权或外层提交。"""

    source_image: control_image.ControlImage = field(repr=False)
    migration_id: str
    limit_cents: int
    call_count: int
    reserved_cents: int
    history_json: str = field(repr=False)
    migration_manifest_sha256: str
    unresolved_legacy_call_ids: tuple[int, ...] = field(repr=False)
    activation_blockers: tuple[str, ...]
    state: str = field(default="SOURCE_CANDIDATE", init=False)

    @property
    def history_records(self):
        # 每次返回独立 JSON 副本，外部修改不能改变候选中的完整历史。
        return json.loads(self.history_json)


def capture_on_connection(
    connection, *, migration_id, max_rows=control_image.MAX_ROWS, max_bytes=control_image.MAX_BYTES
):
    """消费已有真实 SQLite 事务，不另开连接、写行、DDL 或拥有事务出口。

    调用方须在共同停写与已授权源范围下持有该连接。这里不接受文件路径或
    自报 records，单次 SOURCE_CANDIDATE 也不能代替 source_cut/激活验证。
    """
    try:
        if (
            not isinstance(connection, Connection)
            or connection.closed
            or connection.dialect.name != "sqlite"
            or not connection.in_transaction()
            or connection.get_transaction() is None
            or not connection.get_transaction().is_active
            or not connection.connection.driver_connection.in_transaction
            or not isinstance(migration_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration_id) is None
            or type(max_rows) is not int
            or not 1 <= max_rows <= control_image.MAX_ROWS
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= control_image.MAX_BYTES
        ):
            raise LegacyBudgetUnavailable(_SAFE)
        if tuple(sorted(inspect(connection).get_table_names())) != _NAMES:
            # 漏表或混入其他状态不能被旧 SELECT 列表默默隐藏。
            raise LegacyBudgetUnavailable(_SAFE)
        image = control_image.capture_control_image(
            connection, _TABLES, max_rows=max_rows, max_bytes=max_bytes
        )
        entries = {entry["name"]: entry for entry in image.to_dict()["tables"]}
        limits = entries[_LIMIT.name]["rows"]
        if len(limits) != 1 or limits[0][0] != 1:
            raise LegacyBudgetUnavailable(_SAFE)
        limit = budget._limit(limits[0][1])
        originals = [
            dict(zip(_CALLS.columns.keys(), row, strict=True))
            for row in entries[_CALLS.name]["rows"]
        ]
        # 复用现有历史验证器；未知 usage 键严格拒绝，不丢键或用0代替。
        history = budget._history(originals, migration_id)
        reserved = sum(record["reserved_cents"] for record in history)
        if reserved > limit:
            raise LegacyBudgetUnavailable(_SAFE)
        unresolved = tuple(
            record["legacy_id"] for record in history if record["status"] == "RESERVED"
        )
        return LegacyBudgetCandidate(
            source_image=image,
            migration_id=migration_id,
            limit_cents=limit,
            call_count=len(history),
            reserved_cents=reserved,
            history_json=budget._json(history),
            migration_manifest_sha256=budget._migration_manifest(migration_id, limit, history),
            unresolved_legacy_call_ids=unresolved,
            # 无可信 task/op/phase 关联，不能猜摘要、续发、退款或改写旧 RESERVED。
            activation_blockers=("UNLINKED_LEGACY_RESERVED",) if unresolved else (),
        )
    except LegacyBudgetUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 底层SQL/历史内容不得随异常传播
        raise LegacyBudgetUnavailable(_SAFE) from None
