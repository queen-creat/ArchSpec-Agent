"""待审核正文检索：只定位候选文字，不形成合规结论。"""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from archspec.documents.models import DocumentChunkRecord
from archspec.official_documents.clause_units import CLAUSE, clause_runs
from archspec.official_documents.document_sections import (
    document_section,
    prefer_primary_clauses,
    section_rows,
)
from archspec.search.bm25 import BM25Searcher
from archspec.search.tokenizer import LexicalTokenizer

INDEX_VERSION = "official-lexical-index-v2"
CLAUSE_PATTERN = re.compile(r"\d+(?:\.\d+){2,3}")


def algorithm_identifiers():
    """成绩绑定实际排序、分词和BM25实现；改算法后旧门槛不能继续生效。"""
    from archspec.documents import chunker
    from archspec.official_documents import clause_units, document_sections
    from archspec.search import bm25, tokenizer

    files = [Path(__file__), Path(bm25.__file__), Path(tokenizer.__file__),
             Path(clause_units.__file__), Path(chunker.__file__), Path(document_sections.__file__)]
    fingerprint = hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()
    return {"V1": f"BM25_UNIFIED_V1:{fingerprint}", "V2": f"BM25_UNIFIED_V2:{fingerprint}"}


def _title_weight(chunk):
    """只降低明确目录、独立章标题；短正文、未知文字不能直接删除。"""
    text = chunk["text"].strip()
    if re.search(r"(?:^|\n)\s*目\s*录\s*(?:\n|$)", text):
        return 0.01
    if re.search(r"[.…·]{3,}\s*\d+\s*(?:\n|$)", text):
        return 0.01
    if chunk["clause_number"] and CLAUSE_PATTERN.fullmatch(chunk["clause_number"]):
        return 1.0
    if re.fullmatch(r"第[一二三四五六七八九十百\d]+[章节篇]\s*[^。；！？\n]{1,45}", text):
        return 0.01
    heading = chunk.get("heading_path")
    if heading and text == heading.strip() and len(text) <= 60:
        return 0.01
    if re.fullmatch(r"\d+(?:\.\d+)?\s+[^。；！？\n]{1,30}", text):
        return 0.01
    return 1.0


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _index(snapshot, index_dir):
    """派生索引只读已有文字；坏索引从文字重建，完全不进入PDF/OCR流程。"""
    identity = {"version": INDEX_VERSION, "algorithm": algorithm_identifiers()["V2"],
                "source_id": snapshot["source_id"],
                "snapshot_id": snapshot["snapshot_id"], "chunks": snapshot["chunks"]}
    key = hashlib.sha256(_serialize(identity).encode("utf-8")).hexdigest()
    path = Path(index_dir) / f"{key}.json" if index_dir is not None else None
    if path is not None:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            rows = saved["rows"]
            if (saved["key"] == key and len(rows) == len(snapshot["chunks"])
                    and saved["checksum"] == hashlib.sha256(_serialize(rows).encode()).hexdigest()
                    and all(isinstance(row["tokens"], dict)
                            and row["weight"] in {0.01, 1.0}
                            and all(isinstance(term, str) and type(count) is int and count > 0
                                    for term, count in row["tokens"].items()) for row in rows)):
                return rows
        except (OSError, ValueError, KeyError, TypeError):
            pass
    tokenizer = LexicalTokenizer()
    rows = [{"tokens": dict(Counter(tokenizer.tokenize(chunk["text"]))),
             "weight": _title_weight(chunk)} for chunk in snapshot["chunks"]]
    if path is not None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid4()}.tmp")
            payload = {"key": key, "version": INDEX_VERSION, "rows": rows,
                       "checksum": hashlib.sha256(_serialize(rows).encode()).hexdigest()}
            temporary.write_text(_serialize(payload), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # 派生缓存不能写不妨碍只读检索，不改原正文缓存。
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return rows


def _rank_v2(query, chunks, rows):
    query_tokens = set(LexicalTokenizer().tokenize(query))
    counters = [row["tokens"] for row in rows]
    lengths = [sum(counter.values()) for counter in counters]
    average_length = sum(lengths) / len(lengths) if lengths else 0
    if not query_tokens or not average_length:
        return []
    frequencies = Counter(term for counter in counters for term in counter)
    inverse = {term: math.log(1 + (len(chunks) - frequencies[term] + 0.5)
                              / (frequencies[term] + 0.5)) for term in query_tokens}
    hits = []
    for chunk, row, length in zip(chunks, rows, lengths, strict=True):
        score = 0
        for term in query_tokens:
            count = row["tokens"].get(term, 0)
            if count:
                score += inverse[term] * count * 2.5 / (count + 1.5 * (0.25 + 0.75 * length / average_length))
        if score > 0:
            hits.append((chunk, round(score * row["weight"], 6)))
    return sorted(hits, key=lambda hit: (-hit[1], hit[0].chunk_index))


def _unique_clauses(hits, origins, chunks):
    """完整条款合并展示，但保留全部续页与原片段；章节号、未知文字不合并。"""
    groups, keys, runs = {}, {}, {}
    for chunk in chunks:
        source = origins[chunk.id]
        identity = source["source_id"], source["snapshot_id"]
        if identity not in runs:
            runs[identity] = clause_runs(section_rows(source))
        index = int(chunk.id.rsplit(":", 1)[1])
        if index in runs[identity]:
            key = (*identity, runs[identity][index][0])
            keys[chunk.id] = key
            groups.setdefault(key, []).append(chunk)
    seen, result, fragments = set(), [], {}
    for chunk, score in hits:
        snapshot = origins[chunk.id]
        key = keys.get(chunk.id)
        if key not in groups:
            result.append((chunk, score))
            continue
        if key in seen:
            continue
        seen.add(key)
        parts = groups[key]
        if len(parts) > 1:
            fragments[chunk.id] = [{"id": part.id, "text": part.text,
                                     "source_id": snapshot["source_id"],
                                     "snapshot_id": snapshot["snapshot_id"],
                                     "clause_number": part.clause_number,
                                     "page_start": part.page_start, "page_end": part.page_end}
                                    for part in parts]
            text = "\n\n".join(dict.fromkeys(part.text for part in parts))
            chunk = replace(chunk, text=text, character_count=len(text),
                            page_start=min(part.page_start for part in parts),
                            page_end=max(part.page_end for part in parts))
        result.append((chunk, score))
    return result, fragments


def _result(snapshot, chunk, score):
    # 一段可能跨页，将所涉及页面的识别风险一并带给使用者。
    warnings = {"PENDING_VERSION_CHECK", "PENDING_USAGE_REVIEW"}
    for page in snapshot["pages"]:
        if chunk.page_start <= page["pdf_page"] <= chunk.page_end:
            warnings.update(page["warnings"])
    section = document_section(snapshot, chunk.page_start, chunk.page_end)
    return {
        **({"document_section": section} if section else {}),
        "id": chunk.id,
        "snapshot_id": snapshot["snapshot_id"],
        "source_id": snapshot["source_id"],
        "standard_code": snapshot["standard_code"],
        **({"source_format": "HTML", "page_semantics": "HTML_RENDERED"}
           if snapshot.get("source_format") == "HTML" else {}),
        "title": snapshot["title"],
        "text": chunk.text,
        "clause_number": chunk.clause_number,
        **({"page_count": snapshot["page_count"]} if "page_count" in snapshot else {}),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_url": snapshot["page_url"],
        "status": "PENDING_REVIEW",
        "warnings": sorted(warnings),
        "score": score,
    }


def _chunk(snapshot, index, chunk_id, order):
    chunk = snapshot["chunks"][index]
    return DocumentChunkRecord(
        id=chunk_id, document_id=snapshot["snapshot_id"], chunk_index=order,
        text=chunk["text"], page_start=chunk["page_start"], page_end=chunk["page_end"],
        clause_number=chunk["clause_number"], heading_path=chunk["heading_path"],
        character_count=len(chunk["text"]),
    )



def ranking_snapshots(snapshots, query):
    """逐来源核权后，仅同一固定公告与PDF省去重复排名，不合并其他发布背景。"""
    identities = [(row.get("source_id"), row.get("snapshot_id")) for row in snapshots]
    # 精确条号仍逐出处返回；重复或不明输入沿原验证链处理，不能借消重掩盖坏身份。
    if (CLAUSE.fullmatch(query) or any(not all(isinstance(value, str) for value in pair) for pair in identities)
            or len(set(identities)) != len(identities)):
        return snapshots
    selected, known = [], {}
    for snapshot in snapshots:
        hashes = tuple(snapshot.get(key) for key in ("sha256", "snapshot_id", "processing_payload_sha256"))
        code = snapshot.get("standard_code")
        if (snapshot.get("source_format", "PDF") != "PDF" or not isinstance(code, str)
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes)):
            selected.append(snapshot)
            continue
        # 同一附件也可能被不同公告再次发布；只有完整的共同发布身份
        # 才能省略另一个入口。缺公告证明时保留各自来源，不猜地域或效力。
        notice, page_url = snapshot.get("announcement_hash"), snapshot.get("page_url")
        if (not isinstance(notice, str) or not re.fullmatch(r"[a-f0-9]{64}", notice)
                or not isinstance(page_url, str) or not page_url.strip()):
            selected.append(snapshot)
            continue
        key = (*hashes, code, notice, page_url)
        previous = known.get(key)
        # 除原件/处理指纹外再核完整片段和页警告；相似题名、同条号、缺指纹都不够。
        if (previous is not None and previous["chunks"] == snapshot["chunks"]
                and [(page["pdf_page"], page["warnings"]) for page in previous["pages"]]
                == [(page["pdf_page"], page["warnings"]) for page in snapshot["pages"]]):
            continue
        known.setdefault(key, snapshot)
        selected.append(snapshot)
    return selected


def _diverse_reading_hits(hits, origins, preferred):
    """初读同级候选优先不同原文；别名后置但不删除，也不借用它的身份或权限。"""
    groups, representatives, priorities, deferred = {}, {}, {}, set()
    for chunk, score in hits:
        source = origins[chunk.id]
        original = source.get("sha256")
        code = source.get("standard_code")
        if (source.get("source_format", "PDF") == "PDF"
                and isinstance(original, str) and re.fullmatch(r"[a-f0-9]{64}", original)
                and isinstance(code, str) and code.strip()
                and isinstance(chunk.clause_number, str) and CLAUSE.fullmatch(chunk.clause_number)):
            # 同原文、页界、分数及阅读层级才比较发布证明；不提升较低分或借用别名身份。
            key = (chunk.id in preferred, original, code, chunk.clause_number,
                   chunk.page_start, chunk.page_end, chunk.text, score)
            groups[chunk.id] = key
            notice, page_url = source.get("announcement_hash"), source.get("page_url")
            bound = (isinstance(notice, str) and bool(re.fullmatch(r"[a-f0-9]{64}", notice))
                     and isinstance(page_url, str) and bool(page_url.strip()))
            previous = representatives.get(key)
            # 已绑定发布公告优先于缺证明入口；同等证明保持原排名，不推断现行性。
            if previous is None or (bound and not previous[1]):
                representatives[key] = (source["source_id"], bound)
    for chunk, _ in hits:
        key = groups.get(chunk.id)
        duplicate = key is not None and representatives[key][0] != origins[chunk.id]["source_id"]
        priorities[chunk.id] = (chunk.id not in preferred, duplicate)
        if duplicate:
            deferred.add(chunk.id)
    return sorted(hits, key=lambda hit: priorities[hit[0].id]), deferred


def search_snapshots(snapshots: list[dict], query: str, top_k: int,
                     ranking_version="V1", index_dir=None, lookup_plan=None) -> dict:
    """所有书共享一个语料集统计；不能直接比较逐本算出的分数。"""
    if ranking_version not in {"V1", "V2"}:
        raise ValueError("排序版本只能为V1或V2")
    snapshots = ranking_snapshots(snapshots, query)
    chunks, rows = [], []
    origins = {}
    for snapshot in snapshots:
        if ranking_version == "V2":
            rows.extend(_index(snapshot, index_dir))
        for index in range(len(snapshot["chunks"])):
            chunk_id = f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
            chunks.append(_chunk(snapshot, index, chunk_id, len(chunks)))
            origins[chunk_id] = snapshot
    if CLAUSE.fullmatch(query):
        hits = [(chunk, None) for chunk in chunks if chunk.clause_number == query]
        method = "EXACT_CLAUSE"
    elif ranking_version == "V2":
        hits = _rank_v2(query, chunks, rows)
        method = "BM25_UNIFIED_V2"
    else:
        hits = [(hit.chunk, hit.score) for hit in
                BM25Searcher(LexicalTokenizer()).search(query, chunks, len(chunks) if lookup_plan is not None else top_k)]
        method = "BM25_UNIFIED_V1"
    fragments = {}
    if ranking_version == "V2":
        hits, fragments = _unique_clauses(hits, origins, chunks)
    selection = None
    if lookup_plan is not None and not CLAUSE.fullmatch(query):
        from archspec.official_documents.lookup import (
            LookupPlan,
            _preferred_reading_indices,
            _role_reading_indices,
        )

        if not isinstance(lookup_plan, LookupPlan) or lookup_plan.parallel_queries:
            raise ValueError("单题读取择取需要本次共同计划")
        raw = hits[:top_k]
        raw_query = lookup_plan.question_plan.search_query
        # 概念检索只负责实际择取，不能把它的排名记成原问排名。
        # 对照仅重用本批已核来源的内存语料，不再读盘、下载或扩大初读数量。
        if raw_query != query:
            if ranking_version == "V2":
                raw, _ = _unique_clauses(_rank_v2(raw_query, chunks, rows), origins, chunks)
                raw = raw[:top_k]
            else:
                raw = [(hit.chunk, hit.score) for hit in
                       BM25Searcher(LexicalTokenizer()).search(raw_query, chunks, top_k)]
        # 同一固定快照只做一次角色扫描，关键词候选和补充候选复用结果。
        role_rows = [(snapshot, _role_reading_indices(lookup_plan, snapshot)) for snapshot in snapshots]
        preferred = {f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
                     for snapshot, indices in role_rows
                     for index in _preferred_reading_indices(lookup_plan, snapshot, role_indices=indices)}
        # 角色对应可找到原问无字面交集的正文；只补当前已授权语料的阅读候选。
        # 零分明确表示没有关键词分数，不能伪造BM25命中或修改raw_hits。
        alias_ids = {f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
                     for snapshot, indices in role_rows for index in indices}
        existing = {chunk.id for chunk, _ in hits}
        hits.extend((chunk, 0.0) for chunk in chunks if chunk.id in alias_ids and chunk.id not in existing)
        # 不改V1/V2分数，同组内保持原顺序；只有同一次检索的前五个择取结果进入初读。
        hits.sort(key=lambda hit: hit[0].id not in preferred)
        diversity = None
        question = lookup_plan.question_plan
        if (not question.requested_source_ids and not question.explicit_source_scope
                and not question.explicit_clause_numbers):
            hits, deferred = _diverse_reading_hits(hits, origins, preferred)
            diversity = {"method": "DISTINCT_ORIGINAL_READING_V1",
                         "deferred_alias_ids": [chunk.id for chunk, _ in raw if chunk.id in deferred]}
        section_swaps = []
        if "条文说明" not in raw_query:
            hits, section_swaps = prefer_primary_clauses(hits, origins)
        fields = ("id", "source_id", "snapshot_id", "clause_number", "page_start", "page_end")
        raw_rows = [_result(origins[chunk.id], chunk, score) for chunk, score in raw]
        selection = {"method": "READING_PRIORITY_V1", "limit": top_k,
                     "raw_query": raw_query, "selection_query": query,
                     "ranking_queries": 1 + int(raw_query != query),
                     "raw_hits": [{key: row.get(key) for key in fields} for row in raw_rows],
                     "preferred_ids": [chunk.id for chunk, _ in hits[:top_k] if chunk.id in preferred]}
        if section_swaps:
            selection["section_priority"] = {"method": "PRIMARY_COUNTERPART_V1", "swaps": section_swaps}
        if alias_ids:
            selection["role_alias_ids"] = [chunk.id for chunk, _ in hits[:top_k] if chunk.id in alias_ids]
        if diversity is not None:
            selection["diversity"] = diversity
    hits = hits[:top_k]
    return {
        **({"candidate_selection": selection} if selection is not None else {}),
        "method": method,
        "results": [{**_result(origins[chunk.id], chunk, score),
                     **({"fragments": fragments[chunk.id]} if chunk.id in fragments else {})}
                    for chunk, score in hits],
    }


def search_snapshot(snapshot: dict, query: str, top_k: int) -> dict:
    chunks = [
        _chunk(snapshot, index, f"{snapshot['snapshot_id']}:{index}", index)
        for index in range(len(snapshot["chunks"]))
    ]
    # 单独输入条款号时严格匹配，不能将其他相似数字算作命中。
    if CLAUSE.fullmatch(query):
        hits = [(chunk, None) for chunk in chunks if chunk.clause_number == query][:top_k]
        method = "EXACT_CLAUSE"
    else:
        hits = [
            (hit.chunk, hit.score)
            for hit in BM25Searcher(LexicalTokenizer()).search(query, chunks, top_k)
        ]
        method = "BM25_V1"

    results = [_result(snapshot, chunk, score) for chunk, score in hits]
    return {
        "query": query,
        "source_id": snapshot["source_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "status": "PENDING_REVIEW",
        "method": method,
        "searched_chunks": len(chunks),
        "processed_at": snapshot["processed_at"],
        "notice": "仅搜索这一份缓存正文；相关度不是正确率。版本、使用范围和识别文字尚未核验，不能直接作为设计依据。",
        "results": results,
    }
