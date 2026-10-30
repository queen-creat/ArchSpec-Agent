"""部署主进程的私有接收与回环代理；不导入业务配置或保存后台密钥。"""
from __future__ import annotations

import base64
import hmac
import http.client
import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

_BLOCK = 64 * 1024
_PART_LIMIT = 4 * 1024 * 1024
_REQUEST_LIMIT = 128 * 1024 * 1024
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailer", "transfer-encoding", "upgrade"})
_PART = re.compile(r"/__bootstrap/parts/(0|[1-9][0-9]{0,5})")
_PENDING = "云端资料正在准备，请稍后刷新；可查看部署进度。"
_PENDING_STAGES = {
    "INSTALL_EXPLICIT_GOVERNANCE": "正在核对已有权限记录，完成后将恢复网站。",
    "RESTORE_REMOTE_BACKEND": "正在恢复正文与检索服务，完成核对后即可使用。",
    "VERIFY_PERSISTED_CONTENT_USE_REJECTION": "正文服务已启动，正在完成权限检查。",
}
_UNAVAILABLE = "云端操作暂未完成，请查看部署进度；未自动重复提交。"


class _RequestFrameError(ValueError):
    def __init__(self, status):
        self.status = status


class _BoundedBody:
    """到达 Content-Length 即返回 EOF；不得等待客户端关闭持久连接。"""
    def __init__(self, stream, length):
        self.stream = stream
        self.remaining = length

    def read(self, size=-1):
        if self.remaining == 0 or size == 0:
            return b""
        count = min(self.remaining, _BLOCK, size if size >= 0 else _BLOCK)
        content = self.stream.read(count)
        if not content or len(content) != count:
            raise _RequestFrameError(400)
        self.remaining -= len(content)
        return content

    def readinto(self, target):
        content = self.read(len(target))
        target[:len(content)] = content
        return len(content)


def _hop_headers(headers):
    # Connection 可额外登记逐跳字段，两端均移除，不把客户端代理配置传入网站。
    names = set(_HOP)
    for value in headers.get_all("Connection", []):
        names.update(part.strip().lower() for part in value.split(","))
    return names


def make_handler(manager, username, password):
    """manager 只提供 status/put_part/publish/backend_port，不接收凭据或任意目标地址。"""
    if not username or not password or ":" in username:
        raise ValueError("云端入口必须配置现有登录账号与密码")
    # cloud_probe 的认证在闭包内，无公共 helper；沿用同一恒时 Basic 字节合同。
    expected = b"Basic " + base64.b64encode((username + ":" + password).encode("utf-8"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _authorized(self):
            supplied = self.headers.get_all("Authorization", [])
            if len(supplied) != 1:
                return False
            return hmac.compare_digest(supplied[0].encode("utf-8"), expected)

        def _json(self, status, value):
            content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._started = True
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="ArchSpec Agent", charset="UTF-8"')
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(content)

        def _same_origin(self):
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origins = self.headers.get_all("Origin", [])
            if not origins:
                return True
            hosts = self.headers.get_all("Host", [])
            if len(origins) != 1 or len(hosts) != 1:
                return False
            try:
                value = urlsplit(origins[0])
                return (value.scheme in {"http", "https"} and value.netloc == hosts[0]
                        and not value.path and not value.query and not value.fragment
                        and value.username is None and value.password is None)
            except ValueError:
                return False

        def _length(self, *, required=False, maximum=_REQUEST_LIMIT):
            if self.headers.get_all("Transfer-Encoding"):
                raise _RequestFrameError(400)
            values = self.headers.get_all("Content-Length", [])
            if len(values) > 1:
                raise _RequestFrameError(400)
            if not values:
                if required:
                    raise _RequestFrameError(411)
                return 0
            if re.fullmatch(r"[0-9]{1,12}", values[0]) is None:
                raise _RequestFrameError(400)
            length = int(values[0])
            if length > maximum:
                raise _RequestFrameError(413)
            return length

        def _proxy(self, port, length):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("后端端口不可用")
            # 目标仅是本机固定子进程；保留原 path/query/Host，AccessGate 同源校验仍有效。
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                omitted = _hop_headers(self.headers) | {"content-length"}
                for name, value in self.headers.items():
                    if name.lower() not in omitted:
                        connection.putheader(name, value)
                connection.putheader("Content-Length", str(length))
                connection.putheader("Connection", "close")
                connection.endheaders()
                body = _BoundedBody(self.rfile, length)
                while chunk := body.read():
                    connection.send(chunk)
                # 连接/上传只等30秒；正文送达后，普通响应沿现有客户端120秒预算。
                # 所有透传方法共用这条规则，避免查询/阅读/进度各自补长超时白名单。
                # 原同步问答还允许两个120秒模型阶段及前后复核，沿用300秒上限。
                response_timeout = 120
                if self.command == "POST" and self.path in {"/api/official-qa", "/api/official-qa/model"}:
                    response_timeout = 300
                connection.sock.settimeout(response_timeout)
                # 没有连接重试：POST/PUT/PATCH/DELETE 的未知结果不能重放。
                response = connection.getresponse()
                omitted = _hop_headers(response.headers)
                if response.chunked:
                    omitted.add("content-length")
                self._started = True
                self.send_response_only(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in omitted:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    while chunk := response.read(_BLOCK):
                        self.wfile.write(chunk)
            finally:
                connection.close()

        def _handle(self):
            self._started = False
            self.connection.settimeout(30)
            try:
                if self.path == "/health" and self.command in {"GET", "HEAD"}:
                    failed = manager.status().get("state") == "FAILED"
                    self._json(503 if failed else 200, {"status": "unavailable" if failed else "running"})
                    return
                if not self._authorized():
                    self._json(401, {"detail": "需要服务器访问账号和密码"})
                    return
                if (not self.path.startswith("/") or "\r" in self.path or "\n" in self.path):
                    self._json(400, {"detail": "请求路径无效"})
                    return
                if self.command not in {"GET", "HEAD", "OPTIONS"} and not self._same_origin():
                    self._json(403, {"detail": "跨站写请求已拒绝"})
                    return
                if self.path == "/status":
                    if self.command not in {"GET", "HEAD"}:
                        self._json(405, {"detail": "部署进度只支持读取"})
                    else:
                        self._json(200, manager.status())
                    return
                if self.path.startswith("/__bootstrap/"):
                    match = _PART.fullmatch(self.path)
                    if match is None and self.path != "/__bootstrap/publish":
                        self._json(404, {"detail": "准备入口不存在"})
                        return
                    if self.command != "POST":
                        self._json(405, {"detail": "准备操作只支持提交"})
                        return
                    length = self._length(required=True, maximum=_PART_LIMIT if match else 0)
                    if match:
                        body = _BoundedBody(self.rfile, length)
                        result = manager.put_part(int(match.group(1)), body, length)
                        if body.remaining:
                            raise _RequestFrameError(400)
                    else:
                        result = manager.publish()
                    self._json(200, result)
                    return
                current = manager.status()
                if current.get("state") != "READY":
                    # 只翻译已知服务端阶段，不向用户回显异常、路径或不可信阶段原文。
                    detail = _PENDING_STAGES.get(current.get("stage"), _PENDING)
                    if current.get("state") == "FAILED":
                        detail = "网站恢复失败，请查看部署进度；未自动重启或重复提交。"
                    self._json(503, {"detail": detail})
                    return
                length = self._length(required=self.command in {"POST", "PUT", "PATCH"})
                try:
                    self._proxy(manager.backend_port, length)
                except Exception:  # noqa: BLE001 - 不能回显后端路径/正文/连接信息或重试写入。
                    if not self._started:
                        self._json(502, {"detail": "网站连接暂时不可用，请稍后重试。"})
            except _RequestFrameError as error:
                if not self._started:
                    self._json(error.status, {"detail": "请求正文长度或分块格式无效"})
            except Exception:  # noqa: BLE001 - 外部 manager/网络错误只输出固定中文状态。
                if not self._started:
                    try:
                        self._json(503, {"detail": _UNAVAILABLE})
                    except (OSError, ValueError):
                        pass
            finally:
                self.close_connection = True

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

        def log_message(self, *_):
            return  # 不记录 Authorization、原始正文或后台异常。

    return Handler
