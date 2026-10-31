"""用户自带PDF的本机查阅；不注册成政府来源，也不进入自动答案证据池。"""
import re
from contextlib import contextmanager
from math import isfinite
from typing import Annotated
from urllib.parse import quote

import pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from archspec.assisted_labeling.models import DocumentProfileRow, DocumentRole
from archspec.assisted_labeling.repository import AssistedLabelingRepository
from archspec.documents.models import DocumentRow
from archspec.documents.schemas import DocumentChunkResponse
from archspec.documents.service import (
    DocumentService,
    DocumentUploadError,
    FileTooLarge,
    UploadDocument,
)
from archspec.search.bm25 import BM25Searcher
from archspec.search.tokenizer import LexicalTokenizer

HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class ReferenceSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=8, ge=1, le=10)


def summary(record):
    return {"id": record.id, "original_name": record.original_name, "sha256": record.sha256,
            "status": record.status, "has_text": bool(record.extracted_text),
            "source_kind": "LOCAL_REFERENCE", "is_citable": False}


@contextmanager
def fixed_reference(service, document_id, sha256, operation):
    """先核角色、请求身份及当前使用条件，再消费与登记SHA相同的稳定副本。"""
    record = service.repository.get(document_id)
    profile = AssistedLabelingRepository(service.repository.session).get_profile(document_id)
    if (record is None or record.media_type != "application/pdf"
            or profile is None or profile.role != DocumentRole.REGULATION):
        raise HTTPException(404, "本机参考PDF不存在")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256) or sha256 != record.sha256:
        raise HTTPException(409, "文件身份已变化，请重新选择本机PDF")
    service._check_read(document_id, operation)
    try:
        with service.verified_original(record) as path:
            yield record, path
    except DocumentUploadError:
        raise HTTPException(409, "原件无法核对，请检查文件或重新导入") from None


@contextmanager
def opened_pdf(path):
    """只读取已核副本，不执行PDF脚本，不识别或外发扫描图片。"""
    try:
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass or pdf.page_count < 1:
                raise HTTPException(422, "PDF需要密码或没有页面，请先在本机处理后重新导入")
            yield pdf
    except (pymupdf.FileDataError, RuntimeError, ValueError):
        raise HTTPException(422, "PDF无法读取，请核对原文件") from None


def build_router(get_service):
    router = APIRouter(prefix="/local-references", tags=["本机PDF"])
    Service = Annotated[DocumentService, Depends(get_service)]

    @router.get("")
    def list_references(service: Service, response: Response):
        response.headers.update(HEADERS)
        # 清单只取题录，不为列文件名加载全部PDF提取正文。
        statement = select(
            DocumentRow.id, DocumentRow.original_name, DocumentRow.sha256, DocumentRow.status,
            (func.length(func.coalesce(DocumentRow.extracted_text, "")) > 0).label("has_text"),
        ).join(DocumentProfileRow, DocumentProfileRow.document_id == DocumentRow.id).where(
            DocumentRow.media_type == "application/pdf", DocumentProfileRow.role == DocumentRole.REGULATION,
        ).order_by(DocumentRow.created_at.desc())
        return [{**row._mapping, "source_kind": "LOCAL_REFERENCE", "is_citable": False}
                for row in service.repository.session.execute(statement)]

    @router.post("", status_code=201)
    async def upload_reference(service: Service, response: Response,
                               file: Annotated[UploadFile, File()]):
        if file.content_type != "application/pdf":
            raise HTTPException(415, "请选择PDF文件")
        content = await file.read(service.max_file_bytes + 1)
        if len(content) > service.max_file_bytes:
            raise HTTPException(413, "PDF超过本机文件大小限制")
        if not content.startswith(b"%PDF-"):
            raise HTTPException(400, "文件不是可识别的PDF")
        try:
            record = service.upload_and_parse(UploadDocument(
                file.filename or "本机参考.pdf", "application/pdf", content))
        except FileTooLarge:
            raise HTTPException(413, "PDF超过本机文件大小限制") from None
        except DocumentUploadError:
            raise HTTPException(400, "PDF未能保存，请核对文件") from None
        # 用户主动导入的是参考资料角色；这不认证发布机关、版本或正文准确性。
        AssistedLabelingRepository(service.repository.session).upsert_profile(
            record.id, DocumentRole.REGULATION, 1.0, ["用户导入本机参考PDF"], True)
        response.headers.update(HEADERS)
        return summary(record)

    @router.get("/{document_id}/{sha256}")
    def info(document_id: str, sha256: str, service: Service, response: Response):
        with fixed_reference(service, document_id, sha256, "ORIGINAL_PAGE") as (record, path), opened_pdf(path) as pdf:
            result = {**summary(record), "page_count": pdf.page_count}
        response.headers.update(HEADERS)
        return result

    @router.post("/{document_id}/{sha256}/search")
    def search(document_id: str, sha256: str, request: ReferenceSearch,
               service: Service, response: Response):
        with fixed_reference(service, document_id, sha256, "LOCAL_SEARCH") as (record, _):
            service._check_read(document_id, "LOCAL_EXCERPT")
            chunks = service.chunk_repository.list_for_document(document_id)
            hits = BM25Searcher(LexicalTokenizer()).search(request.query.strip(), chunks, request.top_k)
            result = {**summary(record), "results": [
                {**DocumentChunkResponse.from_record(hit.chunk).model_dump(), "score": hit.score}
                for hit in hits]}
        response.headers.update(HEADERS)
        return result

    @router.get("/{document_id}/{sha256}/pages/{page_number}.png")
    def page(document_id: str, sha256: str, page_number: int, service: Service):
        with fixed_reference(service, document_id, sha256, "ORIGINAL_PAGE") as (_, path), opened_pdf(path) as pdf:
            if not 1 <= page_number <= pdf.page_count:
                raise HTTPException(404, "PDF中没有这一页")
            original = pdf[page_number - 1]
            area = original.rect.get_area()
            if not isfinite(area) or area <= 0 or area * 2.56 > 16_000_000:
                raise HTTPException(422, "页面过大，请下载原件在本机查看")
            content = original.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False).tobytes("png")
        return Response(content, media_type="image/png", headers=HEADERS)

    @router.get("/{document_id}/{sha256}/original")
    def original(document_id: str, sha256: str, service: Service):
        with fixed_reference(service, document_id, sha256, "LOCAL_FULL_TEXT_READ") as (record, path):
            content = path.read_bytes()
        return Response(content, media_type="application/pdf", headers={
            **HEADERS, "Content-Disposition": "attachment; filename*=UTF-8''" + quote(record.original_name, safe="")})

    return router
