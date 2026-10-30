"""评测业务：校验人工答案，运行检索，并保存可追溯指标。"""

import time

from archspec.documents.models import DocumentStatus
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.evaluation.metrics import QueryRanking, calculate_metrics, find_first_relevant_rank
from archspec.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationCaseStatus,
    EvaluationResultRecord,
    EvaluationRunRecord,
    ResultDraft,
    RetrievalAlgorithm,
)
from archspec.evaluation.repository import EvaluationRepository
from archspec.search.hybrid import HybridSearchService
from archspec.search.semantic import SemanticSearchService
from archspec.search.service import SearchService


class EvaluationError(ValueError):
    """可以转换成明确接口提示的评测错误。"""


class DocumentNotFound(EvaluationError):
    pass


class CaseNotFound(EvaluationError):
    pass


class EvaluationValidationError(EvaluationError):
    pass


class NoVerifiedCases(EvaluationError):
    pass


class EvaluationService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        chunk_repository: DocumentChunkRepository,
        evaluation_repository: EvaluationRepository,
        search_service: SearchService,
        semantic_search_service: SemanticSearchService | None = None,
        hybrid_search_service: HybridSearchService | None = None,
    ) -> None:
        self.document_repository = document_repository
        self.chunk_repository = chunk_repository
        self.evaluation_repository = evaluation_repository
        self.search_service = search_service
        self.semantic_search_service = semantic_search_service
        self.hybrid_search_service = hybrid_search_service

    def create_case(
        self,
        document_id: str,
        query: str,
        expected_clause_numbers: list[str],
        source_note: str,
        status: EvaluationCaseStatus,
    ) -> EvaluationCaseRecord:
        document = self.document_repository.get(document_id)
        if document is None:
            raise DocumentNotFound("文档不存在")
        if document.status != DocumentStatus.PARSED:
            raise EvaluationValidationError("只有解析成功的文档才能建立评测题")

        expected = list(dict.fromkeys(expected_clause_numbers))
        if status == EvaluationCaseStatus.VERIFIED:
            if not expected:
                raise EvaluationValidationError("正式评测题必须填写正确条款号")
            available = {
                chunk.clause_number
                for chunk in self.chunk_repository.list_for_document(document_id)
                if chunk.clause_number is not None
            }
            missing = [clause for clause in expected if clause not in available]
            if missing:
                raise EvaluationValidationError(
                    f"以下正确条款在文档中不存在：{', '.join(missing)}"
                )

        return self.evaluation_repository.create_case(
            document_id, query, expected, source_note, status
        )

    def get_case(self, case_id: str) -> EvaluationCaseRecord:
        case = self.evaluation_repository.get_case(case_id)
        if case is None:
            raise CaseNotFound("评测题不存在")
        return case

    def list_cases(
        self,
        document_id: str | None,
        status: EvaluationCaseStatus | None,
    ) -> list[EvaluationCaseRecord]:
        return self.evaluation_repository.list_cases(document_id, status)

    def run(
        self,
        document_id: str,
        algorithm: RetrievalAlgorithm = RetrievalAlgorithm.BM25_V1,
    ) -> EvaluationRunRecord:
        document = self.document_repository.get(document_id)
        if document is None:
            raise DocumentNotFound("文档不存在")
        cases = self.evaluation_repository.list_cases(
            document_id, EvaluationCaseStatus.VERIFIED
        )
        if not cases:
            raise NoVerifiedCases("这份文档还没有经过人工确认的正式评测题")

        self._validate_algorithm_available(algorithm)
        run = self.evaluation_repository.create_run(document_id, algorithm.value)
        try:
            drafts: list[ResultDraft] = []
            rankings: list[QueryRanking] = []
            for case in cases:
                started = time.perf_counter()
                hits = self._search(document_id, case.query, algorithm)
                latency_ms = round((time.perf_counter() - started) * 1000, 6)
                if hits is None:
                    raise DocumentNotFound("文档不存在")
                retrieved_clauses = [hit.chunk.clause_number for hit in hits]
                rank = find_first_relevant_rank(
                    set(case.expected_clause_numbers), retrieved_clauses
                )
                rankings.append(QueryRanking(rank))
                drafts.append(
                    ResultDraft(
                        case_id=case.id,
                        query=case.query,
                        expected_clause_numbers=case.expected_clause_numbers,
                        first_relevant_rank=rank,
                        retrieved_clause_numbers=retrieved_clauses,
                        retrieved_scores=[hit.score for hit in hits],
                        latency_ms=latency_ms,
                    )
                )

            metrics = calculate_metrics(rankings)
            average_latency = round(
                sum(draft.latency_ms for draft in drafts) / len(drafts), 6
            )
            return self.evaluation_repository.complete_run(
                run.id, metrics, average_latency, drafts
            )
        except Exception as error:
            self.evaluation_repository.fail_run(run.id, str(error))
            raise

    def _validate_algorithm_available(self, algorithm: RetrievalAlgorithm) -> None:
        if (
            algorithm == RetrievalAlgorithm.SEMANTIC_V1
            and self.semantic_search_service is None
        ):
            raise EvaluationValidationError("语义检索服务尚未启用")
        if (
            algorithm == RetrievalAlgorithm.HYBRID_RRF_V1
            and self.hybrid_search_service is None
        ):
            raise EvaluationValidationError("混合检索服务尚未启用")

    def _search(self, document_id: str, query: str, algorithm: RetrievalAlgorithm):
        if algorithm == RetrievalAlgorithm.SEMANTIC_V1:
            assert self.semantic_search_service is not None
            return self.semantic_search_service.search_many([document_id], query, 5)
        if algorithm == RetrievalAlgorithm.HYBRID_RRF_V1:
            assert self.hybrid_search_service is not None
            return self.hybrid_search_service.search_many([document_id], query, 5)
        hits = self.search_service.search(document_id, query, 5)
        if hits is None:
            raise DocumentNotFound("文档不存在")
        return hits

    def get_run(
        self, run_id: str
    ) -> tuple[EvaluationRunRecord, list[EvaluationResultRecord]] | None:
        run = self.evaluation_repository.get_run(run_id)
        if run is None:
            return None
        return run, self.evaluation_repository.list_results(run_id)
