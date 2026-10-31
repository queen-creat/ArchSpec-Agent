"""规范文档 REST API（通过网址操作文档的接口）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from archspec.config import settings
from archspec.database import get_session
from archspec.documents.chunker import DocumentChunker
from archspec.documents.local_references import build_router
from archspec.documents.parser import DocumentParser
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository
from archspec.documents.schemas import DocumentChunkResponse, DocumentResponse
from archspec.documents.service import (
    DocumentService,
    DocumentUploadError,
    FileTooLarge,
    UploadDocument,
)

router = APIRouter(prefix="/api/documents", tags=["documents"])


def get_document_service(
    session: Annotated[Session, Depends(get_session)],
) -> DocumentService:
    """组装业务服务；单独做成依赖后，自动测试可以安全替换数据库。"""
    return DocumentService(
        DocumentRepository(session),
        DocumentChunkRepository(session),
        DocumentParser(),
        DocumentChunker(),
        settings.upload_dir,
        settings.max_file_bytes,
    )


router.include_router(build_router(get_document_service))


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    service: Annotated[DocumentService, Depends(get_document_service)],
    file: Annotated[UploadFile, File(description="TXT、DOCX 或文本型 PDF")],
) -> DocumentResponse:
    """接收教师上传的文件，保存原件，并立即尝试提取文字。"""
    try:
        record = service.upload_and_parse(
            UploadDocument(
                name=file.filename or "未命名文件",
                media_type=file.content_type or "application/octet-stream",
                content=await file.read(),
            )
        )
    except FileTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except DocumentUploadError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return DocumentResponse.from_record(record)


@router.get("", response_model=list[DocumentResponse])
def list_documents(
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> list[DocumentResponse]:
    """返回可供普通网页选择的文档清单。"""
    return [DocumentResponse.from_record(record) for record in service.list_documents()]


@router.post("/{document_id}/reprocess", response_model=DocumentResponse)
def reprocess_document(
    document_id: str,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """让旧文档使用最新结构识别规则，不要求用户重新上传。"""
    try:
        record = service.reprocess(document_id)
    except DocumentUploadError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if record is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocumentResponse.from_record(record)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    record = service.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocumentResponse.from_record(record)


@router.get("/{document_id}/chunks", response_model=list[DocumentChunkResponse])
def get_document_chunks(
    document_id: str,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> list[DocumentChunkResponse]:
    chunks = service.list_chunks(document_id)
    if chunks is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return [DocumentChunkResponse.from_record(chunk) for chunk in chunks]
