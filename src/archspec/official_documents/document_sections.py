"""只依据固定页面的明确说明封页区分文档部分，不修改提取或快照。"""
import re

from archspec.official_discovery.editions import without_editions
from archspec.official_discovery.public_queries import extract_public_codes
from archspec.official_documents.clause_units import CLAUSE, DIRECTORY


def commentary_boundary(record):
    """未知、HTML或多重封页保持未知，不借目录和正文中的引用推断分区。"""
    if record.get("source_format", "PDF") != "PDF":
        return None
    from archspec.official_documents.read_views import IndexedSnapshot, TransientSearchSnapshot

    if isinstance(record, (IndexedSnapshot, TransientSearchSnapshot)):
        return record.commentary_boundary_page
    codes = extract_public_codes(record.get("standard_code", ""))
    title = re.sub(r"[\s《》〈〉（）()]+", "", without_editions(record.get("title", "")))
    if len(codes) != 1 or not title:
        return None
    boundaries = []
    for page in record.get("pages", ()):
        text = page.get("text", "")
        if not isinstance(text, str) or len(text) > 1000:
            continue
        lines = [re.sub(r"\s+", "", line) for line in text.splitlines() if line.strip()]
        if ("条文说明" not in lines or "目录" in "".join(lines)
                or DIRECTORY.search(text) or len("".join(lines)) > 240):
            continue
        if title not in re.sub(r"[\s《》〈〉（）()]+", "", without_editions(text)):
            continue
        if extract_public_codes(text) != codes:
            continue
        number = page.get("pdf_page")
        if type(number) is int and number > 1:
            boundaries.append(number)
    return boundaries[0] if len(boundaries) == 1 else None


def document_section(record, start, end):
    """MAIN_TEXT仅指说明封页前的文档部分，不提供正文完整性或工程效力证明。"""
    boundary = commentary_boundary(record)
    if boundary is None:
        return None
    kind = "MAIN_TEXT" if end < boundary else "COMMENTARY" if start >= boundary else "MIXED"
    return {"kind": kind, "boundary_page": boundary, "basis": "EXPLICIT_COMMENTARY_COVER"}


def prefer_primary_clauses(hits, origins):
    """只交换同来源、同固定快照、同条号的已召回候选，不增搜、不改分数。"""
    boundaries = {}
    for record in origins.values():
        if id(record) not in boundaries:
            boundaries[id(record)] = commentary_boundary(record)
    def kind(chunk):
        boundary = boundaries[id(origins[chunk.id])]
        if boundary is None:
            return None
        return ("MAIN_TEXT" if chunk.page_end < boundary else
                "COMMENTARY" if chunk.page_start >= boundary else "MIXED")
    def key(chunk):
        record = origins[chunk.id]
        return record["source_id"], record["snapshot_id"], chunk.clause_number
    primary = {}
    for chunk, _ in hits:
        if (CLAUSE.fullmatch(chunk.clause_number or "") and kind(chunk) == "MAIN_TEXT"
                and not DIRECTORY.search(chunk.text)):
            primary.setdefault(key(chunk), chunk.id)
    output, positions = list(hits), {chunk.id: i for i, (chunk, _) in enumerate(hits)}
    swaps = []
    for i in range(len(output)):
        chunk = output[i][0]
        target = primary.get(key(chunk)) if kind(chunk) == "COMMENTARY" else None
        j = positions.get(target, -1)
        if j > i:
            other = output[j][0]
            output[i], output[j] = output[j], output[i]
            positions[chunk.id], positions[other.id] = j, i
            swaps.append({"primary_id": other.id, "commentary_id": chunk.id})
    return output, swaps


def section_rows(record):
    """只给本次分组派生分区标记；旧固定片段和处理身份不变。"""
    boundary = commentary_boundary(record)
    if boundary is None:
        return record["chunks"]
    return [{**row, "document_part": ("MAIN_TEXT" if row["page_end"] < boundary else
            "COMMENTARY" if row["page_start"] >= boundary else "MIXED")}
            for row in record["chunks"]]
