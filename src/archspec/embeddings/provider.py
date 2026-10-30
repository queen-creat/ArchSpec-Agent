"""隔离本地模型库，让业务代码和自动测试不依赖具体模型实现。"""

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DEFAULT_MODEL_DIMENSION = 512
DEFAULT_DOCUMENT_BATCH_SIZE = 1


def _document_batch_size(value):
    """普通资料与固定官方编码共用上限；配置错误在任何模型加载前拒绝。"""
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError("编码器批大小须为1至32的整数")
    return value


class EmbeddingUnavailableError(RuntimeError):
    """本地语义模型暂时不能加载或执行。"""


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class _FastEmbedModel(Protocol):
    embedding_size: int

    def passage_embed(self, texts: Iterable[str], *, batch_size: int, parallel: int | None) -> Iterable[Any]: ...

    def query_embed(self, texts: Iterable[str]) -> Iterable[Any]: ...


ModelFactory = Callable[..., _FastEmbedModel]


class FastEmbedProvider:
    """延迟初始化 FastEmbed，只有第一次语义检索才加载模型。"""

    def __init__(
        self,
        cache_dir: Path,
        model_name: str = DEFAULT_MODEL_NAME,
        model_factory: ModelFactory | None = None,
        *,
        document_batch_size: int = DEFAULT_DOCUMENT_BATCH_SIZE,
    ) -> None:
        self.document_batch_size = _document_batch_size(document_batch_size)
        self.cache_dir = cache_dir
        self._model_name = model_name
        self.model_factory = model_factory
        self._model: _FastEmbedModel | None = None
        self._load_error: EmbeddingUnavailableError | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return int(self._get_model().embedding_size)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = [self._as_list(item) for item in self._get_model().passage_embed(
            texts,batch_size=self.document_batch_size,parallel=None)]
        self._validate_vectors(vectors, len(texts))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("查询文字不能为空")
        vectors = [self._as_list(item) for item in self._get_model().query_embed([text])]
        self._validate_vectors(vectors, 1)
        return vectors[0]

    def _get_model(self) -> _FastEmbedModel:
        if self._load_error is not None:
            raise self._load_error
        if self._model is None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            factory = self.model_factory
            if factory is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError as error:
                    raise RuntimeError(
                        "本地语义模型组件尚未安装，请安装 fastembed"
                    ) from error
                factory = TextEmbedding
            try:
                self._model = factory(
                    model_name=self.model_name,
                    cache_dir=str(self.cache_dir),
                    lazy_load=True,
                )
            except Exception as error:
                self._load_error = EmbeddingUnavailableError(
                    f"本地语义模型加载失败：{error}"
                )
                raise self._load_error from error
        return self._model

    @staticmethod
    def _as_list(vector: Any) -> list[float]:
        raw = vector.tolist() if hasattr(vector, "tolist") else vector
        return [float(value) for value in raw]

    def _validate_vectors(self, vectors: list[list[float]], expected_count: int) -> None:
        if len(vectors) != expected_count:
            raise ValueError("模型返回的向量数量与输入文字数量不一致")
        expected_dimension = self.dimension
        if any(len(vector) != expected_dimension for vector in vectors):
            raise ValueError("模型返回的向量维度不正确")


class PinnedFastEmbedProvider:
    """运行期只用已核固定五件套；沿用实验编码身份，不全局拦截其它请求的网络。"""
    def __init__(self, model_dir, *, document_batch_size=DEFAULT_DOCUMENT_BATCH_SIZE):
        # 免费实例固定小批执行，保留完整输入/输出；批大小不改变模型文件身份。
        self.document_batch_size = _document_batch_size(document_batch_size)
        from archspec.official_documents.semantic_experiment import OfflineFastEmbedProvider

        self._pinned = OfflineFastEmbedProvider(model_dir)
        self.model_fingerprint = self._pinned.model_fingerprint

    def iter_documents(self, texts):
        """正式迁移逐条消费SDK结果，避免在归一化前保留整批Python坐标。"""
        for item in self._pinned._get_model().passage_embed(
                texts, batch_size=self.document_batch_size, parallel=None):
            yield item.tolist()

    def embed_documents(self, texts):
        return list(self.iter_documents(texts))

    def embed_query(self, text):
        return next(iter(self._pinned._get_model().query_embed([text], parallel=None))).tolist()
