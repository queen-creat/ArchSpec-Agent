"""独立服务器入口：复用原应用，访问门统一保护页面、资产和业务API。"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


@dataclass(frozen=True)
class ServerSettings:
    username: str
    password: str = field(repr=False)
    data_dir: Path


def load_server_settings(environ=None):
    """配置先核验，拒绝缺密码或误用本机资料；不创建任何目录。"""
    env = os.environ if environ is None else environ
    username = env.get("ARCHSPEC_SERVER_USER", "archspec")
    password = env.get("ARCHSPEC_SERVER_PASSWORD", "")
    directory = env.get("ARCHSPEC_DATA_DIR", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("服务器访问账号配置无效")
    if not 16 <= len(password) <= 128 or any(ord(c) < 32 for c in password):
        raise ValueError("服务器访问密码至少16个字符，不能含控制字符")
    if not directory:
        raise ValueError("服务器必须指定独立ARCHSPEC_DATA_DIR")
    data_dir = Path(directory).resolve()
    primary = Path(__file__).resolve().parents[2] / "data"
    if data_dir == primary.resolve():
        raise ValueError("服务器入口不能使用本机原data目录")
    return ServerSettings(username, password, data_dir)


class AccessGate:
    """纯ASGI门不提前读取上传正文；原应用的生命周期及流式响应保持。"""
    def __init__(self, application, username, password, *, server_settings=None):
        self.application = application
        self.server_settings = server_settings
        self.source_instance_uuid = str(uuid4())
        self.username = username.encode("utf-8")
        self.password_digest = hashlib.sha256(password.encode("utf-8")).digest()

    def capture_source(self, destination, *, timeout=30, keep_frozen=False):
        """可信迁移入口保全当前服务器；不新增公开HTTP导出或发送私有数据。"""
        from archspec.persistence.source_capture import capture_server_source
        return capture_server_source(self, destination, timeout=timeout, keep_frozen=keep_frozen)

    async def _reject(self, send, status):
        headers = [(b"content-type", b"application/json; charset=utf-8"),
                   (b"cache-control", b"private, no-store")]
        if status == 401:
            headers.append((b"www-authenticate", b'Basic realm="ArchSpec Agent", charset="UTF-8"'))
        message = "需要服务器访问账号和密码" if status == 401 else "跨站写请求已拒绝"
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body",
                    "body": json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")})

    def _authorized(self, headers):
        values = [value for name, value in headers if name.lower() == b"authorization"]
        if len(values) != 1:
            return False
        try:
            scheme, encoded = values[0].split(b" ", 1)
            if scheme.lower() != b"basic":
                return False
            username, password = base64.b64decode(encoded, validate=True).split(b":", 1)
        except (ValueError, binascii.Error):
            return False
        return (hmac.compare_digest(username, self.username)
                & hmac.compare_digest(hashlib.sha256(password).digest(), self.password_digest))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # 原应用没有WebSocket业务；未来新增时必须另行授权，不能绕过HTTP门。
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.application(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        if path == "/api/health" and method in {"GET", "HEAD"}:
            await self.application(scope, receive, send)
            return
        if not self._authorized(scope["headers"]):
            await self._reject(send, 401)
            return
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers = {k.lower(): v for k, v in scope["headers"]}
            origin = headers.get(b"origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin.decode("ascii"))
                    same = (parsed.scheme in {"http", "https"}
                            and parsed.netloc.encode("ascii") == headers.get(b"host")
                            and not parsed.path and not parsed.query and not parsed.fragment)
                except (UnicodeError, ValueError):
                    same = False
                if not same:
                    await self._reject(send, 403)
                    return
            if headers.get(b"sec-fetch-site") == b"cross-site":
                await self._reject(send, 403)
                return
        await self.application(scope, receive, send)


def load_backend_mode(environ=None):
    """模式先解析；未知模式不进入目录、种子、建表或worker副作用。"""
    env = os.environ if environ is None else environ
    mode = env.get("ARCHSPEC_PERSISTENCE_BACKEND", "LOCAL")
    if mode not in {"LOCAL", "REMOTE"}:
        raise ValueError("ARCHSPEC_PERSISTENCE_BACKEND只能为LOCAL或REMOTE")
    return mode


def create_server_app(*, bootstrap_seed=True):
    """先选择完整后端，REMOTE不走本地种子/建表；访问门和模型配置保留。"""
    config = load_server_settings()
    mode = load_backend_mode()
    if mode == "REMOTE":
        from archspec.persistence.remote_startup import prepare_remote_startup, startup_scope
        restored = prepare_remote_startup(config)
        previous = os.environ.get("ARCHSPEC_DATA_DIR")
        try:
            # 云端已完整回读的新缓存是本进程资料根，不覆盖旧data目录。
            os.environ["ARCHSPEC_DATA_DIR"] = str(restored.data_dir)
            with startup_scope(restored):
                from archspec.config import settings
                if settings.data_dir.resolve() != restored.data_dir.resolve():
                    raise ValueError("应用已导入其他资料根，请在新进程启动云端入口")
                from archspec.main import app
                app.state.runtime_bindings = restored.build_bindings()
                app.state.official_source_registry = app.state.runtime_bindings.registry
                app.state.cloud_generation = restored
                app.state.persistence_backend = "REMOTE"
            return AccessGate(app, config.username, config.password, server_settings=config)
        except BaseException as original_error:
            try:
                restored.close()
            except Exception:  # noqa: BLE001 - 不让清理失败掩盖实际启动错误。
                original_error.add_note("云端启动资源释放未完整确认，恢复缓存保留供核验")
            finally:
                # 即使资源释放失败，也不把调用进程遗留在临时缓存目录选择。
                if previous is None:
                    os.environ.pop("ARCHSPEC_DATA_DIR", None)
                else:
                    os.environ["ARCHSPEC_DATA_DIR"] = previous
            raise
    from archspec.persistence.config import check_storage_connections
    check_storage_connections()
    # 本地/演示原路径保留，真实密钥仍由服务端环境配置。
    os.environ["ARCHSPEC_DATA_DIR"] = str(config.data_dir)
    seed_directory = os.getenv("ARCHSPEC_DEMO_SEED_DIR")
    if seed_directory and bootstrap_seed:
        from archspec.demo_seed import bootstrap_demo_seed
        bootstrap_demo_seed(seed_directory, config.data_dir)
    from archspec.config import settings
    if settings.data_dir.resolve() != config.data_dir:
        raise ValueError("应用已导入其他数据根；请在新进程启动服务器入口")
    from archspec.database import create_qa_task_tables, engine
    from archspec.main import app
    create_qa_task_tables(engine)
    app.state.persistence_backend = "LOCAL"
    return AccessGate(app, config.username, config.password, server_settings=config)


def main():
    """遵循Render PORT，仅一个工作进程，避免队列共享资料重复执行。"""
    port = os.getenv("PORT", "10000")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("服务器PORT配置无效")
    import uvicorn
    uvicorn.run(create_server_app(), host="0.0.0.0", port=int(port),
                workers=1, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
