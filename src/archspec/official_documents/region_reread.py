"""已有固定疑点的本机局部重读，不更正正文，不提高核验状态。"""

import json
import re
import threading
from importlib.metadata import version
from math import isfinite

import numpy as np
import pymupdf

from archspec.official_documents.ocr import LOADED_SOURCE_IDENTITY as OCR_IDENTITY
from archspec.official_documents.processing_cache import publish_json, stable_hash
from archspec.official_documents.runtime_identity import frozen_source_identity

_REGION_LOCK = threading.Lock()


class RegionRereadBusy(Exception):
    """本机只运行一个局部任务，忙时由界面保留原页并提供重试。"""


def _recognize(pixels):
    # 复用已安装的本机引擎，绝不发送原图或文字给外部模型。
    from archspec.official_documents.ocr import _engine
    result = _engine()(pixels)
    if result.txts is None:
        return []
    return [{"text": str(text), "score": float(score), "box": box.tolist()}
            for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True)]


def _candidates(rows, pix, target):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("局部识别行数无效")
    candidates = []
    for row in rows:
        text, score, points = row.get("text"), row.get("score"), row.get("box")
        if (not isinstance(text, str) or not 1 <= len(text) <= 5000
                or type(score) not in {int, float} or not isfinite(score) or not 0 <= score <= 1
                or not isinstance(points, list) or len(points) != 4
                or any(not isinstance(p, list) or len(p) != 2
                       or any(type(v) not in {int, float} or not isfinite(v) for v in p) for p in points)):
            raise ValueError("局部识别文字或位置无效")
        box = pymupdf.Rect(min(p[0] for p in points), min(p[1] for p in points),
                           max(p[0] for p in points), max(p[1] for p in points))
        if box.is_empty or not pymupdf.Rect(0, 0, pix.width, pix.height).contains(box):
            raise ValueError("局部识别位置超出图片")
        original_box = pymupdf.Rect((box.x0 + pix.x) / 3, (box.y0 + pix.y) / 3,
                                    (box.x1 + pix.x) / 3, (box.y1 + pix.y) / 3)
        # 边距内读到的旁边正文不能用来替代目标疑字，必须与原疑点有真实交集。
        if (original_box & target).is_empty:
            continue
        candidates.append({"text": text, "score": score, "pdf_box": list(original_box)})
    if len(candidates) > 20 or sum(len(row["text"]) for row in candidates) > 20000:
        raise ValueError("局部候选超出展示上限")
    return candidates


def _load(path, identity):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 128000:
        raise ValueError("局部重读缓存无法安全读取，旧记录未覆盖")
    try:
        envelope = json.loads(path.read_bytes())
        result = envelope["result"]
        if (envelope["identity"] != identity or envelope["result_sha256"] != stable_hash(result)
                or result["reread_id"] != stable_hash(identity)
                or any(result[key] != identity[key] for key in
                       ("source_id", "snapshot_id", "pdf_page", "chunk_id", "row_index"))
                or result["original"] != identity["original"] or result["clip"] != identity["clip"]
                or result["status"] not in {"SAME", "DIFFERENT", "NO_TEXT"}
                or result["review_status"] != "PENDING_ORIGINAL_CONFIRMATION"):
            raise ValueError("缓存对照身份不一致")
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("局部重读缓存损坏，旧记录未覆盖") from error
    return result


def reread_issue(service, source_id, snapshot_id, page_number, chunk_id, row_index, *, recognize=None):
    if type(row_index) is not int or not 0 <= row_index <= 100000:
        raise ValueError("识别行编号无效")
    if not _REGION_LOCK.acquire(blocking=False):
        raise RegionRereadBusy("另一个局部重读正在运行，请稍后重试")
    try:
        layout = service.page_layout(source_id, snapshot_id, page_number, chunk_id)
        risk = layout["recognition"]
        issue = next((r for r in risk["issues"] if r["row_index"] == row_index), None)
        if risk["scope"] != "CURRENT_FRAGMENT" or not issue or issue["box"] is None:
            raise ValueError("未取得当前条文疑点的可靠位置，请对照整页")
        # 原页接口已验证两种历史编号；内部统一为来源限定编号，复用不能跨书。
        canonical_chunk = f"{source_id}:{snapshot_id}:{chunk_id.rsplit(':', 1)[1]}"
        with service._fixed_page(source_id, snapshot_id, page_number) as (record, page):
            service._check_read(source_id, snapshot_id, "LOCAL_EXCERPT")
            raw = record["pages"][page_number - 1]["raw_lines"][row_index]
            if raw["text"] != issue["text"] or raw["score"] != issue["score"]:
                raise ValueError("识别记录已变化，请重新定位")
            x0, y0, x1, y1 = issue["box"]
            target = pymupdf.Rect(x0 * page.rect.width, y0 * page.rect.height,
                                  x1 * page.rect.width, y1 * page.rect.height)
            clip = pymupdf.Rect(target.x0 - 4, target.y0 - 4, target.x1 + 4, target.y1 + 4) & page.rect
            raster = (clip * pymupdf.Matrix(3, 3)).irect
            if clip.is_empty or raster.width * raster.height > 2_000_000:
                raise ValueError("疑点区域过大，无法安全进行局部重读")
            identity = {"source_id": source_id, "snapshot_id": snapshot_id, "pdf_page": page_number,
                "chunk_id": canonical_chunk, "row_index": row_index, "snapshot_fingerprint": stable_hash(record),
                "file_sha256": record["sha256"], "original": {"text": raw["text"], "score": raw["score"]},
                "clip": list(clip), "scale": 3, "max_pixels": 2_000_000,
                "processor": [LOADED_SOURCE_IDENTITY.sha256, OCR_IDENTITY.sha256, version("rapidocr")]}
            folder = service.cache_dir.parent / "official-region-rereads"
            if folder.is_symlink():
                raise ValueError("局部重读目录不是项目内独立目录")
            path = folder / (stable_hash(identity) + ".json")
            result = _load(path, identity)
            cache_hit = result is not None
            if result is None:
                try:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=clip, alpha=False)
                    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                    candidates = _candidates((recognize or _recognize)(pixels), pix, target)
                except (ValueError, RuntimeError, TypeError, AttributeError, KeyError) as error:
                    raise ValueError("局部识别失败，原文与原页保留，可重试") from error
                same = re.sub(r"\s+", "", "\n".join(row["text"] for row in candidates)) == re.sub(r"\s+", "", raw["text"])
                result = {key: identity[key] for key in ("source_id", "snapshot_id", "pdf_page", "chunk_id", "row_index")}
                result.update(reread_id=stable_hash(identity), original=identity["original"], candidates=candidates,
                    clip=list(clip), status="NO_TEXT" if not candidates else "SAME" if same else "DIFFERENT",
                    review_status="PENDING_ORIGINAL_CONFIRMATION")
        # 识别期间权限或原件可能变化，复用也不能绕过原页和文字的即时门槛。
        with service._fixed_page(source_id, snapshot_id, page_number) as (current, _):
            service._check_read(source_id, snapshot_id, "LOCAL_EXCERPT")
            if stable_hash(current) != identity["snapshot_fingerprint"]:
                raise ValueError("固定正文读取期间发生变化，未发布重读结果")
        if not cache_hit:
            publish_json(path, {"identity": identity, "result": result, "result_sha256": stable_hash(result)})
        return {**result, "chunk_id": chunk_id, "cache_hit": cache_hit}
    finally:
        _REGION_LOCK.release()


LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
