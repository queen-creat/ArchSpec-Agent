"""按照标题和条款结构，把文档整理成可检索的小片段。"""

import re
from dataclasses import dataclass

from archspec.documents.parser import SourceBlock

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip
# 本轮识别三级或四级数字条款，例如 4.2.12 / 4.2.1.12。
# 两级数字可能是尺寸（1.0倍、0.90m）或章节，不据此另起条款。
# 编号后不能紧接数字或“.数字”，避免正则回退并截短编号；
# 正文允许为空，保留单独占一行的完整编号，再接上后续行。
CLAUSE_PATTERN = re.compile(
    r"^(?P<number>\d+(?:\.\d+){2,3})(?!\d|\.\d)[.、]?\s*(?P<body>.*)$"
)
# 中文条号原样保存；不把“第×条规定的……”这类折行引用另起正文。
ARTICLE_NUMBER = r"第[一二三四五六七八九十百千万零〇0-9]+条"
ARTICLE_PATTERN = re.compile(rf"^(?P<number>{ARTICLE_NUMBER})(?P<body>.*)$")
ARTICLE_REFERENCE = re.compile(r"^(?:规定|要求|所|的|执行|及第|和第|至第)")
HEADING_PATTERN = re.compile(r"^第[一二三四五六七八九十百\d]+[章节篇]\s*.+$")
# 行首“4.1.2 条执行”是上一句折行后的引用，不是4.1.2的新正文。
# 只认明确引用用语，不能误伤“条形基础”等以“条”字开头的真实正文。
CLAUSE_REFERENCE_BODY = re.compile(r"^条(?:\s|[，。；、]|执行|规定|要求|所|的(?:规定|要求)|和|及|至|款|$)")
# 独立附录/附表标题结束上一条，编号不必是阿拉伯数字；行内“附录A中的”不算标题。
ANNEX_HEADING = re.compile(
    r"^附(?:录|表|件)\s*[A-Za-zＡ-Ｚａ-ｚ一二三四五六七八九十百\d]+(?:\s*[:：、.．]\s*.*|\s+.*)?$"
)
# 只认独立附录表题；“按表A.0.1规定”等行内引用不证明该页是表格。
ANNEX_TABLE_CAPTION = re.compile(
    r"^(?:续)?表\s*[A-Za-zＡ-Ｚａ-ｚ](?:\s*[.．]\s*\d+){1,3}(?:\s+[^。；;]{1,60})?$"
)
LEVEL_ONE_PATTERN = re.compile(r"^[一二三四五六七八九十百]+、\s*.+$")
LEVEL_TWO_PATTERN = re.compile(r"^（[一二三四五六七八九十百]+）\s*.+$")
NUMERIC_HEADING = re.compile(r"^(?P<number>\d+(?:\.\d+)?)[ \t]+(?P<title>\S.*)$")
STAGE_PATTERN = re.compile(
    r"^(?P<title>第[一二三四五六七八九十百]+阶段)"
    r"(?:[（(][^）)]*[）)])?\s*[:：]?\s*(?P<body>.*)$"
)


def clause_start(text):
    """正文行首编号的共同判断，供切分与固定原页定位使用。"""
    number = CLAUSE_PATTERN.match(text)
    if number and not CLAUSE_REFERENCE_BODY.match(number["body"]):
        return number
    article = ARTICLE_PATTERN.match(text)
    if (article and not ARTICLE_REFERENCE.match(article["body"].lstrip())
            and not re.search(r"[.…⋯·]{2,}\s*\d", text)):
        return article
    return None


class NoValidChunks(ValueError):
    """正文无法生成任何有效片段。"""


@dataclass(frozen=True)
class ChunkDraft:
    """尚未写入数据库的规范小卡片。"""

    text: str
    page_start: int | None
    page_end: int | None
    clause_number: str | None
    heading_path: str | None


@dataclass
class _PendingChunk:
    texts: list[str]
    pages: list[int | None]
    clause_number: str | None
    heading_path: str | None


class DocumentChunker:
    """优先按条款切分，条款过长时再按自然语句边界切分。"""

    def __init__(self, max_characters: int = 800) -> None:
        if max_characters < 1:
            raise ValueError("最大片段长度必须大于 0")
        self.max_characters = max_characters

    def chunk(self, blocks: tuple[SourceBlock, ...]) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        pending: _PendingChunk | None = None
        level_one_heading: str | None = None
        level_two_heading: str | None = None

        # 一个 PDF 页面或 TXT 文件内部可能包含多条规范，先按非空行展开，
        # 同时继承原来源页码，才能逐行识别条款开头。
        expanded_blocks = [
            SourceBlock(line.strip(), block.page_number, block.block_index)
            for block in blocks
            for line in block.text.splitlines()
            if line.strip()
        ]

        annex_table_pages = {block.page_number for block in expanded_blocks
                             if block.page_number is not None
                             and ANNEX_TABLE_CAPTION.fullmatch(block.text)}
        seen_body_numbers = set()
        unresolved = self._unresolved_numeric_boundaries(expanded_blocks)

        for index, block in enumerate(expanded_blocks):
            text = block.text.strip()
            if not text:
                continue

            if index in unresolved:
                drafts.extend(self._finish(pending))
                if unresolved[index]:
                    level_two_heading = text
                # 连续疑似漏点行只证明应停止沿用旧条；原字保留，不补点或猜新条号。
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                    clause_number=None, heading_path=self._heading_path(level_one_heading, level_two_heading))
                continue

            annex_heading = bool(ANNEX_HEADING.fullmatch(level_one_heading or ""))
            annex_table = annex_heading and block.page_number in annex_table_pages
            # 表中的两级编号不是新章节，不能清掉已经取得的附录路径。
            numeric = None if annex_table else NUMERIC_HEADING.fullmatch(text)
            if numeric and self._confirmed_numeric_heading(numeric, expanded_blocks[index + 1:index + 9]):
                drafts.extend(self._finish(pending))
                if "." in numeric["number"]:
                    level_two_heading = text
                else:
                    level_one_heading, level_two_heading = text, None
                # 数字标题单独保存；不删原文，不让下一章挂在上一条的续页中。
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                    clause_number=None, heading_path=self._heading_path(level_one_heading, level_two_heading))
                continue

            if ANNEX_HEADING.fullmatch(text):
                drafts.extend(self._finish(pending))
                level_one_heading, level_two_heading = text, None
                # 标题原样保留为无条号片段，不把附录正文继续挂到上一条。
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                                        clause_number=None, heading_path=text)
                continue

            # 数字条款中的“一、/（一）”可能是条内枚举，不能当任务书标题丢掉。
            # 真正的第X章/节/篇仍结束当前条款；标题路径与原行分别保存。
            inside_numbered_clause = pending is not None and pending.clause_number is not None
            # 数字正文之后的独立短标题，只有后接实际括号单元且前句已经结束时
            # 才确认新父标题；条内带条件的枚举行继续归原条款，不丢正文。
            named_parent = (inside_numbered_clause and LEVEL_ONE_PATTERN.fullmatch(text)
                            and len(text) <= 60 and not re.search(r"[。；，：:…]|应|必须|不得", text)
                            and pending.texts[-1].rstrip().endswith("。")
                            and index + 1 < len(expanded_blocks)
                            and LEVEL_TWO_PATTERN.match(expanded_blocks[index + 1].text))
            if HEADING_PATTERN.match(text) or (
                LEVEL_ONE_PATTERN.match(text) and (not inside_numbered_clause or named_parent)
            ):
                drafts.extend(self._finish(pending))
                level_one_heading = text
                level_two_heading = None
                # 标题也可能带有条件正文，不能只放进标签而从检索文字中丢掉。
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                    clause_number=None, heading_path=level_one_heading)
                continue

            if LEVEL_TWO_PATTERN.match(text) and not inside_numbered_clause:
                drafts.extend(self._finish(pending))
                level_two_heading = text
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                    clause_number=None, heading_path=self._heading_path(level_one_heading, level_two_heading))
                continue

            stage_match = STAGE_PATTERN.match(text)
            if stage_match:
                drafts.extend(self._finish(pending))
                level_two_heading = stage_match.group("title")
                # 原阶段行保留冒号、正文与标题；空正文标题也必须可读取。
                pending = _PendingChunk(texts=[text], pages=self._page_list(block.page_number),
                    clause_number=None, heading_path=self._heading_path(level_one_heading, level_two_heading))
                continue

            clause_match = clause_start(text)
            if clause_match:
                drafts.extend(self._finish(pending))
                number = clause_match.group("number")
                # 同时具备附录路径、该页独立表题、此前正文同号三项证明，
                # 才将重复行保留为无条号阅读片段；不删原字，不猜表格数值。
                reference = annex_table and number in seen_body_numbers
                if not annex_heading:
                    seen_body_numbers.add(number)
                pending = _PendingChunk(
                    texts=[text],
                    pages=self._page_list(block.page_number),
                    clause_number=None if reference else number,
                    heading_path=self._heading_path(
                        level_one_heading, level_two_heading
                    ),
                )
                continue

            if pending is None:
                pending = _PendingChunk(
                    texts=[text],
                    pages=self._page_list(block.page_number),
                    clause_number=None,
                    heading_path=self._heading_path(
                        level_one_heading, level_two_heading
                    ),
                )
            else:
                pending.texts.append(text)
                pending.pages.append(block.page_number)

        drafts.extend(self._finish(pending))
        if not drafts:
            raise NoValidChunks("文档没有生成有效片段")
        return drafts

    @staticmethod
    def _unresolved_numeric_boundaries(blocks):
        """短节标题后连续1、2开头的疑似连号只作待核边界，不推定三级编号。"""
        boundaries = {}
        parent_pattern = re.compile(r"(?P<number>[1-9]\d*\.\d+)\s+[\u4e00-\u9fff]{2,30}")
        for index, block in enumerate(blocks):
            parent = parent_pattern.fullmatch(block.text)
            if parent is None:
                continue
            child_pattern = re.compile(re.escape(parent["number"])
                + r"(?P<suffix>[1-9]\d?)(?![\d.])\s*(?P<body>[\u4e00-\u9fff].*)$")
            children = []
            for offset, following in enumerate(blocks[index+1:index+161], 1):
                text = following.text
                child = child_pattern.match(text)
                if child and re.search(r"应|宜|不得|可以|设置", child["body"]):
                    if int(child["suffix"]) != len(children) + 1:
                        break
                    children.append(index + offset)
                    # 节后近邻必须先有两个连续独立起点，不能凭单个数值认结构。
                    if len(children) == 2 and offset > 8:
                        children = []
                        break
                    continue
                numeric = NUMERIC_HEADING.fullmatch(text)
                if (clause_start(text) or ANNEX_HEADING.fullmatch(text)
                        or HEADING_PATTERN.match(text) or numeric and "." in numeric["number"]):
                    break
            if len(children) >= 2:
                boundaries[index] = True
                boundaries.update(dict.fromkeys(children, False))
        return boundaries

    @staticmethod
    def _confirmed_numeric_heading(match, following):
        """后续实际条号须属于这个章节；纯尺寸、条内枚举和目录不提供证明。"""
        title = match["title"]
        if (len(title) > 60 or any(mark in title for mark in ("。", "；", "，", "…"))
                or re.match(r"(?:倍|米|平方米|mm\b|cm\b|m\b|%|℃|°)", title, re.IGNORECASE)):
            return False
        for block in following[:3]:
            text = block.text.strip()
            # 紧邻的章节或页脚可跳过，普通正文不能跨过去猜章节。
            clause = CLAUSE_PATTERN.match(text)
            if clause:
                return (not CLAUSE_REFERENCE_BODY.match(clause["body"])
                        and clause["number"].startswith(match["number"] + "."))
            if not NUMERIC_HEADING.fullmatch(text) and not text.isdigit():
                break
        # 首条的开头可能漏识别；后面两个独立且递增的实际子条号仍能证明节边界。
        # 中间无编号文字只归此节，绝不反推它的条号；遇其它章节条号立即停止。
        prefix = match["number"] + "."
        children = []
        for block in following:
            clause = clause_start(block.text.strip())
            if clause is None:
                continue
            number = clause["number"]
            if (not number.startswith(prefix) or not number[len(prefix):].isdigit()):
                return False
            value = int(number[len(prefix):])
            if children and value <= children[-1]:
                return False
            children.append(value)
            if len(children) == 2:
                return True
        return False

    @staticmethod
    def _page_list(page_number: int | None) -> list[int | None]:
        # 与texts逐行对齐，未知页不能使后续行错位。
        return [page_number]

    @staticmethod
    def _heading_path(level_one: str | None, level_two: str | None) -> str | None:
        parts = [part for part in (level_one, level_two) if part]
        return " > ".join(parts) if parts else None

    def _finish(self, pending: _PendingChunk | None) -> list[ChunkDraft]:
        if pending is None:
            return []
        text = "\n".join(part for part in pending.texts if part.strip()).strip()
        if not text:
            return []
        # 切片页码只来自实际重叠的原行；不能让整条的首末页污染每一片。
        lines, offset = [], 0
        for line, page in zip(pending.texts, pending.pages, strict=True):
            lines.append((offset, offset + len(line), page))
            offset += len(line) + 1
        drafts, cursor = [], 0
        for part in self._split_text(text):
            start = text.index(part, cursor)
            end = start + len(part)
            pages = [page for left, right, page in lines
                     if page is not None and left < end and right > start]
            drafts.append(ChunkDraft(
                text=part, page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                clause_number=pending.clause_number, heading_path=pending.heading_path))
            cursor = end
        return drafts

    def _split_text(self, text: str) -> list[str]:
        parts: list[str] = []
        remaining = text.strip()
        while len(remaining) > self.max_characters:
            window = remaining[: self.max_characters]
            boundary = max(window.rfind(mark) for mark in ("\n", "。", "；"))
            split_at = boundary + 1 if boundary >= 0 else self.max_characters
            if part := remaining[:split_at].strip():
                parts.append(part)
            remaining = remaining[split_at:].strip()
        if remaining:
            parts.append(remaining)
        return parts
LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
