"""使用可解释规则判断项目资料类型。"""

from archspec.project_materials.models import (
    MaterialClassification,
    MaterialClassificationStatus,
)
from archspec.projects.models import ProjectDocumentType


class ProjectMaterialClassifier:
    """分类只用于减少表单操作，使用者仍可修改建议类型。"""

    RULES = (
        (
            ProjectDocumentType.PRELIMINARY_DESCRIPTION,
            ("初步设计说明", "初设说明"),
            "识别到初步设计说明特征",
        ),
        (
            ProjectDocumentType.DESIGN_DESCRIPTION,
            ("施工图设计说明", "施工图说明"),
            "识别到施工图设计说明特征",
        ),
        (
            ProjectDocumentType.SCHEME_DESCRIPTION,
            ("方案设计说明", "方案说明"),
            "识别到方案说明特征",
        ),
        (
            ProjectDocumentType.PROJECT_REQUIREMENT,
            ("设计任务书", "任务书", "项目要求", "设计条件", "成果要求"),
            "识别到项目要求或任务书特征",
        ),
    )

    def classify(self, name: str, text: str) -> MaterialClassification:
        combined = f"{name}\n{text}"
        for document_type, keywords, reason in self.RULES:
            matched = [keyword for keyword in keywords if keyword in combined]
            if matched:
                return MaterialClassification(
                    document_type=document_type,
                    confidence=min(0.95, 0.72 + len(matched) * 0.06),
                    status=MaterialClassificationStatus.SUGGESTED,
                    reasons=[reason, f"匹配：{'、'.join(matched)}"],
                )
        return MaterialClassification(
            document_type=ProjectDocumentType.OTHER,
            confidence=0.4,
            status=MaterialClassificationStatus.NEEDS_REVIEW,
            reasons=["没有识别到明确的任务书或设计说明特征"],
        )
