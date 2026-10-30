"""任务书辅助标注流程：自动提取要求，检索规范，再由教师确认。"""

from dataclasses import dataclass

from archspec.assisted_labeling.classifier import DocumentClassifier
from archspec.assisted_labeling.extractor import RequirementExtractor
from archspec.assisted_labeling.models import (
    DocumentProfileRecord,
    DocumentRole,
    RequirementConfirmationRecord,
    RequirementItemRecord,
    RequirementStatus,
)
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.documents.models import DocumentStatus
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.evaluation.models import EvaluationCaseStatus
from archspec.evaluation.service import EvaluationService
from archspec.search.hybrid import HybridSearchService
from archspec.search.service import SearchService


class AssistedLabelingError(ValueError):
    """可以安全显示给普通用户的辅助标注错误。"""


class AssistedLabelingNotFound(AssistedLabelingError):
    pass


class AssistedLabelingValidationError(AssistedLabelingError):
    pass


@dataclass(frozen=True)
class ClauseSuggestion:
    """一条可能相关的规范条文；分数只用于排序，不代表专业结论。"""

    regulation_document_id: str
    regulation_name: str
    clause_number: str
    heading_path: str | None
    text: str
    page_start: int | None
    page_end: int | None
    score: float
    retrieval_mode: str


@dataclass(frozen=True)
class RetrievalStatus:
    """告诉页面当前实际使用了哪一种检索方式。"""

    mode: str
    label: str
    available: bool
    message: str


@dataclass(frozen=True)
class LabeledDocument:
    id: str
    original_name: str
    role: DocumentRole


class AssistedLabelingService:
    def __init__(
        self,
        document_repository: DocumentRepository,
        chunk_repository: DocumentChunkRepository,
        repository: AssistedLabelingRepository,
        classifier: DocumentClassifier,
        extractor: RequirementExtractor,
        search_service: SearchService,
        evaluation_service: EvaluationService,
        hybrid_search_service: HybridSearchService | None = None,
    ) -> None:
        self.document_repository = document_repository
        self.chunk_repository = chunk_repository
        self.repository = repository
        self.classifier = classifier
        self.extractor = extractor
        self.search_service = search_service
        self.evaluation_service = evaluation_service
        self.hybrid_search_service = hybrid_search_service
        self._retrieval_status = self._default_retrieval_status()

    def _default_retrieval_status(self) -> RetrievalStatus:
        if self.hybrid_search_service is not None:
            return RetrievalStatus(
                mode="HYBRID_RRF_V1",
                label="混合检索",
                available=True,
                message="本地语义检索与关键词检索已启用",
            )
        return RetrievalStatus(
            mode="BM25_V1",
            label="关键词检索",
            available=True,
            message="当前使用关键词检索",
        )

    def get_retrieval_status(self) -> RetrievalStatus:
        """返回最近一次自动推荐所使用的检索方式。"""

        return self._retrieval_status

    def analyze_document(
        self, document_id: str, forced_role: DocumentRole | None = None
    ) -> DocumentProfileRecord:
        document = self.document_repository.get(document_id)
        if document is None:
            raise AssistedLabelingNotFound("文档不存在")
        if document.status != DocumentStatus.PARSED:
            raise AssistedLabelingValidationError("文档解析成功后才能识别类型")
        chunks = self.chunk_repository.list_for_document(document_id)
        classification = self.classifier.classify(
            document.original_name, document.extracted_text or "", chunks
        )
        return self.repository.upsert_profile(
            document_id=document_id,
            role=forced_role or classification.role,
            confidence=1.0 if forced_role else classification.confidence,
            reasons=["由用户选择文档类型"] if forced_role else classification.reasons,
            confirmed_by_user=forced_role is not None,
        )

    def list_documents(self) -> list[LabeledDocument]:
        choices: list[LabeledDocument] = []
        for document in self.document_repository.list():
            if document.status != DocumentStatus.PARSED:
                continue
            profile = self.repository.get_profile(document.id)
            if profile is None:
                profile = self.analyze_document(document.id)
            choices.append(
                LabeledDocument(document.id, document.original_name, profile.role)
            )
        return choices

    def get_profile(self, document_id: str) -> DocumentProfileRecord:
        profile = self.repository.get_profile(document_id)
        if profile is None:
            return self.analyze_document(document_id)
        return profile

    def extract_requirements(self, document_id: str) -> list[RequirementItemRecord]:
        profile = self.get_profile(document_id)
        if profile.role != DocumentRole.TASK_BRIEF:
            raise AssistedLabelingValidationError("请选择一份设计任务书")
        chunks = self.chunk_repository.list_for_document(document_id)
        drafts = self.extractor.extract(chunks)
        if not drafts:
            raise AssistedLabelingValidationError("任务书中暂未识别到明确的设计要求")
        return self.repository.replace_requirements(document_id, drafts)

    def list_requirements(self, document_id: str) -> list[RequirementItemRecord]:
        return self.repository.list_requirements(document_id)

    def get_requirement(self, requirement_id: str) -> RequirementItemRecord:
        requirement = self.repository.get_requirement(requirement_id)
        if requirement is None:
            raise AssistedLabelingNotFound("设计要求不存在")
        return requirement

    def suggest(
        self,
        requirement_id: str,
        regulation_document_id: str,
        top_k: int = 3,
    ) -> list[ClauseSuggestion]:
        requirement = self.get_requirement(requirement_id)
        regulation = self.get_profile(regulation_document_id)
        if regulation.role != DocumentRole.REGULATION:
            raise AssistedLabelingValidationError("请选择一份建筑规范")
        hits = self.search_service.search(
            regulation_document_id, requirement.query, max(top_k * 4, 10)
        )
        if hits is None:
            raise AssistedLabelingNotFound("建筑规范不存在")
        suggestions: list[ClauseSuggestion] = []
        seen: set[str] = set()
        for hit in hits:
            clause_number = hit.chunk.clause_number
            if clause_number is None or clause_number in seen:
                continue
            seen.add(clause_number)
            suggestions.append(
                ClauseSuggestion(
                    regulation_document_id=regulation_document_id,
                    regulation_name=self.document_repository.get(
                        regulation_document_id
                    ).original_name,
                    clause_number=clause_number,
                    heading_path=hit.chunk.heading_path,
                    text=hit.chunk.text,
                    page_start=hit.chunk.page_start,
                    page_end=hit.chunk.page_end,
                    score=hit.score,
                    retrieval_mode="BM25_V1",
                )
            )
            if len(suggestions) == top_k:
                break
        return suggestions

    def suggest_across_regulations(
        self,
        requirement_id: str,
        top_k: int = 3,
        eligible_regulation_document_ids: list[str] | None = None,
    ) -> list[ClauseSuggestion]:
        """从所有已确认角色的建筑规范中统一召回并排序候选条文。"""
        requirement = self.get_requirement(requirement_id)
        documents = self.list_documents()
        regulation_documents = [
            item for item in documents if item.role == DocumentRole.REGULATION
        ]
        if eligible_regulation_document_ids is not None:
            eligible = set(eligible_regulation_document_ids)
            regulation_documents = [
                item for item in regulation_documents if item.id in eligible
            ]
        if not regulation_documents:
            raise AssistedLabelingValidationError("没有可用于本次审查的现行建筑规范")
        document_names = {
            item.id: item.original_name for item in regulation_documents
        }
        candidate_count = max(top_k * 4, 10)
        retrieval_mode = "BM25_V1"
        if self.hybrid_search_service is not None:
            try:
                hits = self.hybrid_search_service.search_many(
                    list(document_names), requirement.query, candidate_count
                )
                retrieval_mode = "HYBRID_RRF_V1"
                self._retrieval_status = RetrievalStatus(
                    mode=retrieval_mode,
                    label="混合检索",
                    available=True,
                    message="本地语义检索与关键词检索已启用",
                )
            except (RuntimeError, OSError, ValueError) as error:
                # 本地模型首次下载失败或暂时无法加载时，仍允许老师继续工作。
                retrieval_mode = "BM25_FALLBACK"
                self._retrieval_status = RetrievalStatus(
                    mode=retrieval_mode,
                    label="关键词检索回退",
                    available=False,
                    message=f"本地语义模型不可用：{error}",
                )
                hits = self.search_service.search_many(
                    list(document_names), requirement.query, candidate_count
                )
        else:
            hits = self.search_service.search_many(
                list(document_names), requirement.query, candidate_count
            )
            self._retrieval_status = self._default_retrieval_status()
        suggestions: list[ClauseSuggestion] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            clause_number = hit.chunk.clause_number
            key = (hit.chunk.document_id, clause_number or "")
            if clause_number is None or key in seen:
                continue
            seen.add(key)
            suggestions.append(
                ClauseSuggestion(
                    regulation_document_id=hit.chunk.document_id,
                    regulation_name=document_names[hit.chunk.document_id],
                    clause_number=clause_number,
                    heading_path=hit.chunk.heading_path,
                    text=hit.chunk.text,
                    page_start=hit.chunk.page_start,
                    page_end=hit.chunk.page_end,
                    score=hit.score,
                    retrieval_mode=retrieval_mode,
                )
            )
            if len(suggestions) == top_k:
                break
        return suggestions

    def confirm(
        self,
        requirement_id: str,
        regulation_document_id: str,
        clause_numbers: list[str],
    ) -> RequirementConfirmationRecord:
        existing = self.repository.get_confirmation(
            requirement_id, regulation_document_id
        )
        if existing is not None:
            return existing
        requirement = self.get_requirement(requirement_id)
        selected = list(dict.fromkeys(clause_numbers))
        if not selected:
            raise AssistedLabelingValidationError("请至少选择一条规范条文")
        regulation = self.document_repository.get(regulation_document_id)
        if regulation is None:
            raise AssistedLabelingNotFound("建筑规范不存在")
        case = self.evaluation_service.create_case(
            document_id=regulation_document_id,
            query=requirement.query,
            expected_clause_numbers=selected,
            source_note=f"任务书要求：{requirement.text}",
            status=EvaluationCaseStatus.VERIFIED,
        )
        confirmation = self.repository.create_confirmation(
            requirement_id,
            regulation_document_id,
            selected,
            case.id,
        )
        self.repository.mark_requirement_status(
            requirement_id, RequirementStatus.CONFIRMED
        )
        return confirmation

    def mark_no_evidence(self, requirement_id: str) -> RequirementItemRecord:
        self.get_requirement(requirement_id)
        return self.repository.mark_requirement_status(
            requirement_id, RequirementStatus.NO_EVIDENCE
        )
