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
            and row.get("document_part") != "MIXED"
            and row.get("document_part") == previous[1].get("document_part")
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



def clause_label_issues(rows, indices):
    """固定标签须与片段开头的明确条头一致；只报告，不改旧标签或原字。

    续文没有独立条头时不猜归属；折行引用沿共同clause_start规则排除。
    只检查当前完整阅读单元，不把邻接条文的问题传给所选正文。
    """
    findings = []
    for index in indices:
        row = rows[index]
        label = row.get("clause_number")
        if not CLAUSE.fullmatch(label or ""):
            continue
        first = next((line.strip() for line in row["text"].splitlines() if line.strip()), "")
        anchor = clause_start(first)
        if anchor is None or anchor["number"] == label:
            continue
        findings.append({"kind": "CLAUSE_LABEL_MISMATCH", "clause_number": label,
            "observed_clause_number": anchor["number"], "chunk_index": index,
            "page_start": row["page_start"], "page_end": row["page_end"],
            "message": "固定条号标签与正文起始条号不一致；保留原文和原页，不作为完整摘录。"})
    return findings

def _scanned_list_gaps(record, selected):
    """整条续文一起核显式列表跳号；只报告风险，不补编号或原字。"""
    if not selected or not any(page.get("method") in {"OCR", "HYBRID"}
            and any(row["page_start"] <= page["pdf_page"] <= row["page_end"] for row in selected)
            for page in record["pages"]):
        return []
    text = "\n".join(row["text"] for row in selected)
    introduction = re.search(r"(?:下列|以下)(?:规定|要求|条件|措施)\s*[：:]", text)
    if introduction is None:
        return []
    previous, style, findings = None, None, []
    for line in text[introduction.end():].splitlines():
        stripped = line.strip()
        anchor = clause_start(stripped)
        if (anchor and CLAUSE.fullmatch(anchor["number"]) or re.match(
                r"^(?:注(?:释)?|附注|条文说明)(?:[：:\d\s]|$)|^(?:表|图)\s*\d", stripped)):
            break
        marker = re.match(r"^([1-9]\d?)([、.)）．]|[ \t]+)?[ \t]*(?=[\u3400-\u9fff])", stripped)
        if marker is None:
            continue
        number, separator = int(marker[1]), marker[2]
        current_style = ("plain" if separator is None or separator.isspace()
                         else "paren" if separator in {"）", ")"} else separator.replace("．", "."))
        if style is None:
            if separator is None and number != 1:
                continue
            style, previous = current_style, 0
        if current_style != style:
            continue  # 内嵌括号编号不混入外层序列。
        if separator is None and number != previous + 1:
            continue  # 无分隔数字可能是人数/层数等续行，只用连续项防止假缺号。
        if number <= previous:
            break  # 同样格式重起或倒序时不猜列表层级。
        if number > previous + 1:
            missing = list(range(previous + 1, number))
            findings.append({"kind": "OCR_LIST_NUMBER_GAP",
                "clause_number": selected[0].get("clause_number"),
                "previous_item_number": previous or None, "next_item_number": number,
                "missing_item_numbers": missing[:20], "missing_item_count": len(missing),
                "page_start": min(row["page_start"] for row in selected),
                "page_end": max(row["page_end"] for row in selected),
                "message": "扫描条文的编号分项存在跳项，不能排除条件漏识别；保留原文和原页，本次不作为完整摘录。"})
            if len(findings) >= 8:
                break
        previous = number
    return findings


def scanned_clause_gaps(record, indices):
    """只读核扫描条文的编号分项及相邻同层条号；不补文字或把风险传给后条。"""
    selected = [record["chunks"][index] for index in indices]
    findings = _scanned_list_gaps(record, selected)
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
