"""数据库公共基础设施。"""

import os
from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# 公开兼容别名保留；模型声明与有副作用的运行选择分离。
from archspec import database_schema
from archspec.config import settings
from archspec.persistence.remote_startup import current_startup

Base = database_schema.Base

_selected_startup = current_startup()
if _selected_startup is not None:
    engine = _selected_startup.engine
else:
    if os.getenv("ARCHSPEC_PERSISTENCE_BACKEND", "LOCAL") != "LOCAL":
        raise RuntimeError("已选择云端但启动数据尚未核验，未建立本地数据库")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """每次接口请求获得独立数据库会话，请求结束后自动关闭。"""
    with open_session() as session:
        yield session


def open_session():
    """既有默认治理与HTTP共用选定库；未配置沿用可覆盖的SessionLocal。"""
    from archspec.persistence.runtime_bindings import current_runtime
    selected = current_runtime()
    return selected.sessions() if selected is not None else SessionLocal()


def create_legacy_tables(database_engine: Engine) -> None:
    """旧业务表单独建表；治理与QA任务表不纳入这一接口。"""
    Base.metadata.create_all(database_engine, tables=[
        table for table in Base.metadata.sorted_tables
        if not table.name.startswith(("governance_", "qa_"))
    ])


def apply_sqlite_compatibility_migrations(database_engine: Engine) -> None:
    """给学习阶段已有 SQLite 数据库补列，避免升级功能后丢失旧数据。"""

    if database_engine.dialect.name != "sqlite":
        return
    additions = {
        "verification_status": "VARCHAR(8) NOT NULL DEFAULT 'PENDING'",
        "verified_at": "DATETIME",
        "verification_message": (
            "TEXT NOT NULL DEFAULT '尚未连接具体官方公告页面'"
        ),
        "announcement_number": "VARCHAR(300)",
        "source_updated_at": "DATE",
        "replaced_by_code": "VARCHAR(100)",
        "content_permission": "VARCHAR(20) NOT NULL DEFAULT 'METADATA_ONLY'",
    }
    with database_engine.begin() as connection:
        table_exists = connection.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='standard_catalog'"
            )
        ).first()
        if table_exists is None:
            return
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(standard_catalog)"))
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE standard_catalog ADD COLUMN {name} {definition}")
                )



def create_qa_task_tables(database_engine: Engine) -> None:
    """仅由LOCAL应用启动或明确升级调用；请求与云恢复不隐式建立任务表。"""
    from archspec.official_qa.task_models import QAOperationRow, QATaskRow

    with database_engine.begin() as connection:
        Base.metadata.create_all(connection, tables=[
            QATaskRow.__table__, QAOperationRow.__table__])
