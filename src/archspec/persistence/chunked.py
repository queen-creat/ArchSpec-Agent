"""固定二进制资产的不可变分块适配；不裁页、不激活目录或控制状态。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .r2 import R2Store, _expected_hash, _regular_file, _safe_name

SCHEMA = "CHUNKED_ASSET_V1"
MAX_ASSET_BYTES = 128 * 1024 * 1024
MAX_PART_BYTES = 4 * 1024 * 1024
MAX_PARTS = 128
MAX_DESCRIPTOR_BYTES = 256 * 1024
_READ_BYTES = 1024 * 1024


class ChunkedConflict(ValueError):
    """本机或远端身份不一致；不允许覆盖已有字节。"""


class ChunkedUnavailable(RuntimeError):
    """存储或本机暂存不可用；消息不携带凭据及上游内容。"""


class ChunkedNotFound(LookupError):
    """固定说明或分块不存在；不能当成已迁移或已恢复。"""


class RawObjectStore(Protocol):
    """底层对象存储自行处理前缀、不可覆盖写入及连接生命周期。"""

    def put_file(
        self, key: str, path: Path | str, expected_sha256: str, *,
        content_type: str = "application/octet-stream",
    ) -> str: ...

    def download_file(
        self, key: str, destination: Path | str, expected_sha256: str, *,
        expected_size: int | None = None,
    ) -> Path: ...

    def read_bytes(self, key: str, *, max_bytes: int = MAX_DESCRIPTOR_BYTES) -> bytes: ...


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ChunkedConflict("分块资产的字节数或数量超出合同")
    return value


def _content_type(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 256
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ChunkedConflict("分块资产的内容类型无效")
    return value


def _logical_key(value: str) -> str:
    key = _safe_name(value)
    # 原始块与逻辑说明使用不同键域，禁止把描述符递归当原件读取。
    if key == "_parts" or key.startswith("_parts/"):
        raise ChunkedConflict("原始分块键不能用作逻辑资产键")
    return key


def _local_path(value: Path | str) -> Path:
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ChunkedConflict("资产暂存路径不能包含回退目录")
    R2Store._safe_parents(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ChunkedConflict("资产路径不能是符号链接或目录联接")
    return path


def _absent(path: Path) -> None:
    _local_path(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ChunkedUnavailable("资产目标无法安全检查") from None
    raise ChunkedConflict("资产目标已存在，禁止覆盖旧文件")


def _raw_call(function, *args, **kwargs):
    """保留可处理的故障类别，但不向上泄露供应商响应或合成秘密。"""
    try:
        return function(*args, **kwargs)
    except ValueError:
        raise ChunkedConflict("原始对象的身份或不可覆盖合同无法核对") from None
    except LookupError:
        raise ChunkedNotFound("固定资产说明或原始分块不存在") from None
    except Exception:  # noqa: BLE001 - 外部边界只允许固定安全错误
        raise ChunkedUnavailable("原始对象存储不可用，本次资产操作未完成") from None


class _OwnedWorkspace(AbstractContextManager):
    """只清理本次排他创建的平面目录和明确登记文件，不递归删除旧记录。"""

    def __init__(self, staging_dir: Path):
        _local_path(staging_dir)
        self.root = Path(tempfile.mkdtemp(prefix=".chunked-", dir=staging_dir))
        self.staging_dir = staging_dir
        self.identity = self.root.lstat()
        self.paths: set[Path] = set()

    def file(self, name: str) -> Path:
        path = self.root / name
        _local_path(path)
        self.paths.add(path)
        return path

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            _local_path(self.root)
            actual = self.root.lstat()
            if (
                not stat.S_ISDIR(actual.st_mode)
                or (actual.st_dev, actual.st_ino) != (self.identity.st_dev, self.identity.st_ino)
                or self.root.parent != self.staging_dir
                or not self.root.resolve().is_relative_to(self.staging_dir.resolve())
            ):
                raise ChunkedUnavailable("专属资产暂存目录身份改变，未执行清理")
            for path in self.paths:
                # 只删除登记在本目录内的叶文件；外来目录和其他旧记录均不会被递归删除。
                if path.parent != self.root or not path.absolute().is_relative_to(self.root):
                    raise ChunkedUnavailable("资产暂存文件超出本次清理范围")
                R2Store._safe_parents(path)
                path.unlink(missing_ok=True)
            self.root.rmdir()
        except FileNotFoundError:
            # 本次目录已经不存在时，没有需要继续删除的其他目标。
            if self.root.exists() or self.root.is_symlink():
                raise ChunkedUnavailable("资产暂存目录无法完整清理") from None
        except (OSError, ValueError):
            raise ChunkedUnavailable("资产专属暂存无法安全清理") from None
        return False


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ChunkedConflict("分块说明包含重复字段")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ChunkedConflict("分块说明不能包含非有限数字")


def _descriptor(data: bytes, expected_sha256: str, expected_size: int | None) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_DESCRIPTOR_BYTES:
        raise ChunkedConflict("分块说明不是预算内的原始字节")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ChunkedConflict("分块说明不是完整有效的JSON") from None
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "sha256", "size_bytes", "content_type", "part_size", "parts"
        }
        or value["schema_version"] != SCHEMA
    ):
        raise ChunkedConflict("分块说明的版本或字段无效")
    digest = _expected_hash(value["sha256"])
    size = _integer(value["size_bytes"], 0, MAX_ASSET_BYTES)
    part_size = _integer(value["part_size"], 1, MAX_PART_BYTES)
    _content_type(value["content_type"])
    parts = value["parts"]
    if (
        digest != expected_sha256 or (expected_size is not None and size != expected_size)
        or not isinstance(parts, list) or len(parts) > MAX_PARTS
        or (size == 0) != (len(parts) == 0)
    ):
        raise ChunkedConflict("分块说明与完整资产的登记身份不一致")
    total = 0
    for index, part in enumerate(parts):
        if (
            not isinstance(part, dict) or set(part) != {"index", "key", "sha256", "size_bytes"}
            or type(part["index"]) is not int or part["index"] != index
        ):
            raise ChunkedConflict("分块的字段或原始顺序无效")
        part_digest = _expected_hash(part["sha256"])
        length = _integer(part["size_bytes"], 1, part_size)
        if (
            part["key"] != "_parts/" + part_digest
            or (index < len(parts) - 1 and length != part_size)
        ):
            raise ChunkedConflict("分块不是绑定指纹的内容地址或长度不一致")
        total += length
    if total != size:
        raise ChunkedConflict("分块长度之和与完整原件不一致")
    return value


def _verified_part(path: Path, part: dict, complete_digest=None, sink=None) -> int:
    """同一核验入口供上传前暂存复查和下载原件还原使用。"""
    _local_path(path)
    digest = hashlib.sha256()
    size = 0
    with _regular_file(path) as stream:
        if os.fstat(stream.fileno()).st_size != part["size_bytes"]:
            raise ChunkedConflict("本机分块长度与登记不一致")
        while block := stream.read(_READ_BYTES):
            size += len(block)
            if size > part["size_bytes"]:
                raise ChunkedConflict("落盘分块超过登记长度")
            digest.update(block)
            if complete_digest is not None:
                complete_digest.update(block)
            if sink is not None:
                sink.write(block)
    if size != part["size_bytes"] or digest.hexdigest() != part["sha256"]:
        raise ChunkedConflict("落盘分块实际字节与登记不一致")
    return size


class ChunkedObjectStore:
    """包裹不可覆盖原始仓库；传入仓库的关闭及健康检查仍由调用方负责。"""

    def __init__(
        self, store: RawObjectStore, staging_dir: Path | str, *,
        part_size: int = MAX_PART_BYTES,
    ):
        self.part_size = _integer(part_size, 1, MAX_PART_BYTES)
        for name in ("put_file", "download_file", "read_bytes"):
            if not callable(getattr(store, name, None)):
                raise ChunkedConflict("原始对象仓库未提供完整固定资产接口")
        self.store = store
        self.staging_dir = _local_path(staging_dir)
        try:
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            _local_path(self.staging_dir)
            if not self.staging_dir.is_dir():
                raise ChunkedConflict("资产暂存根必须是普通目录")
        except OSError:
            raise ChunkedUnavailable("资产暂存根无法安全创建") from None

    def put_file(
        self, key: str, path: Path | str, expected_sha256: str, *,
        content_type: str = "application/octet-stream",
    ) -> str:
        """全文件与本机分块先核齐；块写入都成功后才最后写逻辑说明。"""
        key = _logical_key(key)
        expected_sha256 = _expected_hash(expected_sha256)
        content_type = _content_type(content_type)
        source_path = _local_path(path)
        try:
            with _regular_file(source_path) as source, _OwnedWorkspace(self.staging_dir) as work:
                _integer(os.fstat(source.fileno()).st_size, 0, MAX_ASSET_BYTES)
                digest = hashlib.sha256()
                total = 0
                parts = []
                paths = []
                while piece := source.read(self.part_size):
                    total += len(piece)
                    if total > MAX_ASSET_BYTES or len(parts) >= MAX_PARTS:
                        raise ChunkedConflict("完整原件或分块数量超过预算")
                    digest.update(piece)
                    piece_digest = hashlib.sha256(piece).hexdigest()
                    index = len(parts)
                    piece_path = work.file(f"upload-{index:03d}.bin")
                    with piece_path.open("xb") as sink:
                        sink.write(piece)
                        sink.flush()
                        os.fsync(sink.fileno())
                    paths.append(piece_path)
                    parts.append({"index": index, "key": "_parts/" + piece_digest,
                                  "sha256": piece_digest, "size_bytes": len(piece)})
                if digest.hexdigest() != expected_sha256:
                    raise ChunkedConflict("完整原件SHA256与登记不一致，未发送任何字节")
                # 完整切分后再次逐份核暂存字节；任何坏块都在第一次远端写之前拒绝。
                staged_digest = hashlib.sha256()
                staged_size = sum(
                    _verified_part(piece_path, part, staged_digest)
                    for part, piece_path in zip(parts, paths, strict=True)
                )
                if staged_size != total or staged_digest.hexdigest() != expected_sha256:
                    raise ChunkedConflict("本机完整分块暂存与登记不一致，未发送任何字节")
                description = {
                    "schema_version": SCHEMA, "sha256": expected_sha256,
                    "size_bytes": total, "content_type": content_type,
                    "part_size": self.part_size, "parts": parts,
                }
                encoded = json.dumps(
                    description, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                _descriptor(encoded, expected_sha256, total)
                description_path = work.file("descriptor.json")
                description_path.write_bytes(encoded)
                # 返回值是底层完整键；后续请求仍只使用相对键，不能二次加前缀。
                for part, piece_path in zip(parts, paths, strict=True):
                    _raw_call(self.store.put_file, part["key"], piece_path, part["sha256"])
                return _raw_call(
                    self.store.put_file, key, description_path, hashlib.sha256(encoded).hexdigest(),
                    content_type="application/json",
                )
        except OSError:
            raise ChunkedUnavailable("完整原件的本机读取或专属暂存失败") from None

    def download_file(
        self, key: str, destination: Path | str, expected_sha256: str, *,
        expected_size: int | None = None,
    ) -> Path:
        """逐块重核并完整还原原件，全部SHA通过后排他发布到新本机路径。"""
        key = _logical_key(key)
        expected_sha256 = _expected_hash(expected_sha256)
        if expected_size is not None:
            _integer(expected_size, 0, MAX_ASSET_BYTES)
        destination_path = _local_path(destination)
        _absent(destination_path)
        description = _descriptor(
            _raw_call(self.store.read_bytes, key, max_bytes=MAX_DESCRIPTOR_BYTES),
            expected_sha256, expected_size,
        )
        try:
            with _OwnedWorkspace(self.staging_dir) as work:
                assembled = work.file("assembled.bin")
                complete_digest = hashlib.sha256()
                total = 0
                with assembled.open("xb") as sink:
                    for part in description["parts"]:
                        piece_path = work.file(f"download-{part['index']:03d}.bin")
                        _raw_call(
                            self.store.download_file, part["key"], piece_path, part["sha256"],
                            expected_size=part["size_bytes"],
                        )
                        total += _verified_part(piece_path, part, complete_digest, sink)
                    sink.flush()
                    os.fsync(sink.fileno())
                if (
                    total != description["size_bytes"]
                    or complete_digest.hexdigest() != expected_sha256
                ):
                    raise ChunkedConflict("完整还原资产的SHA256或长度与原件不一致")
                # 发布依据是实际落盘完整字节，而非仅写入输入的累计指纹。
                _verified_part(assembled, description)
                _absent(destination_path)
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                _absent(destination_path)
                try:
                    # 硬链接是排他、原子发布；跨文件系统失败时绝不回退为覆盖复制。
                    os.link(assembled, destination_path)
                except FileExistsError:
                    raise ChunkedConflict("资产目标在发布期间已存在，禁止覆盖") from None
        except OSError:
            raise ChunkedUnavailable("完整资产无法安全还原到本机新路径") from None
        return Path(destination)
