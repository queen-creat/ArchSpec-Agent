"""显式个人共享入口：沿用本机完整应用，增加表单登录和服务器访问门。"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit

from archspec.deployment import AccessGate


class PersonalAccessGate(AccessGate):
    """业务路由仍走原访问门；浏览器可用受保护的短期会话替代Basic弹窗。"""

    cookie_name = "archspec_personal_session"
    session_seconds = 8 * 60 * 60

    def __init__(self, application, username, password):
        super().__init__(application, username, password)
        # 只从当前账号凭据派生独立用途的签名密钥，不在cookie中保存账号或口令。
        self._session_key = hmac.new(self.password_digest,
            b"ArchSpec Personal Cookie v1\0" + self.username, hashlib.sha256).digest()

    @staticmethod
    def _host(headers):
        values = [value for name, value in headers if name.lower() == b"host"]
        if len(values) != 1 or not 1 <= len(values[0]) <= 255:
            return None
        host = values[0]
        try:
            text = host.decode("ascii")
            parsed = urlsplit("//" + text)
            if (any(ord(character) <= 32 or ord(character) == 127 for character in text)
                    or parsed.netloc != text or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment):
                return None
            _ = parsed.port  # 校验端口格式；签名仍绑定原始Host，不做宽松归一化。
        except (UnicodeError, ValueError):
            return None
        return host

    @classmethod
    def _local_host(cls, headers):
        host = cls._host(headers)
        return (host is not None
                and urlsplit("//" + host.decode("ascii")).hostname in {"127.0.0.1", "localhost", "::1"})

    @classmethod
    def _same_origin(cls, headers):
        host = cls._host(headers)
        origins = [value for name, value in headers if name.lower() == b"origin"]
        if host is None or len(origins) != 1:
            return False
        if any(name.lower() == b"sec-fetch-site" and value.lower() == b"cross-site"
               for name, value in headers):
            return False
        try:
            parsed = urlsplit(origins[0].decode("ascii"))
            # 隧道内层是HTTP，公网Origin须为HTTPS；不信任转发协议头。
            schemes = {"http", "https"} if cls._local_host(headers) else {"https"}
            return (parsed.scheme in schemes and parsed.netloc.encode("ascii") == host
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except (UnicodeError, ValueError):
            return False

    def _signature(self, host, expiry, nonce):
        message = b"\0".join((host, expiry.encode("ascii"), nonce.encode("ascii")))
        return hmac.new(self._session_key, message, hashlib.sha256).hexdigest()

    def _authorized(self, headers):
        # 显式Authorization只能沿原Basic合同，不能靠附带cookie掩盖错误凭据。
        if any(name.lower() == b"authorization" for name, _ in headers):
            return super()._authorized(headers)
        host = self._host(headers)
        cookies = [value for name, value in headers if name.lower() == b"cookie"]
        if host is None or len(cookies) != 1 or len(cookies[0]) > 4096:
            return False
        try:
            jar = SimpleCookie()
            jar.load(cookies[0].decode("ascii"))
            value = jar[self.cookie_name].value
            expiry, nonce, signature = value.split(".")
            if (not expiry.isdecimal() or len(expiry) > 12
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32}", nonce)
                    or not re.fullmatch(r"[a-f0-9]{64}", signature)
                    or not 0 < int(expiry) - int(time.time()) <= self.session_seconds):
                return False
            return hmac.compare_digest(signature, self._signature(host, expiry, nonce))
        except (UnicodeError, CookieError, KeyError, ValueError):
            return False

    async def _reject(self, send, status):
        if status != 401:
            await super()._reject(send, status)
            return
        # 浏览器不弹Basic对话框；API仍明确返回401，不把登录页面当API结果。
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"private, no-store")]})
        await send({"type": "http.response.body", "body": json.dumps(
            {"detail": "请先登录个人入口"}, ensure_ascii=False).encode("utf-8")})

    async def _login_page(self, send, *, invalid=False, status=200, head=False):
        message = "登录失败，请核对账号和密码。" if invalid else "请输入个人入口的访问账号和密码。"
        # 独立HTML不请求资产或业务API；错误页从不回显用户提交的输入。
        page = ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 ArchSpec</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f5f8;
font:16px/1.6 system-ui,sans-serif;color:#172536}main{box-sizing:border-box;width:calc(100% - 32px);
max-width:360px;padding:24px;background:white;border-radius:16px}h1{font-size:24px;margin:0 0 12px}
label{display:block;margin-top:16px}input,button{box-sizing:border-box;width:100%;font:inherit;
padding:10px 12px;border:1px solid #aeb9c6;border-radius:8px}button{margin-top:24px;background:#164e78;
color:white;border:0;cursor:pointer}p{color:#536275}</style><main><h1>登录 ArchSpec</h1><p>'''
                + message + '''</p><form method="post" action="/login">
<label for="username">账号</label><input id="username" name="username" autocomplete="username"
maxlength="64" required><label for="password">密码</label><input id="password" name="password"
type="password" autocomplete="current-password" minlength="16" maxlength="128" required>
<button type="submit">登录</button></form></main></html>''').encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"cache-control", b"private, no-store"),
            (b"content-security-policy", (b"default-src 'none'; style-src 'unsafe-inline'; "
             b"form-action 'self'; frame-ancestors 'none'; base-uri 'none'")),
            (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": b"" if head else page})

    def _cookie(self, scope, *, clear=False):
        headers = scope["headers"]
        if clear:
            value = ""
        else:
            expiry = str(int(time.time()) + self.session_seconds)
            nonce = secrets.token_urlsafe(24)
            value = f"{expiry}.{nonce}.{self._signature(self._host(headers), expiry, nonce)}"
        cookie = f"{self.cookie_name}={value}; HttpOnly; Path=/; SameSite=Strict"
        if clear:
            cookie += "; Max-Age=0"
        # 公网Host无条件Secure；本机HTTP才允许不加，转发头不能降低这个要求。
        if not self._local_host(headers) or scope.get("scheme") == "https":
            cookie += "; Secure"
        return cookie.encode("ascii")

    async def _redirect(self, scope, send, *, logout=False):
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", b"/login" if logout else b"/"),
            (b"set-cookie", self._cookie(scope, clear=logout)),
            (b"cache-control", b"private, no-store")]})
        await send({"type": "http.response.body", "body": b""})

    async def _login(self, scope, receive, send):
        headers = scope["headers"]
        if not self._same_origin(headers):
            await self._reject(send, 403)
            return
        types = [value for name, value in headers if name.lower() == b"content-type"]
        if len(types) != 1 or types[0].split(b";", 1)[0].strip().lower() != b"application/x-www-form-urlencoded":
            await self._login_page(send, invalid=True, status=400)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self._login_page(send, invalid=True, status=400)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 4096:
                await self._login_page(send, invalid=True, status=400)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            form = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                            encoding="utf-8", errors="strict", max_num_fields=4)
            if set(form) != {"username", "password"} or any(len(value) != 1 for value in form.values()):
                raise ValueError("登录字段无效")
            encoded = base64.b64encode((form["username"][0] + ":" + form["password"][0]).encode("utf-8"))
            # 复用原恒时账号/口令比较，不另建账号或认证资料。
            authorized = super()._authorized([(b"authorization", b"Basic " + encoded)])
        except (UnicodeError, ValueError):
            authorized = False
        if not authorized:
            await self._login_page(send, invalid=True, status=401)
            return
        await self._redirect(scope, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            method, path, headers = scope["method"], scope["path"], scope["headers"]
            if method in {"GET", "HEAD"} and (path == "/login" or path == "/" and not self._authorized(headers)):
                await self._login_page(send, head=method == "HEAD")
                return
            if path == "/login" and method == "POST":
                await self._login(scope, receive, send)
                return
            if path == "/logout" and method == "POST":
                if not self._authorized(headers):
                    await self._reject(send, 401)
                elif not self._same_origin(headers):
                    await self._reject(send, 403)
                else:
                    await self._redirect(scope, send, logout=True)
                return
            # cookie写请求必须有正确Origin；Basic继续采用原门的API/CSRF合同。
            if (method not in {"GET", "HEAD", "OPTIONS"}
                    and not any(name.lower() == b"authorization" for name, _ in headers)
                    and self._authorized(headers) and not self._same_origin(headers)):
                await self._reject(send, 403)
                return
        # 健康接口、其余权限与生命周期全部由原门处理，不复制业务白名单。
        await super().__call__(scope, receive, send)


def create_personal_app(*, application=None):
    """凭据只取当前进程；application仅供合成验证，不另建业务生命周期。"""
    username = os.environ.get("ARCHSPEC_PERSONAL_USER", "")
    password = os.environ.get("ARCHSPEC_PERSONAL_PASSWORD", "")
    # 沿既有服务器入口的账号/密码长度合同，拒绝缺配置后裸露原应用。
    # 这里不调用云端/演示设置，也不改变原数据目录、权限或累计预算。
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("个人入口必须配置有效访问账号")
    if (not 16 <= len(password) <= 128 or not password.strip()
            or any(ord(character) < 32 for character in password)):
        raise ValueError("个人入口密码须为16至128个字符，不能含控制字符或全为空白")
    if application is None:
        # 配置核齐后才导入真实应用；ASGI lifespan由AccessGate原样转交。
        from archspec.main import app

        application = app
    return PersonalAccessGate(application, username, password)
