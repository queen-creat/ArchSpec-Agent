"""固定片段中的条号出现位置；连续切片才是续文，不按条号跨全书合并。"""

import re

from archspec.documents.chunker import ARTICLE_NUMBER, clause_start

ARTICLE = re.compile(ARTICLE_NUMBER)
CLAUSE = re.compile(r"\d+(?:\.\d+){2,3}|" + ARTICLE_NUMBER)
DIRECTORY = re.compile(r"[.…⋯·]{2,}\s*\d")


def clause_runs(rows):
    """返回每片段所属的连续条款位置，不改原字、页码或快照身份。

    同号在附录、说明、目录等位置再次出现时是独立候选。未取得完整结构时
    不猜是哪一种正文，只按固定片段顺序和标题边界阻止远处同号误拼。
    页码有空隙可能是未提取到文字的页，不能单凭空隙丢弃真实续文。
    """
    groups, current, previous = [], None, None
    for position, row in enumerate(rows):
        index = row.get("index", position)
        number = row.get("clause_number")
        valid = CLAUSE.fullmatch(number or "") is not None
        # 再次出现完整中文条头是新位置；切片续文和条内折行引用仍沿原组。
        start = clause_start(row["text"].strip()) if ARTICLE.fullmatch(number or "") else None
        adjacent = (valid and start is None and previous is not None and current is not None
            and index == previous[0] + 1 and number == previous[1].get("clause_number")
            and row.get("heading_path") == previous[1].get("heading_path")
            and not DIRECTORY.search(row["text"])
            and not DIRECTORY.search(previous[1]["text"]))
        if not valid:
            current = None
        elif adjacent:
            current.append(index)
        else:
            current = [index]
            groups.append(current)
        previous = index, row
    return {index: tuple(group) for group in groups for index in group}


def clause_indices(rows, selected_index):
    """普通阅读和原页定位使用相同物理范围；无条号保留单片段。"""
    return clause_runs(rows).get(selected_index, (selected_index,))


def scanned_clause_gaps(record, indices):
    """只读检测当前扫描条文之后的同层级缺号；不猜缺字或把风险传给后条。"""
    selected = [record["chunks"][index] for index in indices]
    findings = []
    for page in record["pages"]:
        if page.get("method") not in {"OCR", "HYBRID"}:
            continue
        targets = {row.get("clause_number") for row in selected
                   if row["page_start"] <= page["pdf_page"] <= row["page_end"]}
        if not targets:
            continue
        ocr_numbers = set()
        for row in page.get("raw_lines", []):
            anchor = clause_start(row.get("text", "").strip())
            if (anchor and row.get("origin") == "OCR" and row.get("included_in_text")):
                ocr_numbers.add(anchor["number"])
        previous = None
        for line in page.get("text", "").splitlines():
            anchor = clause_start(line.strip())
            if not anchor:
                continue
            number = anchor["number"]
            if not re.fullmatch(r"\d+(?:\.\d+){2,3}", number):
                previous = None
                continue
            parts = tuple(int(value) for value in number.split("."))
            if previous:
                prior, prior_parts = previous
                if (prior in targets and parts[:-1] == prior_parts[:-1]
                        and parts[-1] > prior_parts[-1] + 1
                        and (page["method"] == "OCR" or prior in ocr_numbers)):
                    findings.append({"kind": "OCR_CLAUSE_NUMBER_GAP", "clause_number": prior,
                        "next_clause_number": number, "pdf_page": page["pdf_page"],
                        "message": "扫描正文的相邻条号存在缺口，不能排除漏行或跨条串接；保留文字供原页核对，不作为完整摘录。"})
            previous = number, parts
    return findings
