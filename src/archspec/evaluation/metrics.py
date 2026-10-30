"""不依赖数据库的排名指标计算。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryRanking:
    """一道题中，第一个正确条款出现的位置；没有命中时为 None。"""

    first_relevant_rank: int | None


@dataclass(frozen=True)
class EvaluationMetrics:
    """一次评测的题目数量、召回率和平均倒数排名，单位均为百分比。"""

    case_count: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float


def find_first_relevant_rank(
    expected: set[str],
    retrieved: list[str | None],
) -> int | None:
    """返回任意正确条款首次出现的排名，排名从 1 开始。"""
    return next(
        (
            rank
            for rank, clause_number in enumerate(retrieved, start=1)
            if clause_number in expected
        ),
        None,
    )


def calculate_metrics(rankings: list[QueryRanking]) -> EvaluationMetrics:
    """把逐题排名汇总成 Recall@K 和 MRR。"""
    if not rankings:
        raise ValueError("评测题不能为空")

    total = len(rankings)

    def recall_at(limit: int) -> float:
        hit_count = sum(
            ranking.first_relevant_rank is not None
            and ranking.first_relevant_rank <= limit
            for ranking in rankings
        )
        return round(100 * hit_count / total, 6)

    reciprocal_rank_sum = sum(
        0 if ranking.first_relevant_rank is None else 1 / ranking.first_relevant_rank
        for ranking in rankings
    )
    return EvaluationMetrics(
        case_count=total,
        recall_at_1=recall_at(1),
        recall_at_3=recall_at(3),
        recall_at_5=recall_at(5),
        mrr=round(100 * reciprocal_rank_sum / total, 6),
    )
