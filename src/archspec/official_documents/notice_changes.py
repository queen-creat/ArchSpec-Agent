"""本机冻结公告中的跨书调整线索；只投影观察，不创建或替代核验事件。"""

import json
import re
from uuid import UUID

from archspec.official_discovery.public_queries import extract_public_codes, normalize_standard_code
from archspec.official_documents.processing_cache import stable_hash

DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def saved_notice_changes(cache_dir, fixed):
    """有界查已保存档案，精确含年份编号匹配；不下载、不扫描问题或原件全文。"""
    target = normalize_standard_code(fixed['standard_code'])
    items, seen, scanned, skipped, limited = [], set(), 0, 0, False
    root = cache_dir / 'source-metadata' / 'observations'
    # 本机观察范围不能冒充全网完整变更记录，也不暗示当前云端档案已遍历。
    if target and re.search(r'-\d{4}$', target) and root.exists():
        for path in root.glob('*.json'):
            if not DIGEST.fullmatch(path.stem):
                continue
            if scanned >= 500:
                limited = True
                break
            scanned += 1
            try:
                if path.stat().st_size > 256 * 1024:
                    skipped += 1
                    continue
                data = json.loads(path.read_bytes())
                if (not isinstance(data, dict) or data.get('metadata_id') != path.stem
                        or stable_hash({k: v for k, v in data.items() if k != 'metadata_id'}) != path.stem
                        or data.get('schema_version') != 'FIXED_SOURCE_V1'):
                    skipped += 1
                    continue
                observation = data.get('notice_observation') or {}
                notice = observation.get('notice') or {}
                if not notice:
                    continue
                if (str(UUID(observation['run_id'])) != observation['run_id']
                        or not DIGEST.fullmatch(observation['run_sha256'])
                        or not DIGEST.fullmatch(notice['text_sha256'])
                        or notice.get('url') != data['source_url']
                        or notice.get('standard_code') != data['standard_code']
                        or notice.get('standard_title') != data['title']
                        or not any(a.get('url') == data['attachment_url'] for a in notice.get('attachments', []))):
                    skipped += 1
                    continue
                evidence = notice.get('change_evidence')
                scope = notice.get('change_scope')
                if not isinstance(evidence, str) or scope not in {'PARTIAL_CLAUSES', 'FULL_STANDARD'}:
                    continue
                visible = evidence[:16000]
                if target not in extract_public_codes(visible):
                    continue
                identity = (notice['url'], notice['text_sha256'])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(items) >= 10:
                    limited = True
                    continue
                items.append({'notice_metadata_id': path.stem, 'run_id': observation['run_id'],
                    'notice_sha256': notice['text_sha256'], 'observed_source_id': data['source_id'],
                    'observed_snapshot_id': data['snapshot_id'],
                    'title': notice.get('title') or f"《{data['title']}》对应已保存公告", 'url': notice['url'],
                    'change_scope': scope, 'target_code': target, 'evidence': visible,
                    'excerpt_truncated': len(evidence) > len(visible) or notice.get('change_evidence_truncated') is True,
                    'effective_at_observed': data['fields']['effective_at']['value']})
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                skipped += 1
    return {'schema_version': 'SAVED_NOTICE_CHANGES_V1', 'source_id': fixed['source_id'],
        'snapshot_id': fixed['snapshot_id'], 'snapshot_fingerprint': fixed['snapshot_fingerprint'],
        'standard_code': fixed['standard_code'], 'scope': 'LOCAL_SAVED_METADATA_ONLY',
        'version_status': 'NOT_VERIFIED', 'scanned': scanned, 'skipped': skipped,
        'limited': limited, 'items': items,
        'notice': '仅核对本机已保存公告的精确编号线索；没有命中不代表没有调整。公告摘录不替代完整原文、现行性、具体条款或工程适用核验。'}
