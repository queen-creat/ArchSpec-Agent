"""固定原页PNG及描述；准备与读取分开，旧资产不覆盖，控制状态不在此保存。"""
import hashlib
import json
import os
import re
import struct
from pathlib import Path
from uuid import uuid4

import pymupdf

_SCHEMA = "OFFICIAL_PAGE_IMAGE_V1"
_RENDERER = "pymupdf:" + pymupdf.VersionBind + ":RGB:1.5:PNG"
_PROFILE = hashlib.sha256(_RENDERER.encode("ascii")).hexdigest()
_DIGEST = r"[a-f0-9]{64}"
_SOURCE = r"(?:gb55031|official-[a-f0-9]{32})"
_MAX_BYTES = 20 * 1024 * 1024
_MAX_PIXELS = 10_000_000


def _root(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("原页目录不允许符号链接")
    return path.resolve()


def _identity(source_id, snapshot_id, document_sha256, page_number):
    if (not isinstance(source_id, str) or re.fullmatch(_SOURCE, source_id) is None
            or any(not isinstance(value, str) or re.fullmatch(_DIGEST, value) is None
                   for value in (snapshot_id, document_sha256))
            or type(page_number) is not int or not 1 <= page_number <= 10000):
        raise ValueError("固定原页身份或页码无效")
    return {"schema_version": _SCHEMA, "source_id": source_id, "snapshot_id": snapshot_id,
            "document_sha256": document_sha256, "page_number": page_number, "renderer": _RENDERER}


def _dimensions(content):
    if (not isinstance(content, bytes) or not 24 <= len(content) <= _MAX_BYTES
            or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR"):
        raise ValueError("原页必须是预算内的完整PNG")
    width, height = struct.unpack(">II", content[16:24])
    if not width or not height or width * height > _MAX_PIXELS:
        raise ValueError("原页像素超过读取预算")
    try:
        pixmap = pymupdf.Pixmap(content)
        if (pixmap.width, pixmap.height) != (width, height):
            raise ValueError("原页尺寸无法核对")
    except (RuntimeError, ValueError) as error:
        raise ValueError("原页PNG无法完整解析") from error
    return width, height

def page_image_renderer():
    """计算产物只绑定渲染器；来源和快照描述由运行期仓库重新生成。"""
    return _RENDERER


def validate_page_image(content):
    """构建与导入共用完整PNG校验，不重新渲染PDF。"""
    return _dimensions(content)


class PageImageRepository:
    """只读取明确身份的已准备原页；不下载、不临时渲染或访问其它资料。"""
    def __init__(self, cache_dir):
        self.cache_dir = _root(cache_dir)

    def _paths(self, identity):
        relative = Path("page-images") / identity["source_id"] / identity["snapshot_id"] / identity["document_sha256"] / _PROFILE / str(identity["page_number"])
        result = [self.cache_dir / relative.with_suffix(suffix) for suffix in (".png", ".json")]
        for path in result:
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("固定原页文件不能通过链接跳转")
            if not path.resolve().is_relative_to(self.cache_dir):
                raise ValueError("固定原页路径越界")
        return result

    @staticmethod
    def _publish(path, content):
        # 硬链接排他发布完整暂存文件；同身份坏文件保留，不替换或悄悄修复。
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise ValueError("已有固定原页资产不同，拒绝覆盖")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + uuid4().hex + ".part")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not path.is_file() or path.read_bytes() != content:
                    raise ValueError("并行固定原页发布存在冲突") from None
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, source_id, snapshot_id, document_sha256, page_number, content):
        identity = _identity(source_id, snapshot_id, document_sha256, page_number)
        width, height = _dimensions(content)
        record = {**identity, "width": width, "height": height,
                  "image_sha256": hashlib.sha256(content).hexdigest()}
        png, descriptor = self._paths(identity)
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        self._publish(png, content)
        self._publish(descriptor, encoded)
        return {**record, "asset_paths": ["official-cache/" + path.relative_to(self.cache_dir).as_posix()
                                          for path in (png, descriptor)]}

    def read(self, source_id, snapshot_id, document_sha256, page_number):
        identity = _identity(source_id, snapshot_id, document_sha256, page_number)
        png, descriptor = self._paths(identity)
        if not png.is_file() or not descriptor.is_file():
            raise LookupError("该固定原页尚未完整准备或恢复")
        if descriptor.stat().st_size > 4096 or png.stat().st_size > _MAX_BYTES:
            raise ValueError("固定原页资产超过读取预算")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("固定原页描述字段重复")
                result[key] = value
            return result
        record = json.loads(descriptor.read_bytes(), object_pairs_hook=unique)
        if (not isinstance(record, dict) or set(record) != set(identity) | {"width", "height", "image_sha256"}
                or any(record.get(key) != value for key, value in identity.items())
                or not isinstance(record.get("image_sha256"), str)
                or re.fullmatch(_DIGEST, record["image_sha256"]) is None):
            raise ValueError("固定原页描述与请求身份不一致")
        content = png.read_bytes()
        if hashlib.sha256(content).hexdigest() != record["image_sha256"]:
            raise ValueError("固定原页图片指纹已变化")
        if _dimensions(content) != (record["width"], record["height"]):
            raise ValueError("固定原页描述尺寸不一致")
        return content
