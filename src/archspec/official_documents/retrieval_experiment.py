"""独立的只读检索试验；不改变已上线V1/V2，也不读取模型密钥。"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from archspec.official_documents.search import (
    CLAUSE_PATTERN,
    _chunk,
    _result,
    _title_weight,
)
from archspec.search.bm25 import BM25Searcher
from archspec.search.tokenizer import LexicalTokenizer

METHODS = ("ALIASES_ONLY", "STRUCTURE_ONLY", "ALIASES_STRUCTURE")
# 只扩展说法，不补用途、等级、数值或地方规定。规则对所有规范一视同仁。
ALIASES = (("楼高", "建筑高度"), ("怎么量", "如何计算测量"),
           ("计量", "计算测量"), ("消防分区", "防火分区"),
           ("逃生距离", "疏散距离"), ("逃生出口", "安全出口"),
           ("晒太阳", "日照"), ("停车位", "停车泊位"))
HEADING = re.compile(r"(\d+(?:\.\d+)?)\s+[^。；！？\n]{1,45}")
RRF_OFFSET = 60


def _directory(row):
    return bool(re.search(r"(?:…{2,}|[.·]{3,})\s*\d+\s*$", row["text"].strip())
                or re.search(r"(?:^|\n)\s*目\s*录\s*(?:\n|$)", row["text"]))


def _weight(row):
    # 中文“……”只有两个字符；仍是明确目录引导线，不合并进正文。
    if _directory(row):
        return 0.01
    return _title_weight(row)


def experiment_identifiers():
    """派生实验标识独立于旧成绩；实现变化时不能沿用实验成绩。"""
    from archspec.official_documents import clause_units, search
    from archspec.search import bm25, tokenizer

    paths = [Path(__file__), Path(search.__file__), Path(bm25.__file__), Path(tokenizer.__file__),
             Path(clause_units.__file__)]
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in paths)).hexdigest()
    return {method: f"OFFICIAL_{method}:{digest}" for method in METHODS}


def expanded_queries(query):
    rewritten = query
    for before, after in ALIASES:
        rewritten = rewritten.replace(before, after)
    # 不把未变化的表达重复加入融合；保留原问题，方便检查是否改变了条件。
    return list(dict.fromkeys([query, rewritten]))


def _headings(snapshot):
    """标题只在同书、相同节前缀、邻近原页继承，目录混行不当成标题。"""
    anchors = {}
    result = []
    for row in snapshot["chunks"]:
        text = row["text"].strip()
        match = HEADING.fullmatch(text)
        if (match and not _directory(row) and row["clause_number"] == match[1]
                and _title_weight(row) == 0.01):
            anchors[match[1]] = (text, row["page_start"], row["page_end"])
        clause = row.get("clause_number")
        section = clause.rsplit(".", 1)[0] if clause and CLAUSE_PATTERN.fullmatch(clause) else None
        anchor = anchors.get(section)
        if anchor and 0 <= row["page_start"] - anchor[2] <= 1:
            result.append(anchor[0])
        else:
            # 只有heading_path标签没有原快照中邻近的标题，不能作为结构召回依据。
            result.append("")
    return result


def _add_rank(scores, hits, weight):
    # RRF（按名次融合）不直接混合两个尺度不同的BM25分数。
    for rank, hit in enumerate(hits, 1):
        scores[hit.chunk.id] += weight / (RRF_OFFSET + rank)


def search_experiment(snapshots, query, top_k=5, *, method="ALIASES_STRUCTURE"):
    """只接受调用者已核对的固定语料；不读原件、不联网、不识别、不写旧索引。"""
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 500
            or type(top_k) is not int or not 1 <= top_k <= 20 or method not in METHODS
            or len(snapshots) > 20 or sum(len(row["chunks"]) for row in snapshots) > 20000):
        raise ValueError("实验问题、排序或完整语料预算无效")
    query = query.strip()
    chunks, metadata, headings, weights = [], {}, [], {}
    for snapshot in snapshots:
        titles = _headings(snapshot)
        for index, row in enumerate(snapshot["chunks"]):
            chunk_id = f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
            chunk = _chunk(snapshot, index, chunk_id, len(chunks))
            chunks.append(chunk)
            metadata[chunk_id] = snapshot
            headings.append(titles[index])
            weights[chunk_id] = _weight(row)
    queries = [query] if method == "STRUCTURE_ONLY" else expanded_queries(query)
    exact = bool(CLAUSE_PATTERN.fullmatch(query))
    scores = defaultdict(float)
    heading_by_id = dict(zip((chunk.id for chunk in chunks), headings, strict=True))
    if exact:
        queries = [query]
        hits = [(chunk, None) for chunk in chunks if chunk.clause_number == query]
    else:
        searcher = BM25Searcher(LexicalTokenizer())
        for index, expression in enumerate(queries):
            _add_rank(scores, searcher.search(expression, chunks, len(chunks)),
                      1.0 if index == 0 else 0.6)
        if method != "ALIASES_ONLY":
            from dataclasses import replace
            title_chunks = [replace(chunk, text=heading_by_id[chunk.id],
                                    character_count=len(heading_by_id[chunk.id])) for chunk in chunks]
            for expression in queries:
                _add_rank(scores, searcher.search(expression, title_chunks, len(chunks)), 0.45)
        # 匹配接近时，小幅优先有明确完整条号的正文；未知文字仍留在结果中。
        hits = [(chunk, round(scores[chunk.id] * (weights[chunk.id] *
                  (1.1 if chunk.clause_number and CLAUSE_PATTERN.fullmatch(chunk.clause_number)
                   and weights[chunk.id] == 1 else 1.0)
                  if method != "ALIASES_ONLY" else 1.0), 9))
                for chunk in chunks if scores[chunk.id] > 0]
        hits.sort(key=lambda item: (-item[1], item[0].chunk_index))

    # 正文去重不改代表片段，不把整条的拼接文字/首尾页冒充该编号的原文。
    from archspec.official_documents.clause_units import clause_runs

    body_groups, unit_keys = defaultdict(list), {}
    for snapshot in snapshots:
        for index, run in clause_runs(snapshot["chunks"]).items():
            identifier = f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
            unit_keys[identifier] = (snapshot["source_id"], snapshot["snapshot_id"], run[0])
    for chunk in chunks:
        if chunk.id in unit_keys and weights[chunk.id] == 1:
            body_groups[unit_keys[chunk.id]].append(chunk)
    selected, seen = [], set()
    for chunk, score in hits:
        origin = metadata[chunk.id]
        key = unit_keys.get(chunk.id)
        parts = body_groups.get(key, []) if weights[chunk.id] == 1 else []
        if method != "ALIASES_ONLY" and parts:
            if key in seen:
                continue
            seen.add(key)
        row = _result(origin, chunk, score)
        row["retrieval_heading"] = heading_by_id[chunk.id]
        if method != "ALIASES_ONLY" and len(parts) > 1:
            row["fragments"] = [{field: _result(origin, part, None)[field] for field in
                                 ("id", "source_id", "snapshot_id", "clause_number", "text",
                                  "page_start", "page_end")} for part in parts]
        selected.append(row)
        if len(selected) == top_k:
            break
    return {"method": "EXACT_CLAUSE" if exact else method, "expanded_queries": queries,
            "semantic_model_used": False, "results": selected,
            "algorithm_identifier": experiment_identifiers()[method],
            "status": "EXPERIMENT_NOT_DEFAULT"}
