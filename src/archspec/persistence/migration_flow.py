"""统一迁移流水线：固定资产与全部向量先写读核齐，再由SQL事务发布六组件。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
from contextlib import nullcontext
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import Column, MetaData, String, Table, Text, insert, inspect, select

from archspec.persistence import activation, control_image, evidence_manifest, sql_namespace
from archspec.persistence.control_migration import _decode, _inputs, import_cut_controls

_SCHEMA='CLOUD_GENERATION_METADATA_V1'
_SAFE='云端资料、索引或控制记录未完整核验，本次没有切换可用数据'
_META=MetaData()
_GENERATIONS=Table('external_generation_manifests',_META,
    Column('candidate_id',String(64),primary_key=True),
    Column('metadata_sha256',String(64),nullable=False),
    Column('metadata_json',Text,nullable=False))


class CloudMigrationUnavailable(RuntimeError):
    """安全失败；不回显提供方响应、任务内容、账号或路径。"""


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _asset_bytes(cut,path):
    raw=dict(cut.external.files)
    entries={e['path']:e for e in evidence_manifest._check_manifest(_decode(raw['asset-manifest.json']))}
    entry=entries.get(path)
    if entry is None or entry['size_bytes']>8*1024*1024:
        raise CloudMigrationUnavailable(_SAFE)
    if cut.external.assets is None:
        content=raw[path]
    else:
        from archspec.persistence.control_migration import _BoundedBuffer
        buffer=_BoundedBuffer();cut.external.assets.copy_to(path,buffer);content=buffer.getvalue()
    if len(content)!=entry['size_bytes'] or hashlib.sha256(content).hexdigest()!=entry['sha256']:
        raise CloudMigrationUnavailable(_SAFE)
    return content


def _prepare(cut,corpus,documents):
    from archspec.official_documents.metadata_filter import SourceMetadataFilter
    from archspec.official_documents.processing_cache import stable_hash
    from archspec.official_documents.semantic_experiment import (
        PreparedSemanticCorpus,
        _runtime_fingerprint,
    )
    from archspec.official_documents.service import OfficialDocumentService
    if (not isinstance(corpus,PreparedSemanticCorpus) or not isinstance(documents,OfficialDocumentService)
            or corpus.runtime_fingerprint != _runtime_fingerprint(corpus)
            or len(corpus.chunks)!=len(corpus.vectors) or len(corpus.chunks)>20_000
            or len(corpus.snapshots)>20):
        raise CloudMigrationUnavailable(_SAFE)
    payload,pointers,state=_inputs(cut)
    original={p['source_id']:_decode(_asset_bytes(cut,'official-cache/'+p['source_id']+'/'+p['snapshot_id']+'.json')) for p in pointers}
    from archspec.standard_governance.compatibility import ContentUseDenied
    eligibility={}; eligible={}
    for source,snapshot in original.items():
        if documents._source(source)['kind']!='FULL_TEXT':
            eligibility[source]={'snapshot_id':snapshot['snapshot_id'],'state':'NOT_FULL_TEXT','operation':'LOCAL_SEARCH'}
            continue
        try:
            for operation in ('LOCAL_SEARCH','LOCAL_EXCERPT'):
                documents._check_read(source,snapshot['snapshot_id'],operation)
        except ContentUseDenied:
            # 与现有检索共用搜索及摘录资格；拒绝仍完整归档，不把损坏伪装为拒绝。
            eligibility[source]={'snapshot_id':snapshot['snapshot_id'],'state':'DENIED',
                                 'operation':operation,'code':'CONTENT_USE_NOT_AUTHORIZED'}
        else:
            eligibility[source]={'snapshot_id':snapshot['snapshot_id'],'state':'ALLOWED','operation':'LOCAL_SEARCH'}
            eligible[source]=snapshot
    if (len(corpus.snapshots)!=len(eligible) or {row['source_id'] for row in corpus.snapshots}!=set(eligible)
            or any(eligible[row['source_id']]!=row for row in corpus.snapshots)):
        raise CloudMigrationUnavailable(_SAFE)
    from archspec.official_documents.search import _chunk
    expected_chunks=[]; expected_origins={}
    for snapshot in corpus.snapshots:
        for index in range(len(snapshot['chunks'])):
            identifier=f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
            expected_chunks.append(_chunk(snapshot,index,identifier,len(expected_chunks)))
            expected_origins[identifier]=snapshot
    if tuple(expected_chunks)!=corpus.chunks or expected_origins!=corpus.origins:
        raise CloudMigrationUnavailable(_SAFE)
    profiles={'__eligibility__':eligibility}
    for snapshot in corpus.snapshots:
        source,pin=snapshot['source_id'],snapshot['snapshot_id']
        binding=_decode(_asset_bytes(cut,'official-cache/source-metadata/bindings/'+source+'/'+pin+'.json'))
        metadata_id=binding['metadata_id']
        observation=_decode(_asset_bytes(cut,'official-cache/source-metadata/observations/'+metadata_id+'.json'))
        fixed,current,profile_sha=documents._metadata_filter_profile(snapshot,metadata_id=metadata_id)
        pointer=next(p for p in pointers if p['source_id']==source)
        if (fixed!=observation or stable_hash({k:v for k,v in fixed.items() if k!='metadata_id'})!=metadata_id
                or any(binding.get(k)!=fixed.get(k) for k in ('source_id','snapshot_id','snapshot_fingerprint','file_sha256','metadata_id'))
                or fixed['snapshot_fingerprint']!=pointer['snapshot_sha256']
                or fixed['file_sha256']!=pointer['document_sha256']
                or not SourceMetadataFilter().matches(fixed,current)):
            raise CloudMigrationUnavailable(_SAFE)
        # 与原服务的状态指纹一致：评估时刻不代表效力/权限事件发生变化。
        profiles[source]=(fixed,{k:v for k,v in current.items() if k!='evaluated_at'},profile_sha)
    return _points_for_corpus(corpus,profiles,cut.source_cut_sha256),payload,pointers,state,profiles


class _CorpusPoints:
    """原始坐标保持连续双精度；切片最多只展开调用方当前上传批次。"""
    def __init__(self, corpus, profiles, cut_sha):
        from archspec.official_documents.semantic_experiment import _PackedVectors
        self.chunks = corpus.chunks
        self.vectors = (corpus.vectors if isinstance(corpus.vectors, _PackedVectors)
                        else _PackedVectors(corpus.vectors,len(corpus.chunks),normalize=False))
        self.origins = {c.id:(corpus.origins[c.id]['source_id'],corpus.origins[c.id]['snapshot_id']) for c in self.chunks}
        self.profiles = copy.deepcopy(profiles)
        self.model_fingerprint = corpus.model_fingerprint;self.cut_sha = cut_sha
        if len(self.vectors) != len(self.chunks):raise CloudMigrationUnavailable(_SAFE)
        seen = set()
        for point in self:
            if point['id'] in seen:raise CloudMigrationUnavailable(_SAFE)
            seen.add(point['id'])

    def __len__(self):return len(self.chunks)

    def __eq__(self, other):
        # 发布前的第二次完整复核仍比较内容，不能把新有界容器按对象地址比较。
        if not isinstance(other,_CorpusPoints):return NotImplemented
        return (self.chunks==other.chunks and self.vectors==other.vectors
                and self.origins==other.origins and self.profiles==other.profiles
                and self.model_fingerprint==other.model_fingerprint and self.cut_sha==other.cut_sha)

    def __iter__(self):
        for index in range(len(self)):yield self[index]

    def __getitem__(self, index):
        from archspec.official_documents.metadata_filter import _fixed_field
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        chunk=self.chunks[index];vector=self.vectors[index]
        source,snapshot_id=self.origins[chunk.id];fixed,current,profile_sha=self.profiles[source]
        if len(vector)!=512 or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):
            raise CloudMigrationUnavailable(_SAFE)
        norm=math.hypot(*vector)
        if not math.isclose(norm,1.0,rel_tol=1e-6,abs_tol=1e-7):
            raise CloudMigrationUnavailable(_SAFE)
        identifier=str(uuid5(NAMESPACE_URL,'archspec-cloud:'+self.cut_sha+':'+self.model_fingerprint+':'+chunk.id))
        status=current['version_status']
        point_payload={'source_id':source,'snapshot_id':snapshot_id,'chunk_id':chunk.id,
            'model_fingerprint':self.model_fingerprint,'migration_cut_sha256':self.cut_sha,
            'chunk_sha256':hashlib.sha256(chunk.text.encode('utf-8')).hexdigest(),
            'page_start':chunk.page_start,'page_end':chunk.page_end,'document_sha256':fixed['file_sha256'],
            'snapshot_sha256':fixed['snapshot_fingerprint'],'metadata_id':fixed['metadata_id'],'profile_sha256':profile_sha,
            'level':_fixed_field(fixed,'level',{'CODE_CLUE','NOTICE_OBSERVED'}) or 'UNKNOWN',
            'region':_fixed_field(fixed,'region',{'NOTICE_OBSERVED'}) or 'UNKNOWN',
            'mandatory_status':'UNKNOWN',
            'lifecycle_status':status['value'] if status['verification_state']=='VERIFIED' else 'UNKNOWN'}
        # 强条未知保留UNKNOWN；不从GB编号、现行或“应”推定。
        return {'id':identifier,'vector':list(vector),'payload':point_payload}


def _points_for_corpus(corpus,profiles,cut_sha):
    """在网络副作用前核验全部行，迁移和持续发布共用同一有界点序列。"""
    try:return _CorpusPoints(corpus,profiles,cut_sha)
    except ValueError:raise CloudMigrationUnavailable(_SAFE) from None

def _same_point(actual,expected):
    return (actual['id']==expected['id'] and actual['payload']==expected['payload']
        and len(actual['vector'])==len(expected['vector'])
        and all(math.isclose(a,b,rel_tol=2e-6,abs_tol=1e-7) for a,b in zip(actual['vector'],expected['vector'],strict=True)))


def _write_verify_vectors(points,store,cut_sha):
    store.ensure_collection(prepare_filter_indexes=True)
    for start in range(0,len(points),128):
        batch=points[start:start+128];old={p['id']:p for p in store.retrieve_points([p['id'] for p in batch])}
        missing=[]
        for point in batch:
            if point['id'] in old:
                if not _same_point(old[point['id']],point):raise CloudMigrationUnavailable(_SAFE)
            else:missing.append(point)
        if missing:store.upsert(missing)
    for start in range(0,len(points),128):
        batch=points[start:start+128];actual={p['id']:p for p in store.retrieve_points([p['id'] for p in batch])}
        if set(actual)!={p['id'] for p in batch} or any(not _same_point(actual[p['id']],p) for p in batch):
            raise CloudMigrationUnavailable(_SAFE)
    if store.count_points(migration_cut_sha256=cut_sha)!=len(points):
        raise CloudMigrationUnavailable(_SAFE)


def _stage_assets(cut,work):
    manifest=_decode(dict(cut.external.files)['asset-manifest.json'])
    for entry in evidence_manifest._check_manifest(manifest):
        path=work/entry['path'];path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as output:
            if cut.external.assets is None:output.write(dict(cut.external.files)[entry['path']])
            else:cut.external.assets.copy_to(entry['path'],output)
        if path.stat().st_size!=entry['size_bytes'] or evidence_manifest._sha(path)!=entry['sha256']:
            raise CloudMigrationUnavailable(_SAFE)
    return manifest


def _vector_artifacts(points,corpus,store,work,collection):
    binary=work/'vectors.bin'
    with binary.open('xb') as output:
        output.write(b'ARCHSPEC_VECTOR_F32_V1\0');output.write(struct.pack('<II',512,len(points)))
        for point in points:output.write(struct.pack('<512f',*point['vector']))
    digest=evidence_manifest._sha(binary);binary_key='evidence/vector-indices/'+digest+'.bin'
    # 坐标用float32二进制保存，20k×512约40MiB，元信息不重复保存大数组。
    index={'schema_version':'CLOUD_VECTOR_INDEX_V1','dimension':512,'count':len(points),
        'model_fingerprint':corpus.model_fingerprint,'corpus_key':corpus.key,'collection':collection,
        'binary_key':binary_key,'binary_sha256':digest,'binary_size_bytes':binary.stat().st_size,
        'points':[{'id':p['id'],'payload':p['payload']} for p in points]}
    content=_canonical(index)
    if len(content)>32*1024*1024:raise CloudMigrationUnavailable(_SAFE)
    index_file=work/'vector-index.json';index_file.write_bytes(content)
    index_sha=hashlib.sha256(content).hexdigest();index_key='evidence/vector-indices/'+index_sha+'.json'
    store.put_file(binary_key,binary,digest)
    store.put_file(index_key,index_file,index_sha,content_type='application/json')
    for key,path,sha in ((binary_key,binary,digest),(index_key,index_file,index_sha)):
        restored=work/('verified-'+path.name)
        store.download_file(key,restored,sha,expected_size=path.stat().st_size)
        if evidence_manifest._sha(restored)!=sha:raise CloudMigrationUnavailable(_SAFE)
    return {k:index[k] for k in ('dimension','count','model_fingerprint','corpus_key','collection','binary_key','binary_sha256','binary_size_bytes')}|{
        'index_key':index_key,'index_sha256':index_sha,'index_size_bytes':len(content)}


def _table(connection,check=True):
    table=sql_namespace.bind_tables(connection,[_GENERATIONS])[0]
    if check:
        if not inspect(connection).has_table(table.name,schema=table.schema):raise CloudMigrationUnavailable(_SAFE)
        control_image._physical_schema(connection,table)
    return table


def _source_identity_body(cut):
    """唯一的切面身份编码，原型与原生捕获共用相同字段。"""
    registration=cut.registration
    body={'version':cut.source_scope,'registration':{
        'service':registration.source_service_id,'instance':registration.source_instance_id,
        'authorization_scope':registration.authorization_scope_sha256,
        'writer_inventory':registration.writer_inventory_version,'writer_evidence':registration.writer_evidence,
        'main_table_names':registration.main_table_names,'budget_table_names':registration.budget_table_names,
        'authorized_project_ids':registration.authorized_project_ids},
        'freeze':[cut.write_epoch,cut.freeze_nonce],'main':cut.main_image.sha256,'budget':cut.budget_image.sha256,
        'external':[(name,len(content),hashlib.sha256(content).hexdigest()) for name,content in cut.external.files],
        'runtime':hashlib.sha256(cut.external.runtime_json.encode('utf-8')).hexdigest(),
        'closure':hashlib.sha256(cut.closure_json.encode('utf-8')).hexdigest()}
    if cut.external.assets is not None:body['asset_manifest']=cut.external.assets.manifest_id
    if cut.legacy_budget is not None:
        old=cut.legacy_budget
        body['legacy_budget']={'migration_id':old.migration_id,'manifest_sha256':old.migration_manifest_sha256,
                              'limit_cents':old.limit_cents,'call_count':old.call_count,'reserved_cents':old.reserved_cents}
    return body


def _source_identity(cut):
    body = _source_identity_body(cut)
    if _sha(body)!=cut.source_cut_sha256:raise CloudMigrationUnavailable(_SAFE)
    # 元组与SQL JSON数组采用同一规范形式；不把无损序列化当作身份改变。
    return _decode(_canonical(body))


def _record_metadata(engine,body,candidate):
    content=_canonical(body)
    if len(content)>8*1024*1024:raise CloudMigrationUnavailable(_SAFE)
    with control_image.image_transaction(engine,write=True) as connection:
        table=_table(connection,False);table.create(connection,checkfirst=True)
        table=_table(connection)
        existing=connection.execute(select(table).where(table.c.candidate_id==candidate.candidate_id)).mappings().one_or_none()
        row={'candidate_id':candidate.candidate_id,'metadata_sha256':hashlib.sha256(content).hexdigest(),'metadata_json':content.decode('utf-8')}
        if existing is not None:
            if dict(existing)!=row:raise CloudMigrationUnavailable(_SAFE)
        else:connection.execute(insert(table).values(**row))


def read_generation_metadata(engine,candidate):
    """启动只读固定云端描述，不安装schema、补历史或退回本地。"""
    try:
        with control_image.image_transaction(engine) as connection:
            return _read_metadata(connection,candidate)
    except CloudMigrationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 启动只给安全错误，不泄露私有SQL/路径
        raise CloudMigrationUnavailable(_SAFE) from None


def _read_metadata(connection,candidate):
    table=_table(connection)
    row=connection.execute(select(table).where(table.c.candidate_id==candidate.candidate_id)).mappings().one()
    content=row['metadata_json'].encode('utf-8')
    if hashlib.sha256(content).hexdigest()!=row['metadata_sha256']:raise CloudMigrationUnavailable(_SAFE)
    body=_decode(content)
    if (body['schema_version']!=_SCHEMA or body['migration_id']!=candidate.migration_id
        or body['generation']!=candidate.generation or body['source_cut_sha256']!=candidate.source_cut_sha256
        or body['component_sha256s']!=candidate.component_sha256s):raise CloudMigrationUnavailable(_SAFE)
    evidence_manifest._check_manifest(body['asset_manifest'])
    expected={'assets':_sha({'manifest':body['asset_manifest'],'vector_index':body['vector_index']}),
              'catalog':_sha({'registry':body['registry_payload'],'snapshots':body['snapshot_pointers'],'eligibility':body['retrieval_eligibility']}),
              'governance':body['main_sha256'],'qa_tasks':body['main_sha256'],'budget':body['budget_sha256'],
              'acquisition':body['acquisition_sha256']}
    identity=body['source_identity']
    if (expected!=candidate.component_sha256s or _sha(identity)!=candidate.source_cut_sha256
            or body['source_scope']!=identity['version']
            or body['main_table_names']!=identity['registration']['main_table_names']
            or body['main_sha256']!=identity['main'] or body['budget_sha256']!=identity['budget']):
        raise CloudMigrationUnavailable(_SAFE)
    if body['source_scope'] == 'SERVER_UNIFIED_CUT_V1':
        # 原生闭包随代次保存并重核；不能只把合成scope改成生产字符串。
        closure = body['source_closure']
        authorization = closure['authorization']
        source_manifest = closure['source_manifest_sha256']
        activation._digest(source_manifest)
        activation._uuid(identity['registration']['instance'])
        if (closure['version'] != 'SERVER_COMPLETE_CLOSURE_V1'
                or closure['components'] != sorted(activation.COMPONENTS)
                or _sha(closure) != identity['closure']
                or authorization['archive_private_files'] is not True
                or authorization['source_manifest_sha256'] != source_manifest
                or authorization['projects'] != identity['registration']['authorized_project_ids']
                or len(set(authorization['projects'])) != len(authorization['projects'])
                or _sha(authorization) != identity['registration']['authorization_scope']
                or identity['registration']['writer_inventory'] != 'SERVER_ACCESS_GATE_AND_SOURCE_RECHECK_V1'
                or identity['registration']['writer_evidence'] != [['NATIVE_SOURCE_CAPTURE', source_manifest]]
                or closure['asset_manifest_id'] != body['asset_manifest']['manifest_id']
                or body['asset_manifest']['schema_version'] != evidence_manifest.SERVER_SCHEMA):
            raise CloudMigrationUnavailable(_SAFE)
    return body


@dataclass(frozen=True)
class CloudPublicationReceipt:
    active: object
    asset_count: int
    vector_count: int
    metadata_sha256: str
    state: str='CLOUD_GENERATION_PUBLISHED'


def publish_cut(cut,target_engine,main_tables,budget_tables,*,migration_id,generation,documents,corpus,
                object_store,vector_store,staging_root,expected_active_sha256,native_source=None):
    """单一受控入口：许可/身份→控制迁入→资产与向量实读→六组件SQL发布。

    合成范围收据保留source_scope，不能作为真实服务器生产代次的启动授权。
    """
    from archspec.persistence import acquisition_state, budget, snapshot_catalog, source_registry
    from archspec.persistence.chunked import ChunkedObjectStore
    from archspec.persistence.qdrant import QdrantStore
    try:
        if not isinstance(object_store,ChunkedObjectStore) or not isinstance(vector_store,QdrantStore) or vector_store.dimension!=512:
            raise CloudMigrationUnavailable(_SAFE)
        # 固定源和调用参数先核验，避免错误输入也产生云端写入。
        activation._generation(generation)
        activation._uuid(migration_id)
        if expected_active_sha256 is not None:activation._digest(expected_active_sha256)
        if cut.source_scope == 'SERVER_UNIFIED_CUT_V1':
            from archspec.persistence.source_capture import ServerSourceCapture
            if type(native_source) is not ServerSourceCapture:
                raise CloudMigrationUnavailable(_SAFE)
            native_source.validate_cut(cut)
        elif native_source is not None:
            raise CloudMigrationUnavailable(_SAFE)
        source_identity=_source_identity(cut)
        points,payload,pointers,state,profiles=_prepare(cut,corpus,documents)
        # 网络、编码和原件完整回读都在SQL事务外；固定源仍由协调器保持冻结。
        controls=import_cut_controls(cut,target_engine,main_tables,budget_tables,migration_id=migration_id)
        object_store.store.health_check()
        root=evidence_manifest._root(staging_root);root.mkdir(parents=True,exist_ok=True)
        work=root/('.cloud-publication-'+uuid4().hex);work.mkdir()
        manifest=_stage_assets(cut,work)
        evidence_manifest.publish_manifest(work,manifest,object_store)
        restored=evidence_manifest.restore_manifest(manifest,work/'verified-assets',object_store)
        if restored['manifest_id']!=manifest['manifest_id']:raise CloudMigrationUnavailable(_SAFE)
        _write_verify_vectors(points,vector_store,cut.source_cut_sha256)
        index=_vector_artifacts(points,corpus,object_store,work,vector_store.collection)
        # 远程传输完成后重核实际源许可及档案；不以初次允许掩盖后来撤销。
        refreshed,_,_,_,new_profiles=_prepare(cut,corpus,documents)
        if refreshed!=points or new_profiles!=profiles:raise CloudMigrationUnavailable(_SAFE)
        with (native_source.publication_guard() if native_source is not None else nullcontext()):
            component_hashes={'assets':_sha({'manifest':manifest,'vector_index':index}),
                'catalog':_sha({'registry':payload,'snapshots':pointers,'eligibility':profiles['__eligibility__']}),
                'governance':cut.main_image.sha256,'qa_tasks':cut.main_image.sha256,
                'budget':cut.budget_image.sha256,'acquisition':_sha(state)}
            candidate=activation._candidate(migration_id=migration_id,generation=generation,
                source_cut_sha256=cut.source_cut_sha256,component_sha256s=component_hashes)
            body={'schema_version':_SCHEMA,'source_scope':cut.source_scope,'migration_id':migration_id,'generation':generation,
                'source_cut_sha256':cut.source_cut_sha256,'component_sha256s':component_hashes,
                'asset_manifest':manifest,'vector_index':index,'registry_payload':payload,'snapshot_pointers':pointers,
                'retrieval_eligibility':profiles['__eligibility__'],
                'main_table_names':sorted(t.name for t in main_tables),'main_sha256':controls.main_sha256,
                'budget_sha256':controls.budget_sha256,'acquisition_sha256':_sha(state),'source_identity':source_identity}
            if cut.source_scope == 'SERVER_UNIFIED_CUT_V1':
                body['source_closure'] = _decode(cut.closure_json.encode('utf-8'))
            _record_metadata(target_engine,body,candidate)
            activation.install_schema(target_engine)
            def verify_sql(connection,selected,component):
                if selected!=candidate or _read_metadata(connection,selected)!=body:raise CloudMigrationUnavailable(_SAFE)
                if component in {'governance','qa_tasks'}:
                    control_image.verify_control_image(connection,cut.main_image,list(main_tables))
                elif component=='catalog':
                    _,events=source_registry._state(connection)
                    control,rows,heads,_=snapshot_catalog._state(connection)
                    actual=[{key:rows[head['revision']-1][key] for key in ('source_id','snapshot_id','snapshot_sha256','document_sha256')} for head in heads.values()]
                    if (events[-1]['payload_json']!=source_registry._json(payload)
                        or any(events[0][k]!=body[k] or control[k]!=body[k] for k in ('migration_id','source_cut_sha256'))
                        or sorted(actual,key=lambda p:p['source_id'])!=pointers):raise CloudMigrationUnavailable(_SAFE)
                elif component=='acquisition':
                    _,row,actual=acquisition_state._read(connection)
                    if actual!=state or any(row[k]!=body[k] for k in ('migration_id','source_cut_sha256')):raise CloudMigrationUnavailable(_SAFE)
                elif component=='budget':
                    if cut.legacy_budget is None:control_image.verify_control_image(connection,cut.budget_image,list(budget_tables))
                    else:
                        limit,calls=budget._state(connection)
                        if limit!=cut.legacy_budget.limit_cents or calls!=cut.legacy_budget.history_records:raise CloudMigrationUnavailable(_SAFE)
                return True
            def verify_generation(connection,selected,receipts):
                if len(receipts)!=len(activation.COMPONENTS):raise CloudMigrationUnavailable(_SAFE)
                for component in activation.COMPONENTS:verify_sql(connection,selected,component)
                return True
            ledger=activation.SQLActivationLedger(target_engine,verify_component=verify_sql,verify_generation=verify_generation)
            # 未知提交的重读分支只在完整实读与SQL核验以后返回旧收据，不重复发布。
            try:active=ledger.read_active()
            except activation.ActivationUnavailable:active=None
            if active is not None and active.candidate_id==candidate.candidate_id:
                with control_image.image_transaction(target_engine) as connection:
                    for component in activation.COMPONENTS:verify_sql(connection,candidate,component)
            else:
                selected=ledger.stage_candidate(migration_id=migration_id,generation=generation,
                    source_cut_sha256=cut.source_cut_sha256,component_sha256s=component_hashes)
                for component in activation.COMPONENTS:ledger.register_component(selected.candidate_id,component)
                active=ledger.publish(selected.candidate_id,expected_active_sha256=expected_active_sha256)
                if ledger.read_active()!=active:raise CloudMigrationUnavailable(_SAFE)
            return CloudPublicationReceipt(active,len(manifest['entries']),len(points),_sha(body))
    except CloudMigrationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 私有控制内容、提供方密钥/响应或路径不得泄露
        raise CloudMigrationUnavailable(_SAFE) from None