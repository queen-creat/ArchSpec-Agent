"""纯数据库表声明基类；导入模型不会选择数据库、建目录或打开连接。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """与database.Base保持同一对象，供全部原业务表共同继承。"""


def registered_control_tables():
    """捕获与恢复共用的业务表声明；仅在服务器模型已加载后调用，不建表或开连接。"""
    from archspec.official_qa.task_leases import _LEASES, _PHASES

    return {**Base.metadata.tables, _LEASES.name: _LEASES, _PHASES.name: _PHASES}
