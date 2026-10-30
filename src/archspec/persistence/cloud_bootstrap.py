"""固定资料包私有接收与原生迁移接线；不接受任意路径、SQL或凭据上传。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from threading import RLock, Thread
from uuid import UUID, uuid4

_PART = 4 * 1024 * 1024
_MAX = 512 * 1024 * 1024
_FIELDS = {'schema_version','archive_sha256','size_bytes','part_size','parts','max_expanded_bytes',
           'migration_id','generation','model_fingerprint','full_text_sources','total_sources','vector_count','corpus_key'}
_SAFE = '云端初始化未完成；未清空数据、重置预算或回退本地演示库'


def _digest(value):
    if not isinstance(value,str) or re.fullmatch('[a-f0-9]{64}',value) is None:
        raise ValueError('固定资料指纹无效')
    return value


def _manifest(value):
    if (not isinstance(value,dict) or set(value)!=_FIELDS or value['schema_version']!='ARCHSPEC_BOOTSTRAP_PACKAGE_V1'
            or value['part_size']!=_PART or type(value['size_bytes']) is not int
            or not 0<value['size_bytes']<=128*1024*1024 or value['max_expanded_bytes']!=_MAX
            or value['generation']!=1 or str(UUID(value['migration_id']))!=value['migration_id']
            or not isinstance(value['parts'],list) or not 1<=len(value['parts'])<=32):
        raise ValueError('固定资料清单无效')
    for name in ('archive_sha256','model_fingerprint','corpus_key'): _digest(value[name])
    total=0
    for i,part in enumerate(value['parts']):
        expected=min(_PART,value['size_bytes']-i*_PART)
        if (set(part)!={'index','size_bytes','sha256'} or type(part['index']) is not int or part['index']!=i
                or type(part['size_bytes']) is not int or part['size_bytes']!=expected or expected<=0):
            raise ValueError('资料块清单不连续')
        _digest(part['sha256']); total+=part['size_bytes']
    if total!=value['size_bytes']:raise ValueError('资料块长度不完整')
    for name in ('full_text_sources','total_sources','vector_count'):
        if type(value[name]) is not int or value[name]<=0:raise ValueError('资料数量无效')
    return value


def read_manifest(path):
    path=Path(path)
    if path.stat().st_size>64*1024:raise ValueError('资料清单过大')
    return _manifest(json.loads(path.read_text(encoding='utf-8')))


def _sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


class PackageReceiver:
    """逐块完整读回签收；服务重启后仍能从同一私有键恢复固定包。"""
    def __init__(self,manifest,work,store_factory):
        self.manifest=_manifest(manifest); self.work=Path(work); self.work.mkdir()
        self.store_factory=store_factory; self._received=set(); self._lock=RLock()

    def status(self):
        with self._lock:return {'received_parts':len(self._received),'total_parts':len(self.manifest['parts'])}

    def _part(self,index):
        if type(index) is not int or not 0<=index<len(self.manifest['parts']):raise ValueError('资料块编号不符')
        return self.manifest['parts'][index]

    def _key(self,index):return 'parts/'+str(index).zfill(4)

    def put_part(self,index,stream,length):
        part=self._part(index)
        if type(length) is not int or length!=part['size_bytes']:raise ValueError('资料块长度不符')
        run=self.work/uuid4().hex; run.mkdir(); path=run/'part'
        try:
            remaining=length; digest=hashlib.sha256()
            with path.open('xb') as output:
                while remaining:
                    block=stream.read(min(1024*1024,remaining))
                    if not isinstance(block,bytes) or not block or len(block)>remaining:raise ValueError('资料块未完整到达')
                    output.write(block); digest.update(block); remaining-=len(block)
            if digest.hexdigest()!=part['sha256']:raise ValueError('资料块指纹不符')
            with self.store_factory() as store:
                store.health_check()
                store.put_file(self._key(index),path,part['sha256'])
                store.download_file(self._key(index),run/'verified',part['sha256'],expected_size=length)
            with self._lock:self._received.add(index)
            return {'state':'PART_VERIFIED','index':index,**self.status()}
        finally:shutil.rmtree(run)

    def assemble(self,destination):
        destination=Path(destination); digest=hashlib.sha256(); size=0
        with destination.open('xb') as output, self.store_factory() as store:
            store.health_check()
            for part in self.manifest['parts']:
                path=self.work/('read-'+uuid4().hex)
                try:
                    store.download_file(self._key(part['index']),path,part['sha256'],expected_size=part['size_bytes'])
                    with path.open('rb') as source:
                        while block:=source.read(1024*1024):output.write(block);digest.update(block);size+=len(block)
                    with self._lock:self._received.add(part['index'])
                finally:path.unlink(missing_ok=True)
        if size!=self.manifest['size_bytes'] or digest.hexdigest()!=self.manifest['archive_sha256']:
            raise ValueError('完整资料包指纹不符')
        return destination


def _allowed(name,manifest):
    parts=PurePosixPath(name).parts
    if ('\\' in name or not parts or any(p in {'.','..'} or p.startswith('.') for p in parts)
            or PurePosixPath(name).is_absolute() or ':' in name or '/'.join(parts)!=name):return False
    if name=='prepared-vectors/'+manifest['corpus_key']+'.json':return True
    if name in {'source/archspec.db','source/model-budget.sqlite3'}:return True
    return (name.startswith(('source/official-cache/','source/official-discovery/'))
            and Path(name).suffix in {'.json','.pdf'} and re.fullmatch('[A-Za-z0-9_./-]+',name) is not None)


def unpack_package(archive,destination,manifest):
    """先预检整包，再排他解包；不覆盖任何旧目录和规范原件。"""
    manifest=_manifest(manifest); archive=Path(archive); destination=Path(destination)
    if archive.stat().st_size!=manifest['size_bytes'] or _sha(archive)!=manifest['archive_sha256']:
        raise ValueError('完整资料包身份不符')
    with zipfile.ZipFile(archive) as package:
        entries=package.infolist(); names=[item.filename for item in entries]
        if (not 1<=len(entries)<=20000 or len(set(names))!=len(names)
                or sum(item.file_size for item in entries)>manifest['max_expanded_bytes']):
            raise ValueError('资料包文件数量、重复成员或展开大小不符')
        for item in entries:
            mode=item.external_attr>>16
            if (not _allowed(item.filename,manifest) or item.is_dir() or item.flag_bits&1
                    or stat.S_IFMT(mode) not in {0,stat.S_IFREG}):
                raise ValueError('资料包存在未登记路径或链接')
        destination.mkdir()
        for item in entries:
            path=destination/item.filename; path.parent.mkdir(parents=True,exist_ok=True)
            with package.open(item) as source,path.open('xb') as output:shutil.copyfileobj(source,output,1024*1024)
    return destination


@contextmanager
def configured_private_parts(manifest):
    from archspec.persistence.config import _supabase_objects, load_supabase_settings
    settings=load_supabase_settings(dict(os.environ,SUPABASE_PREFIX='archspec/bootstrap/v1/'+manifest['archive_sha256']))
    if settings is None:raise ValueError(_SAFE)
    store=_supabase_objects(settings)
    try:yield store
    finally:store.close()

def read_cloud_audit(engine,metadata):
    """只读当前控制记录，输出数量和完整摘要；不返回私有原文或任何凭据。"""
    from collections import Counter

    from sqlalchemy import text

    from archspec.persistence import budget, control_image
    from archspec.persistence.activation import SQLActivationLedger
    from archspec.standard_governance.migration import read_cloud_governance_contract
    def encode(value):
        if isinstance(value,dict):return {key:encode(item) for key,item in value.items()}
        if isinstance(value,(list,tuple)):return [encode(item) for item in value]
        return control_image._encode(value)
    def digest(value):return hashlib.sha256(budget._json(value).encode('utf-8')).hexdigest()
    with control_image.image_transaction(engine) as connection:
        limit,calls=budget._state(connection)
        rows=sorted((dict(row) for row in calls),key=lambda row:row['call_token'])
        schema='archspec_control' if engine.dialect.name=='postgresql' else 'main'
        # 与安装、fresh恢复共用同一真实active/原metadata/独立安装回执合同。
        active=SQLActivationLedger(engine)._active(connection)
        names=read_cloud_governance_contract(connection,active=active,metadata=metadata)
        policies={}; count=0
        for name in names:
            # schema和表名均来自固定方言/已核表登记；不接收客户端SQL或名称。
            values=connection.execute(text('SELECT * FROM "'+schema+'"."'+name+'"')).mappings().all()
            policies[name]=sorted(budget._json(encode(dict(row))) for row in values)
            count+=len(values)
    return {'budget':{'limit_cents':limit,'call_count':len(rows),
            'reserved_cents':sum(row['reserved_cents'] for row in rows),
            'states':dict(Counter(row['status'] for row in rows)), 'sha256':digest({'limit':limit,'calls':rows})},
        'governance':{'row_count':count,'sha256':digest(policies)},
        'assets':{'count':len(metadata['asset_manifest']['entries']),'sha256':digest(metadata['asset_manifest'])},
        'vector_count':metadata['vector_index']['count'],'model_fingerprint':metadata['vector_index']['model_fingerprint'],
        'snapshot_count':len(metadata['snapshot_pointers'])}


_CANARY_NAME='archspec-cloud-permission-canary-v1.txt'
_CANARY_CONTENT=b'ARCHSPEC SELF-AUTHORED CLOUD PERMISSION CANARY V1. No normative facts. Access must be denied.\n'
_CANARY_ACTOR='archspec-cloud-permission-canary-v1'
_CANARY_NOTE='自写非规范文本，仅验证云端拒绝权限与重启恢复；不是规范审批或工程证据。'


def _canary_documents(engine):
    """只定位固定自写文件的题录，避免启动扫描/读取其他用户上传全文。"""
    from sqlalchemy import select

    from archspec.documents.models import DocumentRow
    from archspec.persistence import control_image, sql_namespace
    with control_image.image_transaction(engine) as connection:
        table=sql_namespace.bind_tables(connection,[DocumentRow.__table__])[0]
        rows=connection.execute(select(table.c.id,table.c.original_name,table.c.sha256,
            table.c.size_bytes,table.c.status).where(table.c.original_name==_CANARY_NAME)).mappings().all()
        return [dict(row) for row in rows]


def _canary_binding(engine,document_id):
    """仅只读固定自写文档的真实旧绑定；不通过重新POST来推断未知提交。"""
    from sqlalchemy import select

    from archspec.persistence import control_image, sql_namespace
    from archspec.standard_governance.models import LegacyBindingRow
    with control_image.image_transaction(engine) as connection:
        table=sql_namespace.bind_tables(connection,[LegacyBindingRow.__table__])[0]
        rows=connection.execute(select(table).where(table.c.legacy_kind=='DOCUMENT',
            table.c.legacy_key==document_id)).mappings().all()
        if len(rows)!=1:raise ValueError(_SAFE)
        return dict(rows[0])


def _verify_permission_canary(port,credential,admin_token,engine):
    """只走现有真实回环HTTP；首次自写一条REJECT，此后三次恢复全部只读。"""
    import http.client
    from datetime import UTC, datetime, timedelta

    if type(port) is not int or not 1<=port<=65535:raise ValueError(_SAFE)
    file_sha=hashlib.sha256(_CANARY_CONTENT).hexdigest()
    def request(method,path,*,payload=None,content=None,content_type=None,admin=False,expected=200):
        # 固定loopback主机/默认Host；不伪造XFF或公网Origin，也不重试任何POST。
        headers={'Authorization':'Basic '+credential}
        if payload is not None:
            content=json.dumps(payload,ensure_ascii=False).encode('utf-8');content_type='application/json'
        if content is not None:headers['Content-Type']=content_type
        if admin:headers['X-Archspec-Admin-Token']=admin_token
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=30)
        try:
            connection.request(method,path,body=content,headers=headers)
            response=connection.getresponse();body=response.read(1024*1024+1)
            if response.status!=expected or len(body)>1024*1024:raise ValueError(_SAFE)
            return json.loads(body)
        finally:connection.close()
    matches=_canary_documents(engine)
    if len(matches)>1:raise ValueError(_SAFE)
    created=not matches
    if created:
        # 固定自写几十字节TXT；没有模型、下载、规范审批或任何真实规范变更。
        boundary='archspec-canary-'+uuid4().hex
        content=(('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="'+_CANARY_NAME
            +'"\r\nContent-Type: text/plain\r\n\r\n').encode('ascii')+_CANARY_CONTENT
            +('\r\n--'+boundary+'--\r\n').encode('ascii'))
        document=request('POST','/api/documents',content=content,
            content_type='multipart/form-data; boundary='+boundary,expected=201)
    else:document=matches[0]
    document_id=str(UUID(document['id']))
    if (document.get('original_name')!=_CANARY_NAME or document.get('sha256')!=file_sha
            or document.get('size_bytes')!=len(_CANARY_CONTENT) or document.get('status')!='PARSED'):
        raise ValueError(_SAFE)
    if created:
        observed=request('POST','/api/standard-governance/sources/from-record',
            payload={'kind':'DOCUMENT','key':document_id},admin=True,expected=201)
        source_id=observed['source']['id'];artifact_id=observed['artifact']['id']
    # 已有marker缺来源或事件属于未知/不完整提交，停止而不再次写入。
    binding=_canary_binding(engine,document_id)
    if created and (binding['source_record_id']!=source_id or binding['artifact_binding_id']!=artifact_id):
        raise ValueError(_SAFE)
    source_id=binding['source_record_id'];artifact_id=binding['artifact_binding_id']
    source_id=str(UUID(source_id));artifact_id=str(UUID(artifact_id))
    source=request('GET','/api/standard-governance/sources/'+source_id)
    manifest_sha=_digest(source['manifest_sha256'])
    if source.get('file_sha256')!=file_sha or source.get('source_type')!='LOCAL_UPLOAD':raise ValueError(_SAFE)
    history_path='/api/standard-governance/subjects/SOURCE/'+source_id+'/verifications'
    history=request('GET',history_path)
    if created:
        if history:raise ValueError(_SAFE)
        now=datetime.now(UTC)
        request('POST','/api/standard-governance/verifications',payload={
            'subject_kind':'SOURCE','subject_id':source_id,'scope':'CONTENT_USE','decision':'REJECT',
            'expected_manifest_sha256':manifest_sha,'expected_head_id':None,
            'basis_source_record_id':source_id,'basis_note':_CANARY_NOTE,
            'checked_as_of':(now-timedelta(seconds=1)).isoformat(),
            'valid_until':(now+timedelta(days=365)).isoformat(),'statement':{}},admin=True,expected=201)
        history=request('GET',history_path)
    if (not isinstance(history,list) or len(history)!=1):raise ValueError(_SAFE)
    event=history[0]
    if (event.get('subject_kind')!='SOURCE' or event.get('subject_id')!=source_id
            or event.get('scope')!='CONTENT_USE' or event.get('decision')!='REJECT'
            or event.get('subject_manifest_sha256')!=manifest_sha or event.get('actor_id')!=_CANARY_ACTOR
            or event.get('basis_source_record_id')!=source_id or event.get('basis_note')!=_CANARY_NOTE
            or event.get('statement')!={}):raise ValueError(_SAFE)
    event_id=str(UUID(event['id']))
    # 不能只接受任意403：确认是原业务使用权限拒绝，而非登录/主机配置失败。
    for suffix in ('','/chunks'):
        denied=request('GET','/api/documents/'+document_id+suffix,expected=403)
        if denied.get('detail',{}).get('code')!='CONTENT_USE_NOT_AUTHORIZED':raise ValueError(_SAFE)
    return {'state':'VERIFIED','document_id':document_id,'source_id':source_id,'artifact_id':artifact_id,
        'event_id':event_id,'file_sha256':file_sha,'manifest_sha256':manifest_sha,
        'document_status':403,'chunks_status':403}


class BootstrapManager:
    """首次发布与已发布恢复分开；未知结果绝不视为空库再初始化。"""
    def __init__(self,manifest,work,*,store_factory=None,cloud_check=None,start_backend=None,publisher=None):
        self.manifest=_manifest(manifest); self.work=Path(work); self.work.mkdir()
        self._lock=RLock(); self.backend_port=None; self._backend_process=None; self._uploads=0; self._stopping=False; self._owned=[]
        self._state={'state':'CHECKING_CLOUD','stage':'PRECHECK','boot_id':str(uuid4()),
            'archive_sha256':manifest['archive_sha256'],'migration_id':manifest['migration_id']}
        self.receiver=PackageReceiver(manifest,self.work/'parts',store_factory or (lambda:configured_private_parts(manifest)))
        self._check=cloud_check or self._cloud_check
        self._backend=start_backend or self._start_backend
        self._publisher=publisher or self._publish_native

    def status(self):
        with self._lock:
            if (self._state['state']=='READY' and self._backend_process is not None
                    and self._backend_process.poll() is not None):
                self.backend_port=None
                self._state.update(state='FAILED',stage='REMOTE_PROCESS_EXITED',message=_SAFE)
            return {**self._state,**self.receiver.status()}

    def _change(self,**values):
        with self._lock:
            if not self._stopping:self._state.update(values)

    def _spawn(self,command,*,env,stdout):
        # 进程登记和停止判定共用短锁；不扫描PID或接管其他进程。
        with self._lock:
            if self._stopping:raise ValueError('服务正在停止，不再启动子进程')
            process=subprocess.Popen(command,env=env,stdout=stdout,stderr=stdout)
            self._owned.append(process)
            return process

    def close(self):
        with self._lock:
            self._stopping=True
            self._state.update(state='STOPPING',stage='OWNED_PROCESSES_SHUTDOWN')
            processes=list(self._owned)
        for process in processes:
            if process.poll() is None:
                try:process.terminate()
                except ProcessLookupError:pass
            try:process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=25)
            with self._lock:self._owned.remove(process)

    def initialize(self):
        with self._lock:
            if self._stopping:return
        try:
            if self._check():
                self._change(state='RESTORING_WEBSITE',stage='REMOTE_STARTUP')
                self.backend_port=self._backend(); self._change(state='READY',stage='REMOTE_PUBLISHED')
            else:self._change(state='WAITING_SOURCE',stage='PRIVATE_PACKAGE_UPLOAD')
        except Exception as failure: # noqa: BLE001 - 仅保留无原文的异常类别与包内帧。
            self._failed(failure)

    def put_part(self,index,stream,length):
        with self._lock:
            if self._state['state']!='WAITING_SOURCE':raise ValueError('当前阶段不接收初始化资料')
            self._uploads+=1
        try:return self.receiver.put_part(index,stream,length)
        finally:
            with self._lock:self._uploads-=1

    def publish(self):
        with self._lock:
            if self._state['state']!='WAITING_SOURCE' or self._uploads:raise ValueError('初始化已启动、上传在途或不可用，不重复捕获源')
            self._change(state='PUBLISHING',stage='VERIFY_PRIVATE_PACKAGE')
            Thread(target=self._complete,name='archspec-cloud-initialization',daemon=True).start()
        return self.status()

    def _complete(self):
        try:
            self._publisher(self.receiver,self.work,self._progress)
            self._change(state='RESTORING_WEBSITE',stage='REMOTE_STARTUP')
            self.backend_port=self._backend(); self._change(state='READY',stage='REMOTE_PUBLISHED')
        except Exception as failure: # noqa: BLE001 - 未知提交不重捕获、不重置预算。
            self._failed(failure)

    def _failed(self,failure):
        from archspec.persistence.migration_operator import safe_diagnostic
        self._change(state='FAILED',message=_SAFE,diagnostic=safe_diagnostic(failure))

    def _progress(self,value):self._change(stage=value)

    def _cloud_check(self):
        from sqlalchemy import text

        from archspec.persistence.postgres import (
            create_postgres_runtime_engine,
            load_postgres_settings,
        )
        settings=load_postgres_settings()
        if settings is None:raise ValueError(_SAFE)
        engine=create_postgres_runtime_engine(settings)
        try:
            with engine.connect() as connection:
                present=connection.execute(text("SELECT to_regclass('archspec_control.external_state_active')")).scalar_one()
                if present is None:
                    count=connection.execute(text("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='archspec_control'")).scalar_one()
                    if count:raise ValueError(_SAFE)
                    return False
            _active,metadata=self._published_generation(engine)
            self._change(cloud_audit=read_cloud_audit(engine,metadata))
            return True
        finally:engine.dispose()

    def _published_generation(self,engine):
        from archspec.persistence.activation import SQLActivationLedger
        from archspec.persistence.migration_flow import read_generation_metadata
        ledger=SQLActivationLedger(engine);active=ledger.read_active()
        if active.migration_id!=self.manifest['migration_id'] or active.generation!=self.manifest['generation']:
            raise ValueError(_SAFE)
        metadata=read_generation_metadata(engine,ledger.get_candidate(active.candidate_id))
        if (metadata['source_scope']!='SERVER_UNIFIED_CUT_V1'
                or metadata['vector_index']['count']!=self.manifest['vector_count']
                or metadata['vector_index']['model_fingerprint']!=self.manifest['model_fingerprint']):
            raise ValueError(_SAFE)
        return active,metadata

    def _prepare_governance(self,engine):
        from archspec.standard_governance.migration import install_cloud_governance
        # 部署固定显式开关，不因HTTP请求或缺表自动决定安装，更不导入/审批真实规范。
        if os.getenv('ARCHSPEC_CLOUD_INSTALL_GOVERNANCE')!='1':raise ValueError(_SAFE)
        active,metadata=self._published_generation(engine)
        before=read_cloud_audit(engine,metadata)
        installed=install_cloud_governance(engine,expected_active_sha256=active.activation_sha256)
        after=read_cloud_audit(engine,metadata)
        if after['budget']!=before['budget']:raise ValueError(_SAFE)
        self._change(governance_installation=installed,cloud_audit=after)
        return metadata

    def _publish_native(self,receiver,work,progress):
        archive=receiver.assemble(work/'initial-source.zip')
        progress('EXTRACT_VERIFIED_SOURCE')
        root=unpack_package(archive,work/'unpacked',self.manifest)
        plan={'schema_version':'SERVER_CLOUD_MIGRATION_PLAN_V1','migration_id':self.manifest['migration_id'],
            'generation':self.manifest['generation'],'expected_active_sha256':None,'authorized_project_ids':[],
            'archive_private_files':True,'exclusive_source_confirmed':True,
            'expected_model_fingerprint':self.manifest['model_fingerprint']}
        plan_path=work/'migration-plan.json'; plan_path.write_text(json.dumps(plan),encoding='utf-8')
        env=dict(os.environ,ARCHSPEC_PERSISTENCE_BACKEND='LOCAL',ARCHSPEC_DATA_DIR=str(root/'source'),
                 ARCHSPEC_MIGRATION_VECTOR_CACHE_DIR=str(root/'prepared-vectors'))
        env.pop('ARCHSPEC_DEMO_SEED_DIR',None)
        # 子进程不承载对外请求；沿用原生停写/捕获/事务发布，密钥仅从环境继承。
        output=work/'native-migration'
        with (work/'migration-process.log').open('xb') as log:
            process=self._spawn([sys.executable,'-B','-m','archspec.persistence.migration_operator',
                '--plan',str(plan_path),'--output-root',str(output)],env=env,stdout=log)
            while process.poll() is None:
                path=output/'progress.jsonl'
                if path.exists():
                    rows=path.read_text(encoding='utf-8').splitlines()
                    if rows:
                        stage=json.loads(rows[-1]).get('stage')
                        if stage in {'PRECHECK','MODEL','PREPARE','CAPTURE','PUBLISH'}:progress(stage)
                time.sleep(2)
        if process.returncode!=0 or not (output/'result.json').is_file():
            failure_path=output/'failure.json'
            if failure_path.is_file():
                failure=json.loads(failure_path.read_text(encoding='utf-8'))
                self._change(native_diagnostic=failure.get('diagnostic'),stage=failure.get('stage','PUBLISH'))
            raise ValueError(_SAFE)
        result=json.loads((output/'result.json').read_text(encoding='utf-8'))
        if (result['active']['migration_id']!=self.manifest['migration_id']
                or result['vector_count']!=self.manifest['vector_count']):raise ValueError(_SAFE)
        if not self._cloud_check():raise ValueError(_SAFE)
        self._change(publication={'asset_count':result['asset_count'],'vector_count':result['vector_count'],
            'activation_sha256':result['active']['activation_sha256']})

    def _start_backend(self):
        from archspec.persistence.postgres import (
            create_postgres_runtime_engine,
            load_postgres_settings,
        )
        settings=load_postgres_settings()
        if settings is None:raise ValueError(_SAFE)
        engine=create_postgres_runtime_engine(settings)
        try:
            self._progress('INSTALL_EXPLICIT_GOVERNANCE')
            metadata=self._prepare_governance(engine)
            # 安装已完成才进入恢复阶段；不能把后续等待一直显示成安装尚未结束。
            self._progress('RESTORE_REMOTE_BACKEND')
            return self._launch_backend(engine,metadata)
        finally:engine.dispose()

    def _launch_backend(self,engine,metadata):
        import base64
        import http.client
        import secrets
        directory=self.work/('remote-'+uuid4().hex); directory.mkdir()
        port=10001
        env=dict(os.environ,ARCHSPEC_PERSISTENCE_BACKEND='REMOTE',ARCHSPEC_DATA_DIR=str(directory),
                 ARCHSPEC_BOOTSTRAP_BACKEND_PORT=str(port))
        env.pop('ARCHSPEC_DEMO_SEED_DIR',None); env.pop('ARCHSPEC_MIGRATION_VECTOR_CACHE_DIR',None)
        token=secrets.token_urlsafe(32)
        env.update(STANDARD_VERIFICATION_ENABLED='true',STANDARD_ADMIN_TOKEN=token,STANDARD_ADMIN_ACTOR_ID=_CANARY_ACTOR)
        # 父进程不导入业务配置；REMOTE在新解释器完成全量云端回读后才开放网站。
        log=(self.work/'website-process.log').open('xb')
        try:
            process=self._spawn([sys.executable,'-B','-m','archspec.persistence.cloud_bootstrap','--serve-backend'],
                env=env,stdout=log)
        finally:log.close()
        self._backend_process=process
        credential=base64.b64encode((env['ARCHSPEC_SERVER_USER']+':'+env['ARCHSPEC_SERVER_PASSWORD']).encode()).decode()
        deadline=time.monotonic()+1800
        while process.poll() is None and time.monotonic()<deadline:
            ready=False
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            try:
                connection.request('GET','/api/storage/status',headers={'Authorization':'Basic '+credential})
                response=connection.getresponse(); body=response.read(65537)
                if response.status==200 and len(body)<=65536:
                    result=json.loads(body)
                    if result.get('active_backend')=='REMOTE' and result.get('data_migration')=='PUBLISHED':
                        ready=True
            except (OSError,ValueError,http.client.HTTPException):pass
            finally:connection.close()
            if ready:
                self._progress('VERIFY_PERSISTED_CONTENT_USE_REJECTION')
                canary=_verify_permission_canary(port,credential,token,engine)
                before=self.status()['cloud_audit'];after=read_cloud_audit(engine,metadata)
                if after['budget']!=before['budget']:raise ValueError(_SAFE)
                self._change(permission_canary=canary,cloud_audit=after)
                return port
            time.sleep(2)
        if process.poll() is None:process.terminate()
        path=self.work/'website-process.log'
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-65536))
            for line in stream.read().decode('utf-8',errors='replace').splitlines():
                try:record=json.loads(line)
                except ValueError:continue
                if isinstance(record,dict) and record.get('state')=='FAILED':
                    self._change(backend_diagnostic=record.get('diagnostic'))
        raise ValueError(_SAFE)


def main():
    if sys.argv[1:]==['--serve-backend']:
        import uvicorn

        from archspec.deployment import create_server_app
        try:
            uvicorn.run(create_server_app(bootstrap_seed=False),host='127.0.0.1',
                port=int(os.environ['ARCHSPEC_BOOTSTRAP_BACKEND_PORT']),workers=1,proxy_headers=True,forwarded_allow_ips='127.0.0.1')
        except Exception as failure: # noqa: BLE001 - 不向平台日志打印驱动和SQL原文。
            from archspec.persistence.migration_operator import safe_diagnostic
            print(json.dumps({'state':'FAILED','diagnostic':safe_diagnostic(failure)}),flush=True)
            raise SystemExit(1) from None
        return
    if sys.argv[1:]:raise ValueError('未知启动参数')
    from http.server import ThreadingHTTPServer

    from archspec.persistence.cloud_gateway import make_handler
    manifest=read_manifest('/app/bootstrap-package.json')
    directory=Path(tempfile.mkdtemp(prefix='archspec-cloud-site-'))/'bootstrap'
    manager=BootstrapManager(manifest,directory)
    server=ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','10000'))),
        make_handler(manager,os.getenv('ARCHSPEC_SERVER_USER'),os.getenv('ARCHSPEC_SERVER_PASSWORD')))
    import signal
    def stop(_number,_frame):
        # 在主线程跳出serve_forever，避免同线程shutdown死锁；finally回收所有自有子进程。
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    server.daemon_threads=True
    Thread(target=manager.initialize,name='archspec-cloud-startup',daemon=True).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        try:manager.close()
        finally:server.server_close()


if __name__=='__main__':main()
