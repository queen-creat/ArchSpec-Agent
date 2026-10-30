"""固定本进程真正加载的处理身份，不把后来编辑的磁盘源码当作已生效代码。"""

import hashlib
import inspect
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# 只排除本改动新增的两个完整登记语句，保留其余字节与原换行。
# 这是兼容旧缓存所需：增加身份登记并未改变提取算法，不应使已完成的OCR失效。
# 任意其他注释、空白、算法代码仍参与原有文件哈希；重复登记明确报错，不能静默忽略。
_REGISTRATION_LINES = (
    b"from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip",
    b"LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)",
)


@dataclass(frozen=True)
class LoadedSourceIdentity:
    source_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class LoadedProcessorIdentity:
    parser_version: str
    ocr_version: str
    chunker_version: str


def frozen_source_identity(file_name):
    """每个依赖在自己的模块加载时调用；服务不能等到请求时重新读取该文件。"""
    lines = Path(file_name).read_bytes().splitlines(keepends=True)
    for registration in _REGISTRATION_LINES:
        if sum(line.rstrip(b"\r\n") == registration for line in lines) > 1:
            raise ValueError("处理模块重复登记加载身份，不能建立可靠处理版本")
    source = b"".join(line for line in lines
                      if line.rstrip(b"\r\n") not in _REGISTRATION_LINES)
    return LoadedSourceIdentity(source, hashlib.sha256(source).hexdigest())


def frozen_processor_identity(process, page_source, ocr_source, chunk_source, *, parser_sources=()):
    """只在模块加载末尾固定；新增提取依赖参与身份，未声明依赖的旧格式保持。"""
    parser_hash = hashlib.sha256(inspect.getsource(process).encode())
    parser_hash.update(page_source.source_bytes)
    for source in parser_sources:
        parser_hash.update(source.source_bytes)
    try:
        engine_version = version("rapidocr")
    except PackageNotFoundError:
        engine_version = "not-installed"
    return LoadedProcessorIdentity(
        parser_version=(f"official-pdf-v2:pymupdf-{version('pymupdf')}:"
                        f"parser-{parser_hash.hexdigest()}"),
        ocr_version=f"rapidocr-{engine_version}:adapter-{ocr_source.sha256}",
        chunker_version=chunk_source.sha256,
    )
