"""实际图表提及的共同词法；附图页只导航，不证明图意、数值或续页完整。"""

import re

MENTION = re.compile(r"(附?图|表)\s*([A-Z](?:[-.]\d+)*|\d+(?:[-.]\d+)*)(?![A-Za-z\d])")
HEAD = re.compile(r"^(附?图)\s*([A-Z](?:[-.]\d+)*|\d+(?:[-.]\d+)*)(?![A-Za-z\d])(?:\s+(.+))?$")
DIRECTORY = re.compile(r"[.…⋯·]{2,}")


def artifact_mentions(text):
    """保留实际原字和码点偏移，读取入口与图表缺口沿同一词法。"""
    return MENTION.finditer(text)


def figure_page_references(text, pages):
    """只找实际独立图题；不从目录印刷页号、相邻页或提取数字推导范围。"""
    mentions = {}
    for match in artifact_mentions(text):
        if match[1] == "表":
            continue
        label = match[1] + match[2]
        mentions.setdefault(label, {"label": label, "original": match[0],
                                    "start": match.start(), "end": match.end()})
    selected = list(mentions)[:8]
    targets = {label: [] for label in selected}
    for page in pages:
        lines = [line.strip() for line in page["text"].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            match = HEAD.fullmatch(line)
            if match is None or DIRECTORY.search(line):
                continue
            label = match[1] + match[2]
            if label not in targets:
                continue
            title = match[3] or (lines[index + 1] if index + 1 < len(lines) else "")
            # 独立图号后还须有真实题名；正文引用句、目录和图内数值不充当图题。
            if (not title or len(title) > 120 or DIRECTORY.search(title)
                    or re.match(r"[\d（(]|的|规定|所示|中|应|要求|内", title)
                    or re.search(r"[。；;！？]", title) or HEAD.fullmatch(title)):
                continue
            number = page["pdf_page"]
            if any(row["pdf_page"] == number for row in targets[label]):
                continue
            targets[label].append({"pdf_page": number,
                                   "caption": line if match[3] else line + "\n" + title})
    return {"figure_references": [{**mentions[label], "targets": targets[label][:8],
                                    "targets_truncated": len(targets[label]) > 8}
                                   for label in selected],
            "references_truncated": len(mentions) > 8}
