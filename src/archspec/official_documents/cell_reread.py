"""固定表格的漏字格本机补读，独立保存候选，不替换正文或核验结果。"""

import json
from importlib.metadata import version
from math import isfinite

from archspec.official_documents.cell_pixels import LOADED_SOURCE_IDENTITY as CROP_IDENTITY
from archspec.official_documents.cell_pixels import cell_clip, crop_cell
from archspec.official_documents.ocr import LOADED_SOURCE_IDENTITY as OCR_IDENTITY
from archspec.official_documents.processing_cache import publish_json, stable_hash
from archspec.official_documents.runtime_identity import frozen_source_identity
from archspec.official_documents.table_cells import unread_ink_cells


def _recognize(pixels):
    from rapidocr.main import RapidOCRError

    from archspec.official_documents.ocr import _engine
    # 直接使用同一本机识别器，不切换共享引擎的use_det等可变配置。
    try:
        result = _engine().recognize_txt([pixels])
    except RapidOCRError as error:
        raise ValueError("单元格识别未返回有效文字") from error
    if result.txts is None:
        return []
    return [{"text": str(text), "score": float(score)}
            for text, score in zip(result.txts, result.scores, strict=True)]


def _validate(rows):
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError("单元格补读候选无效")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"text", "score"}
                or not isinstance(row["text"], str) or not 1 <= len(row["text"]) <= 500
                or type(row["score"]) not in {int, float} or not isfinite(row["score"])
                or not 0 <= row["score"] <= 1):
            raise ValueError("单元格补读文字无效")
    return rows


def reread_cell(service, source_id, snapshot_id, page_number, chunk_id, table_id, row, column, *, recognize=None):
    if (not isinstance(table_id, str) or type(row) is not int or type(column) is not int
            or not 1 <= row <= 40 or not 1 <= column <= 40):
        raise ValueError("表格位置无效")
    # 布局入口已核对固定快照、条文范围和原页/文字许可；浏览器不能提交任意裁剪框。
    layout = service.page_layout(source_id, snapshot_id, page_number, chunk_id)
    table = next((t for t in layout['tables'] if t['id'] == table_id), None)
    if table is None:
        raise LookupError("当前原页没有此表格")
    canonical_chunk = f"{source_id}:{snapshot_id}:{chunk_id.rsplit(':', 1)[1]}"
    with service._fixed_page(source_id, snapshot_id, page_number) as (record, page):
        service._check_read(source_id, snapshot_id, "LOCAL_EXCERPT")
        cell = next((c for c in unread_ink_cells(page, table.get('reading_grid'))
                     if c['row'] == row and c['column'] == column), None)
        if cell is None:
            raise ValueError("此格没有已定位的漏字字迹，请对照原页")
        if page.rotation:
            raise ValueError("旋转原页暂不支持单元格补读，请对照原图")
        clip, _ = cell_clip(page, cell['box'])
        identity = {'source_id': source_id, 'snapshot_id': snapshot_id, 'pdf_page': page_number,
            'chunk_id': canonical_chunk, 'table_id': table_id, 'cell': cell,
            'snapshot_fingerprint': stable_hash(record), 'file_sha256': record['sha256'],
            'clip': list(clip), 'scale': 6, 'max_pixels': 1_000_000,
            'processor': [LOADED_SOURCE_IDENTITY.sha256, OCR_IDENTITY.sha256, CROP_IDENTITY.sha256, version('rapidocr')]}
        folder = service.cache_dir.parent / 'official-cell-rereads'
        if folder.is_symlink():
            raise ValueError("单元格补读目录不是项目内独立目录")
        path = folder / (stable_hash(identity) + '.json')
        result = None
        if path.exists():
            try:
                if path.is_symlink() or path.stat().st_size > 128000:
                    raise ValueError('unsafe cache')
                saved = json.loads(path.read_bytes()); result = saved['result']
                if (saved['identity'] != identity or saved['result_sha256'] != stable_hash(result)
                        or result['reread_id'] != stable_hash(identity)
                        or any(result[k] != identity[k] for k in identity)
                        or result['review_status'] != 'PENDING_ORIGINAL_CONFIRMATION'
                        or result['is_citable'] is not False
                        or result['status'] != ('CANDIDATE' if result['candidates'] else 'NO_TEXT')):
                    raise ValueError('identity mismatch')
                _validate(result['candidates'])
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError("单元格补读缓存损坏，旧记录未覆盖") from error
        cache_hit = result is not None
        if result is None:
            crop = crop_cell(page, clip)
            pixels = crop['pixels']
            try:
                candidates = _validate((recognize or _recognize)(pixels))
            except (ValueError, RuntimeError, TypeError, AttributeError, KeyError) as error:
                raise ValueError("单元格补读失败，原页保留，可重试") from error
            result = {**identity, 'reread_id': stable_hash(identity), 'candidates': candidates,
                'tight_pdf_box': crop['tight_pdf_box'],
                'padding_pixels': crop['padding_pixels'], 'status': 'CANDIDATE' if candidates else 'NO_TEXT',
                'review_status': 'PENDING_ORIGINAL_CONFIRMATION', 'is_citable': False}
    # 重读或复用均重新核权限与固定身份；不把计算前的许可沿用到输出。
    with service._fixed_page(source_id, snapshot_id, page_number) as (current, _):
        service._check_read(source_id, snapshot_id, 'LOCAL_EXCERPT')
        if stable_hash(current) != identity['snapshot_fingerprint']:
            raise ValueError("固定正文读取期间发生变化，未发布补读结果")
    if not cache_hit:
        publish_json(path, {'identity': identity, 'result': result, 'result_sha256': stable_hash(result)})
    return {**result, 'chunk_id': chunk_id, 'cache_hit': cache_hit}


LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
