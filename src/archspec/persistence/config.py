"""外部连接的显式环境配置；连接检查不冒充数据迁移成功。"""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class StorageSettings:
    qdrant_url: str
    qdrant_api_key: str = field(repr=False)
    r2_endpoint_url: str
    r2_bucket: str
    r2_access_key_id: str = field(repr=False)
    r2_secret_access_key: str = field(repr=False)
    qdrant_collection: str = "archspec_official_v1"
    qdrant_dimension: int = 512
    r2_prefix: str = "archspec/v1"


def _endpoint(value, key, *, qdrant=False):
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        valid_host = (
            hostname.endswith(".cloud.qdrant.io")
            and len(hostname) <= 253
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                    for part in hostname.split("."))
        ) if qdrant else bool(re.fullmatch(
            r"[a-f0-9]{32}\.r2\.cloudflarestorage\.com", hostname))
        if (parsed.scheme != "https" or not valid_host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")
                or parsed.port not in ((None, 443, 6333) if qdrant else (None, 443))):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{key}必须是官方HTTPS服务端点，不能含凭据或附加路径") from None
    # 去掉默认443端口，确保配置检查与R2 SDK端点合同一致。
    return f"https://{hostname}" + (":6333" if qdrant and parsed.port == 6333 else "")


@dataclass(frozen=True)
class QdrantSettings:
    """只读检查可独立读取Qdrant配置，不要求先创建R2账户。"""
    qdrant_url: str
    qdrant_api_key: str = field(repr=False)
    qdrant_collection: str = "archspec_official_v1"
    qdrant_dimension: int = 512


@dataclass(frozen=True)
class R2Settings:
    r2_endpoint_url: str
    r2_bucket: str
    r2_access_key_id: str = field(repr=False)
    r2_secret_access_key: str = field(repr=False)
    r2_prefix: str = "archspec/v1"


QDRANT_REQUIRED_KEYS = ("QDRANT_URL", "QDRANT_API_KEY")
R2_REQUIRED_KEYS = ("R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def _required_values(env, keys):
    for key in keys:
        value = env.get(key)
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(f"外部存储缺少或无效配置：{key}")


def load_qdrant_settings(environ=None):
    """无配置时不联网；配置了一部分时明确拒绝，凭据不会进入repr或错误。"""
    env = os.environ if environ is None else environ
    if not any(key in env for key in QDRANT_REQUIRED_KEYS):
        return None
    _required_values(env, QDRANT_REQUIRED_KEYS)
    if (len(env["QDRANT_API_KEY"]) > 4096
            or any(not 33 <= ord(char) <= 126 for char in env["QDRANT_API_KEY"])):
        raise ValueError("QDRANT_API_KEY必须是可用于请求头的非空凭据")
    collection = env.get("QDRANT_COLLECTION", "archspec_official_v1")
    if not isinstance(collection, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", collection):
        raise ValueError("QDRANT_COLLECTION必须是独立集合名")
    dimension = env.get("QDRANT_DIMENSION", "512")
    if not isinstance(dimension, str) or not dimension.isascii() or not dimension.isdigit():
        raise ValueError("QDRANT_DIMENSION必须是正整数")
    if not 1 <= int(dimension) <= 65536:
        raise ValueError("QDRANT_DIMENSION超出支持范围")
    return QdrantSettings(
        qdrant_url=_endpoint(env["QDRANT_URL"], "QDRANT_URL", qdrant=True),
        qdrant_api_key=env["QDRANT_API_KEY"], qdrant_collection=collection,
        qdrant_dimension=int(dimension))


def load_r2_settings(environ=None):
    """这里只核对分项配置，不打开对象客户端或声称桶权限已验证。"""
    env = os.environ if environ is None else environ
    if not any(key in env for key in R2_REQUIRED_KEYS):
        return None
    _required_values(env, R2_REQUIRED_KEYS)
    bucket = env["R2_BUCKET"]
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
            or ".." in bucket):
        raise ValueError("R2_BUCKET必须是有效的私有桶名称")
    prefix = env.get("R2_PREFIX", "archspec/v1")
    if not isinstance(prefix, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise ValueError("R2_PREFIX必须是无路径回退的独立存储前缀")
    return R2Settings(
        r2_endpoint_url=_endpoint(env["R2_ENDPOINT_URL"], "R2_ENDPOINT_URL"),
        r2_bucket=bucket, r2_access_key_id=env["R2_ACCESS_KEY_ID"],
        r2_secret_access_key=env["R2_SECRET_ACCESS_KEY"], r2_prefix=prefix)


def load_storage_settings(environ=None):
    """全启动预检仍要求两项存储齐备，默认本地路径不变。"""
    env = os.environ if environ is None else environ
    switch = env.get("ARCHSPEC_STORAGE_PREFLIGHT", "false")
    if switch not in {"true", "false"}:
        raise ValueError("ARCHSPEC_STORAGE_PREFLIGHT只能明确设置为true或false")
    if switch == "false":
        return None
    # 保留完整预检先拒绝缺配置的行为，不把分项连接成功当成持久模式。
    _required_values(env, QDRANT_REQUIRED_KEYS + R2_REQUIRED_KEYS)
    vectors = load_qdrant_settings(env)
    objects = load_r2_settings(env)
    return StorageSettings(
        qdrant_url=vectors.qdrant_url, qdrant_api_key=vectors.qdrant_api_key,
        r2_endpoint_url=objects.r2_endpoint_url, r2_bucket=objects.r2_bucket,
        r2_access_key_id=objects.r2_access_key_id, r2_secret_access_key=objects.r2_secret_access_key,
        qdrant_collection=vectors.qdrant_collection, qdrant_dimension=vectors.qdrant_dimension,
        r2_prefix=objects.r2_prefix)


@dataclass(frozen=True)
class SupabaseSettings:
    """免结算账户的分项连接配置；不自动切换官方资产或事务数据路径。"""
    supabase_url: str
    supabase_secret_key: str = field(repr=False)
    supabase_bucket: str
    supabase_prefix: str = "archspec/v1"


SUPABASE_REQUIRED_KEYS = ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_BUCKET")


def load_supabase_settings(environ=None):
    """现代后台密钥不作为JWT；这里只核配置，不创建网络客户端。"""
    env = os.environ if environ is None else environ
    if not any(key in env for key in SUPABASE_REQUIRED_KEYS):
        return None
    _required_values(env, SUPABASE_REQUIRED_KEYS)
    try:
        parsed = urlsplit(env["SUPABASE_URL"])
        hostname = parsed.hostname or ""
        if (parsed.scheme != "https"
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co", hostname)
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("SUPABASE_URL必须是托管项目HTTPS根地址") from None
    secret = env["SUPABASE_SECRET_KEY"]
    if (not secret.startswith("sb_secret_") or not 11 <= len(secret) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in secret)):
        raise ValueError("SUPABASE_SECRET_KEY必须是现代后台Secret Key")
    bucket = env["SUPABASE_BUCKET"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ValueError("SUPABASE_BUCKET必须是独立私有桶名称")
    prefix = env.get("SUPABASE_PREFIX", "archspec/v1")
    if not isinstance(prefix, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise ValueError("SUPABASE_PREFIX必须是无路径回退的独立前缀")
    return SupabaseSettings("https://" + hostname, secret, bucket, prefix)


def _supabase_objects(settings):
    # 只有显式分项检查或后续迁移调用时，才加载对象适配器和它的资源。
    from archspec.persistence.supabase import SupabaseObjectStore
    return SupabaseObjectStore(settings.supabase_url, settings.supabase_secret_key,
                               settings.supabase_bucket, prefix=settings.supabase_prefix)


def _objects(settings):
    from archspec.persistence.r2 import R2Store
    return R2Store(settings.r2_endpoint_url, settings.r2_bucket, settings.r2_access_key_id,
                   settings.r2_secret_access_key, prefix=settings.r2_prefix)


def _vectors(settings):
    from archspec.persistence.qdrant import QdrantStore
    return QdrantStore(settings.qdrant_url, settings.qdrant_api_key,
                       settings.qdrant_collection, settings.qdrant_dimension)


def _probe(factory, settings, operation):
    store = factory(settings)
    try:
        getattr(store, operation)()
    finally:
        # 仅释放探测工厂所提供的短生命周期适配器，不保留隐式连接池。
        close = getattr(store, "close", None)
        if callable(close):
            close()


def check_storage_connections(environ=None, *, vector_factory=None, object_factory=None):
    """在seed、DB或后台工人之前执行；返回值仅说明连接与集合配置可用。"""
    settings = load_storage_settings(environ)
    if settings is None:
        return {"checked": False, "data_migration": "NOT_COMPLETED"}
    # 先只读核桶权限，再允许建立空向量集合；不自动创建桶或升级账户。
    try:
        _probe(object_factory or _objects, settings, "health_check")
    except (RuntimeError, ValueError, ImportError, OSError):
        raise RuntimeError("R2存储连接检查失败，服务器未启动；请核对桶权限与配置") from None
    try:
        _probe(vector_factory or _vectors, settings, "ensure_collection")
    except (RuntimeError, ValueError, ImportError, OSError):
        raise RuntimeError("Qdrant连接或集合身份检查失败，服务器未启动") from None
    return {"checked": True, "r2": "reachable", "qdrant": "reachable",
            "data_migration": "NOT_COMPLETED"}
