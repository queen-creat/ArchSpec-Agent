"""云端已发布数据的只读恢复与服务器启动选择；不建空控制库或本地回退。"""
from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from archspec.persistence import activation, control_image, evidence_manifest, sql_namespace
from archspec.persistence.migration_flow import _same_point, read_generation_metadata

SERVER_SOURCE_SCOPE='SERVER_UNIFIED_CUT_V1'
_CURRENT=ContextVar('archspec_verified_cloud_startup',default=None)
_ISSUER=object()
_MAGIC=b'ARCHSPEC_VECTOR_F32_V1\0'
_SAFE='云端启动核验未完成，未创建本地控制库或回退旧种子'


class RemoteStartupUnavailable(RuntimeError):
    code='REMOTE_STARTUP_UNAVAILABLE'


def current_startup():
    return _CURRENT.get()


@contextmanager
def startup_scope(restored):
    if type(restored) is not RestoredCloudGeneration or restored._issuer is not _ISSUER or restored.closed or restored.closing:
        raise RemoteStartupUnavailable(_SAFE)
    token=_CURRENT.set(restored)
    try:yield restored
    finally:_CURRENT.reset(token)


def _cleanup(directory,parent):
    # 只删除本次排他创建且仍位于已核父目录内的缓存，不删除用户旧资料。
    resolved=directory.resolve();base=parent.resolve()
    if (directory.is_symlink() or not resolved.is_relative_to(base) or resolved==base
            or not directory.name.startswith('.archspec-cloud-')):
        raise RemoteStartupUnavailable(_SAFE)
    shutil.rmtree(directory)


class RestoredCloudGeneration:
    """仅持有已回读缓存和原SQL引擎；业务绑定仍须校验真实表及任务租约。"""
    def __init__(self,issuer,engine,data_dir,scratch,parent,active,metadata,vector_count,clock=None,*,objects=None,vectors=None,governance_table_names=(),read_view_assets=None,deferred_page_assets=()):
        if issuer is not _ISSUER:raise RemoteStartupUnavailable(_SAFE)
        self._issuer=issuer;self.engine=engine;self.data_dir=data_dir
        self.scratch_dir=scratch;self.parent=parent;self.active=active;self.metadata=metadata
        self.vector_count=vector_count;self.closed=False;self._owned_resources=[];self._clock=clock
        self._acquisition=None;self.closing=False
        self._objects=objects;self._vectors=vectors
        self.governance_table_names=tuple(governance_table_names)
        self.deferred_page_assets=tuple(dict(entry) for entry in deferred_page_assets)
        # 绑定本次实际恢复的合并资产，包含后续publication；不改原代次metadata身份。
        self._read_view_assets_json=evidence_manifest._canonical(read_view_assets or metadata['asset_manifest'])
    def attach_acquisition(self, worker):
        """把本代次的实际正文工人纳入资源归属，不接受客户端自报已停止。"""
        from archspec.official_documents.jobs import AcquisitionService
        from archspec.persistence.acquisition_state import SQLAcquisitionState
        if (self.closed or self.closing or not isinstance(worker, AcquisitionService)
                or not isinstance(worker.state_repository, SQLAcquisitionState)
                or worker.state_repository.engine is not self.engine
                or worker.cache_dir.resolve() != self.data_dir/'official-acquisitions'
                or worker.documents.cache_dir.resolve() != self.data_dir/'official-cache'
                or worker.gate is None
                or self._acquisition is not None and self._acquisition is not worker):
            raise RemoteStartupUnavailable(_SAFE)
        self._acquisition=worker

    def close(self):
        if self.closed:return
        self.closing=True
        worker=self._acquisition
        if worker is not None:
            status=worker.gate.status()
            if not worker.is_stopped() or not status['healthy'] or status['active_writers']:
                raise RemoteStartupUnavailable('云端工人或在途请求尚未结束，连接与恢复缓存保留')
        failed=[]
        for close in reversed(self._owned_resources):
            try:close()
            except Exception:failed.insert(0,close)  # noqa: BLE001 - 全部资源均尝试，失败原文不外泄。
        self._owned_resources=failed
        if failed:
            raise RemoteStartupUnavailable('云端专属连接释放未完成，恢复缓存保留供核验')
        # 只有所有实际工人/请求和专属连接均结束后才清理本实例缓存。
        _cleanup(self.scratch_dir,self.parent)
        self.closed=True
    def runtime_status(self, runtime):
        """网页只报告本次已核启动身份；不把探活或合成恢复冒充阶段验收。"""
        from archspec.persistence.runtime_bindings import RuntimeBindingsUnavailable
        if (self._issuer is not _ISSUER or self.closed or self.closing or runtime is None
                or runtime.engine is not self.engine
                or runtime.cache_dir.parent.resolve() != self.data_dir.resolve()):
            raise RuntimeBindingsUnavailable("云端启动身份与当前服务不一致")
        runtime.validate_selection()
        scope = self.metadata['source_scope']
        result = {
            "active_backend": "REMOTE",
            "data_migration": "PUBLISHED" if scope == SERVER_SOURCE_SCOPE else "SYNTHETIC_ONLY",
            "active_generation": {
                "generation": self.active.generation,
                "candidate_id": self.active.candidate_id,
                "verification": "CORE_ASSETS_VERIFIED" if self.deferred_page_assets else "STARTUP_VERIFIED",
                "source_scope": scope,
            },
        }
        if self.deferred_page_assets:
            from archspec.persistence.page_image_cache import CloudPageImageRepository
            repository=runtime.page_image_repository
            if not isinstance(repository, CloudPageImageRepository) or repository.generation is not self:
                raise RuntimeBindingsUnavailable("原页按需恢复仓库与本次云端启动不一致")
            result["page_image_recovery"]=repository.recovery_status()
        return result

    def build_bindings(self):
        """在真实服务器导入上下文组合同一个库；绝不在这里补缺失表。"""
        if current_startup() is not self or self.closed or self.closing:raise RemoteStartupUnavailable(_SAFE)
        from archspec.database import engine
        if engine is not self.engine:raise RemoteStartupUnavailable(_SAFE)
        return self.verified_runtime_bindings()

    def verified_runtime_bindings(self):
        """网站启动与操作复验共用全部运行合同；不选默认库、不建表或启动工人。"""
        if self._issuer is not _ISSUER or self.closed or self.closing:raise RemoteStartupUnavailable(_SAFE)
        engine=self.engine
        from archspec.database_schema import registered_control_tables
        from archspec.official_documents.page_images import PageImageRepository
        from archspec.official_documents.registry import OfficialSourceRegistry
        from archspec.persistence.acquisition_state import SQLAcquisitionState
        from archspec.persistence.budget import SQLBudgetJournal
        from archspec.persistence.runtime_bindings import ServiceRuntimeBindings
        from archspec.persistence.snapshot_catalog import SQLSnapshotCatalog
        from archspec.persistence.source_registry import SQLSourceRegistryBackend
        registered=registered_control_tables()
        definitions=[]
        from archspec.standard_governance.migration import read_cloud_governance_contract
        with control_image.image_transaction(engine) as connection:
            current_governance=read_cloud_governance_contract(connection,active=self.active,metadata=self.metadata)
        if current_governance!=self.governance_table_names:raise RemoteStartupUnavailable(_SAFE)
        for name in sorted(set(self.metadata['main_table_names'])|set(current_governance)):
            if name not in registered:raise RemoteStartupUnavailable(_SAFE)
            definitions.append(registered[name])
        # 活跃业务可以追加任务/核验事件；只核完整结构、引用与当前状态，不强求旧行SHA。
        with control_image.image_transaction(engine) as connection:
            control_image.capture_control_image(connection,definitions)
        cache=self.data_dir/'official-cache'
        registry=OfficialSourceRegistry(cache,backend=SQLSourceRegistryBackend(engine))
        from archspec.persistence.snapshot_publication import RuntimeSnapshotPublisher
        publisher=RuntimeSnapshotPublisher(self,self._objects,self._vectors)
        from archspec.persistence.business_files import RuntimeBusinessFiles
        files=RuntimeBusinessFiles(self,self._objects)
        from archspec.official_documents.read_views import SnapshotReadViews,build_read_views
        instance=str(uuid4())
        images=PageImageRepository(cache)
        if self.deferred_page_assets:
            from archspec.persistence.page_image_cache import CloudPageImageRepository
            images=CloudPageImageRepository(self)
        runtime=ServiceRuntimeBindings(engine,cache,registry,SQLBudgetJournal(engine),instance,clock=self._clock,
            snapshot_catalog=SQLSnapshotCatalog(engine,cache,publisher=publisher),snapshot_publisher=publisher,business_files=files,
            page_image_repository=images,acquisition_state=SQLAcquisitionState(engine),
            snapshot_read_views=SnapshotReadViews(engine,cache,instance))
        build_read_views(runtime,self)
        return runtime


def _decode_index(path):
    if path.stat().st_size>32*1024*1024:raise RemoteStartupUnavailable(_SAFE)
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise RemoteStartupUnavailable(_SAFE)
            value[key]=item
        return value
    def invalid(_):raise RemoteStartupUnavailable(_SAFE)
    with path.open('r',encoding='utf-8') as stream:
        return json.load(stream,object_pairs_hook=unique,parse_constant=invalid)


def _verify_vectors(metadata,objects,vectors,scratch):
    descriptor=metadata['vector_index'];count=descriptor['count']
    if (type(count) is not int or not 0<=count<=20_000 or descriptor['dimension']!=512
            or vectors.dimension!=512 or vectors.collection!=descriptor['collection']):raise RemoteStartupUnavailable(_SAFE)
    vectors.health_check()  # 只读，不建立缺失集合或上传缺失向量。
    index_path=scratch/'vector-index.json';binary_path=scratch/'vectors.bin'
    objects.download_file(descriptor['index_key'],index_path,descriptor['index_sha256'],expected_size=descriptor['index_size_bytes'])
    objects.download_file(descriptor['binary_key'],binary_path,descriptor['binary_sha256'],expected_size=descriptor['binary_size_bytes'])
    if evidence_manifest._sha(index_path)!=descriptor['index_sha256'] or evidence_manifest._sha(binary_path)!=descriptor['binary_sha256']:
        raise RemoteStartupUnavailable(_SAFE)
    index=_decode_index(index_path)
    if (any(index[k]!=descriptor[k] for k in ('dimension','count','model_fingerprint','corpus_key','collection',
                                             'binary_key','binary_sha256','binary_size_bytes'))
            or index['schema_version']!='CLOUD_VECTOR_INDEX_V1' or len(index['points'])!=count
            or binary_path.stat().st_size!=len(_MAGIC)+8+count*512*4):raise RemoteStartupUnavailable(_SAFE)
    expected_pairs={(source,item['snapshot_id']) for source,item in metadata['retrieval_eligibility'].items() if item['state']=='ALLOWED'}
    identifiers=set()
    with binary_path.open('rb') as stream:
        if stream.read(len(_MAGIC))!=_MAGIC or struct.unpack('<II',stream.read(8))!=(512,count):raise RemoteStartupUnavailable(_SAFE)
        for offset in range(0,count,128):
            batch=[]
            for row in index['points'][offset:offset+128]:
                identifier=row['id'];payload=row['payload']
                if (identifier in identifiers or (payload['source_id'],payload['snapshot_id']) not in expected_pairs
                        or payload['model_fingerprint']!=descriptor['model_fingerprint']
                        or payload['migration_cut_sha256']!=metadata['source_cut_sha256']):raise RemoteStartupUnavailable(_SAFE)
                identifiers.add(identifier)
                batch.append({'id':identifier,'payload':payload,'vector':struct.unpack('<512f',stream.read(512*4))})
            actual={row['id']:row for row in vectors.retrieve_points([row['id'] for row in batch])}
            if set(actual)!={row['id'] for row in batch} or any(not _same_point(actual[row['id']],row) for row in batch):
                raise RemoteStartupUnavailable(_SAFE)
    if vectors.count_points(migration_cut_sha256=metadata['source_cut_sha256'])!=count:raise RemoteStartupUnavailable(_SAFE)
    return count


def _verify_current_controls(engine,metadata,data_dir,*,assets=None,publications=None,file_state=None,active=None):
    from archspec.persistence import acquisition_state, budget, snapshot_catalog, source_registry
    from archspec.standard_governance.migration import read_cloud_governance_contract
    with control_image.image_transaction(engine) as connection:
        read_cloud_governance_contract(connection,active=active,metadata=metadata)
        _,history=source_registry._state(connection)
        catalog,rows,heads,_=snapshot_catalog._state(connection)
        _,queue_row,state=acquisition_state._read(connection)
        for row in (history[0],catalog,queue_row):
            if any(row[k]!=metadata[k] for k in ('migration_id','source_cut_sha256')):raise RemoteStartupUnavailable(_SAFE)
        if history[0]['payload_json']!=source_registry._json(metadata['registry_payload']):raise RemoteStartupUnavailable(_SAFE)
        budget._state(connection)  # 当前追加费用保留，不重置到迁移时的余额或行数。
        if publications is not None:
            from archspec.persistence.snapshot_publication import read_publications
            if read_publications(connection,model_fingerprint=metadata['vector_index']['model_fingerprint']) != publications:
                raise RemoteStartupUnavailable(_SAFE)
        if file_state is not None:
            from archspec.persistence.business_files import read_state
            if read_state(connection) != file_state:raise RemoteStartupUnavailable(_SAFE)
        entries={row['path']:row for row in (assets or metadata['asset_manifest'])['entries']}
        def fixed(source,pin,digest=None):
            relative='official-cache/'+source+'/'+pin+'.json';entry=entries.get(relative)
            if entry is None or (digest is not None and entry['sha256']!=digest):raise RemoteStartupUnavailable(_SAFE)
            path=data_dir/relative
            if evidence_manifest._sha(path)!=entry['sha256']:raise RemoteStartupUnavailable(_SAFE)
        for head in heads.values():
            row=rows[head['revision']-1];fixed(row['source_id'],row['snapshot_id'],row['snapshot_sha256'])
        for job in state['jobs'].values():
            if job['state']=='READY':fixed(job['source_id'],job['snapshot_id'])
        # 文档SQL记录与原件闭包同时核验，不能有记录却缺文件或字节不符。
        from sqlalchemy import select

        from archspec.documents.models import DocumentRow
        if metadata['source_scope']==SERVER_SOURCE_SCOPE and 'documents' not in metadata['main_table_names']:
            raise RemoteStartupUnavailable(_SAFE)
        # 旧算法夹具明确只有synthetic表，不能误当正式网站或强查不存在的业务表。
        if 'documents' in metadata['main_table_names']:
            table=sql_namespace.bind_tables(connection,[DocumentRow.__table__])[0]
            for row in connection.execute(select(table.c.stored_name,table.c.sha256,table.c.size_bytes)).mappings():
                relative='uploads/'+row['stored_name'];entry=entries.get(relative)
                if (entry is None or entry['sha256']!=row['sha256'] or entry['size_bytes']!=row['size_bytes']
                        or evidence_manifest._sha(data_dir/relative)!=row['sha256']):raise RemoteStartupUnavailable(_SAFE)


def restore_published_generation(engine,object_store,vector_store,*,parent_dir,allowed_source_scopes=None,clock=None,defer_page_images=False):
    """算法测试显式允许合成scope；实际服务器入口只使用生产scope默认值。"""
    scratch=None;parent=None
    try:
        active=activation.SQLActivationLedger(engine).read_active()
        candidate=activation.SQLActivationLedger(engine).get_candidate(active.candidate_id)
        metadata=read_generation_metadata(engine,candidate)
        scopes={SERVER_SOURCE_SCOPE} if allowed_source_scopes is None else allowed_source_scopes
        if metadata['source_scope'] not in scopes:raise RemoteStartupUnavailable(_SAFE)
        parent=evidence_manifest._root(parent_dir)
        if not parent.is_dir():raise RemoteStartupUnavailable(_SAFE)
        scratch=Path(tempfile.mkdtemp(prefix='.archspec-cloud-',dir=parent))
        from archspec.persistence.business_files import merged_assets, read_state
        from archspec.persistence.snapshot_publication import read_publications
        from archspec.standard_governance.migration import read_cloud_governance_contract
        with control_image.image_transaction(engine) as connection:
            governance_names=read_cloud_governance_contract(connection,active=active,metadata=metadata)
            publications=read_publications(connection,model_fingerprint=metadata['vector_index']['model_fingerprint'])
            file_state=read_state(connection)
        assets=merged_assets(metadata,publications,file_state)
        restored=evidence_manifest.restore_manifest(assets,scratch/'assets',object_store,
            defer_page_images=defer_page_images)
        data_dir=Path(restored['directory'])
        count=_verify_vectors(metadata,object_store,vector_store,scratch)
        for number,body in enumerate(publications):
            journal_scratch=scratch/('snapshot-index-'+str(number));journal_scratch.mkdir()
            count+=_verify_vectors({'vector_index':body['vector_index'],
                'source_cut_sha256':body['publication_sha256'],
                'retrieval_eligibility':body['retrieval_eligibility']},object_store,vector_store,journal_scratch)
        _verify_current_controls(engine,metadata,data_dir,assets=assets,publications=publications,file_state=file_state,active=active)
        if activation.SQLActivationLedger(engine).read_active()!=active:raise RemoteStartupUnavailable(_SAFE)
        from archspec.persistence.chunked import ChunkedObjectStore
        runtime_objects=ChunkedObjectStore(object_store.store,scratch/'runtime-parts',part_size=object_store.part_size)
        return RestoredCloudGeneration(_ISSUER,engine,data_dir,scratch,parent,active,metadata,count,clock=clock,
            objects=runtime_objects,vectors=vector_store,governance_table_names=governance_names,read_view_assets=assets,
            deferred_page_assets=restored.get('deferred_page_assets',()))
    except Exception:  # noqa: BLE001 - 不泄露SQL、连接串、私有载荷或本机路径
        if scratch is not None:_cleanup(scratch,parent)
        raise RemoteStartupUnavailable(_SAFE) from None


def prepare_remote_startup(server_config):
    """create_server_app的正式入口；只读现有PG/私有Supabase/Qdrant，不安装或降级。"""
    resources=[]
    try:
        from archspec.persistence import config
        from archspec.persistence.chunked import ChunkedObjectStore
        from archspec.persistence.postgres import (
            create_postgres_runtime_engine,
            load_postgres_settings,
        )
        page_mode=os.getenv('ARCHSPEC_PAGE_IMAGE_RESTORE','EAGER')
        if page_mode not in {'EAGER','ON_READ'}:
            raise RemoteStartupUnavailable(_SAFE)
        pg=load_postgres_settings();storage=config.load_supabase_settings();index=config.load_qdrant_settings()
        if pg is None or storage is None or index is None:raise RemoteStartupUnavailable(_SAFE)
        engine=create_postgres_runtime_engine(pg).execution_options(schema_translate_map={None:'archspec_control'})
        resources.append(engine.dispose)
        raw=config._supabase_objects(storage);resources.append(raw.close)
        vectors=config._vectors(index);resources.append(vectors.close)
        # 完整代次和生产范围先核，才允许创建恢复暂存区；缺配置不触碰data。
        active=activation.SQLActivationLedger(engine).read_active()
        candidate=activation.SQLActivationLedger(engine).get_candidate(active.candidate_id)
        metadata=read_generation_metadata(engine,candidate)
        if metadata['source_scope']!=SERVER_SOURCE_SCOPE:raise RemoteStartupUnavailable(_SAFE)
        parent=evidence_manifest._root(server_config.data_dir.parent)
        raw.health_check()
        # 原对象适配器按4MiB分块；暂存放在明确服务器数据父目录内。
        parts=Path(tempfile.mkdtemp(prefix='.archspec-cloud-',dir=parent))
        try:
            store=ChunkedObjectStore(raw,parts/'parts')
            restored=restore_published_generation(engine,store,vectors,parent_dir=parent,
                defer_page_images=page_mode=='ON_READ')
        finally:
            _cleanup(parts,parent)
        restored._owned_resources=resources
        return restored
    except Exception:  # noqa: BLE001 - 清理专属资源，不回显提供方信息或尝试本地库
        cleanup_failed=False
        for close in reversed(resources):
            try:close()
            except Exception:cleanup_failed=True  # noqa: BLE001 - 仅保留清理结果，不回显提供方异常
        message=_SAFE if not cleanup_failed else _SAFE+"；专属连接清理结果未确认"
        raise RemoteStartupUnavailable(message) from None