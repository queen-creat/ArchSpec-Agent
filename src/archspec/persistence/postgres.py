"""Supabase Session pooler的安全PG连接候选；不迁移或激活业务数据。"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

import certifi
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_POOLER = re.compile(rf"(?:{_HOST_LABEL}\.)+pooler\.supabase\.com")
_PROJECT = re.compile(_HOST_LABEL)
_CONNECT_SECONDS = 5
_SQL_MILLISECONDS = 5000
# 网页正常读取实测超过9秒；本地排队与网络建连分开，仍只允许两个运行连接。
_RUNTIME_POOL_WAIT_SECONDS = 30


class PostgresUnavailable(RuntimeError):
    """连接、探测或资源释放失败；不含数据库URL、密码或上游响应。"""

    code = "UNAVAILABLE"


_CONFIGURATION_MESSAGES = {
    "DSN_FORMAT": "ARCHSPEC_DATABASE_URL不能为空、含前后空白或控制字符。",
    "DSN_PARSE": "ARCHSPEC_DATABASE_URL只填完整连接URI，不要附带psql命令、引号或说明文字。",
    "DRIVER": "连接URI开头须为postgresql://或postgresql+psycopg://。",
    "PROJECT_URL": "SUPABASE_URL须为同一项目的HTTPS根地址。",
    "POOLER_HOST": "数据库地址须使用Supabase的Session pooler；密码含特殊字符时请先按URI编码。",
    "SESSION_PORT": "Session pooler端口须为5432，请勿使用事务池端口。",
    "DATABASE_NAME": "连接URI末尾的数据库名须为postgres。",
    "PROJECT_USERNAME": "数据库用户名须为postgres.加当前Supabase项目编号，请复制同项目的Session pooler URI。",
    "PASSWORD_MISSING": "连接URI缺少数据库密码；后台Secret key不能替代数据库密码。",
    "PASSWORD_PLACEHOLDER": "请将[YOUR-PASSWORD]替换为创建Supabase项目时设置的数据库密码，并去掉方括号。",
    "PASSWORD_CONTROL": "数据库密码含不允许的控制字符，请重新复制完整连接URI。",
    "QUERY_OPTIONS": "连接URI附加参数仅允许sslmode=require或verify-full；密码中的特殊字符请按URI编码。",
    "URI_FRAGMENT": "连接URI含未编码的#；若它属于数据库密码，请按URI编码。",
}


class PostgresConfigurationError(ValueError):
    """只携带服务端固定原因码和中文提示；不保存输入串、字段值或原异常。"""
    def __init__(self, code):
        self.code = code if code in _CONFIGURATION_MESSAGES else "DSN_PARSE"
        super().__init__(_CONFIGURATION_MESSAGES[self.code])


def _url_parts(value: URL, project_ref: str) -> URL:
    """逐字段拒绝且脱敏；仍只接受同项目Session pooler，不能覆盖TLS或端点。"""
    try:
        host = value.host or ""
        password = value.password
        if value.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise PostgresConfigurationError("DRIVER")
        if not isinstance(project_ref, str) or _PROJECT.fullmatch(project_ref) is None:
            raise PostgresConfigurationError("PROJECT_URL")
        if _POOLER.fullmatch(host) is None or len(host) > 253:
            raise PostgresConfigurationError("POOLER_HOST")
        if value.port != 5432:
            raise PostgresConfigurationError("SESSION_PORT")
        if value.database != "postgres":
            raise PostgresConfigurationError("DATABASE_NAME")
        if value.username != "postgres." + project_ref:
            raise PostgresConfigurationError("PROJECT_USERNAME")
        if not isinstance(password, str) or not password.strip():
            raise PostgresConfigurationError("PASSWORD_MISSING")
        if re.search(r"\[YOUR[-_ ]?PASSWORD\]", password, re.IGNORECASE):
            raise PostgresConfigurationError("PASSWORD_PLACEHOLDER")
        if any(unicodedata.category(char).startswith("C") for char in password):
            raise PostgresConfigurationError("PASSWORD_CONTROL")
        if (set(value.query) - {"sslmode"}
                or value.query.get("sslmode") not in {None, "require", "verify-full"}):
            raise PostgresConfigurationError("QUERY_OPTIONS")
    except PostgresConfigurationError:
        raise
    except (TypeError, ValueError, AttributeError):
        raise PostgresConfigurationError("DSN_PARSE") from None
    # 即使复制串携带require，实际引擎也一律使用verify-full和固定根证书。
    return URL.create(
        "postgresql+psycopg", username=value.username, password=password,
        host=host, port=5432, database="postgres",
    )


@dataclass(frozen=True)
class PostgresSettings:
    """完整DSN从repr隐藏；不可作为网页状态对象序列化。"""

    database_url: URL = field(repr=False)
    project_ref: str

    def __post_init__(self):
        object.__setattr__(self, "database_url", _url_parts(self.database_url, self.project_ref))


def _project_ref(value) -> str:
    try:
        if (
            not isinstance(value, str) or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        found = re.fullmatch(rf"({_HOST_LABEL})\.supabase\.co", host)
        if (
            parsed.scheme != "https" or found is None
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise PostgresConfigurationError("PROJECT_URL") from None
    return found[1]


def load_postgres_settings(environ=None) -> PostgresSettings | None:
    """仅解析指定后台环境字段；配置存在但无效时不回退SQLite或其他项目。"""
    environment = os.environ if environ is None else environ
    if "ARCHSPEC_DATABASE_URL" not in environment:
        return None
    value = environment["ARCHSPEC_DATABASE_URL"]
    if (
        not isinstance(value, str) or not value or len(value) > 8192
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise PostgresConfigurationError("DSN_FORMAT")
    project_ref = _project_ref(environment.get("SUPABASE_URL"))
    # urllib会把userinfo中的方括号先当非法IPv6拒绝；只识别密码槽里的已知占位符。
    # 提前解释拒绝原因，不放宽端点、项目、TLS或其他密码的原校验。
    if re.match(r"^postgresql(?:\+psycopg)?://[^:/@]+:\[YOUR[-_ ]?PASSWORD\]@", value, re.IGNORECASE):
        raise PostgresConfigurationError("PASSWORD_PLACEHOLDER")
    try:
        # SQLAlchemy会丢弃空值query，先对原始串保留空值并核对白名单。
        raw = urlsplit(value)
        pairs = parse_qsl(
            raw.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1,
        )
        if "#" in value:
            raise PostgresConfigurationError("URI_FRAGMENT")
        if "?" in value and pairs not in [[("sslmode", "require")], [("sslmode", "verify-full")]]:
            raise PostgresConfigurationError("QUERY_OPTIONS")
        parsed = make_url(value)
    except PostgresConfigurationError:
        raise
    except (TypeError, ValueError, ArgumentError):
        raise PostgresConfigurationError("DSN_PARSE") from None
    return PostgresSettings(parsed, project_ref)


def create_postgres_engine(settings: PostgresSettings):
    """只读连接探测只占一个连接；不用于同时持有请求读事务的运行入口。"""
    return _create_postgres_engine(settings, pool_size=1)


def create_postgres_runtime_engine(settings: PostgresSettings):
    """运行入口允许一个请求读取和一个独立任务写入；不溢出扩容或安装表。"""
    return _create_postgres_engine(settings, pool_size=2, pool_wait_seconds=_RUNTIME_POOL_WAIT_SECONDS)


def _create_postgres_engine(settings: PostgresSettings, *, pool_size: int, pool_wait_seconds: int = _CONNECT_SECONDS):
    """两个用途共享地址、TLS、脱敏与等待上限，避免运行池扩大时漏掉安全配置。"""
    if not isinstance(settings, PostgresSettings):
        raise ValueError("PG连接工厂需要已核验的同项目配置")  # noqa: TRY004 - 配置错误统一拒绝
    try:
        return create_engine(
            settings.database_url,
            pool_size=pool_size, max_overflow=0, pool_timeout=pool_wait_seconds,
            pool_pre_ping=True, pool_recycle=300,
            # 每次身份核验产生独立物理Table；跨请求编译缓存会持有整组副本。
            # 两类云端引擎统一禁用此缓存；目录、权限、预算仍逐次真实读取。
            query_cache_size=0,
            hide_parameters=True, echo=False,
            connect_args={
                "connect_timeout": _CONNECT_SECONDS,
                "sslmode": "verify-full",
                "sslrootcert": certifi.where(),
                "options": (
                    f"-c statement_timeout={_SQL_MILLISECONDS} "
                    f"-c lock_timeout={_SQL_MILLISECONDS} "
                    f"-c idle_in_transaction_session_timeout={_SQL_MILLISECONDS}"
                ),
            },
        )
    except Exception:  # noqa: BLE001 - 驱动/SDK错误不能传播凭据或连接串
        raise PostgresUnavailable("PG连接引擎创建失败，未重试或降低TLS校验") from None


class PostgresStore:
    """只拥有工厂为本实例新建的引擎；不借用或修改全局主业务引擎。"""

    def __init__(self, settings: PostgresSettings, *, engine_factory=None):
        if not isinstance(settings, PostgresSettings):
            raise ValueError("PG探测需要已核验的连接配置")  # noqa: TRY004 - 配置错误统一拒绝
        self._closed = False
        try:
            self.engine = (engine_factory or create_postgres_engine)(settings)
            if not all(callable(getattr(self.engine, name, None)) for name in ("connect", "dispose")):
                raise ValueError
        except Exception:  # noqa: BLE001 - 工厂错误与凭据均不可回显
            raise PostgresUnavailable("PG探测引擎无法安全创建") from None

    def probe_connection(self) -> dict:
        """只执行SELECT 1；结果与连接释放都成功才能说明访问已验证。"""
        if self._closed:
            raise PostgresUnavailable("PG探测连接已关闭")
        try:
            with self.engine.connect() as connection:
                value = connection.execute(text("SELECT 1")).scalar_one()
                if type(value) is not int or value != 1:
                    raise ValueError
        except Exception:  # noqa: BLE001 - 不把DSN、SQL驱动响应或账户信息送到网页
            raise PostgresUnavailable("PG只读连接检查未完成，未迁移或写入数据") from None
        return {"api_access": "VERIFIED"}

    def close(self) -> None:
        """仅释放本次工厂创建的引擎；显式关闭失败也不无限重试。"""
        if not self._closed:
            self._closed = True
            try:
                self.engine.dispose()
            except Exception:  # noqa: BLE001 - 资源释放错误也必须脱敏
                raise PostgresUnavailable("PG专属探测引擎释放失败") from None
