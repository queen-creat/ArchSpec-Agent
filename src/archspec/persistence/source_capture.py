"""真实部署对象的本机源包捕获；保全所有业务状态，不发布云端代次。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, inspect, select

from archspec.persistence import control_image, evidence_manifest, legacy_budget_source

_ISSUER = object()
_SAFE = "服务器源记录未完整捕获，未上传、发布或重置任何原记录"
_FOLDERS = evidence_manifest.SERVER_FOLDERS
_DATABASES = ('archspec.db', 'model-budget.sqlite3')
_SIDE_FILES = frozenset(name + suffix for name in _DATABASES for suffix in ('', '-wal', '-shm', '-journal'))


class ServerSourceCaptureUnavailable(RuntimeError):
    code = 'SERVER_SOURCE_CAPTURE_UNAVAILABLE'


@dataclass(frozen=True)
class ServerSourceCapture:
    directory: Path = field(repr=False)
    main_image: object = field(repr=False)
    budget: object = field(repr=False)
    manifest_sha256: str
    _issuer: object = field(default=None, repr=False)
    _gate: object = field(default=None, repr=False)
    _handle: object = field(default=None, repr=False)
    _receipt: object = field(default=None, repr=False)
    _worker: object = field(default=None, repr=False)
    _was_running: bool = field(default=False, repr=False)
    _runtime_json: str | None = field(default=None, repr=False)
    _source_root: Path | None = field(default=None, repr=False)
    _main_engine: object = field(default=None, repr=False)
    _publication_cut: object = field(default=None, repr=False)
    state: str = field(default='SOURCE_CAPTURED_UNPUBLISHED', init=False)

    def verify(self):
        """迁移前再次核实际复制件，不能只凭曾经生成的成功清单上传。"""
        try:
            if self._issuer is not _ISSUER:
                raise ServerSourceCaptureUnavailable(_SAFE)
            directory = evidence_manifest._root(self.directory)
            for name in ('manifest.json', 'main-control.json', 'budget-control.json', 'runtime-state.json'):
                if not evidence_manifest._root(directory / name).is_file():
                    raise ServerSourceCaptureUnavailable(_SAFE)
            content = (directory / 'manifest.json').read_bytes()
            if hashlib.sha256(content).hexdigest() != self.manifest_sha256:
                raise ServerSourceCaptureUnavailable(_SAFE)
            manifest = json.loads(content)
            if (manifest['schema_version'] != 'SERVER_SOURCE_CAPTURE_V1'
                    or manifest['main_sha256'] != self.main_image.sha256
                    or manifest['budget_sha256'] != self.budget.source_image.sha256
                    or (directory / 'main-control.json').read_bytes() != self.main_image.payload_json.encode('utf-8')
                    or (directory / 'budget-control.json').read_bytes() != self.budget.source_image.payload_json.encode('utf-8')
                    or {p.name for p in directory.iterdir()} - {'manifest.json', 'main-control.json', 'budget-control.json', 'runtime-state.json', 'files'}
                    or self._runtime_json is None
                    or (directory / 'runtime-state.json').read_bytes() != self._runtime_json.encode('utf-8')
                    or manifest['runtime_sha256'] != hashlib.sha256(self._runtime_json.encode('utf-8')).hexdigest()
                    or _inventory(directory / 'files') != manifest['files']):
                raise ServerSourceCaptureUnavailable(_SAFE)
            return self.manifest_sha256
        except Exception:  # noqa: BLE001 - 复制件和源包原值不回显
            raise ServerSourceCaptureUnavailable(_SAFE) from None

    def resume_source(self):
        """仅原进程持有的冻结回执可恢复；包内字符串不能解锁其它暂停。"""
        try:
            if (self._issuer is not _ISSUER or self._handle is None
                    or not self._gate.validate_frozen(self._receipt)):
                raise ServerSourceCaptureUnavailable(_SAFE)
            self._gate.resume(self._handle)
            object.__setattr__(self, '_handle', None)
            if self._was_running:
                self._worker.start()
        except Exception:  # noqa: BLE001 - 不转发工人或数据库的私密错误
            raise ServerSourceCaptureUnavailable(_SAFE) from None

    @contextmanager
    def publication_guard(self):
        """发布前和最终SQL签收期间重核原两库、文件及内存；网络不持这些锁。"""
        ledger_engine = None
        try:
            self.verify()
            if (self._handle is None or self._source_root is None or self._main_engine is None
                    or not self._gate.validate_frozen(self._receipt)):
                raise ServerSourceCaptureUnavailable(_SAFE)
            root = evidence_manifest._root(self._source_root)
            uri = 'file:' + quote((root / 'model-budget.sqlite3').as_posix(), safe='/:') + '?mode=rw'
            ledger_engine = create_engine('sqlite+pysqlite://',
                creator=lambda: sqlite3.connect(uri, uri=True, timeout=5))
            manifest = json.loads((self.directory / 'manifest.json').read_bytes())
            with self._gate.frozen_section(self._receipt), ExitStack() as stack:
                main = stack.enter_context(control_image.image_transaction(self._main_engine, write=True))
                ledger = stack.enter_context(control_image.image_transaction(ledger_engine, write=True))
                def check():
                    if (set(inspect(main).get_table_names()) != set(manifest['main_table_names'])
                            or control_image.capture_control_image(main, self._tables()) != self.main_image
                            or legacy_budget_source.capture_on_connection(ledger,
                                migration_id=self.budget.migration_id) != self.budget
                            or _inventory(root) != manifest['files']
                            or _runtime(self._worker).decode('utf-8') != self._runtime_json):
                        raise ServerSourceCaptureUnavailable(_SAFE)
                check()
                yield
                check()
        except ServerSourceCaptureUnavailable:
            raise
        except Exception:  # noqa: BLE001 - 不把原路径、SQL或行内容带到调用方
            raise ServerSourceCaptureUnavailable(_SAFE) from None
        finally:
            if ledger_engine is not None:
                ledger_engine.dispose()

    def _tables(self):
        from archspec.database_schema import registered_control_tables
        registered = registered_control_tables()
        names = json.loads((self.directory / 'manifest.json').read_bytes())['main_table_names']
        return tuple(registered[name] for name in names)

    def validate_cut(self, cut):
        """正式发布只能消费本对象刚构造的切面，不能把合成收据改一个scope。"""
        if cut is not self._publication_cut or cut.source_scope != 'SERVER_UNIFIED_CUT_V1':
            raise ServerSourceCaptureUnavailable(_SAFE)
        with self.publication_guard():
            pass

    def publish_cloud(self, target_engine, *, migration_id, generation, authorized_project_ids,
                      corpus, expected_model_fingerprint, object_store, vector_store,
                      staging_root, expected_active_sha256, archive_private_files=False):
        """原生源直接接已有控制/资产/向量发布；保留冻结，等待实际网站切换。"""
        from dataclasses import replace

        from archspec.official_documents.registry import OfficialSourceRegistry
        from archspec.persistence import asset_capture, budget, migration_flow, source_cut
        try:
            # 所有授权与历史前置检查均在目标DDL、对象和向量请求之前完成。
            with self.publication_guard():
                manifest = json.loads((self.directory / 'manifest.json').read_bytes())
                if (manifest['activation_blockers'] or type(archive_private_files) is not bool
                        or not archive_private_files
                        or not isinstance(expected_model_fingerprint, str)
                        or not re.fullmatch(r'[a-f0-9]{64}', expected_model_fingerprint)
                        or corpus.model_fingerprint != expected_model_fingerprint):
                    raise ServerSourceCaptureUnavailable(_SAFE)
                main_tables = self._tables()
                images = {entry['name']: entry for entry in self.main_image.to_dict()['tables']}
                def rows(name):
                    entry = images.get(name)
                    if entry is None:
                        return []
                    names = [c['name'] for c in entry['columns']]
                    return [dict(zip(names, map(control_image._decode, row), strict=True))
                            for row in entry['rows']]
                projects = tuple(sorted(row['id'] for row in rows('design_projects')))
                authorized = tuple(sorted(authorized_project_ids))
                if projects != authorized or len(set(authorized)) != len(authorized):
                    raise ServerSourceCaptureUnavailable(_SAFE)
                if any(row['state'] == 'RUNNING' for name in ('qa_tasks', 'qa_operations') for row in rows(name)):
                    raise ServerSourceCaptureUnavailable(_SAFE)
                if target_engine.dialect.name == 'sqlite' and (
                        target_engine.url.database is None
                        or Path(target_engine.url.database).resolve().is_relative_to(self._source_root)):
                    raise ServerSourceCaptureUnavailable(_SAFE)
                # 迁移ID只重绑定幂等身份，完整旧行、预留和限额沿用原值。
                budget_rows = {entry['name']: entry for entry in self.budget.source_image.to_dict()['tables']}
                originals = [dict(zip(legacy_budget_source._CALLS.columns.keys(),
                    map(control_image._decode, row), strict=True))
                    for row in budget_rows[legacy_budget_source._CALLS.name]['rows']]
                history = budget._history(originals, migration_id)
                legacy = replace(self.budget, migration_id=migration_id, history_json=budget._json(history),
                    migration_manifest_sha256=budget._migration_manifest(migration_id, self.budget.limit_cents, history))
                files_root = self.directory / 'files'
                staging = evidence_manifest._root(staging_root)
                if (staging.is_relative_to(self._source_root) or staging.is_relative_to(self.directory)
                        or self._source_root.is_relative_to(staging) or self.directory.is_relative_to(staging)):
                    raise ServerSourceCaptureUnavailable(_SAFE)
                registry_path = files_root / 'official-cache/sources.json'
                registry_payload = (registry_path.read_bytes() if registry_path.exists() else _bytes({'sources': []}))
                runtime = json.loads(self._runtime_json)
                if OfficialSourceRegistry(files_root / 'official-cache').list() != runtime['sources']:
                    raise ServerSourceCaptureUnavailable(_SAFE)
                external_files = [('sources.json', registry_payload),
                    ('asset-manifest.json', b'')]
                for entry in manifest['files']:
                    path = entry['path']
                    if path.startswith(('official-acquisitions/jobs/', 'official-acquisitions/batches/')):
                        if not re.fullmatch(r'official-acquisitions/(jobs|batches)/[a-f0-9-]{36}\.json', path):
                            raise ServerSourceCaptureUnavailable(_SAFE)
                        external_files.append((path.removeprefix('official-acquisitions/'), (files_root / path).read_bytes()))
                if (len(external_files) > source_cut.MAX_EXTERNAL_FILES
                        or sum(len(content) for _, content in external_files) + len(self._runtime_json.encode('utf-8'))
                        > source_cut.MAX_EXTERNAL_BYTES):
                    raise ServerSourceCaptureUnavailable(_SAFE)
            # 原SQL锁已退出；固定复制件通过同一冻结门逐块复制、回读。
            prepared = _prepare_publication_files(self, staging, manifest)
            prepared_inventory = _inventory(prepared)
            assets_manifest = evidence_manifest.build_manifest(prepared,
                [row['path'] for row in prepared_inventory], schema=evidence_manifest.SERVER_SCHEMA)
            external_files = [(name, evidence_manifest._canonical(assets_manifest)
                if name == 'asset-manifest.json' else content) for name, content in external_files]
            if sum(len(content) for _, content in external_files) + len(self._runtime_json.encode('utf-8')) > source_cut.MAX_EXTERNAL_BYTES:
                raise ServerSourceCaptureUnavailable(_SAFE)
            assets = asset_capture.capture_assets(prepared, staging / 'captured',
                assets_manifest, gate=self._gate, freeze_receipt=self._receipt)
            authorization = {'projects': authorized, 'archive_private_files': True,
                'source_manifest_sha256': self.manifest_sha256}
            registration = source_cut.SourceRegistration('archspec-server',
                manifest['source_instance_uuid'], migration_flow._sha(authorization),
                'SERVER_ACCESS_GATE_AND_SOURCE_RECHECK_V1',
                (('NATIVE_SOURCE_CAPTURE', self.manifest_sha256),),
                tuple(t.name for t in main_tables), legacy_budget_source._NAMES, authorized,
                source_scope='SERVER_UNIFIED_CUT_V1', budget_migration_id=migration_id,
                asset_manifest_id=assets.manifest_id)
            closure = {'version': 'SERVER_COMPLETE_CLOSURE_V1',
                'components': sorted(source_cut.COMPONENTS), 'source_manifest_sha256': self.manifest_sha256,
                'authorization': authorization, 'asset_manifest_id': assets.manifest_id}
            cut = source_cut.SourceCut('SERVER_UNIFIED_CUT_V1', registration,
                self._receipt.epoch, self._receipt.nonce, self.main_image, legacy.source_image,
                source_cut.ExternalState(tuple(sorted(external_files)), self._runtime_json, assets),
                source_cut._json(closure), '', legacy)
            cut = replace(cut, source_cut_sha256=migration_flow._sha(migration_flow._source_identity_body(cut)))
            object.__setattr__(self, '_publication_cut', cut)
            return migration_flow.publish_cut(cut, target_engine, main_tables, legacy_budget_source._TABLES,
                migration_id=migration_id, generation=generation, documents=self._worker.documents,
                corpus=corpus, object_store=object_store, vector_store=vector_store,
                staging_root=staging_root, expected_active_sha256=expected_active_sha256, native_source=self)
        except Exception:  # noqa: BLE001 - 不自动解除冻结；未知目标提交留待独立核验
            raise ServerSourceCaptureUnavailable('原生源迁移结果未确认；源保持冻结，请独立核验云端记录') from None


def _prepare_book_pages(source, documents, pointer):
    """仅原生冻结候选可批量准备：整本一次打开，前后完整SHA，各页即时核许可。"""
    import pymupdf

    from archspec.standard_governance.compatibility import ContentUseDenied

    source_id, snapshot_id = pointer['source_id'], pointer['snapshot_id']
    if not source._gate.validate_frozen(source._receipt):raise ServerSourceCaptureUnavailable(_SAFE)
    documents._source(source_id)
    with documents.read_phase():
        fixed = documents.cache_dir / source_id / (snapshot_id + '.json')
        record, _ = documents._load_snapshot(fixed, source_id)
        if record != pointer:raise ServerSourceCaptureUnavailable(_SAFE)
        # 初始拒绝仍只归档原件；开始准备之后的撤回必须拒绝整个发布候选。
        documents._check_read(source_id, snapshot_id, 'ORIGINAL_PAGE')
        path = documents.raw_file_path(record, source_id)
        policy = documents._snapshot_policy(record, source_id)
        before = path.stat()
        if (before.st_size != record['size_bytes'] or before.st_size > policy.max_bytes
                or documents._file_fingerprint(path) != record['sha256']):
            raise ServerSourceCaptureUnavailable(_SAFE)
        repository = documents._selected_page_image_repository()
        with pymupdf.open(path) as pdf:
            if pdf.is_repaired or pdf.is_encrypted or len(pdf) != record['page_count']:
                raise ServerSourceCaptureUnavailable(_SAFE)
            try:
                for number in range(1, record['page_count'] + 1):
                    if not source._gate.validate_frozen(source._receipt):raise ServerSourceCaptureUnavailable(_SAFE)
                    documents._check_read(source_id, snapshot_id, 'ORIGINAL_PAGE')
                    documents._snapshot_policy(record, source_id)
                    if not 1 <= number <= len(pdf):raise ServerSourceCaptureUnavailable(_SAFE)
                    page = pdf[number - 1]
                    raster = (page.rect * pymupdf.Matrix(1.5, 1.5)).irect
                    if raster.width * raster.height > 10_000_000:
                        raise ValueError('原页过大，无法安全生成预览')
                    content = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes('png')
                    repository.save(source_id, snapshot_id, record['sha256'], number, content)
                documents._check_read(source_id, snapshot_id, 'ORIGINAL_PAGE')
            except ContentUseDenied as failure:
                raise ServerSourceCaptureUnavailable(_SAFE) from failure
        # source.verify只核原捕获源；prepared本必须独立末次核完整SHA和文件身份。
        after = path.stat()
        if (documents._file_fingerprint(path) != record['sha256']
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                   != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or not source._gate.validate_frozen(source._receipt)):
            raise ServerSourceCaptureUnavailable(_SAFE)


def _prepare_publication_files(source, staging, manifest):
    """完整保留源文件，在独立候选中补齐已获准原页；不把本地即时渲染当成持久资产。"""
    from uuid import uuid4

    from archspec.official_documents.page_images import PageImageRepository
    from archspec.official_documents.service import OfficialDocumentService
    from archspec.standard_governance.compatibility import ContentUseDenied

    staging.mkdir(parents=True, exist_ok=True)
    prepared = staging / ('.server-prepared-' + uuid4().hex)
    prepared.mkdir()
    for entry in manifest['files']:
        _copy(source.directory / 'files' / entry['path'], prepared / entry['path'], entry)
    documents = OfficialDocumentService(prepared / 'official-cache',
        registry=source._worker.registry, governance=source._worker.documents.governance,
        page_image_repository=PageImageRepository(prepared / 'official-cache'))
    for entry in manifest['files']:
        if not re.fullmatch(r'official-cache/(?:gb55031|official-[a-f0-9]{32})/latest\.json', entry['path']):
            continue
        pointer = json.loads((prepared / entry['path']).read_bytes())
        try:
            _prepare_book_pages(source, documents, pointer)
        except ContentUseDenied:
            # 明确拒绝保留原始资产及拒绝历史，不能为迁移绕过原页权限或猜作成功。
            continue
    source.verify()
    return prepared


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _runtime(worker):
    # 原实例内存也属于源状态：故障时它可能比磁盘多一条中断说明，不能丢掉。
    with worker._lock:
        value = {'jobs': worker._jobs, 'batches': worker._batches,
            'worker_error': worker._worker_error,
            'worker_alive': worker._thread is not None and worker._thread.is_alive(),
            'sources': worker.registry.list()}
        encoded = _bytes(value)
        if len(encoded) > control_image.MAX_BYTES or value['worker_alive']:
            raise ServerSourceCaptureUnavailable(_SAFE)
        return encoded


def _inventory(root):
    """只接受服务器已有业务目录；未知文件和链接不静默排除或随包外传。"""
    result = []; total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in dirs:
            path = parent / name
            if path.is_symlink() or path.relative_to(root).parts[0] not in _FOLDERS:
                raise ServerSourceCaptureUnavailable(_SAFE)
        for name in names:
            path = parent / name; relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ServerSourceCaptureUnavailable(_SAFE)
            if parent == root and name in _SIDE_FILES:
                continue  # 两个已知数据库在共同事务内逐字段捕获，不复制WAL或半份数据库。
            if (path.relative_to(root).parts[0] not in _FOLDERS
                    or path.suffix.lower() in {'.key', '.pem', '.env'} or name.startswith('.env')
                    or info.st_size > evidence_manifest.MAX_OBJECT_BYTES):
                raise ServerSourceCaptureUnavailable(_SAFE)
            evidence_manifest._relative(relative, schema=evidence_manifest.SERVER_SCHEMA)
            total += info.st_size
            if total > evidence_manifest.MAX_TOTAL_BYTES or len(result) >= evidence_manifest.MAX_FILES:
                raise ServerSourceCaptureUnavailable(_SAFE)
            digest = evidence_manifest._sha(path)
            if path.stat().st_size != info.st_size:
                raise ServerSourceCaptureUnavailable(_SAFE)
            result.append({'path': relative, 'sha256': digest, 'size_bytes': info.st_size})
    return sorted(result, key=lambda row: row['path'])


def _copy(source, destination, entry):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(); size = 0
    with source.open('rb') as reader, destination.open('xb') as writer:
        while chunk := reader.read(256 * 1024):
            size += len(chunk)
            if size > entry['size_bytes']:
                raise ServerSourceCaptureUnavailable(_SAFE)
            digest.update(chunk); writer.write(chunk)
        writer.flush(); os.fsync(writer.fileno())
    if size != entry['size_bytes'] or digest.hexdigest() != entry['sha256']:
        raise ServerSourceCaptureUnavailable(_SAFE)


def _documents(connection, inventory):
    """已登记上传原件必须存在且完整；不只迁一行PARSED掩盖原件丢失。"""
    from archspec.documents.models import DocumentRow
    by_path = {row['path']: row for row in inventory}
    if inspect(connection).has_table(DocumentRow.__tablename__):
        for row in connection.execute(select(DocumentRow.__table__)).mappings():
            path = 'uploads/' + row['stored_name']
            entry = by_path.get(path)
            if (entry is None or '/' in row['stored_name'] or '\\' in row['stored_name']
                    or entry['sha256'] != row['sha256'] or entry['size_bytes'] != row['size_bytes']):
                raise ServerSourceCaptureUnavailable(_SAFE)


def capture_server_source(server, destination, *, timeout=30, keep_frozen=False):
    """从实际AccessGate对象取得后端；无客户端配置、假覆盖回调或自动云端签收。"""
    budget_engine = None; handle = None; gate = None; worker = None; was_running = False
    successful = False
    try:
        if type(keep_frozen) is not bool:
            raise ServerSourceCaptureUnavailable(_SAFE)
        from archspec import main
        from archspec.config import settings
        from archspec.database import engine
        from archspec.database_schema import registered_control_tables
        from archspec.deployment import AccessGate
        from archspec.official_documents.acquisition_router import get_runtime
        from archspec.persistence.gate_middleware import SourceWriteGateMiddleware
        from archspec.persistence.source_write_gate import SourceWriteGate
        if (type(server) is not AccessGate or server.application is not main.app
                or server.server_settings is None
                or getattr(main.app.state, 'persistence_backend', None) != 'LOCAL'):
            raise ServerSourceCaptureUnavailable(_SAFE)
        root = evidence_manifest._root(server.server_settings.data_dir)
        output = evidence_manifest._root(destination)
        if (root != settings.data_dir.resolve() or engine.dialect.name != 'sqlite'
                or Path(engine.url.database).resolve() != root / 'archspec.db'
                or output.exists() or output.is_relative_to(root) or root.is_relative_to(output)
                or not output.parent.is_dir()):
            raise ServerSourceCaptureUnavailable(_SAFE)
        gate = main.app.state.source_write_gate
        if (type(gate) is not SourceWriteGate or main.app.dependency_overrides
                or not any(m.cls is SourceWriteGateMiddleware and m.kwargs.get('gate') is gate
                           for m in main.app.user_middleware)):
            raise ServerSourceCaptureUnavailable(_SAFE)
        # 缺账本不能被当成0消费；mode=rw保证连接竞态也不会新建空账本。
        budget_path = evidence_manifest._root(root / 'model-budget.sqlite3')
        if not budget_path.is_file():
            raise ServerSourceCaptureUnavailable(_SAFE)
        uri = 'file:' + quote(budget_path.as_posix(), safe='/:') + '?mode=rw'
        budget_engine = create_engine('sqlite+pysqlite://', creator=lambda: sqlite3.connect(uri, uri=True, timeout=5))
        worker = get_runtime(main.app)
        if (worker.gate is not gate or worker.state_repository is not None
                or worker.registry.backend is not None
                or getattr(worker.documents, 'snapshot_catalog', None) is not None):
            raise ServerSourceCaptureUnavailable(_SAFE)
        was_running = worker._thread is not None and worker._thread.is_alive()
        handle = gate.pause()
        frozen = gate.wait_idle(handle, timeout)
        worker.stop()
        if worker._thread is not None and worker._thread.is_alive():
            raise ServerSourceCaptureUnavailable(_SAFE)
        with gate.frozen_section(frozen), ExitStack() as locks:
            source = locks.enter_context(control_image.image_transaction(engine, write=True))
            ledger = locks.enter_context(control_image.image_transaction(budget_engine, write=True))
            names = set(inspect(source).get_table_names())
            registered = registered_control_tables()
            if not names or names - set(registered):
                raise ServerSourceCaptureUnavailable(_SAFE)
            definitions = tuple(registered[name] for name in sorted(names))
            image = control_image.capture_control_image(source, definitions)
            budget = legacy_budget_source.capture_on_connection(ledger,
                migration_id='server-source-' + server.source_instance_uuid)
            inventory = _inventory(root)
            runtime = _runtime(worker)
            _documents(source, inventory)
            output.mkdir()
            for entry in inventory:
                _copy(root / entry['path'], output / 'files' / entry['path'], entry)
            (output / 'main-control.json').write_bytes(image.payload_json.encode('utf-8'))
            (output / 'budget-control.json').write_bytes(budget.source_image.payload_json.encode('utf-8'))
            (output / 'runtime-state.json').write_bytes(runtime)
            if (image != control_image.capture_control_image(source, definitions)
                    or budget != legacy_budget_source.capture_on_connection(ledger,
                        migration_id='server-source-' + server.source_instance_uuid)
                    or inventory != _inventory(root) or runtime != _runtime(worker)):
                raise ServerSourceCaptureUnavailable(_SAFE)
            manifest = {'schema_version': 'SERVER_SOURCE_CAPTURE_V1',
                'source_instance_uuid': server.source_instance_uuid,
                'state': 'SOURCE_CAPTURED_UNPUBLISHED', 'main_sha256': image.sha256,
                'budget_sha256': budget.source_image.sha256, 'files': inventory,
                'runtime_sha256': hashlib.sha256(runtime).hexdigest(),
                'activation_blockers': list(budget.activation_blockers) + (
                    ['ACQUISITION_WORKER_ERROR'] if worker._worker_error else []),
                'main_table_names': sorted(names)}
        # 两个事务均正常退出后才写成功清单；清单不是生产代次或资料发送许可。
        encoded = _bytes(manifest)
        with (output / 'manifest.json').open('xb') as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        result = ServerSourceCapture(output, image, budget, hashlib.sha256(encoded).hexdigest(),
            _issuer=_ISSUER, _gate=gate if keep_frozen else None, _handle=handle if keep_frozen else None,
            _receipt=frozen if keep_frozen else None, _worker=worker, _was_running=was_running,
            _runtime_json=runtime.decode('utf-8'), _source_root=root, _main_engine=engine)
        result.verify()
        successful = True
        return result
    except Exception:  # noqa: BLE001 - 不回显行值、SQL、原始文件名或配置秘密
        raise ServerSourceCaptureUnavailable(_SAFE) from None
    finally:
        if budget_engine is not None:
            budget_engine.dispose()
        if handle is not None and (not successful or not keep_frozen):
            # 单纯本机保全可以恢复原服务；迁移调用显式保持冻结，网络在SQL锁外进行。
            try:
                gate.resume(handle)
                if was_running:
                    worker.start()
            except Exception:  # noqa: BLE001 - 恢复未知不伪报服务已重新开放
                raise ServerSourceCaptureUnavailable(_SAFE + '；源恢复状态未确认') from None
