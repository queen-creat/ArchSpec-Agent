"""面向教师页面的任务书辅助标注接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from archspec.assisted_labeling.classifier import DocumentClassifier
from archspec.assisted_labeling.extractor import RequirementExtractor
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.assisted_labeling.schemas import (
    AnalyzeDocumentRequest,
    ClauseSuggestionResponse,
    ConfirmationResponse,
    ConfirmRequirementRequest,
    DocumentProfileResponse,
    LabeledDocumentResponse,
    MessageResponse,
    RequirementResponse,
    RetrievalStatusResponse,
    SuggestAllRequest,
    SuggestRequest,
)
from archspec.assisted_labeling.service import (
    AssistedLabelingNotFound,
    AssistedLabelingService,
    AssistedLabelingValidationError,
)
from archspec.database import get_session
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.embeddings.dependencies import default_embedding_provider
from archspec.embeddings.index_service import EmbeddingIndexService
from archspec.embeddings.repository import EmbeddingRepository
from archspec.evaluation.repository import EvaluationRepository
from archspec.evaluation.service import EvaluationService
from archspec.search.bm25 import BM25Searcher
from archspec.search.hybrid import HybridSearchService
from archspec.search.semantic import SemanticSearchService
from archspec.search.service import SearchService
from archspec.search.tokenizer import LexicalTokenizer

router = APIRouter(prefix="/api/assisted-labeling", tags=["任务书辅助标注"])


def get_assisted_labeling_service(
    session: Annotated[Session, Depends(get_session)],
) -> AssistedLabelingService:
    documents = DocumentRepository(session)
    chunks = DocumentChunkRepository(session)
    search = SearchService(documents, chunks, BM25Searcher(LexicalTokenizer()))
    embeddings = EmbeddingRepository(session)
    provider = default_embedding_provider
    index = EmbeddingIndexService(chunks, embeddings, provider)
    semantic = SemanticSearchService(chunks, embeddings, index, provider)
    hybrid = HybridSearchService(search, semantic)
    evaluation = EvaluationService(
        documents,
        chunks,
        EvaluationRepository(session),
        search,
    )
    return AssistedLabelingService(
        documents,
        chunks,
        AssistedLabelingRepository(session),
        DocumentClassifier(),
        RequirementExtractor(),
        search,
        evaluation,
        hybrid,
    )


def _raise_http_error(error: Exception) -> None:
    if isinstance(error, AssistedLabelingNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/retrieval-status", response_model=RetrievalStatusResponse)
def get_retrieval_status(
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> RetrievalStatusResponse:
    return RetrievalStatusResponse.from_record(service.get_retrieval_status())


@router.get("/documents", response_model=list[LabeledDocumentResponse])
def list_documents(
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> list[LabeledDocumentResponse]:
    return [LabeledDocumentResponse.from_record(item) for item in service.list_documents()]


@router.post(
    "/documents/{document_id}/analyze", response_model=DocumentProfileResponse
)
def analyze_document(
    document_id: str,
    request: AnalyzeDocumentRequest,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> DocumentProfileResponse:
    try:
        return DocumentProfileResponse.from_record(
            service.analyze_document(document_id, request.role)
        )
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)


@router.post(
    "/task-briefs/{document_id}/extract",
    response_model=list[RequirementResponse],
)
def extract_requirements(
    document_id: str,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> list[RequirementResponse]:
    try:
        return [
            RequirementResponse.from_record(item)
            for item in service.extract_requirements(document_id)
        ]
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)


@router.get(
    "/task-briefs/{document_id}/requirements",
    response_model=list[RequirementResponse],
)
def list_requirements(
    document_id: str,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> list[RequirementResponse]:
    return [
        RequirementResponse.from_record(item)
        for item in service.list_requirements(document_id)
    ]


@router.post(
    "/requirements/{requirement_id}/suggest",
    response_model=list[ClauseSuggestionResponse],
)
def suggest_clauses(
    requirement_id: str,
    request: SuggestRequest,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> list[ClauseSuggestionResponse]:
    try:
        return [
            ClauseSuggestionResponse.from_record(item)
            for item in service.suggest(
                requirement_id, request.regulation_document_id, request.top_k
            )
        ]
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)


@router.post(
    "/requirements/{requirement_id}/suggest-all",
    response_model=list[ClauseSuggestionResponse],
)
def suggest_clauses_across_regulations(
    requirement_id: str,
    request: SuggestAllRequest,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> list[ClauseSuggestionResponse]:
    try:
        return [
            ClauseSuggestionResponse.from_record(item)
            for item in service.suggest_across_regulations(
                requirement_id, request.top_k
            )
        ]
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)


@router.post(
    "/requirements/{requirement_id}/confirm",
    response_model=ConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
)
def confirm_requirement(
    requirement_id: str,
    request: ConfirmRequirementRequest,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> ConfirmationResponse:
    try:
        return ConfirmationResponse.from_record(
            service.confirm(
                requirement_id,
                request.regulation_document_id,
                request.clause_numbers,
            )
        )
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)


@router.post(
    "/requirements/{requirement_id}/no-evidence",
    response_model=MessageResponse,
)
def mark_no_evidence(
    requirement_id: str,
    service: Annotated[AssistedLabelingService, Depends(get_assisted_labeling_service)],
) -> MessageResponse:
    try:
        service.mark_no_evidence(requirement_id)
        return MessageResponse(message="已标记为暂未找到依据")
    except (AssistedLabelingNotFound, AssistedLabelingValidationError) as error:
        _raise_http_error(error)
