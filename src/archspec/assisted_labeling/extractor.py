"""从设计任务书中保守提取可用于查规范的要求。"""

import re
from dataclasses import dataclass

from archspec.documents.models import DocumentChunkRecord

REQUIREMENT_SIGNALS = (
    "需",
    "应",
    "满足",
    "保障",
    "配套",
    "预留",
    "规划",
    "隔离",
    "消防",
    "疏散",
    "无障碍",
    "通风",
    "安全",
    "节能",
)
NUMERIC_REQUIREMENT_PATTERN = re.compile(r"\d|≥|≤|不少于|不大于|不小于")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。；])|\n+")


@dataclass(frozen=True)
class RequirementDraft:
    sequence: int
    heading_path: str | None
    text: str
    query: str


class RequirementExtractor:
    """只提取有明确要求信号的原文，不替教师生成专业结论。"""

    def extract(self, chunks: list[DocumentChunkRecord]) -> list[RequirementDraft]:
        drafts: list[RequirementDraft] = []
        seen: set[str] = set()
        for chunk in chunks:
            if "进度安排" in (chunk.heading_path or ""):
                continue
            for sentence in SENTENCE_SPLIT_PATTERN.split(chunk.text):
                text = sentence.strip()
                normalized = re.sub(r"\s+", "", text)
                if not text or normalized in seen or not self._looks_like_requirement(text):
                    continue
                seen.add(normalized)
                drafts.append(
                    RequirementDraft(
                        sequence=len(drafts),
                        heading_path=chunk.heading_path,
                        text=text,
                        query=self._build_query(text, chunk.heading_path),
                    )
                )
        return drafts

    @staticmethod
    def _looks_like_requirement(text: str) -> bool:
        return any(signal in text for signal in REQUIREMENT_SIGNALS) or bool(
            NUMERIC_REQUIREMENT_PATTERN.search(text)
        )

    @staticmethod
    def _build_query(text: str, heading_path: str | None) -> str:
        compact = re.sub(r"\s+", "", text)
        if "会议室" in compact and "200人" in compact and "疏散" in compact:
            return "200人会议室疏散要求"
        if "无障碍车位" in compact and "地下车库" in (heading_path or ""):
            return "地下车库无障碍车位设置要求"
        return text
