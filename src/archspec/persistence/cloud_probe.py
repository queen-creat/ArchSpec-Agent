"""独立免费验证进程：复用存储适配器，绝不创建业务库或声称旧源已迁移。"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from archspec.persistence.chunked import ChunkedNotFound, ChunkedObjectStore

PREFIX = 'archspec/cloud-probe/v1'
COLLECTION = 'archspec_cloud_probe_v1'
ALLOWED_HOSTS = {'zjj.sm.gov.cn', 'www.mohurd.gov.cn', 'zrzyhgh.yichang.gov.cn', 'www.yichang.gov.cn'}


@contextmanager
def configured_targets(environment):
    # 由平台fromService注入旧服务已有配置；不读取旧盘，不回显或导出密钥。
    from archspec.persistence import config, postgres
    from archspec.persistence.qdrant import QdrantStore
    env = dict(environment, SUPABASE_PREFIX=PREFIX)
    pg = postgres.load_postgres_settings(env)
    storage = config.load_supabase_settings(env)
    vectors = config.load_qdrant_settings(env)
    if pg is None or storage is None or vectors is None:
        raise ValueError('缺少既有后台配置')
    with ExitStack() as resources:
        database = postgres.PostgresStore(pg)
        resources.callback(database.close)
        objects = config._supabase_objects(storage)
        resources.callback(objects.close)
        index = QdrantStore(vectors.qdrant_url, vectors.qdrant_api_key, COLLECTION, 512)
        resources.callback(index.close)
        yield database, objects, index


def download_public(asset, destination):
    # 仅访问构建时固定的已核政府原址；跳转不自动跟随，拒绝新增第三方地址。
    with (httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client,
          client.stream('GET', asset['url']) as response):
        response.raise_for_status()
        total = 0
        with destination.open('xb') as output:
            for data in response.iter_bytes(1024 * 1024):
                total += len(data)
                if total > asset['size_bytes']:
                    raise ValueError('官方附件超出已登记字节数')
                output.write(data)


def _check_resource_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError('编码器返回数量不符')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != 512
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)
                or not math.isclose(math.hypot(*vector),1.0,rel_tol=2e-5,abs_tol=2e-6)):
            raise ValueError('编码器维度、数值或归一化身份不符')


def _long_resource_documents():
    return ['明确合成长文本仅用于计算资源验收没有规范事实和工程数值。'*32+'明确合成编号'+str(i) for i in range(32)]


def measure_long_embedding_batch(model_dir, *, provider_factory=None):
    """仅构建容器运行的生产入口满长度批次；不联网、不读业务库、不更换模型。"""
    from archspec.embeddings.provider import PinnedFastEmbedProvider
    started = time.perf_counter()
    provider = (provider_factory or PinnedFastEmbedProvider)(model_dir)
    texts = _long_resource_documents()
    tokens = None
    if provider_factory is None:
        from archspec.official_documents.semantic_experiment import offline_guard
        with offline_guard():
            tokens = provider._pinned._get_model().token_count(texts[0])
            if tokens < 512:
                raise ValueError('未覆盖满长度编码输入')
            vectors = provider.embed_documents(texts)
    else:
        vectors = provider.embed_documents(texts)
    _check_resource_vectors(vectors, 32)
    peak = None
    if sys.platform.startswith('linux'):
        import resource
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)*1024
    return {'execution':'EXPLICIT_FAKE_PROVIDER' if provider_factory else 'PINNED_LOCAL_CPU_BUILD',
            'workload':'32_DISTINCT_EXPLICIT_SYNTHETIC_LONG_DOCUMENTS',
            'model_fingerprint':provider.model_fingerprint,'dimension':512,'vectors_verified':32,
            'document_batch_size':getattr(provider,'document_batch_size',32),
            'sdk_token_count_first_document':tokens,'kernel_peak_rss_bytes':peak,
            'process_fits_free_512MiB':None if peak is None else peak < 512*1024*1024,
            'elapsed_seconds':round(time.perf_counter()-started,3),'full_website_proven':False}


def verify_embedding_resources(model_dir, index, *, provider_factory=None, workload="short"):
    """复用固定CPU提供者；仅明确合成文字，全部向量校验后在隔离集合签收。"""
    from archspec.embeddings.provider import PinnedFastEmbedProvider
    started = time.perf_counter()
    provider = (provider_factory or PinnedFastEmbedProvider)(model_dir)
    fingerprint = provider.model_fingerprint
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise ValueError('固定编码器指纹无效')
    if workload not in {'short','long_batch'}:
        raise ValueError('资源核验只接受固定工作负载')
    texts = ['明确合成资源测试文本甲，无规范事实或工程数值。',
             '明确合成资源测试文本乙，仅测试本地编码与存储。', '明确合成资源测试查询。']
    if workload == 'long_batch':
        texts = _long_resource_documents()+[texts[-1]]
    tokens = None
    if provider_factory is None:
        # 只有专用验证服务使用全进程离线护栏；不在实际网站请求中全局禁网。
        from archspec.official_documents.semantic_experiment import offline_guard
        with offline_guard():
            if workload == 'long_batch':
                tokens = provider._pinned._get_model().token_count(texts[0])
                if tokens < 512:
                    raise ValueError('未覆盖满长度资源输入')
            vectors = provider.embed_documents(texts[:-1]) + [provider.embed_query(texts[-1])]
    else:
        vectors = provider.embed_documents(texts[:-1]) + [provider.embed_query(texts[-1])]
    _check_resource_vectors(vectors, len(texts))
    text_hashes = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
    cut = hashlib.sha256(json.dumps(['RESOURCE_PROBE_V1', fingerprint, text_hashes]).encode()).hexdigest()
    points = [{'id': str(uuid5(NAMESPACE_URL, cut+':'+str(n))), 'vector': vector,
               'payload': {'source_id': 'ARCHSPEC_RESOURCE_PROBE', 'snapshot_id': cut,
                   'chunk_id': 'EXPLICIT_SYNTHETIC_RESOURCE_'+str(n), 'model_fingerprint': fingerprint,
                   'region': 'PROBE_ONLY', 'migration_cut_sha256': cut,
                   'text_sha256': text_hashes[n], 'is_storage_probe': True}}
              for n, vector in enumerate(vectors)]
    index.upsert(points)
    rows = {row['id']: row for row in index.retrieve_points([p['id'] for p in points])}
    if set(rows) != {p['id'] for p in points}:
        raise ValueError('真实编码向量回读不完整')
    for point in points:
        row = rows[point['id']]
        if (row['payload'] != point['payload'] or len(row['vector']) != 512
                or any(not math.isclose(a,b,rel_tol=2e-5,abs_tol=2e-6)
                       for a,b in zip(row['vector'],point['vector'],strict=True))):
            raise ValueError('真实编码向量身份或坐标回读不符')
    if index.count_points(migration_cut_sha256=cut) != len(texts):
        raise ValueError('真实编码向量精确计数不符')
    # Linux内核进程峰值不是Render分钟采样；不以此证明完整网站或OCR内存通过。
    peak = None
    if sys.platform.startswith('linux'):
        import resource
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    cgroup = {}
    for label, name in [('peak_bytes','memory.peak'), ('limit_bytes','memory.max')]:
        try:
            cgroup[label] = int((Path('/sys/fs/cgroup')/name).read_text().strip())
        except (OSError, ValueError):
            cgroup[label] = None
    return {'execution': 'EXPLICIT_FAKE_PROVIDER' if provider_factory else 'PINNED_LOCAL_CPU',
            'input_kind': 'EXPLICIT_SYNTHETIC_NON_NORMATIVE_TEXT', 'model_fingerprint': fingerprint,
            'dimension': 512, 'vectors_verified': len(texts), 'workload': workload,
            'document_batch_size':getattr(provider,'document_batch_size',32),
            'sdk_token_count_first_document':tokens, 'kernel_peak_rss_bytes': peak, 'cgroup_memory': cgroup,
            'elapsed_seconds': round(time.perf_counter()-started,3)}


class CloudProbe:
    def __init__(self, environment, assets, work, *, fetch=download_public, targets=configured_targets, embedding_check=None):
        if not isinstance(assets, list) or not 1 <= len(assets) <= 2:
            raise ValueError('只核固定公开原件，最多两份')
        for asset in assets:
            parsed = urlsplit(asset['url'])
            if (set(asset) != {'source_id', 'url', 'sha256', 'size_bytes'}
                    or parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
                    or parsed.username or parsed.password or parsed.fragment
                    or parsed.port not in {None, 443}
                    or not re.fullmatch(r'[a-f0-9]{64}', asset['sha256'])
                    or type(asset['size_bytes']) is not int or not 1 <= asset['size_bytes'] <= 8*1024*1024):
                raise ValueError('公开原件身份或预算无效')
        if len({a['sha256'] for a in assets}) != len(assets):
            raise ValueError('原件不能重复')
        self.environment, self.assets, self.work = dict(environment), copy.deepcopy(assets), Path(work)
        self.fetch, self.targets = fetch, targets
        self.embedding_check = embedding_check
        self.lock = RLock()
        self.state = {'state': 'WAITING', 'phase': 'PRECHECK', 'assets': [],
            'stage_one_migration_complete': False, 'old_histories_restored': False,
            'notice': '本页仅验证真实云端保存与恢复，不能替代旧站完整历史迁移。'}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def change(self, **values):
        with self.lock:
            self.state.update(values)

    def run(self):
        try:
            self.change(state='RUNNING', phase='CHECKING_DATABASE_READONLY')
            self.work.mkdir(parents=True, exist_ok=False)
            with self.targets(self.environment) as (database, raw, index):
                database.probe_connection()  # 只SELECT 1；不建表、不读写旧控制记录。
                self.change(phase='CHECKING_PRIVATE_BUCKET', database='READONLY_VERIFIED')
                raw.health_check()
                chunks = ChunkedObjectStore(raw, self.work/'parts')
                results = []
                for number, asset in enumerate(self.assets):
                    key = 'public-originals/'+asset['sha256']+'.pdf'
                    restored = self.work/('restored-'+str(number)+'.pdf')
                    self.change(phase='RESTORING_PUBLIC_ORIGINAL')
                    try:
                        chunks.download_file(key, restored, asset['sha256'], expected_size=asset['size_bytes'])
                    except ChunkedNotFound:
                        source = self.work/('download-'+str(number)+'.pdf')
                        self.change(phase='DOWNLOADING_VERIFIED_PUBLIC_ORIGINAL')
                        self.fetch(asset, source)
                        with source.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if source.stat().st_size != asset['size_bytes'] or digest != asset['sha256']:
                            raise ValueError('原件指纹不符，未上传')
                        self.change(phase='UPLOADING_IMMUTABLE_PARTS')
                        chunks.put_file(key, source, asset['sha256'], content_type='application/pdf')
                        chunks.download_file(key, restored, asset['sha256'], expected_size=asset['size_bytes'])
                    results.append({'source_id': asset['source_id'], 'sha256': asset['sha256'],
                        'size_bytes': asset['size_bytes'], 'parts': (asset['size_bytes']+chunks.part_size-1)//chunks.part_size})
                    self.change(assets=copy.deepcopy(results))
                self.change(phase='VERIFYING_ISOLATED_VECTOR_WRITE')
                # 明确合成单点只在独立探测集合，绝不混入正式规范索引或伪称真实编码。
                identity = hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()
                point = {'id': str(uuid5(NAMESPACE_URL, 'archspec-cloud-probe-v1:'+identity)),
                    'vector': [1.0]+[0.0]*511, 'payload': {'source_id': 'ARCHSPEC_STORAGE_PROBE',
                        'snapshot_id': identity, 'chunk_id': 'EXPLICIT_SYNTHETIC_NOT_EVIDENCE',
                        'model_fingerprint': 'EXPLICIT_SYNTHETIC_NO_REAL_EMBEDDING', 'region': 'PROBE_ONLY',
                        'migration_cut_sha256': identity, 'is_storage_probe': True}}
                self.change(phase="ENSURING_ISOLATED_COLLECTION")
                index.ensure_collection(prepare_filter_indexes=True)
                self.change(phase="WRITING_ISOLATED_VECTOR", vector_collection="VERIFIED")
                index.upsert([point])
                self.change(phase='READING_ISOLATED_VECTOR', vector_write='COMPLETED')
                rows = index.retrieve_points([point['id']])
                if (len(rows) != 1 or rows[0]['id'] != point['id'] or rows[0]['payload'] != point['payload']
                        or rows[0]['vector'] != point['vector']):
                    raise ValueError('向量实际回读身份不符')
                self.change(phase='COUNTING_ISOLATED_VECTOR', vector_readback='IDENTITY_AND_BYTES_VERIFIED')
                if index.count_points(migration_cut_sha256=identity) != 1:
                    raise ValueError('向量精确计数不符')
                self.change(vector_storage='ROUNDTRIP_VERIFIED_512_EXPLICIT_PROBE')
                if self.embedding_check is not None:
                    self.change(phase='VERIFYING_REAL_EMBEDDING_RESOURCES')
                    self.change(embedding_resources=self.embedding_check(index))
            self.change(state='COMPLETE', phase='FINISHED')
        except Exception as failure:  # noqa: BLE001 - 后台边界不得泄露提供方响应或密钥
            # 任意SDK响应/DSN/路径均不回显；保留失败阶段，不以连接成功冒称资产已恢复。
            # 仅输出项目固定枚举，不能回显SDK响应、异常文字或任意错误码。
            code = getattr(failure, 'code', None)
            safe_code = code if code in {'UNAVAILABLE', 'ACCESS_DENIED', 'INCOMPATIBLE', 'MISSING'} else 'UNAVAILABLE'
            self.change(state='FAILED', error_code=safe_code, error='本阶段未完成；旧站与正式迁移状态未改变。')


def make_handler(probe, username, password):
    if not username or not password or ':' in username:
        raise ValueError('验证服务必须配置现有登录账号与密码')
    expected = b'Basic '+base64.b64encode((username+':'+password).encode('utf-8'))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                body = b'{"status":"running"}'
                self.send_response(200)
            else:
                supplied = self.headers.get('Authorization', '').encode('utf-8')
                if not hmac.compare_digest(supplied, expected):
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="ArchSpec cloud check", charset="UTF-8"')
                    self.end_headers()
                    return
                if self.path not in {'/', '/status'}:
                    self.send_response(404);self.end_headers();return
                body = json.dumps(probe.snapshot(), ensure_ascii=False).encode('utf-8')
                self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self, *_):
            return  # 不记录请求头、用户账号或后台凭据。
    return Handler


def main():
    if sys.argv[1:] == ['--measure-long-batch']:
        report = measure_long_embedding_batch(Path(os.environ['ARCHSPEC_EMBEDDING_MODEL_DIR']))
        print(json.dumps(report,ensure_ascii=False),flush=True)
        return
    assets = json.loads(Path('/app/official-originals.json').read_text(encoding='utf-8'))
    from tempfile import mkdtemp
    parent = Path(mkdtemp(prefix='archspec-cloud-probe-'))
    embedding_check = None
    if os.getenv('ARCHSPEC_VALIDATE_EMBEDDING_RESOURCES', '0') == '1':
        def embedding_check(index):
            if os.getenv('ARCHSPEC_RESOURCE_FULL_SITE','0') == '1':
                from archspec.persistence.site_resource_probe import verify_site_resources
                return verify_site_resources(parent/'site', Path(os.environ['ARCHSPEC_EMBEDDING_MODEL_DIR']), index)
            return verify_embedding_resources(Path(os.environ['ARCHSPEC_EMBEDDING_MODEL_DIR']), index,
                workload=os.getenv('ARCHSPEC_RESOURCE_WORKLOAD','short'))
    probe = CloudProbe(os.environ, assets, parent/'run', embedding_check=embedding_check)
    handler = make_handler(probe, os.getenv('ARCHSPEC_SERVER_USER'), os.getenv('ARCHSPEC_SERVER_PASSWORD'))
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), handler)
    Thread(target=probe.run, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
