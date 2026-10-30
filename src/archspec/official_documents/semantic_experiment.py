"""独立离线语义实验；不接入默认搜索、不保存正式质量门槛成绩。

输入必须由调用方验证本地正文及LOCAL_SEARCH/LOCAL_EXCERPT权限。
SEMANTIC_ONLY保留原片段；HYBRID_V1用等权RRF合并V1和语义两路排名，
不是把不可比的BM25分数与余弦分数直接相加。相关度不代表规范正确性。
"""

import copy
import hashlib
import json
import math
import socket
from array import array
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from archspec.official_documents.clause_units import CLAUSE
from archspec.official_documents.search import _chunk, _result, ranking_snapshots, search_snapshots

INDEX_VERSION = "official-offline-semantic-experiment-v1"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_COMMIT = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_FILES = {
    "model_optimized.onnx": "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38",
    "config.json": "9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3",
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
}


def require_new_output(destination):
    """离线报告只写新目录；脚本与公共合同共用同一保护。"""
    if Path(destination).exists():
        raise FileExistsError("离线实验目录已存在，拒绝覆盖")


def audit_hits(snapshots, hits):
    """逐个对照固定源片段；不把语义分数解释为答案或现行性。"""
    known = {(row["source_id"], row["snapshot_id"]): row for row in snapshots}
    checks = []
    for hit in hits:
        source, snapshot = hit["source_id"], hit["snapshot_id"]
        prefix = f"{source}:{snapshot}:"
        if not hit["id"].startswith(prefix):
            raise ValueError("结果身份没有绑定固定正文")
        original = known[(source, snapshot)]["chunks"][int(hit["id"][len(prefix):])]
        if (not hit["text"] or hit["text"] != original["text"] or any(
                hit[key] != original[key] for key in
                ("clause_number", "page_start", "page_end"))):
            raise ValueError("结果原文、条号或PDF页身份不一致")
        checks.append({"id": hit["id"], "source_id": source, "snapshot_id": snapshot,
                       "clause_number": hit["clause_number"], "page_start": hit["page_start"],
                       "page_end": hit["page_end"], "quote": hit["text"],
                       "quote_exact_substring": True,
                       "original_page_url": f"/api/official-documents/{source}/snapshots/"
                                            f"{snapshot}/pages/{hit['page_start']}.png"})
    return checks


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _canonical_parts(value):
    """双精度矩阵逐行编码；保持旧JSON字节顺序，避免整份坐标字符串副本。"""
    if isinstance(value, _PackedVectors):
        yield '['
        for index, row in enumerate(value):
            if index:yield ','
            yield _canonical(row)
        yield ']'
    elif isinstance(value, dict) and all(isinstance(k,str) for k in value):
        yield '{'
        for index, key in enumerate(sorted(value)):
            if index:yield ','
            yield _canonical(key);yield ':'
            yield from _canonical_parts(value[key])
        yield '}'
    else:
        yield _canonical(value)


def _digest(value):
    digest = hashlib.sha256()
    for part in _canonical_parts(value):digest.update(part.encode('utf-8'))
    return digest.hexdigest()


class _PackedVectors:
    """只读序列接口，内部连续float64；不改变原数值或暴露可写缓冲。"""
    __slots__ = ('__data', '__dimension')

    def __init__(self, rows, count, *, normalize=True):
        data = array('d');dimension = None;seen = 0
        for row in rows:
            if seen >= count:raise ValueError('模型返回向量数量不正确')
            if not isinstance(row, (list, tuple)) or not row:
                raise ValueError('模型返回空向量或格式不正确')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in row):
                raise ValueError('模型向量必须是有限数值')
            dimension = dimension or len(row)
            if len(row) != dimension:raise ValueError('模型向量维度不一致')
            if normalize:
                norm = math.sqrt(sum(v*v for v in row))
                if not norm or not math.isfinite(norm):raise ValueError('模型向量范数必须有限且大于零')
                data.extend(float(v)/norm for v in row)
            else:data.extend(row)
            seen += 1
        if seen != count:raise ValueError('模型返回向量数量不正确')
        self.__data = data;self.__dimension = dimension or 0

    def __len__(self):return len(self.__data)//self.__dimension if self.__dimension else 0

    def __getitem__(self, index):
        if isinstance(index, slice):return tuple(self[i] for i in range(*index.indices(len(self))))
        if index < 0:index += len(self)
        if not 0 <= index < len(self):raise IndexError(index)
        start = index*self.__dimension
        return tuple(self.__data[start:start+self.__dimension])

    def __iter__(self):
        for index in range(len(self)):yield self[index]

    def __eq__(self, other):
        if isinstance(other, _PackedVectors):
            return self.__dimension == other.__dimension and self.__data == other.__data
        if isinstance(other,(list,tuple)):
            return len(self)==len(other) and all(a==tuple(b) for a,b in zip(self,other,strict=True))
        return NotImplemented


def offline_model_manifest(model_dir):
    """只允许已审查的固定五件套；实际哈希而非refs/main或文件大小作凭据。"""
    root = Path(model_dir).resolve()
    files = {}
    for name, expected in MODEL_FILES.items():
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"固定本地模型文件缺失或不是普通文件：{name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"固定本地模型文件哈希不匹配：{name}")
        stat = path.stat()
        files[name] = {"sha256": digest, "size_bytes": stat.st_size,
                       "mtime_ns": stat.st_mtime_ns}
    return {"model_name": MODEL_NAME, "commit": MODEL_COMMIT, "dimension": 512,
            "files": files, "runtime": {name: version(name) for name in
                                       ("fastembed", "onnxruntime", "tokenizers")},
            "encoding": "fastembed passage_embed/query_embed; no text prefix; CPU only"}


def _forbid_network(*args, **kwargs):
    raise RuntimeError("离线语义实验禁止网络或模型下载")


@contextmanager
def offline_guard():
    """构造和编码期间也阻断下载；不阻断必经的本地路径解析download_model。"""
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"HF_HUB_OFFLINE": "1",
                                                       "HF_HUB_DISABLE_TELEMETRY": "1",
                                                       "PYTHONDONTWRITEBYTECODE": "1"}))
        for name in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, name, _forbid_network))
        stack.enter_context(patch.object(socket, "create_connection", _forbid_network))
        # 导入也位于网络拒绝范围内；这里不涉及远程模型调用。
        from fastembed.common import model_management
        for name in ("download_files_from_huggingface", "retrieve_model_gcs",
                     "download_file_from_gcs"):
            stack.enter_context(patch.object(model_management.ModelManagement, name,
                                             _forbid_network))
        for name in ("snapshot_download", "model_info", "list_repo_tree"):
            stack.enter_context(patch.object(model_management, name, _forbid_network))
        stack.enter_context(patch("requests.sessions.Session.request", _forbid_network))
        yield


class OfflineFastEmbedProvider:
    """固定本机模型的专用提供者，独立于生产FastEmbedProvider。"""

    def __init__(self, model_dir):
        self.model_dir = Path(model_dir).resolve()
        self.manifest = offline_model_manifest(self.model_dir)
        # 修改时间用于前后保护核对，但不作为模型语义身份。
        identity = copy.deepcopy(self.manifest)
        for row in identity["files"].values():
            row.pop("mtime_ns")
        self.model_fingerprint = _digest(identity)
        self._model = None

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(
                model_name=MODEL_NAME, cache_dir=str(self.model_dir),
                specific_model_path=str(self.model_dir), local_files_only=True,
                lazy_load=True, providers=["CPUExecutionProvider"], threads=2)
        return self._model

    def embed_documents(self, texts):
        with offline_guard():
            return [item.tolist() for item in
                    self._get_model().passage_embed(texts, batch_size=32, parallel=None)]

    def embed_query(self, query):
        with offline_guard():
            return next(iter(self._get_model().query_embed([query], parallel=None))).tolist()


def _model_fingerprint(provider):
    value = getattr(provider, "model_fingerprint", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("语义实验必须提供固定模型文件和运行版本指纹")
    return value


def _vectors(raw, count, dimension=None):
    if not isinstance(raw, (list, tuple)) or len(raw) != count:
        raise ValueError("模型返回向量数量不正确")
    values = []
    for vector in raw:
        if not isinstance(vector, (list, tuple)) or not vector:
            raise ValueError("模型返回空向量或格式不正确")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise ValueError("模型向量必须是有限数值")
        dimension = dimension or len(vector)
        if len(vector) != dimension:
            raise ValueError("模型向量维度不一致")
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm or not math.isfinite(norm):
            raise ValueError("模型向量范数必须有限且大于零")
        values.append(tuple(float(value) / norm for value in vector))
    return tuple(values)


@dataclass(frozen=True)
class PreparedSemanticCorpus:
    snapshots: tuple
    chunks: tuple
    origins: dict
    vectors: tuple
    model_fingerprint: str
    key: str
    runtime_fingerprint: str = ""


def _check_budget(snapshots):
    if len(snapshots) > 20 or sum(len(row["chunks"]) for row in snapshots) > 20000:
        raise ValueError("完整语料超过20书/20000片段预算，拒绝裁剪后评分")


def _runtime_fingerprint(prepared):
    """运行对象也绑定完整内容，防止嵌套可变对象沿用旧固定身份。"""
    snapshot_digests = {}
    for snapshot in prepared.origins.values():
        identifier = id(snapshot)
        if identifier not in snapshot_digests:
            snapshot_digests[identifier] = _digest(snapshot)
    return _digest({
        "snapshots": prepared.snapshots,
        "chunks": [asdict(chunk) for chunk in prepared.chunks],
        "origins": {identifier: snapshot_digests[id(snapshot)]
                    for identifier, snapshot in prepared.origins.items()},
        "vectors": prepared.vectors, "model_fingerprint": prepared.model_fingerprint,
        "key": prepared.key,
    })


def _read_cached_vectors(path, identity, key, count, expected_dimension):
    """完整核对后只返回连续坐标；解析JSON在运行指纹计算前释放。"""
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (set(saved) != {"identity", "key", "checksum", "vectors"}
                or saved["identity"] != json.loads(_canonical(identity))
                or _digest(saved["identity"]) != key or saved["key"] != key):
            raise ValueError
        if not isinstance(saved["vectors"], list):
            raise TypeError
        vectors = _PackedVectors(saved["vectors"], count, normalize=False)
        # 同一规范JSON逐行求SHA，不再把完整矩阵转成第二份巨大字符串。
        if saved["checksum"] != _digest(vectors):
            raise ValueError
        for row in vectors:
            # 缓存本来已归一化，必须保留原双精度坐标，禁止再次归一化漂移。
            if ((expected_dimension is not None and len(row) != expected_dimension)
                    or not math.isclose(math.hypot(*row), 1.0, rel_tol=1e-12, abs_tol=1e-12)):
                raise ValueError
        return vectors
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("已有语义派生缓存无法安全复用，拒绝覆盖或静默使用") from error


@dataclass(frozen=True)
class PreparedContentVectors:
    """只含计算身份和连续坐标；不承载来源、快照、元信息或任何许可。"""

    model_fingerprint: str
    text_sequence_sha256: str
    vectors: object


def checked_content_vectors(raw, count, *, normalize=False):
    """固定512维产物逐行校验；已归一化缓存保持原坐标，不再归一化漂移。"""
    vectors = _PackedVectors(raw, count, normalize=normalize)
    for row in vectors:
        if len(row) != 512 or not math.isclose(math.hypot(*row), 1.0,
                                              rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("预备向量维度或归一化身份不符")
    return vectors


def prepare_semantic_corpus(snapshots, *, provider, index_dir=None, require_cache=False,
                            expected_dimension=None, prepared_vectors=None):
    """一次准备全部已验证片段；派生缓存不能冒用同号不同正文或不同模型。"""
    fingerprint = _model_fingerprint(provider)
    if expected_dimension is not None and (type(expected_dimension) is not int or expected_dimension < 1):
        raise ValueError("预期向量维度必须为正整数")
    _check_budget(snapshots)
    frozen = tuple(copy.deepcopy(snapshots))
    chunks, origins = [], {}
    for snapshot in ranking_snapshots(frozen, ""):
        for index in range(len(snapshot["chunks"])):
            identifier = f"{snapshot['source_id']}:{snapshot['snapshot_id']}:{index}"
            if identifier in origins:
                raise ValueError("语义实验输入含重复固定片段身份")
            chunks.append(_chunk(snapshot, index, identifier, len(chunks)))
            origins[identifier] = snapshot
    identity = {"version": INDEX_VERSION, "model_fingerprint": fingerprint,
                "snapshots": frozen, "chunk_ids": [chunk.id for chunk in chunks]}
    key = _digest(identity)
    path = Path(index_dir) / f"{key}.json" if index_dir is not None else None
    if prepared_vectors is not None:
        if (not isinstance(prepared_vectors, PreparedContentVectors)
                or prepared_vectors.model_fingerprint != fingerprint
                or prepared_vectors.text_sequence_sha256 != _digest([chunk.text for chunk in chunks])
                or not isinstance(prepared_vectors.vectors, _PackedVectors)
                or len(prepared_vectors.vectors) != len(chunks)
                or expected_dimension not in (None, 512)):
            raise ValueError("预备向量与本次完整条文或固定模型不符")
        vectors = prepared_vectors.vectors
    elif path is not None and (path.exists() or path.is_symlink()):
        vectors = _read_cached_vectors(path, identity, key, len(chunks), expected_dimension)
    else:
        # 已有预编码包为迁移前提时，缺包明确失败；不得悄悄加载模型重新编码。
        if require_cache:
            raise ValueError("要求已有语义派生缓存，但未找到与完整语料和模型匹配的缓存")
        texts = [chunk.text for chunk in chunks]
        generate = getattr(provider, 'iter_documents', None)
        raw = (() if not chunks else generate(texts) if callable(generate) else provider.embed_documents(texts))
        vectors = _PackedVectors(raw, len(chunks))
        if expected_dimension is not None and vectors and len(vectors[0]) != expected_dimension:
            raise ValueError("模型向量维度与预期不符")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"key": key, "identity": identity, "vectors": vectors,
                       "checksum": _digest(vectors)}
            # 排他创建：绝不覆盖已有派生文件。
            with path.open("x", encoding="utf-8") as stream:
                stream.writelines(_canonical_parts(payload))
    prepared = PreparedSemanticCorpus(frozen, tuple(chunks), origins, vectors, fingerprint, key)
    return replace(prepared, runtime_fingerprint=_runtime_fingerprint(prepared))


def search_semantic(snapshots, query, top_k, *, provider, mode, index_dir=None):
    """接受原快照或一次准备的语料；仅用于实验，不授权生产默认切换。"""
    if mode not in {"SEMANTIC_ONLY", "HYBRID_V1"}:
        raise ValueError("语义实验模式只能为SEMANTIC_ONLY或HYBRID_V1")
    if not isinstance(query, str) or not 1 <= len(query) <= 500 or not query.strip():
        raise ValueError("查询必须为1到500字符的非空文字，布尔或数值不能充当查询")
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("结果预算必须为1到20的整数，布尔值不能充当预算")
    query = query.strip()
    original = snapshots.snapshots if isinstance(snapshots, PreparedSemanticCorpus) else snapshots
    _check_budget(original)
    if isinstance(snapshots, PreparedSemanticCorpus):
        try:
            unchanged = _runtime_fingerprint(snapshots) == snapshots.runtime_fingerprint
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ValueError("已准备语料的完整运行身份无法核对，拒绝评分") from error
        if not unchanged:
            raise ValueError("已准备语料的正文、来源、片段、向量或固定键已改变，拒绝评分")
    if CLAUSE.fullmatch(query):
        return search_snapshots(original, query, top_k, ranking_version="V1", index_dir=None)
    prepared = (snapshots if isinstance(snapshots, PreparedSemanticCorpus) else
                prepare_semantic_corpus(snapshots, provider=provider, index_dir=index_dir))
    if prepared.model_fingerprint != _model_fingerprint(provider):
        raise ValueError("已准备语料与当前模型指纹不同")
    if not prepared.chunks:
        return {"method": mode, "results": []}
    vector = _vectors([provider.embed_query(query)], 1, len(prepared.vectors[0]))[0]
    scores = [(chunk, round(sum(left * right for left, right in
                               zip(vector, embedding, strict=True)), 6))
              for chunk, embedding in zip(prepared.chunks, prepared.vectors, strict=True)]
    scores.sort(key=lambda row: (-row[1], row[0].chunk_index))
    if mode == "SEMANTIC_ONLY":
        return {"method": mode, "corpus_key": prepared.key,
                "results": [_result(prepared.origins[chunk.id], chunk, score)
                            for chunk, score in scores[:top_k]]}
    candidate_k = max(20, top_k)
    keyword = search_snapshots(prepared.snapshots, query, candidate_k,
                               ranking_version="V1", index_dir=None)["results"]
    by_id = {chunk.id: chunk for chunk in prepared.chunks}
    fused, ranks = {}, {}
    for route, identifiers in (("keyword_rank", [hit["id"] for hit in keyword]),
                               ("semantic_rank", [chunk.id for chunk, _ in scores[:candidate_k]])):
        for rank, identifier in enumerate(identifiers, 1):
            fused[identifier] = fused.get(identifier, 0) + 1 / (60 + rank)
            ranks.setdefault(identifier, {"keyword_rank": None, "semantic_rank": None})[route] = rank
    ordered = sorted(fused, key=lambda identifier: (-fused[identifier],
                     min(rank for rank in ranks[identifier].values() if rank is not None),
                     by_id[identifier].chunk_index))
    return {"method": mode, "corpus_key": prepared.key, "candidate_k": candidate_k,
            "rank_constant": 60, "fusion": "equal-weight reciprocal rank fusion of V1 + semantic",
            "results": [{**_result(prepared.origins[identifier], by_id[identifier],
                                   round(fused[identifier], 8)), **ranks[identifier]}
                        for identifier in ordered[:top_k]]}
