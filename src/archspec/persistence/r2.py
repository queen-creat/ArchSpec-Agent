"""Cloudflare R2 的私有、不可覆盖对象适配器；本机仅保存已核验的缓存。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, IncompleteReadError

_CHUNK_BYTES = 1024 * 1024
_ENDPOINT = re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class R2Unavailable(RuntimeError):
    """远端不可用或本机缓存读写失败；不包含凭据及上游响应。"""


class R2Conflict(ValueError):
    """对象身份、完整性或已有文件冲突；禁止覆盖旧内容。"""


class R2NotFound(LookupError):
    """指定的私有对象不存在，调用者可区分首次初始化与服务故障。"""


def _safe_name(value: str) -> str:
    # 禁止路径穿越、URL和编码别名，避免键与本地缓存路径产生不同解释。
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or ".." in value
        or any(char in value for char in "\\:%?#")
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(part in {"", "."} for part in value.split("/"))
    ):
        raise ValueError("R2对象名称不安全")
    if len(value.encode("utf-8")) > 1024:
        raise ValueError("R2对象名称超过长度限制")
    return value


def _expected_hash(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("必须提供完整的小写SHA256指纹")
    return value


def _regular_file(path: Path) -> BinaryIO:
    """核对原路径和打开后的句柄，拒绝符号链接及特殊文件。"""
    descriptor = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            raise ValueError("R2文件必须是普通文件且不能是符号链接")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        # 即使lstat与open之间被换成FIFO，也不能阻塞整个请求。
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ValueError("R2文件身份在读取前发生变化")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream
    except OSError:
        raise ValueError("R2文件不存在或无法安全读取") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class R2Store:
    """只写新对象，下载必须核验SHA256；不提供删除或公开链接能力。"""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        *,
        prefix: str = "archspec/v1",
        client=None,
        max_object_bytes: int = 128 * 1024 * 1024,
    ):
        if not isinstance(endpoint, str) or _ENDPOINT.fullmatch(endpoint) is None:
            raise ValueError("R2端点必须是账户专属的HTTPS地址")
        if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
            raise ValueError("R2桶名必须是3至63位小写字母、数字或连字符")
        self.prefix = _safe_name(prefix)
        if isinstance(max_object_bytes, bool) or not isinstance(max_object_bytes, int):
            # 公开配置验证合同统一使用ValueError，方便调用者拒绝非法配置。
            raise ValueError("R2对象字节预算必须是正整数")  # noqa: TRY004
        if max_object_bytes <= 0:
            raise ValueError("R2对象字节预算必须是正整数")
        for credential in (access_key_id, secret_access_key):
            if not isinstance(credential, str) or not credential.strip():
                raise ValueError("R2访问凭据配置不完整")
        self.bucket = bucket
        self.max_object_bytes = max_object_bytes
        # 禁止SDK隐式重试；失败由上层有限流程显式处理，避免收费请求扩散。
        self._owns_client = client is None
        self._client_closed = False
        self.client = (
            client
            if client is not None
            else boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        )

    def close(self) -> None:
        """只关闭本适配器创建的连接；注入连接仍由调用者负责。"""
        if self._owns_client and not self._client_closed:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - 外部边界统一脱敏
                raise R2Unavailable("R2客户端连接关闭失败") from None
            self._client_closed = True

    def _key(self, key: str) -> str:
        return _safe_name(f"{self.prefix}/{_safe_name(key)}")

    @staticmethod
    def _size(expected_size: int | None) -> None:
        if expected_size is not None and (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise ValueError("R2预期字节数必须是非负整数")

    def health_check(self) -> None:
        """只读检查桶的访问能力，不创建、列举、删除或上传对象。"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001 - 外部边界统一脱敏
            raise R2Unavailable("R2不可用：无法确认私有桶的读取能力") from None

    def _hash_stream(self, stream: BinaryIO, sink: BinaryIO | None = None) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        while True:
            block = stream.read(_CHUNK_BYTES)
            if not isinstance(block, bytes):
                raise R2Conflict("R2对象响应体必须是原始字节")
            if not block:
                break
            size += len(block)
            if size > self.max_object_bytes:
                raise R2Conflict("R2对象超过字节预算")
            digest.update(block)
            if sink is not None:
                sink.write(block)
        return digest.hexdigest(), size

    def put_file(
        self,
        key: str,
        path: Path | str,
        expected_sha256: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> str:
        """返回完整对象键；同键仅在远端实际字节完全一致时视为幂等成功。"""
        full_key = self._key(key)
        expected_sha256 = _expected_hash(expected_sha256)
        if (
            not isinstance(content_type, str)
            or not content_type
            or len(content_type) > 256
            or any(unicodedata.category(char).startswith("C") for char in content_type)
        ):
            raise ValueError("R2内容类型不安全")
        source_path = Path(path)
        self._safe_parents(source_path)
        # 独占临时副本与指纹同时生成，避免原文件在hash之后被修改而错贴旧指纹。
        try:
            with _regular_file(source_path) as source, tempfile.TemporaryFile("w+b") as verified:
                digest, size = self._hash_stream(source, verified)
                if digest != expected_sha256:
                    raise R2Conflict("R2上传文件的SHA256与登记不一致")
                verified.seek(0)
                try:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=full_key,
                        Body=verified,
                        IfNoneMatch="*",
                        Metadata={"sha256": expected_sha256},
                        ContentLength=size,
                        ContentType=content_type,
                    )
                except ClientError as error:
                    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if status == 412:
                        self._read_object(full_key, expected_sha256, size)
                    else:
                        raise R2Unavailable("R2不可用：新对象写入失败") from None
                except Exception:  # noqa: BLE001 - 外部边界统一脱敏
                    raise R2Unavailable("R2不可用：新对象写入失败") from None
        except OSError:
            raise R2Unavailable("R2上传文件的本地读取或临时保存失败") from None
        return full_key

    def _response_size(
        self, response: dict, expected_sha256: str, expected_size: int | None
    ) -> int:
        size = response.get("ContentLength")
        metadata = response.get("Metadata")
        details = response.get("ResponseMetadata", {})
        if not isinstance(details, dict):
            raise R2Conflict("R2对象响应头格式无效")
        headers = details.get("HTTPHeaders", {})
        if not isinstance(details, dict) or not isinstance(headers, dict):
            raise R2Conflict("R2对象响应头格式无效")
        headers = {str(name).lower(): value for name, value in headers.items()}
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > self.max_object_bytes
            or (expected_size is not None and size != expected_size)
        ):
            raise R2Conflict("R2对象长度与登记或字节预算不一致")
        if not isinstance(metadata, dict) or metadata.get("sha256") != expected_sha256:
            raise R2Conflict("R2对象指纹标签与登记不一致")
        if details.get("HTTPStatusCode", 200) != 200:
            raise R2Conflict("R2对象不是完整的成功响应")
        if response.get("ContentRange") or headers.get("content-range"):
            raise R2Conflict("R2对象不能是部分内容")
        if response.get("ContentEncoding", "identity") not in {"", "identity"} or headers.get(
            "content-encoding", "identity"
        ) not in {"", "identity"}:
            raise R2Conflict("R2对象存在未允许的内容编码")
        header_length = headers.get("content-length")
        if header_length is not None and (
            not isinstance(header_length, str)
            or not re.fullmatch(r"[0-9]+", header_length)
            or int(header_length) != size
        ):
            raise R2Conflict("R2对象响应头长度与登记不一致")
        return size

    def _read_object(
        self,
        full_key: str,
        expected_sha256: str,
        expected_size: int | None,
        sink: BinaryIO | None = None,
    ) -> None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=full_key)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = error.response.get("Error", {}).get("Code")
            if status == 404 or code in {"NoSuchKey", "NotFound", "404"}:
                raise R2NotFound("R2对象不存在") from None
            raise R2Unavailable("R2不可用：对象读取失败") from None
        except Exception:  # noqa: BLE001 - 外部提供方的未知异常必须统一脱敏
            raise R2Unavailable("R2不可用：对象读取失败") from None
        if not isinstance(response, dict):
            raise R2Conflict("R2对象响应格式无效")
        body = response.get("Body")
        failed = False
        try:
            if (
                body is None
                or not callable(getattr(body, "read", None))
                or not callable(getattr(body, "close", None))
            ):
                raise R2Conflict("R2对象缺少可读取的响应体")
            size = self._response_size(response, expected_sha256, expected_size)
            digest, actual_size = self._hash_stream(body, sink)
            if actual_size != size or digest != expected_sha256:
                raise R2Conflict("R2对象实际字节与登记不一致")
        except IncompleteReadError:
            failed = True
            raise R2Conflict("R2对象实际长度与响应头不一致") from None
        except (BotoCoreError, OSError):
            failed = True
            raise R2Unavailable("R2不可用：对象流式读取或缓存写入失败") from None
        except (R2Conflict, R2NotFound, R2Unavailable):
            failed = True
            raise
        except Exception:  # noqa: BLE001 - 未知流异常不能传播上游响应或凭据
            failed = True
            raise R2Unavailable("R2不可用：对象响应处理失败") from None
        finally:
            # 即使元数据早拒绝或中途读失败，也必须关闭远端流。
            if body is not None and callable(getattr(body, "close", None)):
                try:
                    body.close()
                except Exception:  # noqa: BLE001 - 外部边界统一脱敏
                    if not failed:
                        raise R2Unavailable("R2对象响应流无法正常关闭") from None

    def _existing_matches(
        self, path: Path, expected_sha256: str, expected_size: int | None
    ) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise R2Conflict("R2缓存目标无法安全检查") from None
        try:
            with _regular_file(path) as stream:
                digest, size = self._hash_stream(stream)
        except ValueError:
            raise R2Conflict("R2缓存目标不是可安全读取的普通文件") from None
        except OSError:
            raise R2Unavailable("R2已有缓存读取失败") from None
        if digest != expected_sha256 or (expected_size is not None and size != expected_size):
            raise R2Conflict("R2缓存目标已有不同内容，禁止覆盖")
        return True

    @staticmethod
    def _safe_parents(path: Path) -> None:
        # 已存在的缓存父目录不得跳转到其他目录，Windows目录联接也同样拒绝。
        try:
            if any(
                parent.is_symlink() or getattr(parent, "is_junction", lambda: False)()
                for parent in path.parents
            ):
                raise R2Conflict("R2缓存父目录不能包含符号链接或目录联接")
        except OSError:
            raise R2Conflict("R2缓存父目录无法安全检查") from None

    def download_file(
        self,
        key: str,
        destination: Path | str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
    ) -> Path:
        """核验完整内容后排他发布；已有同字节文件直接复用，不覆盖旧文件。"""
        full_key = self._key(key)
        expected_sha256 = _expected_hash(expected_sha256)
        self._size(expected_size)
        path = Path(destination)
        self._safe_parents(path)
        if self._existing_matches(path, expected_sha256, expected_size):
            return path
        owned_temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._safe_parents(path)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.r2-", suffix=".tmp", dir=path.parent
            )
            owned_temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary:
                self._read_object(full_key, expected_sha256, expected_size, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                # 同目录硬链接是原子、排他发布；没有替换旧目标的回退路径。
                self._safe_parents(path)
                os.link(owned_temporary, path)
            except FileExistsError:
                if not self._existing_matches(path, expected_sha256, expected_size):
                    raise R2Conflict("R2缓存目标在排他发布期间发生变化") from None
        except OSError:
            raise R2Unavailable("R2对象无法安全保存到本机缓存") from None
        finally:
            if owned_temporary is not None:
                try:
                    owned_temporary.unlink(missing_ok=True)
                except OSError:
                    raise R2Unavailable("R2专属临时缓存清理失败") from None
        return path
