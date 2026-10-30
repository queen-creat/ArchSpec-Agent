"""用户主动使用本机语义补查；复用既有固定向量算法，不切换关键词默认排序。"""

import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

_LOCK = Lock()


def configured():
    """仅本机运行且已配置本地模型时显示入口；不下载模型或读取任何密钥。"""
    directory = os.getenv("ARCHSPEC_EMBEDDING_MODEL_DIR")
    return (os.getenv("ARCHSPEC_PERSISTENCE_BACKEND") == "LOCAL"
            and bool(directory) and Path(directory).is_dir())


@lru_cache(maxsize=1)
def _provider(directory):
    from archspec.embeddings.provider import PinnedFastEmbedProvider
    return PinnedFastEmbedProvider(Path(directory))


def provider():
    if not configured():
        raise ValueError("本机语义模型尚未准备；可继续使用关键词查找。")
    return _provider(os.environ["ARCHSPEC_EMBEDDING_MODEL_DIR"])


def search(snapshots, query, top_k, *, cache_dir):
    """输入仅为本轮已核范围/许可的正文；派生向量不能授予读取权限。"""
    from archspec.official_documents.clause_units import CLAUSE

    if CLAUSE.fullmatch(query):
        # The caller already checked source scope and current reading permission.
        # Exact identifiers never require vector preparation or a model download.
        from archspec.official_documents.search import search_snapshots

        return search_snapshots(snapshots, query, top_k, ranking_version="V1", index_dir=None)
    if not configured():
        raise ValueError("本机语义模型尚未准备；可继续使用关键词查找。")
    if not snapshots:
        return {"method": "SEMANTIC_ONLY", "results": []}
    from archspec.official_documents.semantic_experiment import (
        prepare_semantic_corpus,
        search_semantic,
    )
    # 只给当前受控读取快照生成可重建的派生缓存，不改原件、快照或冻结成绩。
    # 同进程串行首次编码，避免两个用户动作同时生成相同派生文件。
    try:
        with _LOCK:
            encoder = provider()
            prepared = prepare_semantic_corpus(
                [dict(row) for row in snapshots], provider=encoder,
                index_dir=Path(cache_dir) / "local-semantic-index")
            return search_semantic(prepared, query, top_k, provider=encoder, mode="SEMANTIC_ONLY")
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("本机按意思查找暂不可用，请使用关键词查找；原正文和原件已保留。") from error
