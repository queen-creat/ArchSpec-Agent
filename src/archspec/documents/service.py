"""文档上传业务流程：校验、保存、解析并记录结果。"""

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from archspec.documents.chunker import DocumentChunker, NoValidChunks
from archspec.documents.models import DocumentChunkRecord, DocumentRecord, DocumentStatus
from archspec.documents.parser import (
    DOCX_MEDIA_TYPE,
    SUPPORTED_MEDIA_TYPES,
    DocumentParseError,
    DocumentParser,
)
from archspec.documents.repository import DocumentChunkRepository, DocumentRepository

MEDIA_TYPE_EXTENSIONS = {
    "text/plain": ".txt",
    DOCX_MEDIA_TYPE: ".docx",
    "application/pdf": ".pdf",
}


class DocumentUploadError(ValueError):
    """上传阶段可直接告诉用户的错误。"""


class EmptyUpload(DocumentUploadError):
    """文件没有任何字节。"""


class FileTooLarge(DocumentUploadError):
    """文件超过系统配置的最大值。"""


class UnsupportedUploadType(DocumentUploadError):
    """文件格式不受支持。"""


@dataclass(frozen=True)
class UploadDocument:
    name: str
    media_type: str
    content: bytes


class DocumentService:
    """Service（业务服务）编排一次上传所需的全部动作。"""

    def __init__(
        self,
        repository: DocumentRepository,
        chunk_repository: DocumentChunkRepository,
        parser: DocumentParser,
        chunker: DocumentChunker,
        upload_dir: Path,
        max_file_bytes: int,
    ) -> None:
        self.repository = repository
        self.chunk_repository = chunk_repository
        self.parser = parser
        self.chunker = chunker
        self.upload_dir = upload_dir
        self.max_file_bytes = max_file_bytes

    def upload_and_parse(self, command: UploadDocument) -> DocumentRecord:
        self._validate(command)
        document_id = str(uuid4())
        stored_name = f"{document_id}{MEDIA_TYPE_EXTENSIONS[command.media_type]}"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        stored_path = self.upload_dir / stored_name
        from archspec.persistence.business_files import write_runtime_file
        if not write_runtime_file(stored_path, command.content, session=self.repository.session):
            stored_path.write_bytes(command.content)

        record = DocumentRecord(
            id=document_id,
            original_name=command.name,
            stored_name=stored_name,
            media_type=command.media_type,
            size_bytes=len(command.content),
            sha256=hashlib.sha256(command.content).hexdigest(),
            status=DocumentStatus.UPLOADED,
            extracted_text=None,
            error_message=None,
            created_at=datetime.now(UTC),
        )
        self.repository.create(record)

        try:
            parsed = self.parser.parse(stored_path, command.media_type)
            drafts = self.chunker.chunk(parsed.blocks)
            self.chunk_repository.replace_for_document(
                document_id,
                drafts,
                commit=False,
            )
            updated = self.repository.update_parse_result(
                document_id,
                text=parsed.text,
                status=DocumentStatus.PARSED,
                error=None,
                commit=False,
            )
            self.repository.commit()
            return updated
        except (DocumentParseError, NoValidChunks) as error:
            # 原文件和失败记录均保留，教师或开发者才能知道问题出在哪里。
            self.repository.rollback()
            self.chunk_repository.replace_for_document(document_id, [], commit=False)
            failed = self.repository.update_parse_result(
                document_id,
                text=None,
                status=DocumentStatus.FAILED,
                error=str(error),
                commit=False,
            )
            self.repository.commit()
            return failed

    def get(self, document_id: str) -> DocumentRecord | None:
        self._check_read(document_id, "LOCAL_FULL_TEXT_READ")
        return self.repository.get(document_id)

    def list_documents(self) -> list[DocumentRecord]:
        from archspec.standard_governance.compatibility import ContentUseDenied
        result = []
        for record in self.repository.list():
            try:
                self._check_read(record.id, "LOCAL_FULL_TEXT_READ")
                result.append(record)
            except ContentUseDenied:
                # 保留题录，不删历史文字；列表不能成为全文读取的旁路。
                result.append(replace(record, extracted_text=None,
                                      error_message="使用条件限制：仅显示题录，不返回正文"))
        return result

    def reprocess(self, document_id: str) -> DocumentRecord | None:
        """用当前解析和切分规则安全重建既有文档片段。"""
        self._check_read(document_id, "LOCAL_FULL_TEXT_READ")
        record = self.repository.get(document_id)
        if record is None:
            return None

        try:
            parsed = self._parse_verified_original(record)
            drafts = self.chunker.chunk(parsed.blocks)
            self.chunk_repository.replace_for_document(
                document_id, drafts, commit=False
            )
            updated = self.repository.update_parse_result(
                document_id,
                text=parsed.text,
                status=DocumentStatus.PARSED,
                error=None,
                commit=False,
            )
            self.repository.commit()
            return updated
        except (DocumentParseError, NoValidChunks) as error:
            self.repository.rollback()
            raise DocumentUploadError(f"重新处理失败：{error}") from error

    def _parse_verified_original(self, record):
        """Parse a bounded verified copy; never rewrite the uploaded original."""
        from tempfile import TemporaryDirectory

        from archspec.persistence.business_files import assert_runtime_file

        name = record.stored_name
        if not name or Path(name).name != name or name in {".", ".."}:
            raise DocumentUploadError("上传原件路径无法核对，原有解析结果已保留")
        original = self.upload_dir / name
        if original.is_symlink():
            raise DocumentUploadError("上传原件路径无法核对，原有解析结果已保留")
        assert_runtime_file(original)
        try:
            with TemporaryDirectory(prefix="archspec-verified-input-") as folder:
                verified = Path(folder) / name
                digest, size = hashlib.sha256(), 0
                with original.open("rb") as source, verified.open("wb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        if size > record.size_bytes:
                            raise DocumentUploadError("上传原件与登记不一致，原有解析结果已保留")
                        digest.update(block)
                        target.write(block)
                if size != record.size_bytes or digest.hexdigest() != record.sha256:
                    raise DocumentUploadError("上传原件与登记不一致，原有解析结果已保留")
                return self.parser.parse(verified, record.media_type)
        except OSError as error:
            raise DocumentUploadError("上传原件无法读取，原有解析结果已保留") from error

    def list_chunks(self, document_id: str) -> list[DocumentChunkRecord] | None:
        """返回有序片段；None 专门表示文档本身不存在。"""
        if self.repository.get(document_id) is None:
            return None
        self._check_read(document_id, "LOCAL_FULL_TEXT_READ")
        return self.chunk_repository.list_for_document(document_id)

    def _check_read(self, document_id, operation):
        from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
        LegacyAdmissionAdapter(self.repository.session, self.upload_dir.parent).assert_document_read(
            document_id, operation)

    def _validate(self, command: UploadDocument) -> None:
        if not command.content:
            raise EmptyUpload("不能上传空文件")
        if command.media_type not in SUPPORTED_MEDIA_TYPES:
            raise UnsupportedUploadType(f"不支持的文件类型：{command.media_type}")
        if len(command.content) > self.max_file_bytes:
            raise FileTooLarge(
                f"文件大小超过限制：最大 {self.max_file_bytes} 字节"
            )
