"""控制状态完整映像正式候选；仅供可信迁移内部调用，未默认接入应用。"""

import base64
import hashlib
import json
import math
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum as PythonEnum
from threading import get_ident
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    bindparam,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy import (
    Enum as SQLAlchemyEnum,
)
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.ddl import sort_tables

from archspec.persistence import sql_namespace

VERSION = "CONTROL_IMAGE_V2"
MAX_ROWS = 50_000
MAX_BYTES = 8 * 1024 * 1024
_SAFE = "控制状态无法取得完整一致映像，未覆盖旧行或提交外层事务"


class ControlImageUnavailable(RuntimeError):
    """事务、类型、结构或源引用无法核对。"""


class _ReferenceMissing(ControlImageUnavailable):
    """指定映像内缺少必要引用行。"""


class ControlImageMismatch(ValueError):
    """目标不是全空或逐字段完整一致，拒绝覆盖。"""


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ControlImage:
    payload_json: str = field(repr=False)
    sha256: str

    def to_dict(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class ImportReceipt:
    state: str
    sha256: str


def _definitions(tables):
    if (
        not isinstance(tables, (list, tuple))
        or not 1 <= len(tables) <= 64
        or len({table.name for table in tables}) != len(tables)
        or any(not table.primary_key.columns or len(table.columns) > 128 for table in tables)
    ):
        raise ControlImageUnavailable(_SAFE)
    return tuple(sorted(tables, key=lambda table: table.name))


def _type_contract(value, dialect):
    # PG native Enum 不能退化成同长度 VARCHAR；SQLite 本身使用 VARCHAR。
    if isinstance(value, SQLAlchemyEnum) and dialect == "postgresql" and value.native_enum:
        return ("enum", True, value.name, value.schema, tuple(value.enums))
    if isinstance(value, Boolean):
        return ("boolean",)
    if isinstance(value, BigInteger):
        return ("bigint",)
    if isinstance(value, SmallInteger):
        return ("smallint",)
    if isinstance(value, Integer):
        return ("integer",)
    if isinstance(value, Float):
        return ("float",)
    if isinstance(value, Numeric):
        return ("numeric", value.precision, value.scale)
    if isinstance(value, Text):
        return ("text",)
    if isinstance(value, String):
        return ("string", value.length)
    if isinstance(value, DateTime):
        return ("datetime", value.timezone if dialect == "postgresql" else None)
    if isinstance(value, Date):
        return ("date",)
    if isinstance(value, LargeBinary):
        return ("bytes",)
    raise ControlImageUnavailable(_SAFE)


def _enum_description(value):
    if not isinstance(value, SQLAlchemyEnum):
        return None
    return {
        "native": bool(value.native_enum),
        "name": value.name,
        "schema": value.schema,
        "labels": list(value.enums),
    }


def _fk_semantics(options):
    """规范化省略的默认行为，但保留实际不同的 FK 行为。"""

    def token(name, default):
        value = options.get(name)
        return default if value is None else " ".join(value.upper().split())

    deferred = options.get("deferrable")
    if deferred is not None and type(deferred) is not bool:
        raise ControlImageUnavailable(_SAFE)
    return {
        "ondelete": token("ondelete", "NO ACTION"),
        "onupdate": token("onupdate", "NO ACTION"),
        "deferrable": deferred is True,
        "initially": token("initially", "IMMEDIATE"),
        "match": token("match", "SIMPLE"),
    }


def _declared_fk_options(rule):
    return _fk_semantics(
        {
            name: getattr(rule, name)
            for name in ("ondelete", "onupdate", "deferrable", "initially", "match")
        }
    )


def _pg_enum_identity(connection, reader, table, column):
    """核实际列 type OID 的命名空间；可见 Enum 的反射 schema 可能被省略。"""
    try:
        if isinstance(reader, _BatchInspector):
            row=reader.enums[(sql_namespace.physical_schema(connection,table.schema),table.name,column.name)]
        else:
            row=connection.execute(text(
                "SELECT tn.nspname AS schema_name,t.typname AS type_name,"
                "array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS labels "
                "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_catalog.pg_type t ON t.oid=a.atttypid "
                "JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace "
                "JOIN pg_catalog.pg_enum e ON e.enumtypid=t.oid "
                "WHERE n.nspname=:schema AND c.relname=:table AND a.attname=:column "
                "AND a.attnum>0 AND NOT a.attisdropped AND t.typtype='e' "
                "GROUP BY tn.nspname,t.typname"),
                {"schema":sql_namespace.physical_schema(connection,table.schema),
                 "table":table.name,"column":column.name}).mappings().one()
        if (
            type(row["schema_name"]) is not str
            or type(row["type_name"]) is not str
            or not isinstance(row["labels"], (list, tuple))
            or any(type(label) is not str for label in row["labels"])
        ):
            raise ControlImageUnavailable(_SAFE)
        return ("enum", True, row["type_name"], row["schema_name"], tuple(row["labels"]))
    except Exception:  # noqa: BLE001 - 不泄露数据库类型名称或底层异常
        raise ControlImageUnavailable(_SAFE) from None


def _physical_foreign_keys(connection, reader, table, schema):
    if connection.dialect.name != "postgresql":
        return reader.get_foreign_keys(table.name, schema=schema)
    reflected = reader.get_foreign_keys(
        table.name, schema=schema, postgresql_ignore_search_path=True
    )
    if not reflected:
        return reflected
    # 反射会把实际default_schema的引用折叠到当前schema；真实confrelid才决定目标身份。
    try:
        if isinstance(reader,_BatchInspector):
            actual=reader.foreign_targets.get((schema,table.name),[])
        else:
            actual = (
                connection.execute(
                    text(
                        "SELECT con.conname AS constraint_name, target_n.nspname AS schema_name, "
                        "target.relname AS table_name FROM pg_catalog.pg_constraint con "
                        "JOIN pg_catalog.pg_class local_rel ON local_rel.oid=con.conrelid "
                        "JOIN pg_catalog.pg_namespace local_n ON local_n.oid=local_rel.relnamespace "
                        "JOIN pg_catalog.pg_class target ON target.oid=con.confrelid "
                        "JOIN pg_catalog.pg_namespace target_n ON target_n.oid=target.relnamespace "
                        "WHERE con.contype='f' AND local_n.nspname=:schema AND local_rel.relname=:table"
                    ),
                    {"schema": schema, "table": table.name},
                )
                .mappings()
                .all()
            )
        by_name = {row["constraint_name"]: row for row in actual}
        if len(by_name) != len(actual) or set(by_name) != {row["name"] for row in reflected}:
            raise ControlImageUnavailable(_SAFE)
        verified = []
        for row in reflected:
            identity = by_name[row["name"]]
            if (
                type(identity["schema_name"]) is not str
                or type(identity["table_name"]) is not str
                or identity["table_name"] != row["referred_table"]
            ):
                raise ControlImageUnavailable(_SAFE)
            verified.append({**row, "referred_schema": identity["schema_name"]})
        return verified
    except Exception:  # noqa: BLE001 - 类型/目录异常不能泄露内部名称
        raise ControlImageUnavailable(_SAFE) from None


def _compare_physical_schema(connection, table, reader):
    """核真实列/PK/FK/唯一及CHECK，防止声明Table掩盖未读取旧字段。"""
    schema = sql_namespace.physical_schema(connection, table.schema)
    if isinstance(reader, _BatchInspector):
        has_trigger = (schema, table.name) in reader.triggers
    elif connection.dialect.name == "sqlite":
        if schema not in {None, "main"}:
            raise ControlImageUnavailable(_SAFE)
        has_trigger = connection.execute(
            text(
                "SELECT 1 FROM main.sqlite_master WHERE type='trigger' AND tbl_name=:table LIMIT 1"
            ),
            {"table": table.name},
        ).first()
    else:
        has_trigger = connection.execute(
            text(
                "SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND c.relname=:table AND n.nspname=:schema LIMIT 1"
            ),
            {"table": table.name, "schema": schema},
        ).first()
    if has_trigger:
        # 组件尚未声明迁移触发器的合同，不能悄悄丢弃或继承未知副作用。
        raise ControlImageUnavailable(_SAFE)
    if any(
        idx.dialect_options.get(dialect, {}).get("where") is not None
        for idx in table.indexes
        for dialect in ("sqlite", "postgresql")
    ):
        raise ControlImageUnavailable(_SAFE)
    reflected_indexes = reader.get_indexes(table.name, schema=schema)
    if any(
        row.get("dialect_options", {}).get(option) is not None
        for row in reflected_indexes
        for option in ("sqlite_where", "postgresql_where")
    ):
        raise ControlImageUnavailable(_SAFE)
    actual = reader.get_columns(table.name, schema=schema)
    expected = {column.name: column for column in table.columns}
    if set(expected) != {column["name"] for column in actual}:
        raise ControlImageUnavailable(_SAFE)
    for column in actual:
        declared = expected[column["name"]]
        actual_contract = _type_contract(column["type"], connection.dialect.name)
        expected_contract = _type_contract(declared.type, connection.dialect.name)
        if expected_contract[0] == "enum":
            if actual_contract[0] != "enum":
                raise ControlImageUnavailable(_SAFE)
            # Enum 类型 schema 独立于表 schema；未显式指定时使用该 Engine 当前 schema。
            expected_contract = (
                "enum",
                True,
                declared.type.name,
                sql_namespace.physical_schema(connection, declared.type.schema),
                tuple(declared.type.enums),
            )
            actual_contract = _pg_enum_identity(connection, reader, table, declared)
        if column["nullable"] != declared.nullable or actual_contract != expected_contract:
            raise ControlImageUnavailable(_SAFE)
    if reader.get_pk_constraint(table.name, schema=schema)["constrained_columns"] != [
        c.name for c in table.primary_key.columns
    ]:
        raise ControlImageUnavailable(_SAFE)
    active_schema = schema
    actual_fk = {
        (
            tuple(row["constrained_columns"]),
            row["referred_schema"] or active_schema,
            row["referred_table"],
            tuple(row["referred_columns"]),
            tuple(_fk_semantics(row.get("options", {})).values()),
        )
        for row in _physical_foreign_keys(connection, reader, table, schema)
    }
    expected_fk = {
        (
            tuple(e.parent.name for e in rule.elements),
            sql_namespace.physical_schema(
                connection, next(iter(rule.elements)).column.table.schema
            ),
            next(iter(rule.elements)).column.table.name,
            tuple(e.column.name for e in rule.elements),
            tuple(_declared_fk_options(rule).values()),
        )
        for rule in table.foreign_key_constraints
    }
    if actual_fk != expected_fk:
        raise ControlImageUnavailable(_SAFE)
    actual_unique = {
        tuple(row["column_names"])
        for row in reader.get_unique_constraints(table.name, schema=schema)
    }
    expected_unique = {
        tuple(column.name for column in rule.columns)
        for rule in table.constraints
        if isinstance(rule, UniqueConstraint)
    }
    if actual_unique != expected_unique:
        raise ControlImageUnavailable(_SAFE)
    actual_indexes = {
        (tuple(row["column_names"]), bool(row["unique"]))
        for row in reflected_indexes
        if not row.get("duplicates_constraint")
    }
    expected_indexes = {
        (tuple(column.name for column in idx.columns), bool(idx.unique)) for idx in table.indexes
    }
    if actual_indexes != expected_indexes:
        raise ControlImageUnavailable(_SAFE)
    checks = [str(rule.sqltext) for rule in table.constraints if isinstance(rule, CheckConstraint)]
    actual_checks = {
        " ".join(row["sqltext"].split())
        for row in reader.get_check_constraints(table.name, schema=schema)
    }
    if connection.dialect.name == "postgresql" and checks:
        # 交由真实PG解析声明生成规范表达式，不手写IN/ANY/类型转换同义规则。
        temporary = Table(
            "_archspec_schema_" + uuid4().hex,
            MetaData(),
            *(Column(c.name, c.type, nullable=c.nullable) for c in table.columns),
            *(CheckConstraint(value) for value in checks),
            # 临时解析表必须显式用pg_temp，不能被None→业务schema映射重定向。
            # 原业务表仍只允许archspec_control，不放宽其物理归属检查。
            schema="pg_temp",
            prefixes=["TEMPORARY"],
            postgresql_on_commit="DROP",
        )
        connection.execute(CreateTable(temporary))
        temp_schema = next(
            name
            for name in connection.exec_driver_sql(
                "SELECT pg_catalog.current_schemas(true)"
            ).scalar_one()
            if name.startswith("pg_temp_")
        )
        checks = [
            row["sqltext"]
            for row in inspect(connection).get_check_constraints(temporary.name, schema=temp_schema)
        ]
    if actual_checks != {" ".join(value.split()) for value in checks}:
        raise ControlImageUnavailable(_SAFE)



class _BatchInspector:
    """一次真实PG批读的局部结果；只交给原逐项比较器，不存入任何跨调用缓存。"""
    def __init__(self,fields,triggers,foreign_targets,enums):
        self.fields,self.triggers,self.foreign_targets,self.enums=fields,triggers,foreign_targets,enums

    def __getattr__(self,name):
        if name not in _BULK_METHODS:raise AttributeError(name)
        def read(table,schema=None,**_options):
            return self.fields[(schema,table)][name]
        return read


_BULK_METHODS={'get_columns':'get_multi_columns','get_indexes':'get_multi_indexes',
    'get_pk_constraint':'get_multi_pk_constraint','get_foreign_keys':'get_multi_foreign_keys',
    'get_unique_constraints':'get_multi_unique_constraints','get_check_constraints':'get_multi_check_constraints'}


def _pg_schema_batch(connection,tables,reader):
    """按物理schema批量反射完整合同，FK和Enum仍核真实OID归属而非可见默认schema。"""
    fields={};groups={}
    for table in tables:
        schema=sql_namespace.physical_schema(connection,table.schema)
        groups.setdefault(schema,[]).append(table.name);fields[(schema,table.name)]={}
    for schema,names in groups.items():
        expected={(schema,name) for name in names}
        for single,bulk in _BULK_METHODS.items():
            options={'postgresql_ignore_search_path':True} if single=='get_foreign_keys' else {}
            rows=getattr(reader,bulk)(schema=schema,filter_names=sorted(names),**options)
            if set(rows)!=expected:raise ControlImageUnavailable(_SAFE)
            for key,value in rows.items():fields[key][single]=value
    identities=list(fields)
    def query(statement):
        return connection.execute(text(statement).bindparams(bindparam('identities',expanding=True)),
            {'identities':identities}).mappings().all()
    trigger_rows=query("SELECT n.nspname AS schema_name,c.relname AS table_name FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE NOT t.tgisinternal AND (n.nspname,c.relname) IN :identities")
    triggers={(row['schema_name'],row['table_name']) for row in trigger_rows}
    foreign_targets={}
    if any(value['get_foreign_keys'] for value in fields.values()):
        foreign_rows=query("SELECT local_n.nspname AS local_schema,local_rel.relname AS local_table,"
            "con.conname AS constraint_name,target_n.nspname AS schema_name,target.relname AS table_name "
            "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class local_rel ON local_rel.oid=con.conrelid "
            "JOIN pg_catalog.pg_namespace local_n ON local_n.oid=local_rel.relnamespace "
            "JOIN pg_catalog.pg_class target ON target.oid=con.confrelid "
            "JOIN pg_catalog.pg_namespace target_n ON target_n.oid=target.relnamespace "
            "WHERE con.contype='f' AND (local_n.nspname,local_rel.relname) IN :identities")
        for row in foreign_rows:foreign_targets.setdefault((row['local_schema'],row['local_table']),[]).append(row)
    enums={}
    if any(isinstance(column.type,SQLAlchemyEnum) and column.type.native_enum for table in tables for column in table.columns):
        enum_rows=query("SELECT n.nspname AS table_schema,c.relname AS table_name,a.attname AS column_name,"
            "tn.nspname AS schema_name,t.typname AS type_name,"
            "array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS labels "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_type t ON t.oid=a.atttypid "
            "JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace "
            "JOIN pg_catalog.pg_enum e ON e.enumtypid=t.oid "
            "WHERE (n.nspname,c.relname) IN :identities AND a.attnum>0 AND NOT a.attisdropped AND t.typtype='e' "
            "GROUP BY n.nspname,c.relname,a.attname,tn.nspname,t.typname")
        for row in enum_rows:
            key=(row['table_schema'],row['table_name'],row['column_name'])
            if key in enums:raise ControlImageUnavailable(_SAFE)
            enums[key]=row
    return _BatchInspector(fields,triggers,foreign_targets,enums)


def _physical_schemas(connection,tables):
    """每次新读取整组完整结构；没有schema结果/Inspector跨调用缓存。"""
    try:
        if (not isinstance(tables,(list,tuple)) or not 1<=len(tables)<=64
                or any(not isinstance(table,Table) for table in tables)):
            raise ControlImageUnavailable(_SAFE)
        identities=[(sql_namespace.physical_schema(connection,table.schema),table.name) for table in tables]
        if len(set(identities))!=len(identities):raise ControlImageUnavailable(_SAFE)
        reader=inspect(connection)
        if connection.dialect.name=='postgresql':reader=_pg_schema_batch(connection,tables,reader)
        for table in tables:_compare_physical_schema(connection,table,reader)
    except ControlImageUnavailable:raise
    except Exception: # noqa: BLE001 - 不回显SQL、原件路径或驱动原文。
        raise ControlImageUnavailable(_SAFE) from None


def _physical_schema(connection,table):
    """单表兼容入口；比较逻辑和完整批读路径相同。"""
    _physical_schemas(connection,[table])


@dataclass
class _SchemaReview:
    connection: object = field(repr=False)
    transaction: object = field(repr=False)
    thread: int
    identities: dict = field(repr=False)
    active: bool = True


def _schema_identities(connection,tables):
    if (not isinstance(tables,(list,tuple)) or not tables or len(tables)>64
            or any(not isinstance(table,Table) for table in tables)):
        raise ControlImageUnavailable(_SAFE)
    keys=[(sql_namespace.physical_schema(connection,table.schema),table.name) for table in tables]
    if len(set(keys))!=len(keys):raise ControlImageUnavailable(_SAFE)
    return {(sql_namespace.physical_schema(connection,table.schema),table.name):_sha(_json(_description(table))) for table in tables}


@contextmanager
def _schema_review_scope(connection,tables):
    """仅本次_assets组合读取使用；退出即失效，不能存成请求/事务schema缓存。"""
    if connection.in_nested_transaction():raise ControlImageUnavailable(_SAFE)
    _physical_schemas(connection,tables)
    transaction=connection.get_transaction()
    if transaction is None or not transaction.is_active:raise ControlImageUnavailable(_SAFE)
    review=_SchemaReview(connection,transaction,get_ident(),_schema_identities(connection,tables))
    try:yield review
    finally:review.active=False


def _assert_schema_review(review,connection,tables):
    if (type(review) is not _SchemaReview or not review.active or review.thread!=get_ident()
            or review.connection is not connection or connection.closed or connection.in_nested_transaction()
            or connection.get_transaction() is not review.transaction or not review.transaction.is_active):
        raise ControlImageUnavailable(_SAFE)
    actual=_schema_identities(connection,tables)
    if not actual or any(review.identities.get(key)!=value for key,value in actual.items()):
        raise ControlImageUnavailable(_SAFE)

def _description(table):
    # 使用调用方已声明且审核的Table定义，不由映像中的表名拼接SQL。
    return {
        "name": table.name,
        "columns": [
            {
                "name": column.name,
                "type": str(column.type),
                "timezone": getattr(column.type, "timezone", None),
                "enum": _enum_description(column.type),
                "nullable": column.nullable,
                "primary_key": column.primary_key,
            }
            for column in table.columns
        ],
        "unique_columns": sorted(
            [
                list(rule.columns.keys())
                for rule in table.constraints
                if isinstance(rule, UniqueConstraint)
            ]
        ),
        "indexes": sorted([[list(idx.columns.keys()), bool(idx.unique)] for idx in table.indexes]),
        "checks": sorted(
            str(rule.sqltext) for rule in table.constraints if isinstance(rule, CheckConstraint)
        ),
        "foreign_keys": sorted(
            [
                {
                    "columns": [element.parent.name for element in constraint.elements],
                    "table": next(iter(constraint.elements)).column.table.name,
                    "schema": next(iter(constraint.elements)).column.table.schema,
                    "target_columns": [element.column.name for element in constraint.elements],
                    **_declared_fk_options(constraint),
                }
                for constraint in table.foreign_key_constraints
            ],
            key=_json,
        ),
    }


def _decimal_identity(value):
    # Decimal.normalize() 会按线程当前 context 舍入；直接处理系数/指数避免精度丢失。
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return "-0" if sign else "0"
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits[:end])
    return ("-" if sign else "") + coefficient + "E" + str(exponent)


def _column_encode(value, column, dialect):
    if value is not None and isinstance(column.type, SQLAlchemyEnum):
        # 使用已声明列的公开 bind processor：默认存 member.name，values_callable 可改为值。
        if isinstance(value, PythonEnum) and type(value) is not column.type.enum_class:
            raise ControlImageUnavailable(_SAFE)
        processor = column.type.bind_processor(dialect)
        value = processor(value) if processor else value
        if type(value) is not str or value not in column.type.enums:
            raise ControlImageUnavailable(_SAFE)
    return _encode(value)


def _encode(value):
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return {"float": value.hex()}
    if isinstance(value, datetime):
        # 既有SQLite业务日期按UTC存储；PG读取可能已有UTC offset，统一相同时间身份。
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return {"datetime": value.astimezone(UTC).isoformat()}
    if type(value) is date:
        return {"date": value.isoformat()}
    if type(value) is bytes:
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal) and value.is_finite():
        return {"decimal": _decimal_identity(value)}
    raise ControlImageUnavailable(_SAFE)


def _decode(value):
    if value is None or type(value) in {str, bool, int}:
        return value
    if not isinstance(value, dict) or len(value) != 1:
        raise ControlImageUnavailable(_SAFE)
    name, content = next(iter(value.items()))
    if name == "float":
        result = float.fromhex(content)
        if not math.isfinite(result):
            raise ControlImageUnavailable(_SAFE)
        return result
    if name == "datetime":
        result = datetime.fromisoformat(content)
        if result.tzinfo is None:
            raise ControlImageUnavailable(_SAFE)
        return result.astimezone(UTC)
    if name == "date":
        return date.fromisoformat(content)
    if name == "bytes":
        return base64.b64decode(content, validate=True)
    if name == "decimal":
        result = Decimal(content)
        if result.is_finite():
            return result
    raise ControlImageUnavailable(_SAFE)


def _transaction_required(connection):
    if not connection.in_transaction():
        raise ControlImageUnavailable(_SAFE)
    if connection.dialect.name == "sqlite":
        # SQLAlchemy begin()本身不确保sqlite3 legacy模式对SELECT发出BEGIN。
        if not connection.connection.driver_connection.in_transaction:
            raise ControlImageUnavailable(_SAFE)
    elif connection.dialect.name == "postgresql":
        if connection.get_isolation_level() not in {"REPEATABLE READ", "SERIALIZABLE"}:
            raise ControlImageUnavailable(_SAFE)
    else:
        raise ControlImageUnavailable(_SAFE)


@contextmanager
def image_transaction(engine, *, write=False, guard=None):
    """只建立单库短一致视图；两个库必须另由共同停写Gate包住。"""
    try:
        with ExitStack() as resources:
            initial = resources.enter_context(engine.connect())
            # 先预留连接避免持路径锁等池；取得保护后再BEGIN，等待Python锁时不挂空闲事务。
            if guard is not None:
                resources.enter_context(guard)
            if engine.dialect.name == "sqlite":
                connection = initial
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
            elif engine.dialect.name == "postgresql":
                connection = initial.execution_options(isolation_level="REPEATABLE READ")
                connection.begin()
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                if write:
                    connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
                    connection.exec_driver_sql(
                        "SELECT pg_catalog.pg_advisory_xact_lock(4706905979947307341)"
                    )
            else:
                raise ControlImageUnavailable(_SAFE)
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except (ControlImageUnavailable, ControlImageMismatch):
        raise
    except Exception:  # noqa: BLE001 - 上游异常可能含私密行值，统一安全失败
        # 数据库异常可能含行值；不向调用方转发私密输入。
        raise ControlImageUnavailable(_SAFE) from None


def _foreign_closure(tables, entries):
    by_name = {row["name"]: row for row in entries}
    selected = {(table.schema, table.name) for table in tables}
    for table in tables:
        for constraint in table.foreign_key_constraints:
            target = next(iter(constraint.elements)).column.table
            if (target.schema, target.name) not in selected or target.name not in by_name:
                raise ControlImageUnavailable(_SAFE)
            current, other = by_name[table.name], by_name[target.name]
            indexes = [list(table.columns.keys()).index(e.parent.name) for e in constraint.elements]
            target_indexes = [
                list(target.columns.keys()).index(e.column.name) for e in constraint.elements
            ]
            allowed = {_json([row[i] for i in target_indexes]) for row in other["rows"]}
            for row in current["rows"]:
                values = [row[i] for i in indexes]
                if any(value is None for value in values):
                    if constraint.match == "FULL" and not all(value is None for value in values):
                        raise ControlImageUnavailable(_SAFE)
                elif _json(values) not in allowed:
                    # 不补传project、不置空归属；缺授权引用闭包时直接拒绝。
                    raise _ReferenceMissing(_SAFE)


def capture_control_image(connection, tables, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES):
    """完整读取指定表，不过滤DENY、历史或RESERVED；无配置/网络/文件遍历。"""
    try:
        _transaction_required(connection)
        tables = _definitions(tables)
        if (
            type(max_rows) is not int
            or not 1 <= max_rows <= MAX_ROWS
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BYTES
        ):
            raise ControlImageUnavailable(_SAFE)
        physical_tables = sql_namespace.bind_tables(connection, tables)
        _physical_schemas(connection,physical_tables)
        entries, count, budget = [], 0, 0
        for table, physical in zip(tables, physical_tables, strict=True):
            entry = _description(table)
            budget += len(_json(entry).encode("utf-8"))
            entry["rows"] = []
            result = connection.execute(select(physical).order_by(*physical.primary_key.columns))
            for raw in result:
                count += 1
                if count > max_rows:
                    raise ControlImageUnavailable(_SAFE)
                row = [
                    _column_encode(value, column, connection.dialect)
                    for column, value in zip(table.columns, raw, strict=True)
                ]
                budget += len(_json(row).encode("utf-8"))
                if budget > max_bytes:
                    raise ControlImageUnavailable(_SAFE)
                entry["rows"].append(row)
            entries.append(entry)
        _foreign_closure(tables, entries)
        payload = _json({"version": VERSION, "tables": entries})
        if len(payload.encode("utf-8")) > max_bytes:
            raise ControlImageUnavailable(_SAFE)
        return ControlImage(payload, _sha(payload))
    except ControlImageUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 上游异常可能含私密行值，统一安全失败
        raise ControlImageUnavailable(_SAFE) from None


def _image(image, tables):
    if (
        not isinstance(image, ControlImage)
        or not isinstance(image.payload_json, str)
        or len(image.payload_json.encode("utf-8")) > MAX_BYTES
        or _sha(image.payload_json) != image.sha256
    ):
        raise ControlImageUnavailable(_SAFE)
    body = image.to_dict()
    if (
        not isinstance(body, dict)
        or set(body) != {"version", "tables"}
        or body["version"] != VERSION
        or _json(body) != image.payload_json
    ):
        raise ControlImageUnavailable(_SAFE)
    entries = body["tables"]
    if len(entries) != len(tables):
        raise ControlImageUnavailable(_SAFE)
    for table, entry in zip(tables, entries, strict=True):
        expected = _description(table)
        if set(entry) != set(expected) | {"rows"} or any(
            entry[k] != v for k, v in expected.items()
        ):
            raise ControlImageUnavailable(_SAFE)
        if any(len(row) != len(table.columns) for row in entry["rows"]):
            raise ControlImageUnavailable(_SAFE)
    _foreign_closure(tables, entries)
    return body


def verify_control_image(connection, image, tables):
    """逐字段核迁移候选；不能用于要求活跃业务永远等于初始整表SHA。"""
    try:
        tables = _definitions(tables)
        _image(image, tables)
        try:
            current = capture_control_image(connection, tables)
        except _ReferenceMissing:
            raise ControlImageMismatch("目标控制状态缺必要引用，拒绝切换") from None
        if current != image:
            raise ControlImageMismatch("控制状态与固定源映像不一致，拒绝切换")
        return True
    except (ControlImageUnavailable, ControlImageMismatch):
        raise
    except Exception:  # noqa: BLE001 - 上游异常可能含私密行值，统一安全失败
        raise ControlImageUnavailable(_SAFE) from None


def import_control_image(connection, image, tables):
    """全空或全一致目标才可暂存，savepoint内完整插入；不提交外层事务。"""
    try:
        _transaction_required(connection)
        tables = _definitions(tables)
        body = _image(image, tables)
        physical_tables = sql_namespace.bind_tables(connection, tables)
        current = capture_control_image(connection, tables)
        if any(row["rows"] for row in current.to_dict()["tables"]):
            verify_control_image(connection, image, tables)
            return ImportReceipt("ALREADY_PRESENT", image.sha256)
        # 不覆盖目标旧记录，约束/插入失败全部回滚本次savepoint。
        mapping = {row["name"]: row for row in body["tables"]}
        with connection.begin_nested():
            for table in sort_tables(physical_tables):
                rows = [
                    {
                        column.name: _decode(value)
                        for column, value in zip(table.columns, row, strict=True)
                    }
                    for row in mapping[table.name]["rows"]
                ]
                if rows:
                    connection.execute(insert(table), rows)
            verify_control_image(connection, image, tables)
        return ImportReceipt("STAGED", image.sha256)
    except (ControlImageUnavailable, ControlImageMismatch):
        raise
    except Exception:  # noqa: BLE001 - 上游异常可能含私密行值，统一安全失败
        raise ControlImageUnavailable(_SAFE) from None
