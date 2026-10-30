"""公开候选书目与本轮读取覆盖共用的缺资料判断；不判断书是否适用。"""
import re
from collections.abc import Mapping

from archspec.official_discovery.public_queries import validated_public_titles


def missing_public_titles(coverage, titles):
    """只报告无正文记录的建议；权限、范围、读取上限和缓存异常均不能借机重取。"""
    titles = validated_public_titles(titles)
    if not isinstance(coverage, list):
        return ()
    missing = []
    for title in titles:
        compact = re.sub(r"\s+", "", title)
        rows = [row for row in coverage if isinstance(row, Mapping)
                and isinstance(row.get("title"), str)
                and re.sub(r"\s+", "", row["title"]) == compact]
        # 修订/说明附件不是完整正文。任何已有正文或未核清的读取障碍都留在本地核对。
        bodies = [row for row in rows if row.get("state") != "EXCLUDED_ATTACHMENT"]
        if not bodies or all(row.get("state") == "NOT_ACQUIRED" for row in bodies):
            missing.append(title)
    return tuple(missing)
