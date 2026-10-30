"""明确年版请求的原字绑定；不以编号年份、发布日期或摘要证明年版。"""
import re

EDITION = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年?\s*版")
_GAP = re.compile(r"[\s《》〈〉（）()的]*")


def bind_editions(query, spans):
    """紧邻公开引用才绑定；多个可能目标或无法对应的年版明确保持未解决。"""
    rows = []
    for match in EDITION.finditer(query):
        # 已登记题名内部的年版属于题名原字，仍由原题名合同保护。
        if any(start <= match.start() and match.end() <= end for start, end, _ in spans):
            continue
        candidates = set()
        ambiguous = False
        for start, end, targets in spans:
            gap = query[end:match.start()] if end <= match.start() else (
                query[match.end():start] if match.end() <= start else None)
            if gap is None or not _GAP.fullmatch(gap):
                continue
            if len(targets) == 1:
                candidates.add(targets[0])
            else:
                ambiguous = True
        target = next(iter(candidates)) if len(candidates) == 1 and not ambiguous else ("UNRESOLVED", "")
        rows.append((*target, match[1]))
    return tuple(dict.fromkeys(rows))


def source_editions(code, title):
    """只读当前来源两个主字段中的显式年版，不能从标准编号后缀借年份。"""
    return {match[1] for value in (code, title) if isinstance(value, str) for match in EDITION.finditer(value)}


def without_editions(value):
    """只供书名/编号身份核对；对外原字段和原件完全不改。"""
    if not isinstance(value, str):
        return value
    return re.sub(r"[（(]\s*[）)]", "", EDITION.sub("", value)).strip()
