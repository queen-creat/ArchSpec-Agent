"""同一源切面到整批控制库迁入；未取得资产/向量证明时不发布活跃代次。"""
from __future__ import annotations

import hashlib
import io
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateSchema

_SAFE = '整批控制记录迁入失败或提交结果未知，未启用云端或重置旧状态'
_MAX_JSON = 8 * 1024 * 1024


class ControlMigrationUnavailable(RuntimeError):
    """失败仅携带固定说明；源继续冻结，成功恢复须另外读取实际目标。"""


@contextmanager
def installation_scope(engine, connection, own_transaction, *, pg_lock):
    """原安装保留独立事务；组合迁移借用真实外层连接，只拥有自己的savepoint。"""
    if connection is None:
        with own_transaction(engine, write=True) as selected:
            yield selected
        return
    if (not isinstance(connection, Connection) or connection.closed
            or connection.engine is not engine or not connection.in_transaction()
            or connection.get_transaction() is None
            or not connection.get_transaction().is_active):
        raise ControlMigrationUnavailable(_SAFE)
    if connection.dialect.name == 'sqlite':
        if not connection.connection.driver_connection.in_transaction:
            raise ControlMigrationUnavailable(_SAFE)
    elif connection.dialect.name == 'postgresql':
        # 锁和超时仍用原组件合同；不让共享事务放松并发及持久提交要求。
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL synchronous_commit = 'on'")
        connection.execute(text('SELECT pg_catalog.pg_advisory_xact_lock(:lock)'), {'lock':pg_lock})
    else:
        raise ControlMigrationUnavailable(_SAFE)
    with connection.begin_nested():
        yield connection


def _json_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _decode(content):
    if not isinstance(content, bytes) or len(content) > _MAX_JSON:
        raise ControlMigrationUnavailable(_SAFE)
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise ControlMigrationUnavailable(_SAFE)
            result[key]=value
        return result
    def invalid(_):
        raise ControlMigrationUnavailable(_SAFE)
    return json.loads(content,object_pairs_hook=pairs,parse_constant=invalid)


class _BoundedBuffer(io.BytesIO):
    def write(self, value):
        if self.tell()+len(value) > _MAX_JSON:
            raise ControlMigrationUnavailable(_SAFE)
        return super().write(value)


def _inputs(cut):
    from archspec.official_documents.registry import SOURCES
    from archspec.persistence import acquisition_state, evidence_manifest, source_registry
    raw=dict(cut.external.files)
    manifest=_decode(raw['asset-manifest.json'])
    entries={e['path']:e for e in evidence_manifest._check_manifest(manifest)}
    def asset(path):
        entry=entries.get(path)
        if entry is None:
            raise ControlMigrationUnavailable(_SAFE)
        if cut.external.assets is None:
            content=raw[path]
        else:
            buffer=_BoundedBuffer()
            cut.external.assets.copy_to(path,buffer)
            content=buffer.getvalue()
        if len(content)!=entry['size_bytes'] or hashlib.sha256(content).hexdigest()!=entry['sha256']:
            raise ControlMigrationUnavailable(_SAFE)
        return content
    # 小资产原型也核每个实际字节，不能只相信登记hash。
    if cut.external.assets is None:
        for path,entry in entries.items():
            content=raw.get(path)
            if content is None or len(content)!=entry['size_bytes'] or hashlib.sha256(content).hexdigest()!=entry['sha256']:
                raise ControlMigrationUnavailable(_SAFE)
    payload=_decode(raw['sources.json'])
    source_registry._payload(payload)
    known=set(SOURCES)|{row['source_id'] for row in payload['sources']}
    jobs={}; batches={}
    for path,content in raw.items():
        target=jobs if path.startswith('jobs/') else batches if path.startswith('batches/') else None
        if target is not None:
            value=_decode(content)
            if path.split('/')[1] != value['id']+'.json' or value['id'] in target:
                raise ControlMigrationUnavailable(_SAFE)
            target[value['id']]=value
    state=acquisition_state._validate({'jobs':jobs,'batches':batches})
    runtime=_decode(cut.external.runtime_json.encode('utf-8'))
    if (runtime['jobs']!=jobs or runtime['batches']!=batches or runtime['worker_alive']
            or runtime['worker_error'] is not None):
        raise ControlMigrationUnavailable(_SAFE)
    pointers=[]
    for path in sorted(entries):
        if re.fullmatch(r'official-cache/(?:gb55031|official-[a-f0-9]{32})/latest\.json', path):
            current=_decode(asset(path)); source=path.split('/')[1]
            snapshot=current['snapshot_id']
            if source not in known or current['source_id'] != source:
                raise ControlMigrationUnavailable(_SAFE)
            fixed_path='official-cache/'+source+'/'+snapshot+'.json'
            fixed=asset(fixed_path)
            if _decode(fixed)!=current:
                raise ControlMigrationUnavailable(_SAFE)
            document=current.get('sha256',current.get('file_sha256'))
            candidates=('official-cache/'+source+'/'+str(document)+'.pdf',
                        'official-cache/content-store/blobs/'+str(document)+'.pdf')
            if not any(p in entries and entries[p]['sha256']==document for p in candidates):
                raise ControlMigrationUnavailable(_SAFE)
            pointers.append({'source_id':source,'snapshot_id':snapshot,
                             'snapshot_sha256':hashlib.sha256(fixed).hexdigest(),'document_sha256':document})
    for job in jobs.values():
        if job['source_id'] not in known:
            raise ControlMigrationUnavailable(_SAFE)
        if job['state']=='READY':
            fixed=_decode(asset('official-cache/'+job['source_id']+'/'+job['snapshot_id']+'.json'))
            if fixed['source_id'] != job['source_id'] or fixed['snapshot_id'] != job['snapshot_id']:
                raise ControlMigrationUnavailable(_SAFE)
    from archspec.persistence import snapshot_catalog
    snapshot_catalog._initial(pointers)
    return payload,pointers,state


@dataclass(frozen=True)
class ControlMigrationReceipt:
    migration_id: str
    source_cut_sha256: str
    main_sha256: str
    budget_sha256: str
    source_count: int
    snapshot_count: int
    job_count: int
    batch_count: int
    state: str = 'CONTROL_IMPORTED_UNPUBLISHED'


def import_cut_controls(cut, target_engine, main_tables, budget_tables, *, migration_id):
    """从已冻结实际切面导入；所有业务行、来源、快照、准备和预算一个事务提交。

    返回仅表示控制状态迁入，资产/向量和默认REMOTE仍须整阶段实际核验。
    """
    from archspec.persistence import (
        acquisition_state,
        budget,
        control_image,
        snapshot_catalog,
        source_registry,
        sql_namespace,
    )
    from archspec.persistence.source_cut import SourceCut
    try:
        if (type(cut) is not SourceCut or not isinstance(migration_id,str)
                or str(UUID(migration_id))!=migration_id
                or re.fullmatch('[a-f0-9]{64}',cut.source_cut_sha256) is None
                or tuple(sorted(t.name for t in main_tables))!=cut.registration.main_table_names
                or tuple(sorted(t.name for t in budget_tables))!=cut.registration.budget_table_names):
            raise ControlMigrationUnavailable(_SAFE)
        payload,pointers,state=_inputs(cut)
        if cut.legacy_budget is not None and (cut.legacy_budget.source_image != cut.budget_image
                or cut.legacy_budget.migration_id != migration_id or cut.legacy_budget.activation_blockers):
            raise ControlMigrationUnavailable(_SAFE)
        originals = None
        if cut.legacy_budget is not None:
            from archspec.persistence import legacy_budget_source
            entries = {e['name']: e for e in cut.budget_image.to_dict()['tables']}
            originals = [{column.name: control_image._decode(value)
                for column, value in zip(legacy_budget_source._CALLS.columns, row, strict=True)}
                for row in entries[legacy_budget_source._CALLS.name]['rows']]
            # 捕获候选已是新格式；安装入口仍收旧格式，按原记录重新核同一转换结果。
            converted = budget._history(originals, migration_id)
            if (converted != cut.legacy_budget.history_records
                    or budget._migration_manifest(migration_id, cut.legacy_budget.limit_cents, converted)
                    != cut.legacy_budget.migration_manifest_sha256):
                raise ControlMigrationUnavailable(_SAFE)
        with control_image.image_transaction(target_engine,write=True) as connection:
            if connection.dialect.name=='postgresql':
                connection.execute(CreateSchema(sql_namespace.PG_SCHEMA,if_not_exists=True))
            physical=sql_namespace.bind_tables(connection,list(main_tables))
            # 只安装明确登记的完整业务表，不改public、不重建来源历史或预算额度。
            physical[0].metadata.create_all(connection,tables=physical)
            control_image.import_control_image(connection,cut.main_image,list(main_tables))
            source_registry.install_schema(target_engine,initial_payload=payload,migration_id=migration_id,
                source_cut_sha256=cut.source_cut_sha256,connection=connection)
            snapshot_catalog.install_schema(target_engine,initial_pointers=pointers,migration_id=migration_id,
                source_cut_sha256=cut.source_cut_sha256,connection=connection)
            from archspec.persistence.snapshot_publication import (
                install_schema as install_publications,
            )
            install_publications(target_engine,connection=connection)
            from archspec.persistence.business_files import install_schema as install_business_files
            install_business_files(target_engine,connection=connection,migration_id=migration_id,
                source_cut_sha256=cut.source_cut_sha256)
            acquisition_state.install_schema(target_engine,initial_state=state,migration_id=migration_id,
                source_cut_sha256=cut.source_cut_sha256,connection=connection)
            if cut.legacy_budget is not None:
                budget.install_budget_schema(target_engine,legacy_records=originals,
                    limit_cents=cut.legacy_budget.limit_cents,migration_id=migration_id,connection=connection)
            else:
                physical_budget=sql_namespace.bind_tables(connection,list(budget_tables))
                physical_budget[0].metadata.create_all(connection,tables=physical_budget)
                control_image.import_control_image(connection,cut.budget_image,list(budget_tables))
            # 原任务已迁入后，在同一事务显式安装运行租约；服务器启动只读验证。
            if {"design_projects", "qa_tasks", "qa_operations"}.issubset({t.name for t in main_tables}):
                from archspec.official_qa.task_leases import install_lease_schema
                install_lease_schema(target_engine, connection=connection)
            # 最后仍在同一外层事务重核全量原记录及组件状态，晚期失败整批回滚。
            control_image.verify_control_image(connection,cut.main_image,list(main_tables))
            if cut.legacy_budget is None:
                control_image.verify_control_image(connection,cut.budget_image,list(budget_tables))
            source_registry._state(connection)
            snapshot_catalog._state(connection)
            acquisition_state._read(connection)
            limit, calls = budget._state(connection)
            if cut.legacy_budget is not None and (limit != cut.legacy_budget.limit_cents
                    or calls != cut.legacy_budget.history_records):
                raise ControlMigrationUnavailable(_SAFE)
        return ControlMigrationReceipt(migration_id,cut.source_cut_sha256,cut.main_image.sha256,
            cut.budget_image.sha256,len(payload['sources'])+1,len(pointers),len(state['jobs']),len(state['batches']))
    except ControlMigrationUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 不传播SQL、任务问题、来源路径或驱动原文
        raise ControlMigrationUnavailable(_SAFE) from None