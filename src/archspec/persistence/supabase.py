"""Supabase私有对象REST适配器；不可覆盖、完整字节核验、仅后端现代密钥。"""
from __future__ import annotations

import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import sleep
from urllib.parse import quote

import httpx

from archspec.persistence.r2 import (
    R2Conflict,
    R2Store,
    R2Unavailable,
    _expected_hash,
    _regular_file,
    _safe_name,
)

_MAX_OBJECT_BYTES = 4 * 1024 * 1024
_MAX_SMALL_BYTES = 256 * 1024
_CHUNK_BYTES = 1024 * 1024
_READ_ATTEMPTS = 3
_READ_BACKOFF_SECONDS = (0.1, 0.3)
_TRANSIENT_READ_STATUSES = frozenset({502, 503, 504})
_ENDPOINT = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co(?::443)?/?"
)
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")


class SupabaseUnavailable(RuntimeError):
    """通信或服务失败；安全状态码与消息不包含密钥或远端原始内容。"""

    def __init__(self, message, *, code="UNAVAILABLE", http_status=None):
        super().__init__(message)
        self.code = code
        # 仅保留固定数值诊断；不保存远端响应、URL、头或任何密钥。
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


class _TransientRead(SupabaseUnavailable):
    """只有认证对象GET的已知短暂故障可签发；不借动态消息或code决定重试。"""

    def __init__(self, *, http_status=None):
        super().__init__("Supabase只读对象暂时不可用", http_status=http_status)


class SupabaseConflict(ValueError):
    """私有性、原字节身份、长度或已有文件不一致，禁止覆盖。"""


class SupabaseNotFound(LookupError):
    """指定桶或私有对象不存在；不与通信失败混同。"""

    code = "MISSING"


def _name(value):
    try:
        return _safe_name(value)
    except ValueError:
        raise ValueError("Supabase对象名称或前缀不安全") from None


class SupabaseObjectStore:
    """最多4MiB的原始对象；大文件由独立分块适配器保持完整原件字节。"""

    def __init__(self, url, secret_key, bucket, *, prefix="archspec/v1", client=None):
        if not isinstance(url, str) or not _ENDPOINT.fullmatch(url):
            raise ValueError("Supabase地址必须是托管项目的HTTPS根端点")
        if (not isinstance(secret_key, str) or not secret_key.startswith("sb_secret_")
                or not 11 <= len(secret_key) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in secret_key)):
            raise ValueError("Supabase必须使用非空的现代后台Secret Key")
        if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket):
            raise ValueError("Supabase桶名必须是3至63位小写字母、数字或连字符")
        self.prefix = _name(prefix)
        self.bucket = bucket
        self.max_object_bytes = _MAX_OBJECT_BYTES
        self._url = url.rstrip("/")
        self._secret_key = secret_key
        self._owns_client = client is None
        self._client_closed = False
        self._client = client if client is not None else httpx.Client(
            timeout=10.0, follow_redirects=False, trust_env=False)

    def close(self):
        """只释放本适配器建立的连接，注入客户端仍归调用方管理。"""
        if self._owns_client and not self._client_closed:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - 外部边界统一脱敏
                raise SupabaseUnavailable("Supabase客户端连接关闭失败") from None
            self._client_closed = True

    def _key(self, key):
        return _name(f"{self.prefix}/{_name(key)}")

    def _object_path(self, full_key, *, authenticated=False):
        mode = "authenticated/" if authenticated else ""
        return "/object/" + mode + self.bucket + "/" + quote(full_key, safe="/")

    @contextmanager
    def _response(self, method, path, *, headers=None, content=None):
        response = None
        failed = False
        try:
            request = self._client.build_request(
                method, self._url + "/storage/v1" + path,
                headers={"apikey": self._secret_key, "accept-encoding": "identity",
                         "user-agent": "ArchSpecAgent/1.0", **(headers or {})},
                content=content, timeout=10.0)
            # 现代密钥只放apikey；不继承注入客户端的用户JWT、Cookie或Basic认证。
            for name in ("authorization", "cookie", "proxy-authorization"):
                request.headers.pop(name, None)
            response = self._client.send(request, stream=True, auth=None, follow_redirects=False)
            yield response
        except (SupabaseUnavailable, SupabaseConflict, SupabaseNotFound):
            failed = True
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError):
            failed = True
            if method == "GET" and path.startswith(("/object/authenticated/", "/bucket/")):
                # 私有对象和指定桶均为只读；写入与其它方法的未知结果仍不重放。
                raise _TransientRead() from None
            raise SupabaseUnavailable("Supabase请求或响应处理失败") from None
        except Exception:  # noqa: BLE001 - HTTP提供方未知异常不能泄露请求或密钥
            failed = True
            raise SupabaseUnavailable("Supabase请求或响应处理失败") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - 响应关闭失败同样脱敏且不覆盖原错误
                    if not failed:
                        raise SupabaseUnavailable("Supabase响应流无法正常关闭") from None

    @staticmethod
    def _length(response, maximum, expected_size=None):
        if response.headers.get("content-range"):
            raise SupabaseConflict("Supabase对象不能是部分内容")
        if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
            raise SupabaseConflict("Supabase对象存在未允许的内容编码")
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        if not re.fullmatch(r"[0-9]{1,20}", raw):
            raise SupabaseConflict("Supabase对象响应长度无效")
        size = int(raw)
        if size > maximum or (expected_size is not None and size != expected_size):
            raise SupabaseConflict("Supabase对象长度与登记或字节预算不一致")
        return size

    def _consume(self, response, maximum, *, sink=None, expected_sha256=None, expected_size=None):
        import hashlib

        announced = self._length(response, maximum, expected_size)
        digest = hashlib.sha256()
        size = 0
        result = bytearray() if sink is None and expected_sha256 is None else None
        # 身份编码保证iter_bytes保持原始字节；小说明不先缓冲整个响应。
        for block in response.iter_bytes(chunk_size=min(_CHUNK_BYTES, maximum + 1)):
            if not isinstance(block, bytes):
                raise SupabaseConflict("Supabase对象响应必须是原始字节")
            size += len(block)
            if size > maximum:
                raise SupabaseConflict("Supabase对象实际字节超过预算")
            digest.update(block)
            if sink is not None:
                sink.write(block)
            if result is not None:
                result.extend(block)
        if ((announced is not None and size != announced)
                or (expected_size is not None and size != expected_size)
                or (expected_sha256 is not None and digest.hexdigest() != expected_sha256)):
            raise SupabaseConflict("Supabase对象实际字节与登记不一致")
        return bytes(result) if result is not None else None

    def _json(self, response):
        try:
            value = json.loads(self._consume(response, _MAX_SMALL_BYTES))
        except (SupabaseConflict, SupabaseUnavailable):
            raise
        except Exception:  # noqa: BLE001 - 坏JSON或过深响应不能返回原始内容
            raise SupabaseUnavailable("Supabase返回了无法解析的数据") from None
        if not isinstance(value, dict):
            raise SupabaseUnavailable("Supabase响应数据合同不正确")
        return value

    def _failure(self, response, document=None):
        status = response.status_code
        # HTTP认证拒绝优先；不能用响应体的“缺失”覆盖真正的权限拒绝。
        if status in {401, 403}:
            raise SupabaseUnavailable("Supabase访问权限未通过", code="ACCESS_DENIED")
        if status == 404:
            raise SupabaseNotFound("Supabase指定私有桶或对象不存在")
        # Supabase官方默认把非500错误封装为HTTP400；三种读取及上传统一解析。
        # 只读有界JSON和明确协议字段，不匹配任意message，也不回显远端内容。
        if status == 400:
            document = self._json(response) if document is None else document
            code = document.get("code")
            semantic_status = document.get("statusCode")
            if code in {"AccessDenied", "InvalidJWT", "ExpiredToken", "InvalidSignature", "SignatureDoesNotMatch"} or semantic_status in {401, 403, "401", "403"}:
                raise SupabaseUnavailable("Supabase访问权限未通过", code="ACCESS_DENIED")
            if (code in {"NoSuchBucket", "NoSuchKey"}
                    and semantic_status in {None, 404, "404"}) or (
                    code is None and document.get("error") == "not_found"
                    and semantic_status in {404, "404"}):
                raise SupabaseNotFound("Supabase指定私有桶或对象不存在")
        raise SupabaseUnavailable("Supabase请求未成功，请核对服务状态与访问权限",
                                  http_status=status)

    def health_check(self):
        """只GET指定桶；短暂无响应沿现有三次预算，仍必须核定私有性。"""
        def read_once():
            with self._response("GET", "/bucket/" + self.bucket) as response:
                if response.status_code in _TRANSIENT_READ_STATUSES:
                    raise _TransientRead(http_status=response.status_code)
                if response.status_code != 200:
                    self._failure(response)
                bucket = self._json(response)
                if bucket.get("id") != self.bucket or bucket.get("public") is not False:
                    raise SupabaseConflict("Supabase指定桶必须存在并明确为私有桶")
        # 不重做启动事务或POST；认证、桶身份和格式错误不签发重读资格。
        return self._read_complete(read_once)

    @staticmethod
    def _safe_parents(path):
        try:
            R2Store._safe_parents(path)
        except R2Conflict:
            raise SupabaseConflict("Supabase缓存父目录不能包含链接或目录联接") from None

    @staticmethod
    def _size(expected_size):
        if expected_size is not None and (
                type(expected_size) is not int or not 0 <= expected_size <= _MAX_OBJECT_BYTES):
            raise ValueError("Supabase预期大小必须在原始对象字节预算内")

    def _hash_stream(self, stream, sink=None):
        # 复用已验的有界流式指纹实现，供应商异常在本边界转换，原R2代码不变。
        try:
            return R2Store._hash_stream(self, stream, sink)
        except R2Conflict:
            raise SupabaseConflict("Supabase文件或对象超过预算或不是原始字节") from None

    def _existing_matches(self, path, expected_sha256, expected_size):
        try:
            return R2Store._existing_matches(self, path, expected_sha256, expected_size)
        except R2Conflict:
            raise SupabaseConflict("Supabase本机目标不安全或已有不同内容，禁止覆盖") from None
        except R2Unavailable:
            raise SupabaseUnavailable("Supabase已有本机缓存无法安全读取") from None

    def put_file(self, key, path, expected_sha256, *, content_type="application/octet-stream"):
        """只POST新对象；已知重复必须完整读取同键真实字节证明幂等。"""
        full_key = self._key(key)
        expected_sha256 = _expected_hash(expected_sha256)
        if (not isinstance(content_type, str) or not 1 <= len(content_type) <= 256
                or any(not 32 <= ord(char) <= 126 for char in content_type)):
            raise ValueError("Supabase内容类型不安全")
        source_path = Path(path)
        self._safe_parents(source_path)
        try:
            # 先做独占已核副本再发HTTP，原文件在发送前变化不能改变登记字节。
            with _regular_file(source_path) as source, tempfile.TemporaryFile(
                    "w+b", dir=source_path.parent) as verified:
                digest, size = self._hash_stream(source, verified)
                if digest != expected_sha256:
                    raise SupabaseConflict("Supabase上传文件SHA256与登记不一致")
                verified.seek(0)
                duplicate = False
                with self._response("POST", self._object_path(full_key),
                                    headers={"x-upsert": "false", "content-type": content_type,
                                             "content-length": str(size)}, content=verified) as response:
                    if response.status_code in {400, 409}:
                        document = self._json(response)
                        duplicate = (
                            document.get("code") in {"KeyAlreadyExists", "ResourceAlreadyExists"}
                            or document.get("error") in {"Duplicate", "Asset Already Exists"}
                            or document.get("message") == "Asset Already Exists")
                        if not duplicate:
                            self._failure(response, document)
                    elif response.status_code in {200, 201}:
                        document = self._json(response)
                        if document.get("Key") != self.bucket + "/" + full_key:
                            raise SupabaseUnavailable("Supabase未确认目标对象写入身份")
                    else:
                        self._failure(response)
                if duplicate:
                    self._read_object(full_key, expected_sha256, size)
        except SupabaseConflict:
            raise
        except ValueError:
            raise SupabaseConflict("Supabase上传源必须是可安全读取的普通文件") from None
        except OSError:
            raise SupabaseUnavailable("Supabase上传源或已核临时副本无法安全读取") from None
        return full_key

    @staticmethod
    def _read_complete(operation):
        """重做一次完整只读操作；不复用半份sink，不包裹任何POST或外层业务事务。"""
        for attempt in range(_READ_ATTEMPTS):
            try:
                return operation()
            except _TransientRead as failure:
                if attempt + 1 == _READ_ATTEMPTS:
                    raise SupabaseUnavailable("Supabase只读对象在有限重读后仍不可用",
                                              http_status=failure.http_status) from None
                sleep(_READ_BACKOFF_SECONDS[attempt])

    @contextmanager
    def _private_object_response(self, full_key):
        # 这里只判一次响应身份，不在yield内部重入，避免流式调用者消费两份不同响应。
        with self._response("GET", self._object_path(full_key, authenticated=True)) as response:
            if response.status_code in _TRANSIENT_READ_STATUSES:
                raise _TransientRead(http_status=response.status_code)
            if response.status_code == 206:
                raise SupabaseConflict("Supabase对象不能是部分内容")
            if response.status_code != 200:
                self._failure(response)
            yield response

    def _read_object(self, full_key, expected_sha256, expected_size, sink=None):
        with self._private_object_response(full_key) as response:
            self._consume(response, _MAX_OBJECT_BYTES, sink=sink,
                          expected_sha256=expected_sha256, expected_size=expected_size)

    def download_file(self, key, destination, expected_sha256, *, expected_size=None):
        """每次完整重读都重新建专属暂存并核SHA后排他发布；旧目标始终不覆盖。"""
        def read_once():
            try:
                # R2的finally先关闭/清理本次暂存，才允许下一次重新分配；内层不重试。
                return R2Store.download_file(
                    self, key, destination, expected_sha256, expected_size=expected_size)
            except R2Conflict:
                raise SupabaseConflict("Supabase本机缓存身份或排他发布失败") from None
            except R2Unavailable:
                raise SupabaseUnavailable("Supabase本机缓存保存或临时清理失败") from None

        return self._read_complete(read_once)

    def read_bytes(self, key, *, max_bytes=_MAX_SMALL_BYTES):
        """有界小说明整份重读；旧的半份JSON缓冲随上次响应丢弃，不拼接下一份字节。"""
        full_key = self._key(key)
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_SMALL_BYTES:
            raise ValueError("Supabase小说明读取预算必须为1至262144字节")

        def read_once():
            with self._private_object_response(full_key) as response:
                return self._consume(response, max_bytes)

        return self._read_complete(read_once)
