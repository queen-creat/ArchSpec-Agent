"""QA任务表显式升级：只读预览、一致备份、短DDL事务和旧业务指纹保护。"""
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect
from sqlalchemy.schema import CreateIndex, CreateTable

from archspec.official_qa.task_models import QAOperationRow, QATaskRow
from archspec.projects.models import DesignProjectRow

TABLES = (QATaskRow.__table__, QAOperationRow.__table__)
TABLE_NAMES = tuple(table.name for table in TABLES)
MIGRATION_ID = "2026-10-13-QA_TASK_V1"


def _identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("旧库标识无法安全核对")
    return '"' + value + '"'


def _business_fingerprint(connection):
    """本机流式哈希全部旧表与schema；对外仅指纹，不输出私人行内容。"""
    digest = hashlib.sha256()
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                "WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    for kind, name, table, sql in schema:
        if table not in TABLE_NAMES:
            digest.update(json.dumps([kind, name, table, sql], ensure_ascii=False).encode())
    names = [row[1] for row in schema if row[0] == "table" and row[1] not in TABLE_NAMES]
    for name in sorted(names):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_identifier(name)})")]
        # 不依赖rowid，兼容有复合主键的WITHOUT ROWID旧表。
        order = ",".join(_identifier(column) for column in columns)
        cursor = connection.execute(f"SELECT * FROM {_identifier(name)} ORDER BY {order}")
        digest.update(name.encode())
        for row in cursor:
            encoded = [({"bytes_hex": item.hex()} if isinstance(item, bytes) else item) for item in row]
            digest.update(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


class QATaskMigration:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "archspec.db"
        if not self.path.is_file():
            raise ValueError("没有现有数据库，不建立空库或隐式任务表")
        assert DesignProjectRow.__table__.name == "design_projects"

    def _readonly(self):
        return sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)

    def preview(self):
        with self._readonly() as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            fingerprint = _business_fingerprint(connection)
        state = "READY_TO_APPLY"
        if "design_projects" not in names:
            state = "SCHEMA_CONFLICT"
        elif names & set(TABLE_NAMES):
            state = "ALREADY_APPLIED" if self._schema_matches() else "SCHEMA_CONFLICT"
        return {"mode": "DRY_RUN", "state": state, "migration_id": MIGRATION_ID,
                "target_tables": list(TABLE_NAMES), "old_business_fingerprint": fingerprint}

    def _schema_matches(self):
        engine = create_engine("sqlite://", creator=self._readonly)
        try:
            reader = inspect(engine)
            names = set(reader.get_table_names())
            if not set(TABLE_NAMES) <= names:
                return False
            for table in TABLES:
                actual = reader.get_columns(table.name)
                expected = {column.name: column for column in table.columns}
                if set(expected) != {column["name"] for column in actual}:
                    return False
                if any(str(column["type"]) != str(expected[column["name"]].type)
                       or column["nullable"] != expected[column["name"]].nullable for column in actual):
                    return False
                if reader.get_pk_constraint(table.name)["constrained_columns"] != [
                        column.name for column in table.primary_key.columns]:
                    return False
                actual_checks = {row["sqltext"] for row in reader.get_check_constraints(table.name)}
                expected_checks = {str(rule.sqltext) for rule in table.constraints if hasattr(rule, "sqltext")}
                if actual_checks != expected_checks:
                    return False
                actual_foreign = {
                    (tuple(row["constrained_columns"]), row["referred_table"],
                     tuple(row["referred_columns"]), row.get("options", {}).get("ondelete"))
                    for row in reader.get_foreign_keys(table.name)}
                expected_foreign = {
                    (tuple(column.name for column in rule.columns),
                     next(iter(rule.elements)).column.table.name,
                     tuple(item.column.name for item in rule.elements), rule.ondelete)
                    for rule in table.foreign_key_constraints}
                if actual_foreign != expected_foreign:
                    return False
                actual_indexes = {
                    (row["name"], tuple(row["column_names"]), bool(row["unique"]))
                    for row in reader.get_indexes(table.name)}
                expected_indexes = {
                    (index.name, tuple(column.name for column in index.columns), bool(index.unique))
                    for index in table.indexes}
                if actual_indexes != expected_indexes:
                    return False
            return True
        finally:
            engine.dispose()

    def apply(self):
        preview = self.preview()
        if preview["state"] == "SCHEMA_CONFLICT":
            raise ValueError("任务表结构不符合当前合同，未更改旧库")
        if preview["state"] == "ALREADY_APPLIED":
            return {**preview, "mode": "ALREADY_APPLIED", "created_tables": [],
                    "old_business_unchanged": True}
        folder = self.data_dir / "qa-task-backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = folder / f"before-{MIGRATION_ID}-{stamp}-{uuid4()}.sqlite"
        with self._readonly() as original, sqlite3.connect(backup_path) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份完整性检查失败，未升级")
            backup_fingerprint = _business_fingerprint(backup)
        with sqlite3.connect(self.path) as current:
            current.execute("BEGIN IMMEDIATE")
            try:
                before = _business_fingerprint(current)
                if before != backup_fingerprint:
                    raise ValueError("备份后旧业务发生变化，未升级，请在空闲时重试")
                dialect = create_engine("sqlite://").dialect
                for table in TABLES:
                    current.execute(str(CreateTable(table).compile(dialect=dialect)))
                    for index in table.indexes:
                        current.execute(str(CreateIndex(index).compile(dialect=dialect)))
                after = _business_fingerprint(current)
                if after != before:
                    raise ValueError("旧业务保护指纹改变，已回滚任务表升级")
                current.commit()
            except BaseException:
                current.rollback()
                raise
        return {"mode": "APPLIED", "migration_id": MIGRATION_ID,
                "created_tables": list(TABLE_NAMES), "backup_path": str(backup_path),
                "old_business_before": before, "old_business_after": after,
                "old_business_unchanged": before == after}
