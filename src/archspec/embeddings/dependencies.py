"""共享重量级模型提供者，避免每个请求重复加载或重复等待下载。"""

from archspec.config import settings
from archspec.embeddings.provider import FastEmbedProvider

# FastEmbedProvider 本身是延迟加载的，导入模块时不会下载模型。
default_embedding_provider = FastEmbedProvider(settings.model_cache_dir)
