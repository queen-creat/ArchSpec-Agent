"""使用可解释规则区分规范、任务书和其他文档。"""

from dataclasses import dataclass

from archspec.assisted_labeling.models import DocumentRole
from archspec.documents.models import DocumentChunkRecord

TASK_BRIEF_KEYWORDS = (
    "毕业设计任务书",
    "设计任务书",
    "课题基本信息",
    "核心功能设计要求",
    "毕业设计进度安排",
)


@dataclass(frozen=True)
class Classification:
    role: DocumentRole
    confidence: float
    reasons: list[str]


class DocumentClassifier:
    """分类理由同时返回给教师，避免神秘的黑盒判断。"""

    def classify(
        self,
        name: str,
        text: str,
        chunks: list[DocumentChunkRecord],
    ) -> Classification:
        clause_count = sum(chunk.clause_number is not None for chunk in chunks)
        if clause_count >= 2:
            return Classification(
                DocumentRole.REGULATION,
                0.95,
                [f"发现 {clause_count} 个规范式条款"],
            )

        combined = f"{name}\n{text}"
        matched = [keyword for keyword in TASK_BRIEF_KEYWORDS if keyword in combined]
        if len(matched) >= 2:
            return Classification(
                DocumentRole.TASK_BRIEF,
                min(0.95, 0.7 + len(matched) * 0.05),
                [f"识别到任务书特征：{keyword}" for keyword in matched],
            )

        return Classification(
            DocumentRole.OTHER,
            0.5,
            ["规范条款和任务书特征均不足"],
        )
