"""固定原件流式捕获；只复制明确选定资产，不激活、联网或覆盖旧文件。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from archspec.persistence import evidence_manifest
from archspec.persistence.source_write_gate import SourceWriteGate

_BLOCK = 256 * 1024
_ISSUER = object()
_SAFE = "固定资产捕获无法完整核验，未覆盖源文件或激活候选"


class AssetCaptureUnavailable(RuntimeError):
    """身份、冻结或完整字节未知，保留现场并拒绝签收。"""


def _stream_contents(source, entry, sink=None):
    """每次最多256KiB；大小和完整SHA均核验，不用mtime代替内容。"""
    digest = hashlib.sha256()
    total = 0
    if os.fstat(source.fileno()).st_size != entry["size_bytes"]:
        raise AssetCaptureUnavailable(_SAFE)
    while chunk := source.read(_BLOCK):
        total += len(chunk)
        if total > entry["size_bytes"]:
            raise AssetCaptureUnavailable(_SAFE)
        digest.update(chunk)
        if sink is not None and sink.write(chunk) != len(chunk):
            raise AssetCaptureUnavailable(_SAFE)
    if (os.fstat(source.fileno()).st_size != entry["size_bytes"]
            or total != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]):
        raise AssetCaptureUnavailable(_SAFE)
    return total


def _stream(path, entry, sink=None):
    with path.open("rb") as source:
        return _stream_contents(source, entry, sink)


@dataclass(frozen=True, init=False)
class AssetCapture:
    """候选原件与源冻结的内部绑定；路径不进入repr、控制JSON或网页。"""
    _source_root: Path = field(repr=False)
    _captured_root: Path = field(repr=False)
    _manifest_json: str = field(repr=False)
    _manifest_id: str
    _gate: object = field(repr=False)
    _receipt: object = field(repr=False)
    _scope: tuple = field(repr=False)

    def __init__(self, issuer, source_root, captured_root, manifest_json, gate, receipt, *, manifest):
        if issuer is not _ISSUER:
            raise AssetCaptureUnavailable(_SAFE)
        # 只消费capture_assets在同一冻结窗口完整验证的清单；不接受外部dict作缓存。
        entries = tuple(MappingProxyType(dict(row)) for row in manifest["entries"])
        index = MappingProxyType({row["path"]: row for row in entries})
        identifier = manifest["manifest_id"]
        scope = (_ISSUER, source_root, captured_root, manifest_json, identifier, gate, receipt,
                 manifest["schema_version"], entries, index)
        for name, value in (
            ("_source_root", source_root), ("_captured_root", captured_root),
            ("_manifest_json", manifest_json), ("_gate", gate), ("_receipt", receipt),
            ("_manifest_id", identifier), ("_scope", scope),
        ):
            object.__setattr__(self, name, value)

    @property
    def manifest(self):
        """返回独立副本，不暴露可修改的固定登记。"""
        return json.loads(self._manifest_json)

    @property
    def manifest_id(self):
        return self._manifest_id

    def _selected(self):
        """签发证明绑定不可变字节和原门；字段被替换时不沿用旧索引。"""
        scope = self._scope
        if (not isinstance(scope, tuple) or len(scope) != 10 or scope[0] is not _ISSUER
                or any(current is not original for current, original in zip(
                    (self._source_root, self._captured_root, self._manifest_json, self._manifest_id,
                     self._gate, self._receipt), scope[1:7], strict=True))
                or not self._gate.validate_frozen(self._receipt)):
            raise AssetCaptureUnavailable(_SAFE)
        return scope[7], scope[8], scope[9]

    def verify(self, *, gate, freeze_receipt):
        """在原同门窗口核源与复制件完整字节；不签收真实writer覆盖。"""
        try:
            if gate is not self._gate or freeze_receipt is not self._receipt:
                raise AssetCaptureUnavailable(_SAFE)
            with gate.frozen_section(freeze_receipt):
                schema, entries, _ = self._selected()
                source = evidence_manifest._root(self._source_root)
                captured = evidence_manifest._root(self._captured_root)
                for entry in entries:
                    _stream(evidence_manifest._file(source, entry["path"], schema=schema), entry)
                    _stream(evidence_manifest._file(captured, entry["path"], schema=schema), entry)
                self._selected()
        except AssetCaptureUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 路径/内容不随异常外传
            raise AssetCaptureUnavailable(_SAFE) from None

    def copy_to(self, relative, sink):
        """只输出已登记复制件，调用方用新候选sink并在成功后发布，失败不得使用部分件。"""
        try:
            schema, _, index = self._selected()
            entry = index.get(relative)
            if entry is None or not callable(getattr(sink, "write", None)):
                raise AssetCaptureUnavailable(_SAFE)
            root = evidence_manifest._root(self._captured_root)
            with evidence_manifest._file(root, relative, schema=schema).open("rb") as source:
                # 先完整预检再输出，复用同一打开文件，防止两次open换成另一份文件。
                _stream_contents(source, entry)
                source.seek(0)
                total = _stream_contents(source, entry, sink)
                self._selected()
                return total
        except AssetCaptureUnavailable:
            raise
        except Exception:  # noqa: BLE001 - sink与文件错误可能含路径或原值
            raise AssetCaptureUnavailable(_SAFE) from None


def capture_assets(source_root, staging_root, manifest, *, gate, freeze_receipt):
    """服务器内部明确选择资产；同门冻结下流式复制，失败目录保留，不删除旧资料。"""
    try:
        if not isinstance(gate, SourceWriteGate):
            raise AssetCaptureUnavailable(_SAFE)
        with gate.frozen_section(freeze_receipt):
            # 先冻结登记副本；回调/调用方后来改字典不能改变捕获范围。
            encoded = evidence_manifest._canonical(manifest)
            if len(encoded) > 8 * 1024 * 1024:
                raise AssetCaptureUnavailable(_SAFE)
            fixed = json.loads(encoded)
            entries = evidence_manifest._check_manifest(fixed)
            source = evidence_manifest._root(source_root)
            staging = evidence_manifest._root(staging_root)
            if any((source / e["path"]).is_relative_to(staging) for e in entries):
                raise AssetCaptureUnavailable(_SAFE)
            # 所有源文件预检；不遍历整个data，不安装、重写或猜缺失文件。
            for entry in entries:
                _stream(evidence_manifest._file(source, entry["path"], schema=fixed["schema_version"]), entry)
            staging.mkdir(parents=True, exist_ok=True)
            staging = evidence_manifest._root(staging)
            captured = staging / (".source-assets-" + uuid4().hex)
            captured.mkdir()
            for entry in entries:
                destination = captured / entry["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                if any(p.is_symlink() for p in (destination, *destination.parents)):
                    raise AssetCaptureUnavailable(_SAFE)
                with destination.open("xb") as sink:
                    _stream(evidence_manifest._file(source, entry["path"], schema=fixed["schema_version"]), entry, sink)
                    sink.flush()
                    os.fsync(sink.fileno())
            candidate = AssetCapture(_ISSUER, source, captured, encoded.decode("utf-8"),
                                     gate, freeze_receipt, manifest=fixed)
            candidate.verify(gate=gate, freeze_receipt=freeze_receipt)
            return candidate
    except AssetCaptureUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 不暴露源、候选路径或文件内容
        raise AssetCaptureUnavailable(_SAFE) from None
