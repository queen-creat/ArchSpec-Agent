"""可信声明的统一物理命名空间；不读取路径、预算或业务状态，不拥有事务。"""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import get_ident
from types import MappingProxyType

from sqlalchemy import (
    Enum,
    MetaData,
    String,
    Table,
    bindparam,
    cast,
    inspect,
    literal,
    null,
    select,
    text,
    union_all,
)
from sqlalchemy.sql import visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Select

PG_SCHEMA = "archspec_control"
_SAFE = "SQL物理命名空间无法唯一核对，未切换来源或提交状态"


class NamespaceUnavailable(RuntimeError):
    """歧义、临时影子或物理重定向必须明确拒绝，不回显名称/连接信息。"""


def physical_schema(connection, declared_schema=None):
    """逻辑schema默认值固定映射；不信任search_path或Inspector的缓存默认值。"""
    try:
        dialect = connection.dialect.name
        if dialect == "sqlite":
            if declared_schema not in {None, "main"}:
                raise NamespaceUnavailable(_SAFE)
            return "main"
        if dialect != "postgresql":
            raise NamespaceUnavailable(_SAFE)
        if declared_schema is None:
            return PG_SCHEMA
        if (
            not isinstance(declared_schema, str)
            or not declared_schema
            or len(declared_schema.encode("utf-8")) > 63
            or declared_schema == "pg_temp"
            or declared_schema.startswith("pg_temp_")
        ):
            raise NamespaceUnavailable(_SAFE)
        # 显式schema来自已审核服务器Table，不新增客户端schema参数。
        return declared_schema
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 配置/方言异常不能泄露DSN或未审核输入
        raise NamespaceUnavailable(_SAFE) from None


def _definitions(tables):
    if (
        not isinstance(tables, (list, tuple))
        or not 1 <= len(tables) <= 64
        or any(not isinstance(table, Table) for table in tables)
        or len({table.name for table in tables}) != len(tables)
    ):
        raise NamespaceUnavailable(_SAFE)
    return tuple(tables)


def _translation_safe(connection, schemas):
    getter = getattr(connection, "get_execution_options", None)
    options = getter() if getter is not None else {}
    mapping = options.get("schema_translate_map")
    if mapping is None:
        return
    if not isinstance(mapping, Mapping) or any(
        mapping.get(schema, schema) != schema for schema in schemas
    ):
        # None映射不影响显式clone；main/private->other/public则不能先核A再执行B。
        raise NamespaceUnavailable(_SAFE)


def reject_temp_shadows(connection, tables):
    """同名TEMP table/view会影响未限定后续读取；在统一低层拒绝歧义。"""
    try:
        tables = _definitions(tables)
        for table in tables:
            physical_schema(connection, table.schema)
        if connection.dialect.name == "sqlite":
            query = (
                "SELECT 1 FROM temp.sqlite_master "
                "WHERE type IN ('table','view') AND name COLLATE NOCASE IN :names LIMIT 1"
            )
        else:
            query = (
                "SELECT 1 FROM pg_catalog.pg_class "
                "WHERE relnamespace=pg_catalog.pg_my_temp_schema() "
                "AND relpersistence='t' AND relkind IN ('r','v','m','f','p') "
                "AND relname IN :names LIMIT 1"
            )
        statement = text(query).bindparams(bindparam("names", type_=String(), expanding=True))
        if connection.execute(statement, {"names": [table.name for table in tables]}).first():
            raise NamespaceUnavailable(_SAFE)
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 不传播数据库响应或源名称
        raise NamespaceUnavailable(_SAFE) from None


def _prepare_tables(connection, tables):
    """返回同一MetaData的物理限定副本；原逻辑Table/Enum/FK保持不变。

    不connect、commit、rollback或DDL；schema创建仅由调用方显式迁移入口拥有。
    """
    try:
        tables = _definitions(tables)
        # SQLite标识符仅ASCII大小写不敏感；Unicode与PG quoted名字不能casefold。
        ascii_fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        identities = {
            (
                physical_schema(connection, table.schema),
                table.name.translate(ascii_fold)
                if connection.dialect.name == "sqlite"
                else table.name,
            )
            for table in tables
        }
        if len(identities) != len(tables):
            raise NamespaceUnavailable(_SAFE)
        schemas = {physical_schema(connection, table.schema) for table in tables}
        if connection.dialect.name == "postgresql":
            schemas.update(
                physical_schema(connection, column.type.schema)
                for table in tables
                for column in table.columns
                if isinstance(column.type, Enum)
            )
        _translation_safe(connection, schemas)
        metadata = MetaData()

        def referred_schema(table, target_schema, constraint, declared_schema):
            return physical_schema(connection, declared_schema)

        bound = tuple(
            table.to_metadata(
                metadata,
                schema=physical_schema(connection, table.schema),
                referred_schema_fn=referred_schema,
            )
            for table in tables
        )
        for original, copied in zip(tables, bound, strict=True):
            for declared, column in zip(original.columns, copied.columns, strict=True):
                if isinstance(declared.type, Enum):
                    # SQLAlchemy SchemaEventTarget正常会复制Enum；共享时宁可拒绝也不改原type。
                    if column.type is declared.type:
                        raise NamespaceUnavailable(_SAFE)
                    column.type.schema = (
                        physical_schema(connection, declared.type.schema)
                        if connection.dialect.name == "postgresql"
                        else declared.type.schema
                    )
        return bound
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - Table/FK解析异常可能含原字段或名称
        raise NamespaceUnavailable(_SAFE) from None


def bind_tables(connection, tables):
    """原公共绑定入口保留即时TEMP检查；纯clone不额外查询物理目录。"""
    bound = _prepare_tables(connection, tables)
    reject_temp_shadows(connection, bound)
    return bound


_COLUMN_SCOPE = ContextVar("archspec_readonly_column_contract", default=None)


@contextmanager
def _column_contract_scope(connection):
    """仅由真实只读短阶段进入；缓存绑定实际根事务，不能跨事务/线程复用。"""
    transaction = connection.get_transaction()
    if connection.closed or transaction is None or not transaction.is_active:
        raise NamespaceUnavailable(_SAFE)
    readonly = (connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
                if connection.dialect.name == "postgresql"
                else connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1)
    if not readonly:
        raise NamespaceUnavailable(_SAFE)
    token = _COLUMN_SCOPE.set((connection, transaction, get_ident(), {}, connection.engine))
    try:
        yield
    finally:
        _COLUMN_SCOPE.reset(token)


def is_read_phase_connection(connection):
    """来源/目录只接受受控真实只读根事务，不能靠一个布尔参数缓存写状态。"""
    scope = _COLUMN_SCOPE.get()
    return (scope is not None and scope[0] is connection and scope[2] == get_ident()
            and not connection.closed and connection.get_transaction() is scope[1]
            and scope[1].is_active and connection.engine is scope[4]
            and not connection.in_nested_transaction())


_CONTRACT_SCOPE = ContextVar("archspec_live_column_contract", default=None)


def _column_key(table):
    return table.schema, table.name, frozenset(table.columns.keys())


def _phase_columns(connection):
    scope = _COLUMN_SCOPE.get()
    if scope is None:
        return None
    selected, transaction, thread, verified, engine = scope
    if (selected is not connection or thread != get_ident() or connection.closed
            or connection.engine is not engine or connection.get_transaction() is not transaction
            or not transaction.is_active):
        raise NamespaceUnavailable(_SAFE)
    # SAVEPOINT后的锁会随回滚释放；根事务旧缓存不能替代当前新检查。
    return None if connection.in_nested_transaction() else verified


def _pg_inventory_statement(tables, unchecked):
    """统一目录语句既供独立核验，也供同语句业务合读；不接受外部目录结果。"""
    # warm阶段可没有待核列；PG空IN必须仍是字符串，不能由NullType生成INTEGER。
    statement = text("""
        WITH scoped_relations AS (
            SELECT n.nspname, c.relname, c.oid
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN :schemas AND c.relkind IN ('r','p') AND c.relpersistence <> 't'
        )
        SELECT 'TABLE' AS kind, r.nspname AS schema_name, r.relname AS table_name,
               CAST(NULL AS text) AS column_name
        FROM scoped_relations r
        UNION ALL
        SELECT 'COLUMN', r.nspname, r.relname, a.attname
        FROM scoped_relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
        WHERE r.relname IN :column_names AND a.attnum>0 AND NOT a.attisdropped
        UNION ALL
        SELECT 'TEMP', CAST(NULL AS text), c.relname, CAST(NULL AS text)
        FROM pg_catalog.pg_class c
        WHERE c.relnamespace=pg_catalog.pg_my_temp_schema() AND c.relpersistence='t'
          AND c.relkind IN ('r','v','m','f','p') AND c.relname IN :names
    """).bindparams(*(bindparam(name, type_=String(), expanding=True) for name in ("schemas", "column_names", "names")))
    parameters = {"schemas": sorted({table.schema for table in tables}),
        "column_names": [table.name for table in unchecked], "names": [table.name for table in tables]}
    return statement, parameters


def _parse_pg_inventory(rows, tables):
    inventory = {schema: set() for schema in {table.schema for table in tables}}
    columns = {}
    for row in rows:
        if row["kind"] == "TEMP":
            raise NamespaceUnavailable(_SAFE)
        schema, name = row["schema_name"], row["table_name"]
        if schema not in inventory or not isinstance(name, str):
            raise NamespaceUnavailable(_SAFE)
        if row["kind"] == "TABLE":
            inventory[schema].add(name)
        elif row["kind"] == "COLUMN" and isinstance(row["column_name"], str):
            columns.setdefault((schema, name), set()).add(row["column_name"])
        else:
            raise NamespaceUnavailable(_SAFE)
    return inventory, columns


def _pg_column_inventory(connection, tables, unchecked):
    statement, parameters = _pg_inventory_statement(tables, unchecked)
    return _parse_pg_inventory(connection.execute(statement, parameters).mappings(), tables)


def _lock_phase_columns(connection, unchecked, cache):
    if cache is not None and unchecked:
        quote = connection.dialect.identifier_preparer.quote_identifier
        names = ", ".join(quote(table.schema) + "." + quote(table.name) for table in unchecked)
        connection.exec_driver_sql("LOCK TABLE " + names + " IN ACCESS SHARE MODE")


def _verify_inventory(tables, unchecked, inventory, columns, cache):
    if any(table.name not in inventory[table.schema] for table in tables):
        raise NamespaceUnavailable(_SAFE)
    if any(set(table.columns.keys()) != columns.get((table.schema, table.name), set())
           for table in unchecked):
        raise NamespaceUnavailable(_SAFE)
    if cache is not None:
        cache.update((_column_key(table), True) for table in unchecked)
    return MappingProxyType({schema: frozenset(names) for schema, names in inventory.items()})


def _checked_inventory(connection, tables):
    cache = _phase_columns(connection)
    unchecked = tuple(table for table in tables if cache is None or _column_key(table) not in cache)
    if connection.dialect.name == "postgresql":
        # 只保护列/表身份；目录和TEMP仍每次即时读取。
        _lock_phase_columns(connection, unchecked, cache)
        inventory, columns = _pg_column_inventory(connection, tables, unchecked)
    else:
        # SQLite保持既有Inspector语义和即时TEMP核对，不伪造PG目录行为。
        reject_temp_shadows(connection, tables)
        inspector = inspect(connection)
        inventory = {schema: set(inspector.get_table_names(schema=schema))
                     for schema in {table.schema for table in tables}}
        columns = {(table.schema, table.name): {row["name"] for row in
                    inspector.get_columns(table.name, schema=table.schema)}
                   for table in unchecked if table.name in inventory[table.schema]}
    return _verify_inventory(tables, unchecked, inventory, columns, cache)


def _quoted_identity(value, *, optional=False):
    if value is None and optional:
        return None, None
    if not isinstance(value, str) or not value:
        raise NamespaceUnavailable(_SAFE)
    return str(value), getattr(value, "quote", None)


def _live_declaration_signatures(connection, tables):
    """纯声明核对：不clone、不读SQL，不把当前事实/权限变成缓存。

    持有对象签名发现原地修改和替换；物理签名允许require使用新的等价子集。
    FK每次取当前colspec/目标对象，不能依赖SQLAlchemy已经memoize的解析结果。
    """
    try:
        tables = _definitions(tables)
        held, physical, schemas = set(), set(), set()
        identities = set()
        ascii_fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        for table in tables:
            schema = physical_schema(connection, table.schema)
            schemas.add(schema)
            name = _quoted_identity(table.name)
            identity = (schema, name[0].translate(ascii_fold)
                        if connection.dialect.name == "sqlite" else name[0])
            if identity in identities:
                raise NamespaceUnavailable(_SAFE)
            identities.add(identity)
            columns, column_shape, enums, keys, key_shape = [], [], [], [], []
            for column in table.columns:
                key, column_name = _quoted_identity(column.key), _quoted_identity(column.name)
                enum = None
                if isinstance(column.type, Enum):
                    enum_schema = (physical_schema(connection, column.type.schema)
                                   if connection.dialect.name == "postgresql" else column.type.schema)
                    if connection.dialect.name == "postgresql":
                        schemas.add(enum_schema)
                    enum = (_quoted_identity(column.type.schema, optional=True),
                            _quoted_identity(column.type.name, optional=True))
                    enums.append((key, _quoted_identity(enum_schema, optional=True), enum[1]))
                columns.append((id(column), key, column_name, id(column.type), enum))
                column_shape.append((key, column_name))
            for key in table.foreign_keys:
                spec = key._get_colspec()
                if not isinstance(spec, str) or not spec:
                    raise NamespaceUnavailable(_SAFE)
                target = key._table_column
                target_identity = None
                if target is not None:
                    if target.table is None:
                        raise NamespaceUnavailable(_SAFE)
                    target_schema, target_name, target_column = target.table.schema, target.table.name, target.key
                    target_identity = (id(target), id(target.table),
                        _quoted_identity(target.table.schema, optional=True), _quoted_identity(target.table.name),
                        _quoted_identity(target.key), _quoted_identity(target.name))
                else:
                    parts = spec.split(".")
                    target_schema = ".".join(parts[:-2]) or None
                    target_name = parts[-2] if len(parts) > 1 else parts[0]
                    target_column = parts[-1] if len(parts) > 1 else None
                referred = physical_schema(connection, target_schema)
                schemas.add(referred)
                parent = _quoted_identity(key.parent.key)
                keys.append((id(key), parent, spec, target_identity))
                # FK复制后是字符串引用；引用标识的quote身份由持有对象签名另外绑定。
                key_shape.append((parent, str(referred), _quoted_identity(target_name)[0],
                                  _quoted_identity(target_column, optional=True)[0]))
            held.add((id(table), _quoted_identity(table.schema, optional=True), name,
                      frozenset(columns), frozenset(keys)))
            physical.add((_quoted_identity(schema), name, frozenset(column_shape),
                          frozenset(enums), frozenset(key_shape)))
        _translation_safe(connection, schemas)
        return frozenset(held), frozenset(physical)
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 变更后的声明/getter不能泄露字段、路径或驱动消息。
        raise NamespaceUnavailable(_SAFE) from None


class _ColumnContract:
    """仅本次with内有效的目录结果；不保存业务行，不成为跨调用的许可缓存。"""
    def __init__(self, connection, declared, tables, inventory):
        self._connection, self._engine = connection, connection.engine
        self._transaction = connection.get_transaction()
        self._nested = connection.get_nested_transaction()
        self._thread, self._live = get_ident(), True
        self._declared = declared
        self._tables = MappingProxyType({table.name: table for table in tables})
        self._inventory = inventory
        self._declared_snapshot = _live_declaration_signatures(connection, declared)
        self._physical_snapshot = _live_declaration_signatures(connection, tables)
        self._signatures = self._physical_snapshot[1]

    def _assert_current(self, connection):
        if (not self._live or _CONTRACT_SCOPE.get() is not self or self._thread != get_ident()
                or connection is not self._connection or connection.closed
                or connection.engine is not self._engine or self._transaction is None
                or connection.get_transaction() is not self._transaction or not self._transaction.is_active
                or connection.get_nested_transaction() is not self._nested):
            raise NamespaceUnavailable(_SAFE)
        # 同一个proof只在入口复制一次；后续纯签名仍即时核当前声明/副本和映射。
        if (_live_declaration_signatures(connection, self._declared) != self._declared_snapshot
                or _live_declaration_signatures(connection, tuple(self._tables.values())) != self._physical_snapshot):
            raise NamespaceUnavailable(_SAFE)

    @property
    def tables(self):
        self._assert_current(self._connection)
        return self._tables

    @property
    def inventory(self):
        self._assert_current(self._connection)
        return self._inventory

    def require(self, connection, definitions):
        """同一合同内可借物理子集；不再bind或重复查询TEMP/列。"""
        self._assert_current(connection)
        _, signatures = _live_declaration_signatures(connection, definitions)
        if not signatures <= self._signatures:
            raise NamespaceUnavailable(_SAFE)
        return tuple(self._tables[table.name] for table in definitions)


@contextmanager
def column_contract(connection, tables):
    """一次实时完整表名/TEMP/列合同；上下文退出、换事务/线程/连接即失效。

    只有受控READ ONLY根事务可复用已持ACCESS SHARE锁的单表列集合。
    普通写事务始终重新批读全部列；inventory、active、receipt、权限从不缓存。
    """
    proof = None
    try:
        declared = _definitions(tables)
        physical = _prepare_tables(connection, declared)
        inventory = _checked_inventory(connection, physical)
        proof = _ColumnContract(connection, declared, physical, inventory)
        token = _CONTRACT_SCOPE.set(proof)
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 数据库/反射异常不能泄露连接或私有路径
        raise NamespaceUnavailable(_SAFE) from None
    try:
        yield proof
    finally:
        proof._live = False
        _CONTRACT_SCOPE.reset(token)


@contextmanager
def column_contract_records(connection, tables, statement_factory):
    """PG一次SELECT合读实时目录与受控业务值；核齐后才签发本次作用域证明。

    工厂仅来自服务器代码，接收物理限定副本并返回显式标签SELECT；不接HTTP SQL、
    外部inventory或available。只复用受ACCESS SHARE保护的列，不缓存目录或业务行。
    """
    proof = None
    try:
        if connection.dialect.name != "postgresql" or not callable(statement_factory):
            raise NamespaceUnavailable(_SAFE)
        declared = _definitions(tables)
        physical = _prepare_tables(connection, declared)
        signatures = (_live_declaration_signatures(connection, declared),
                      _live_declaration_signatures(connection, physical))
        owner = (connection.engine, connection.get_transaction(), connection.get_nested_transaction(), get_ident())
        cache = _phase_columns(connection)
        unchecked = tuple(table for table in physical if cache is None or _column_key(table) not in cache)
        _lock_phase_columns(connection, unchecked, cache)
        mapping = MappingProxyType({table.name: table for table in physical})
        payload = statement_factory(mapping)
        if not isinstance(payload, Select):
            raise NamespaceUnavailable(_SAFE)
        labels = [column.key for column in payload.selected_columns]
        reserved = ("__archspec_kind", "__archspec_schema", "__archspec_table", "__archspec_column")
        if (not labels or len(labels) != len(set(labels)) or any(not isinstance(label, str)
                or not label or label in reserved for label in labels)):
            raise NamespaceUnavailable(_SAFE)
        allowed = {id(table) for table in physical}
        if any(isinstance(part, (TextClause, UpdateBase))
               or isinstance(part, Table) and id(part) not in allowed
               or isinstance(part, Select) and part._for_update_arg is not None
               for part in visitors.iterate(payload)):
            raise NamespaceUnavailable(_SAFE)
        if signatures != (_live_declaration_signatures(connection, declared),
                          _live_declaration_signatures(connection, physical)):
            raise NamespaceUnavailable(_SAFE)
        inventory_sql, parameters = _pg_inventory_statement(physical, unchecked)
        catalog = inventory_sql.columns(kind=String(), schema_name=String(),
            table_name=String(), column_name=String()).params(**parameters).subquery()
        values = payload.subquery()
        catalog_query = select(catalog.c.kind.label(reserved[0]), catalog.c.schema_name.label(reserved[1]),
            catalog.c.table_name.label(reserved[2]), catalog.c.column_name.label(reserved[3]),
            *(cast(null(), column.type).label(column.key) for column in values.c))
        payload_query = select(literal("PAYLOAD").label(reserved[0]),
            *(cast(null(), String()).label(label) for label in reserved[1:]), *values.c)
        catalog_rows, records = [], []
        for row in connection.execute(union_all(catalog_query, payload_query)).mappings():
            if row[reserved[0]] == "PAYLOAD":
                if any(row[label] is not None for label in reserved[1:]):
                    raise NamespaceUnavailable(_SAFE)
                records.append({label: row[label] for label in labels})
            else:
                catalog_rows.append({"kind": row[reserved[0]], "schema_name": row[reserved[1]],
                    "table_name": row[reserved[2]], "column_name": row[reserved[3]]})
        if (connection.closed or connection.engine is not owner[0] or get_ident() != owner[3]
                or connection.get_transaction() is not owner[1] or owner[1] is None or not owner[1].is_active
                or connection.get_nested_transaction() is not owner[2]
                or signatures != (_live_declaration_signatures(connection, declared),
                                  _live_declaration_signatures(connection, physical))):
            raise NamespaceUnavailable(_SAFE)
        inventory, columns = _parse_pg_inventory(catalog_rows, physical)
        checked = _verify_inventory(physical, unchecked, inventory, columns, cache)
        proof = _ColumnContract(connection, declared, physical, checked)
        token = _CONTRACT_SCOPE.set(proof)
    except NamespaceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 编译/驱动/工厂失败不得输出SQL、字段或连接消息。
        raise NamespaceUnavailable(_SAFE) from None
    try:
        yield proof, records
    finally:
        proof._live = False
        _CONTRACT_SCOPE.reset(token)


def validate_columns(connection, tables):
    """兼容原校验入口；内部使用共同批读，不缓存业务身份/许可或完整物理结构。"""
    with column_contract(connection, tables):
        pass
