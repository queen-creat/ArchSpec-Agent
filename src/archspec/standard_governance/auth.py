"""仅供本机维护者的核验入口；不是完整的多用户登录系统。"""

import hmac
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from archspec.config import settings


def maintenance_status() -> dict:
    """只报告配置是否存在，不把凭据或维护者身份返回给普通网页。"""
    return {
        "verification_enabled": settings.standard_verification_enabled,
        "maintenance_auth_configured": bool(
            settings.standard_admin_token and settings.standard_admin_token.strip()
            and settings.standard_admin_actor_id and settings.standard_admin_actor_id.strip()
        ),
        "local_only": True,
    }


def _deny(status_code: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _local_authority(raw_host: str) -> tuple[str, int | None] | None:
    """Host 只能包含明确的本机主机与端口，不接受路径、用户名或伪装域名。"""
    try:
        parsed = urlsplit(f"http://{raw_host}")
        if (parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or raw_host != raw_host.strip()):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        return parsed.hostname, port
    except ValueError:
        return None


def require_standard_maintainer(request: Request) -> str:
    """验证开关、真实连接地址、浏览器来源和凭据，然后返回服务端操作者。"""
    if not settings.standard_verification_enabled:
        _deny(503, "VERIFICATION_DISABLED", "规范核验维护入口默认关闭。")
    token = settings.standard_admin_token
    actor_id = settings.standard_admin_actor_id
    if not token or not token.strip() or not actor_id or not actor_id.strip():
        _deny(503, "VERIFICATION_AUTH_NOT_CONFIGURED", "维护入口尚未配置独立凭据和操作者。")

    # 不读取 X-Forwarded-For 等转发头：它们不能证明连接来自本机。
    try:
        peer_is_local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        peer_is_local = False
    if not peer_is_local:
        _deny(403, "LOCAL_ONLY_REQUIRED", "维护请求必须直接来自本机连接。")

    hosts = request.headers.getlist("host")
    authority = _local_authority(hosts[0]) if len(hosts) == 1 else None
    scheme = request.scope.get("scheme")
    if authority is None or scheme not in {"http", "https"}:
        _deny(403, "ADMIN_ORIGIN_REJECTED", "维护请求的主机或来源不可信。")
    origins = request.headers.getlist("origin")
    if origins:
        try:
            origin = urlsplit(origins[0])
            origin_authority = _local_authority(origin.netloc)
            default_port = 443 if scheme == "https" else 80
            matching_origin = (
                len(origins) == 1 and origin.scheme == scheme
                and not origin.path and not origin.query and not origin.fragment
                and origin_authority is not None
                and origin_authority[0] == authority[0]
                and (origin_authority[1] or default_port) == (authority[1] or default_port)
            )
        except ValueError:
            matching_origin = False
        if not matching_origin:
            _deny(403, "ADMIN_ORIGIN_REJECTED", "维护请求的主机或来源不可信。")

    supplied_tokens = request.headers.getlist("x-archspec-admin-token")
    supplied = supplied_tokens[0] if len(supplied_tokens) == 1 else ""
    # 转成字节再比较，非法/非 ASCII 输入同样只能得到 401，不能触发 500。
    if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        _deny(401, "ADMIN_AUTH_REQUIRED", "需要有效的维护者凭据。")
    return actor_id
