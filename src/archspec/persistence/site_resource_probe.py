"""完整应用资源核验：独立合成资料根、真实HTTP/工人，绝不冒称旧历史迁移。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

_SAFE = '整站资源核验未完成；旧站和正式迁移状态未改变。'


@contextmanager
def _running_site(application):
    """绑定系统分配的回环端口，使用真实HTTP并负责收回本次服务线程。"""
    import httpx
    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port,
        loop='asyncio', lifespan='on', access_log=False, log_level='error',
        workers=1, proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]},
                              name='archspec-resource-http', daemon=True)
    try:
        thread.start()
        deadline = time.monotonic()+20
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise ValueError(_SAFE)
            time.sleep(0.01)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20,
                          follow_redirects=False, trust_env=False) as browser:
            yield browser
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise ValueError(_SAFE)


def verify_site_resources(work, model_dir, index, *, embedding_check=None):
    """必须在未导入业务配置的新进程执行；嵌入核验时保持应用lifespan驻留。"""
    if 'archspec.config' in sys.modules:
        raise ValueError('整站资源核验必须使用未导入业务配置的新进程')
    work = Path(work).absolute()
    from archspec.persistence.r2 import R2Store
    R2Store._safe_parents(work)
    work.mkdir(parents=True, exist_ok=False)
    account = ('resource_user', 'EXPLICIT_SYNTHETIC_RESOURCE_PASSWORD')
    environment = {'ARCHSPEC_DATA_DIR': str(work), 'ARCHSPEC_SERVER_USER': account[0],
        'ARCHSPEC_SERVER_PASSWORD': account[1], 'ARCHSPEC_PERSISTENCE_BACKEND': 'LOCAL',
        'ARCHSPEC_RUNTIME_OCR_ENABLED': 'false', 'ARCHSPEC_STORAGE_PREFLIGHT': 'false',
        'DEEPSEEK_API_KEY': '', 'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}
    started = time.perf_counter()
    database = None
    try:
        with patch.dict(os.environ, environment):
            import fitz

            from archspec.deployment import create_server_app
            server = create_server_app(bootstrap_seed=False)
            from archspec import database
            from archspec.official_documents.service import OfficialDocumentService
            with _running_site(server) as browser:
                if browser.get('/api/health').status_code != 200:
                    raise ValueError(_SAFE)
                if browser.get('/project-workbench.html').status_code != 401:
                    raise ValueError(_SAFE)
                page = browser.get('/project-workbench.html', auth=account)
                if page.status_code != 200:
                    raise ValueError(_SAFE)
                assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)))
                if not assets or any(browser.get(asset, auth=account).status_code != 200 for asset in assets):
                    raise ValueError(_SAFE)
                created = browser.post('/api/projects', auth=account, json={'name': '明确合成整站资源核验项目'})
                if created.status_code != 201:
                    raise ValueError(_SAFE)
                identifier = created.json()['id']
                saved = browser.get('/api/projects/'+identifier, auth=account)
                if saved.status_code != 200 or saved.json()['name'] != '明确合成整站资源核验项目':
                    raise ValueError(_SAFE)
                # 明确合成PDF只用于隔离资源场景；不上传原件、不作为任何工程事实。
                pdf = fitz.open()
                pdf.new_page().insert_text((72,72), '2.1.1 EXPLICIT SYNTHETIC SITE RESOURCE CLAUSE.')
                content = pdf.tobytes();pdf.close()
                documents = OfficialDocumentService(work/'official-cache', fetch=lambda url: content)
                documents.acquire('gb55031')
                snapshot = documents.get('gb55031')
                original = documents.page_image('gb55031',snapshot['snapshot_id'],1)
                search = browser.post('/api/official-documents/gb55031/search', auth=account, json={'query': '2.1.1'})
                if search.status_code != 200 or not search.json()['results']:
                    raise ValueError(_SAFE)
                if search.json()['results'][0]['snapshot_id'] != snapshot['snapshot_id']:
                    raise ValueError(_SAFE)
                image = browser.get('/api/official-documents/gb55031/snapshots/'+snapshot['snapshot_id']+'/pages/1.png', auth=account)
                if image.status_code != 200 or hashlib.sha256(image.content).digest() != hashlib.sha256(original).digest():
                    raise ValueError(_SAFE)
                worker = server.application.state.acquisition_service
                if worker._thread is None or not worker._thread.is_alive():
                    raise ValueError(_SAFE)
                if embedding_check is None:
                    from archspec.persistence.cloud_probe import verify_embedding_resources
                    result = verify_embedding_resources(model_dir,index,workload='long_batch')
                else:
                    result = embedding_check(model_dir,index)
                if browser.get('/api/projects/'+identifier,auth=account).status_code != 200:
                    raise ValueError(_SAFE)
                result['website_resources'] = {'execution': 'REAL_APPLICATION_ISOLATED_SYNTHETIC_RESOURCES',
                    'persistence_backend': 'LOCAL_RESOURCE_ONLY', 'transport': 'REAL_LOOPBACK_HTTP',
                    'project_roundtrip': True,
                    'synthetic_clause_hit': True, 'page_sha256_verified': True, 'assets_checked': len(assets),
                    'application_routes': len(server.application.routes), 'worker_lifespan_started': True,
                    'cloud_migration_complete': False, 'old_histories_restored': False,
                    'total_elapsed_seconds': round(time.perf_counter()-started,3)}
            if worker._thread.is_alive():
                raise ValueError(_SAFE)
            result['website_resources']['worker_lifespan_stopped'] = True
            return result
    except Exception:  # noqa: BLE001 - 验收状态不回显驱动、配置或提供方响应
        raise ValueError(_SAFE) from None
    finally:
        if database is not None:
            database.engine.dispose()


class _ExplicitBuildIndex:
    """只供构建CPU/RSS测量；不声称真实向量仓库或正式业务数据。"""
    def __init__(self):self.points = {}
    def upsert(self, points):self.points.update({p['id']:copy.deepcopy(p) for p in points})
    def retrieve_points(self, identifiers):return [copy.deepcopy(self.points[i]) for i in identifiers if i in self.points]
    def count_points(self, *, migration_cut_sha256):return sum(p['payload']['migration_cut_sha256']==migration_cut_sha256 for p in self.points.values())


def main():
    if sys.argv[1:] != ['--build-check']:
        raise ValueError('整站资源入口只接受固定构建验收命令')
    directory = Path(mkdtemp(prefix='archspec-site-resource-'))/'site'
    result = verify_site_resources(directory, Path(os.environ['ARCHSPEC_EMBEDDING_MODEL_DIR']), _ExplicitBuildIndex())
    result['website_resources']['index_backend'] = 'EXPLICIT_FAKE_BUILD_ONLY'
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if result['kernel_peak_rss_bytes'] >= 512*1024*1024:
        raise ValueError('构建实测已超过Free内存，拒绝替换当前验证实例')


if __name__ == '__main__':main()
