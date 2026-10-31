"""一个主来源最多一个已核备用；独立任务，不借用主来源快照或处理权。"""
import re

from archspec.official_discovery.editions import source_editions, without_editions
from archspec.official_discovery.public_queries import normalize_standard_code

POLICY = "BOUNDED_OFFICIAL_FALLBACK_V1"
SOURCE_FAILURES = frozenset({"ATTACHMENT_SIZE_LIMIT", "ATTACHMENT_INVALID_PDF"})
ACTIVE = frozenset({"QUEUED", "DOWNLOADING", "PROCESSING"})


def document_key(source):
    """编号年份、书名及明确年版均须一致；不以相似题名猜版本。"""
    code = normalize_standard_code(source.get("standard_code"))
    title = source.get("title")
    if not code or not re.search(r"-\d{4}$", code) or not isinstance(title, str) or not title.strip():
        return None
    name = re.sub(r"[\s《》〈〉]", "", without_editions(title))
    editions = tuple(sorted(source_editions(code, title)))
    return (code, name, editions) if name and len(editions) <= 1 else None


def fallback_action(job, jobs):
    """普通任务照常；备用等主任务终结，只有明确来源失败才能执行。"""
    parent_id = job.get("fallback_for")
    if parent_id is None:
        return "RUN"
    parent = jobs[parent_id]
    if parent["state"] in ACTIVE:
        return "WAIT"
    if parent["state"] == "FAILED" and parent.get("failure", {}).get("code") in SOURCE_FAILURES:
        return "RUN"
    return "SKIP"


def checked_fallback(job, jobs):
    """读取时重核依赖，拒绝循环、跨版本和同来源伪备用。"""
    parent_id = job.get("fallback_for")
    if parent_id is None:
        return job.get("state") != "SKIPPED"
    parent = jobs.get(parent_id) if isinstance(parent_id, str) else None
    key = document_key(job)
    return (parent is not None and parent is not job and "fallback_for" not in parent
            and parent.get("kind") == job.get("kind") == "FULL_TEXT"
            and parent.get("source_id") != job.get("source_id")
            and key is not None and key == document_key(parent))
