"""编排项目资料的上传、分类和项目关联。"""

from __future__ import annotations

from archspec.documents.models import DocumentStatus
from archspec.documents.service import DocumentService, UploadDocument
from archspec.project_materials.classifier import ProjectMaterialClassifier
from archspec.project_materials.models import (
    MaterialClassification,
    MaterialClassificationStatus,
    MaterialRecommendationReport,
    MaterialStandardRecommendation,
    MaterialTopic,
    ProjectMaterialRecord,
)
from archspec.project_materials.recommendations import MaterialTopicDetector
from archspec.project_materials.repository import ProjectMaterialRepository
from archspec.projects.models import ProjectDocumentType
from archspec.projects.service import ProjectService
from archspec.reviews.models import ReviewJobRecord, ReviewStepRecord
from archspec.reviews.service import ReviewService
from archspec.standard_catalog.service import StandardCatalogService


class ProjectMaterialError(ValueError):
    pass


class ProjectMaterialNotFound(ProjectMaterialError):
    pass


class ProjectMaterialValidationError(ProjectMaterialError):
    pass


class ProjectMaterialService:
    def __init__(
        self,
        documents: DocumentService,
        projects: ProjectService,
        classifier: ProjectMaterialClassifier,
        repository: ProjectMaterialRepository | None = None,
        reviews: ReviewService | None = None,
        catalog: StandardCatalogService | None = None,
    ) -> None:
        self.documents = documents
        self.projects = projects
        self.classifier = classifier
        self.repository = repository or ProjectMaterialRepository(
            projects.repository.session
        )
        self.reviews = reviews
        self.catalog = catalog
        self.topic_detector = MaterialTopicDetector()

    def upload(
        self,
        project_id: str,
        command: UploadDocument,
        requested_type: ProjectDocumentType | None = None,
    ) -> ProjectMaterialRecord:
        self.projects.get(project_id)
        document = self.documents.upload_and_parse(command)
        if requested_type is not None:
            classification = MaterialClassification(
                requested_type,
                1.0,
                MaterialClassificationStatus.CONFIRMED,
                ["使用者上传时指定资料类型"],
            )
        else:
            classification = self.classifier.classify(
                document.original_name, document.extracted_text or ""
            )
        link = self.projects.attach_document(
            project_id, document.id, classification.document_type
        )
        self.repository.save(link.id, classification)
        return ProjectMaterialRecord(link, classification)

    def list(self, project_id: str) -> list[ProjectMaterialRecord]:
        links = self.projects.list_documents(project_id)
        return [
            ProjectMaterialRecord(
                link,
                self.repository.get(link.id, link.document_type)
                or MaterialClassification(
                    link.document_type,
                    1.0,
                    MaterialClassificationStatus.CONFIRMED,
                    ["由已有项目资料记录迁移"],
                ),
            )
            for link in links
        ]

    def confirm_type(
        self,
        project_id: str,
        link_id: str,
        document_type: ProjectDocumentType,
    ) -> ProjectMaterialRecord:
        self._get_material(project_id, link_id)
        link = self.projects.repository.update_document_type(link_id, document_type)
        classification = MaterialClassification(
            document_type,
            1.0,
            MaterialClassificationStatus.CONFIRMED,
            ["使用者确认资料类型"],
        )
        self.repository.save(link_id, classification)
        return ProjectMaterialRecord(link, classification)

    def reprocess(self, project_id: str, link_id: str) -> ProjectMaterialRecord:
        material = self._get_material(project_id, link_id)
        self.documents.reprocess(material.document_id)
        link = self.projects.repository.get_document_link_by_id(link_id)
        if link is None:
            raise ProjectMaterialNotFound("项目资料不存在")
        classification = self.repository.get(link_id, link.document_type)
        if classification is None:
            classification = material.classification
        return ProjectMaterialRecord(link, classification)

    def start_review(
        self, project_id: str, link_id: str
    ) -> tuple[ReviewJobRecord, list[ReviewStepRecord]]:
        material = self._get_material(project_id, link_id)
        if material.link.document_status != DocumentStatus.PARSED:
            raise ProjectMaterialValidationError("项目资料解析成功后才能发起审查")
        if self.reviews is None:
            raise ProjectMaterialValidationError("审查服务尚未配置")
        regulation_ids = [
            item.id
            for item in self.reviews.assisted_service.list_documents()
            if item.role.value == "REGULATION"
        ]
        eligible = self.reviews.standards_service.verified_current_document_ids(
            regulation_ids
        )
        if not eligible:
            raise ProjectMaterialValidationError(
                "中央规范库当前没有来源已核验的现行规范正文"
            )
        return self.reviews.start(material.document_id, trusted_only=True)

    def recommendations(
        self, project_id: str, link_id: str
    ) -> MaterialRecommendationReport:
        """根据已解析资料列出建议核对的规范，而不是生成规范条文。"""

        material = self._get_material(project_id, link_id)
        document = self.documents.get(material.document_id)
        if (
            document is None
            or document.status != DocumentStatus.PARSED
            or not document.extracted_text
        ):
            raise ProjectMaterialValidationError("项目资料解析成功后才能生成规范清单")
        if self.catalog is None:
            raise ProjectMaterialValidationError("中央规范目录尚未配置")

        detected = self.topic_detector.detect(document.extracted_text)
        standards: list[MaterialStandardRecommendation] = []
        seen_codes: set[str] = set()
        for topic in detected:
            matches = self.catalog.search(topic.search_query, limit=8)
            preferred = next(
                (
                    item
                    for item in matches
                    if item.standard_code == topic.preferred_standard_code
                ),
                None,
            )
            if preferred is None or preferred.standard_code in seen_codes:
                continue
            seen_codes.add(preferred.standard_code)
            terms = "、".join(f"“{term}”" for term in topic.matched_terms)
            standards.append(
                MaterialStandardRecommendation(
                    standard_code=preferred.standard_code,
                    title=preferred.title,
                    lifecycle_status=preferred.lifecycle_status,
                    official_url=preferred.official_url,
                    full_text_available=preferred.full_text_available,
                    matched_topics=[topic.label],
                    reason=f"资料出现{terms}，建议核对{topic.label}相关要求。",
                    verification_status=preferred.verification_status,
                    verified_at=preferred.verified_at,
                    content_permission=preferred.content_permission,
                )
            )

        topics = [
            MaterialTopic(item.key, item.label, item.matched_terms)
            for item in detected
        ]
        notice = (
            "以下是根据资料关键词生成的规范目录建议，不是合规结论；"
            "正文尚未入库的规范，请到官方页面核对现行原文。"
            if standards
            else "没有识别到明确专业主题，可使用快速查规范补充描述设计问题。"
        )
        return MaterialRecommendationReport(
            material_id=material.id,
            document_id=material.document_id,
            topics=topics,
            standards=standards,
            notice=notice,
        )

    def _get_material(
        self, project_id: str, link_id: str
    ) -> ProjectMaterialRecord:
        self.projects.get(project_id)
        link = self.projects.repository.get_document_link_by_id(link_id)
        if link is None or link.project_id != project_id:
            raise ProjectMaterialNotFound("当前项目中不存在这份资料")
        classification = self.repository.get(link.id, link.document_type)
        return ProjectMaterialRecord(
            link,
            classification
            or MaterialClassification(
                link.document_type,
                1.0,
                MaterialClassificationStatus.CONFIRMED,
                ["已有项目资料"],
            ),
        )

    @staticmethod
    def can_start_review(record: ProjectMaterialRecord) -> bool:
        return record.link.document_status == DocumentStatus.PARSED
