"""统一源切面隔离候选；只有合成范围，没有云端授权/迁移/激活入口。"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from uuid import UUID

from sqlalchemy import inspect

from archspec.persistence import (
    asset_capture,
    budget,
    control_image,
    evidence_manifest,
    legacy_budget_source,
)
from archspec.persistence.source_write_gate import SourceWriteGate

VERSION = "SYNTHETIC_UNIFIED_CUT"
MAX_EXTERNAL_BYTES = 8 * 1024 * 1024
MAX_EXTERNAL_FILES = 512
REQUIRED_WRITERS = frozenset({
    "HTTP_API", "ACQUISITION_WORKER", "QA_TASKS", "MODEL_AGENT", "MODEL_QA",
    "MODEL_BUDGET", "GOVERNANCE", "CATALOG", "REGISTRY", "METADATA_FIRST_READ",
    "QUEUE_SUBMIT_RETRY", "ASSET_WRITES", "PROJECT_DOCUMENTS", "STARTUP_SEED_SCHEMA",
    "INDEPENDENT_CLI",
})
COMPONENTS = frozenset({"assets", "catalog", "governance", "qa_tasks", "budget", "acquisition"})
_SAFE = "统一源切面尚未完整核验，源保持当前停写状态，未激活或发送模型"


class SourceCutUnavailable(RuntimeError):
    """任何覆盖、来源闭合、字节、事务出口未知都拒绝签收。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _unique_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise SourceCutUnavailable(_SAFE)
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique, parse_constant=lambda _: _invalid())


def _invalid():
    raise SourceCutUnavailable(_SAFE)


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


@dataclass(frozen=True)
class SourceRegistration:
    source_service_id: str
    source_instance_id: str
    authorization_scope_sha256: str
    writer_inventory_version: str
    writer_evidence: tuple
    main_table_names: tuple
    budget_table_names: tuple
    authorized_project_ids: tuple = field(repr=False)
    source_scope: str = VERSION
    budget_migration_id: str | None = None
    asset_manifest_id: str | None = None


@dataclass(frozen=True)
class ExternalState:
    files: tuple = field(repr=False)
    runtime_json: str = field(repr=False)
    assets: asset_capture.AssetCapture | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _CoverageProof:
    issuer: object = field(repr=False)


class CoverageScope:
    """给可信服务器validator读固定登记与实际writer；不接受客户端布尔证明。"""
    __slots__ = ("__gate", "__registration", "__writers", "_proof")

    def __init__(self, registration, writers, gate):
        self.__registration, self.__writers, self.__gate = registration, writers, gate
        self._proof = None

    @property
    def registration(self):
        return self.__registration

    @property
    def writers(self):
        return self.__writers

    @property
    def gate(self):
        return self.__gate

    def confirm(self):
        if self._proof is not None:
            raise SourceCutUnavailable(_SAFE)
        self._proof = _CoverageProof(self)
        return self._proof


@dataclass(frozen=True)
class _ClosureProof:
    issuer: object = field(repr=False)
    closure_json: str = field(repr=False)


class ClosureContext:
    """回调消费本轮实际捕获值；不暴露SQL连接、commit、文件路径或外部客户端。"""
    __slots__ = ("__budget_image", "__external", "__legacy_budget", "__main_image", "__registration", "_proof")

    def __init__(self, registration, main_image, budget_image, external, legacy_budget=None):
        self.__registration, self.__main_image = registration, main_image
        self.__budget_image, self.__external = budget_image, external
        self.__legacy_budget = legacy_budget
        self._proof = None

    @property
    def registration(self):
        return self.__registration

    @property
    def main_image(self):
        return self.__main_image

    @property
    def budget_image(self):
        return self.__budget_image

    @property
    def external(self):
        return self.__external

    @property
    def legacy_budget(self):
        return self.__legacy_budget

    def confirm(self, *, closure_json):
        if self._proof is not None or not isinstance(closure_json, str):
            raise SourceCutUnavailable(_SAFE)
        if len(closure_json.encode("utf-8")) > 256 * 1024:
            raise SourceCutUnavailable(_SAFE)
        value = _unique_json(closure_json)
        if (not isinstance(value, dict) or value.get("version") != "SYNTHETIC_COMPLETE_CLOSURE_V1"
                or not isinstance(value.get("components"), list)
                or len(value["components"]) != len(COMPONENTS)
                or set(value["components"]) != COMPONENTS):
            raise SourceCutUnavailable(_SAFE)
        self._proof = _ClosureProof(self, _json(value))
        return self._proof


@dataclass(frozen=True)
class SourceCut:
    source_scope: str
    registration: SourceRegistration = field(repr=False)
    write_epoch: str
    freeze_nonce: str = field(repr=False)
    main_image: object = field(repr=False)
    budget_image: object = field(repr=False)
    external: ExternalState = field(repr=False)
    closure_json: str = field(repr=False)
    source_cut_sha256: str
    legacy_budget: object = field(default=None, repr=False)


class SourceCutCoordinator:
    """单一短切面：同门+完整writer证明+同时两库写锁+真实外部字节+一次全图核验。"""

    def __init__(self, main_engine, budget_engine, main_tables, budget_tables, *,
                 gate, registration, writer_bindings, verify_writer_coverage,
                 capture_external, verify_complete_closure,
                 max_rows=50_000, max_bytes=8 * 1024 * 1024):
        if not all(callable(value) for value in
                   (verify_writer_coverage, capture_external, verify_complete_closure)):
            raise SourceCutUnavailable(_SAFE)
        if (not isinstance(gate, SourceWriteGate) or type(registration) is not SourceRegistration
                or registration.source_scope != VERSION or not registration.source_service_id
                or not _uuid(registration.source_instance_id)
                or not _digest(registration.authorization_scope_sha256)
                or not registration.writer_inventory_version
                or type(max_rows) is not int or not 1 <= max_rows <= 50_000
                or type(max_bytes) is not int or not 1 <= max_bytes <= 8 * 1024 * 1024):
            raise SourceCutUnavailable(_SAFE)
        if (main_engine is budget_engine or main_engine.dialect.name != "sqlite"
                or budget_engine.dialect.name != "sqlite"
                or Path(main_engine.url.database).resolve() == Path(budget_engine.url.database).resolve()):
            raise SourceCutUnavailable(_SAFE)
        # 原型覆盖两种合成SQLite账本；真实源登记、PG/跨实例和默认接线仍需另外验收。
        self.main_engine, self.budget_engine = main_engine, budget_engine
        self.main_tables, self.budget_tables = tuple(main_tables), tuple(budget_tables)
        self.gate, self.registration, self.writers = gate, registration, tuple(writer_bindings)
        self.verify_writer_coverage = verify_writer_coverage
        self.capture_external, self.verify_complete_closure = capture_external, verify_complete_closure
        self.images = control_image  # 固定服务器包组件，不从用户路径或data加载。
        self.max_rows, self.max_bytes = max_rows, max_bytes
        if (len(self.main_tables) + len(self.budget_tables) > 64
                or tuple(sorted(table.name for table in self.main_tables)) != registration.main_table_names
                or tuple(sorted(table.name for table in self.budget_tables)) != registration.budget_table_names
                or {table.name for table in self.budget_tables} not in (
                    {budget._BUDGET.name, budget._CALLS.name, budget._MIGRATIONS.name},
                    set(legacy_budget_source._NAMES))):
            raise SourceCutUnavailable(_SAFE)
        if registration.asset_manifest_id is not None and not _digest(registration.asset_manifest_id):
            raise SourceCutUnavailable(_SAFE)
        self._legacy_source = registration.budget_table_names == legacy_budget_source._NAMES
        migration = registration.budget_migration_id
        if (self._legacy_source and (not isinstance(migration, str)
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", migration) is None)
                or not self._legacy_source and migration is not None):
            # 格式由固定服务器登记决定，不根据缺表/空行回退或安装新账本。
            raise SourceCutUnavailable(_SAFE)

    def _coverage(self):
        expected = dict(self.registration.writer_evidence)
        names = [writer.entrypoint_id for writer in self.writers]
        if (len(expected) != len(self.registration.writer_evidence)
                or set(expected) != REQUIRED_WRITERS or len(names) != len(REQUIRED_WRITERS)
                or set(names) != REQUIRED_WRITERS
                or any(not _digest(expected[name]) for name in expected)
                or any(writer.gate is not self.gate or writer.evidence != expected[writer.entrypoint_id]
                       for writer in self.writers)):
            raise SourceCutUnavailable(_SAFE)
        # 名称、同门与hash只是前置；必须由真实组件覆盖validator核已证据化实现。
        scope = CoverageScope(self.registration, self.writers, self.gate)
        proof = self.verify_writer_coverage(scope)
        if proof is not scope._proof or type(proof) is not _CoverageProof or proof.issuer is not scope:
            raise SourceCutUnavailable(_SAFE)

    def _external(self, receipt):
        value = self.capture_external()
        if (type(value) is not ExternalState or not isinstance(value.files, tuple)
                or not 1 <= len(value.files) <= MAX_EXTERNAL_FILES
                or not isinstance(value.runtime_json, str)):
            raise SourceCutUnavailable(_SAFE)
        assets = value.assets
        if self.registration.asset_manifest_id is None:
            if assets is not None:
                raise SourceCutUnavailable(_SAFE)
        elif (type(assets) is not asset_capture.AssetCapture
                or assets.manifest_id != self.registration.asset_manifest_id):
            raise SourceCutUnavailable(_SAFE)
        if assets is not None:
            # 大原件逐块核对，不拼进控制JSON的8MiB内存上限。
            assets.verify(gate=self.gate, freeze_receipt=receipt)
        total = len(value.runtime_json.encode("utf-8"))
        seen = set()
        for pair in value.files:
            if (not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], str)
                    or type(pair[1]) is not bytes):
                raise SourceCutUnavailable(_SAFE)
            name, content = pair
            parts = PurePosixPath(name)
            if ("\\" in name or parts.is_absolute() or any(p in {"..", "."} for p in parts.parts)
                    or str(parts) != name or name in seen or len(name) > 300):
                raise SourceCutUnavailable(_SAFE)
            if not (name in {"sources.json", "asset-manifest.json"}
                    or re.fullmatch(r"(?:jobs|batches)/[a-f0-9-]{36}\.json", name)
                    or assets is None and evidence_manifest._PATH.fullmatch(name)):
                raise SourceCutUnavailable(_SAFE)
            seen.add(name)
            total += len(content) + len(name.encode("utf-8"))
            if total > MAX_EXTERNAL_BYTES:
                raise SourceCutUnavailable(_SAFE)
        if "sources.json" not in seen or "asset-manifest.json" not in seen:
            raise SourceCutUnavailable(_SAFE)
        if assets is not None:
            manifest = _unique_json(dict(value.files)["asset-manifest.json"].decode("utf-8"))
            if manifest != assets.manifest:
                raise SourceCutUnavailable(_SAFE)
        _unique_json(value.runtime_json)
        # bytes与tuple冻结实际输入，回调后再以原字节逐项比较，不以摘要替代闭合。
        return ExternalState(tuple(sorted(value.files)), value.runtime_json, assets)

    def _image(self, connection, tables, table_names):
        # 完整服务端登记也固定物理表库存；漏表不自动create_all或替换为空。
        if tuple(sorted(inspect(connection).get_table_names())) != table_names:
            raise SourceCutUnavailable(_SAFE)
        return self.images.capture_control_image(
            connection, tables, max_rows=self.max_rows, max_bytes=self.max_bytes)

    def _capture_budget(self, connection):
        image = self._image(connection, self.budget_tables, self.registration.budget_table_names)
        if self._legacy_source:
            candidate = legacy_budget_source.capture_on_connection(
                connection, migration_id=self.registration.budget_migration_id,
                max_rows=self.max_rows, max_bytes=self.max_bytes)
            if candidate.source_image != image or candidate.activation_blockers:
                # 旧原始历史与严格映像必须对应；未知预留保留原样，不猜关联或退款。
                raise SourceCutUnavailable(_SAFE)
            return image, candidate
        _, calls = budget._state(connection)
        if any(row["status"] == "RESERVED" for row in calls):
            raise SourceCutUnavailable(_SAFE)
        return image, None

    def publish_cloud(self, freeze_receipt, target_engine, **options):
        """同一切面接资产/向量完整写读核验及SQL发布；完成后源仍冻结待网站切换。"""
        from archspec.persistence.migration_flow import publish_cut
        cut = self.capture(freeze_receipt)
        return publish_cut(cut, target_engine, self.main_tables, self.budget_tables, **options)

    def import_controls(self, freeze_receipt, target_engine, *, migration_id):
        """沿同一冻结切面迁入全部控制记录；源保持冻结，尚不发布资产/向量代次。"""
        from archspec.persistence.control_migration import import_cut_controls
        cut = self.capture(freeze_receipt)
        return import_cut_controls(cut, target_engine, self.main_tables, self.budget_tables,
                                   migration_id=migration_id)

    def capture(self, freeze_receipt):
        try:
            with self.gate.frozen_section(freeze_receipt), ExitStack() as stack:
                self._coverage()
                # 固定顺序；两库写锁全部取得后才允许读取任一个映像或外部库存。
                main = stack.enter_context(self.images.image_transaction(self.main_engine, write=True))
                ledger = stack.enter_context(self.images.image_transaction(self.budget_engine, write=True))
                main_image = self._image(main, self.main_tables, self.registration.main_table_names)
                budget_image, legacy_candidate = self._capture_budget(ledger)
                external = self._external(freeze_receipt)
                context = ClosureContext(
                    self.registration, main_image, budget_image, external, legacy_candidate)
                proof = self.verify_complete_closure(context)
                if (proof is not context._proof or type(proof) is not _ClosureProof
                        or proof.issuer is not context):
                    raise SourceCutUnavailable(_SAFE)
                # 所有可信回调先完成；最后逐库/逐字节复捕必须覆盖回调期间的任何变化。
                self._coverage()
                # 所有复捕仍在同门和两个实际SQL写锁内，任何在途变化使整个cut作废。
                final_main_image = self._image(main, self.main_tables, self.registration.main_table_names)
                final_budget_image, final_legacy = self._capture_budget(ledger)
                if (main_image != final_main_image
                        or budget_image != final_budget_image or legacy_candidate != final_legacy
                        or external != self._external(freeze_receipt)):
                    raise SourceCutUnavailable(_SAFE)
                envelope = {"version": VERSION, "registration": {
                    "service": self.registration.source_service_id,
                    "instance": self.registration.source_instance_id,
                    "authorization_scope": self.registration.authorization_scope_sha256,
                    "writer_inventory": self.registration.writer_inventory_version,
                    "writer_evidence": self.registration.writer_evidence,
                    "main_table_names": self.registration.main_table_names,
                    "budget_table_names": self.registration.budget_table_names,
                    "authorized_project_ids": self.registration.authorized_project_ids},
                    "freeze": [freeze_receipt.epoch, freeze_receipt.nonce],
                    "main": main_image.sha256, "budget": budget_image.sha256,
                    "external": [(name, len(content), _sha(content)) for name, content in external.files],
                    "runtime": _sha(external.runtime_json.encode("utf-8")),
                    "closure": _sha(proof.closure_json.encode("utf-8"))}
                if external.assets is not None:
                    envelope["asset_manifest"] = external.assets.manifest_id
                if legacy_candidate is not None:
                    envelope["legacy_budget"] = {
                        "migration_id": legacy_candidate.migration_id,
                        "manifest_sha256": legacy_candidate.migration_manifest_sha256,
                        "limit_cents": legacy_candidate.limit_cents,
                        "call_count": legacy_candidate.call_count,
                        "reserved_cents": legacy_candidate.reserved_cents,
                    }
                candidate = SourceCut(VERSION, self.registration, freeze_receipt.epoch,
                    freeze_receipt.nonce, main_image, budget_image, external,
                    proof.closure_json, _sha(_json(envelope).encode("utf-8")), legacy_candidate)
            # 两个SQL出口均完成之后才给候选；未知提交/清理失败不返回成功。
            return candidate
        except SourceCutUnavailable:
            raise
        except Exception:  # noqa: BLE001 - SQL/JSON/callback可能含私密原值，统一安全失败
            raise SourceCutUnavailable(_SAFE) from None
