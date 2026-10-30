"""使用 BM25 对规范小卡片进行可解释的关键词相关度排序。"""

import math
from collections import Counter
from dataclasses import dataclass

from archspec.documents.models import DocumentChunkRecord
from archspec.search.tokenizer import LexicalTokenizer


@dataclass(frozen=True)
class SearchHit:
    """一次搜索命中；分数只用于本次结果内部排序。"""

    chunk: DocumentChunkRecord
    score: float


class BM25Searcher:
    """根据词频、词语稀有程度和片段长度计算相关度。"""

    def __init__(
        self,
        tokenizer: LexicalTokenizer,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.tokenizer = tokenizer
        self.k1 = k1
        self.b = b

    def search(
        self,
        query: str,
        chunks: list[DocumentChunkRecord],
        top_k: int,
    ) -> list[SearchHit]:
        query_tokens = set(self.tokenizer.tokenize(query))
        if not query_tokens or not chunks or top_k < 1:
            return []

        counters = [Counter(self.tokenizer.tokenize(chunk.text)) for chunk in chunks]
        lengths = [sum(counter.values()) for counter in counters]
        average_length = sum(lengths) / len(lengths)
        if average_length == 0:
            return []

        hits: list[SearchHit] = []
        document_count = len(chunks)
        # 每个片段的每个词只统计一次出现的文档数，避免跨书检索时重复扫全部片段。
        frequencies = Counter(term for counter in counters for term in counter)
        inverse_frequencies = {
            term: math.log(1 + (document_count - frequencies[term] + 0.5)
                           / (frequencies[term] + 0.5))
            for term in query_tokens
        }
        for chunk, counter, document_length in zip(chunks, counters, lengths, strict=True):
            score = 0.0
            for term in query_tokens:
                term_frequency = counter[term]
                if term_frequency == 0:
                    continue
                inverse_document_frequency = inverse_frequencies[term]
                length_factor = 1 - self.b + self.b * document_length / average_length
                score += inverse_document_frequency * (
                    term_frequency * (self.k1 + 1)
                    / (term_frequency + self.k1 * length_factor)
                )
            if score > 0:
                hits.append(SearchHit(chunk=chunk, score=round(score, 6)))

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_index))
        return hits[:top_k]
