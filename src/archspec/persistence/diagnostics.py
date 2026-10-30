"""服务端分项诊断；配置存在、连接已读和完整迁移是三个不同状态。"""
import os

from archspec.persistence import config
from archspec.persistence.postgres import (
    PostgresConfigurationError,
    PostgresStore,
    load_postgres_settings,
)
from archspec.persistence.qdrant import QdrantUnavailable


def _configuration(env, keys, loader):
    if not any(key in env for key in keys):
        return "NOT_CONFIGURED", None
    if not all(key in env for key in keys):
        return "INCOMPLETE", None
    try:
        return "CONFIGURED", loader(env)
    except (ValueError, TypeError, OverflowError):
        # 输入值只由配置加载器读取；前端只得到有限枚举，不回显端点和凭据。
        return "INVALID", None


def _status(env):
    qdrant_state, qdrant_settings = _configuration(
        env, config.QDRANT_REQUIRED_KEYS, config.load_qdrant_settings)
    r2_state, _ = _configuration(env, config.R2_REQUIRED_KEYS, config.load_r2_settings)
    supabase_state, supabase_settings = _configuration(
        env, config.SUPABASE_REQUIRED_KEYS, config.load_supabase_settings)
    # 项目URL为共享公开配置；仅它存在不意味着用户已配置数据库连接。
    if "ARCHSPEC_DATABASE_URL" not in env:
        postgres_state, postgres_settings = "NOT_CONFIGURED", None
    else:
        postgres_state, postgres_settings = _configuration(
            env, ("ARCHSPEC_DATABASE_URL", "SUPABASE_URL"), load_postgres_settings)
    result = {
        "qdrant": {"configuration": qdrant_state, "api_access": "NOT_CHECKED",
                   "collection": "NOT_CHECKED"},
        "r2": {"configuration": r2_state, "api_access": "NOT_CHECKED"},
        "supabase": {"configuration": supabase_state, "api_access": "NOT_CHECKED",
                     "bucket": "NOT_CHECKED"},
        "postgres": {"configuration": postgres_state, "api_access": "NOT_CHECKED"},
        "active_backend": "LOCAL", "data_migration": "NOT_COMPLETED",
    }
    if postgres_state == "INVALID":
        # 保留所有旧状态字段；附加固定原因码，不回显输入值或更改连接守卫。
        try:
            load_postgres_settings(env)
        except PostgresConfigurationError as error:
            result["postgres_configuration_issue"] = {"code": error.code, "message": str(error)}
        except (ValueError, TypeError, OverflowError):
            result["postgres_configuration_issue"] = {
                "code": "DSN_PARSE", "message": "数据库连接配置无法安全解析，请检查完整URI。",
            }
    return result, qdrant_settings, supabase_settings, postgres_settings


def storage_status(environ=None):
    """GET只核配置，不创建网络客户端，也不探测任何外部服务。"""
    result, _, _, _ = _status(os.environ if environ is None else environ)
    return result


def _check_vectors(result, settings, factory):
    if settings is None:
        return
    try:
        store = (factory or config._vectors)(settings)
        try:
            result["qdrant"].update(store.probe_connection())
        finally:
            store.close()
    except QdrantUnavailable as error:
        if error.code == "INCOMPATIBLE":
            result["qdrant"].update(api_access="VERIFIED", collection="INCOMPATIBLE")
        else:
            result["qdrant"].update(
                api_access="ACCESS_DENIED" if error.code == "ACCESS_DENIED" else "UNAVAILABLE",
                collection="NOT_CHECKED")
    except (RuntimeError, ValueError, ImportError, OSError):
        result["qdrant"].update(api_access="UNAVAILABLE", collection="NOT_CHECKED")


def _check_supabase(result, settings, factory):
    if settings is None:
        return
    try:
        store = (factory or config._supabase_objects)(settings)
        try:
            # 该适配器仅GET指定桶，并且public必须严格为false才可返回成功。
            store.health_check()
        finally:
            store.close()
        result["supabase"].update(api_access="VERIFIED", bucket="PRIVATE")
    except (RuntimeError, ValueError, LookupError, ImportError, OSError) as error:
        code = getattr(error, "code", None)
        if code == "MISSING":
            # 缺桶/404不具备成功响应合同，不能据此证明后台认证已通过。
            result["supabase"].update(api_access="UNAVAILABLE", bucket="MISSING")
        else:
            result["supabase"].update(
                api_access="ACCESS_DENIED" if code == "ACCESS_DENIED" else "UNAVAILABLE",
                bucket="NOT_CHECKED")


def _check_postgres(result, settings, factory):
    if settings is None:
        return
    try:
        store = (factory or PostgresStore)(settings)
        try:
            if store.probe_connection() != {"api_access": "VERIFIED"}:
                raise ValueError("PG只读探测结果合同无法核对")
        finally:
            store.close()
        result["postgres"]["api_access"] = "VERIFIED"
    except (RuntimeError, ValueError, ImportError, OSError):
        # 只读成功不代表schema或控制状态已迁入；SQL响应与连接串不会返回网页。
        result["postgres"]["api_access"] = "UNAVAILABLE"


def check_storage_status(environ=None, *, vector_factory=None, supabase_factory=None,
                         postgres_factory=None):
    """服务分别限时读取：其中一个未配置或失败，不跳过另一个的明确检查。"""
    result, vectors, supabase, postgres = _status(os.environ if environ is None else environ)
    _check_vectors(result, vectors, vector_factory)
    _check_supabase(result, supabase, supabase_factory)
    _check_postgres(result, postgres, postgres_factory)
    return result
