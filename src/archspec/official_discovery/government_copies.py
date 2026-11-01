"""实际读取政府发布页并核主标题、规范编号及同站PDF；摘要只参与寻找入口。"""
import ast
import hashlib
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pymupdf

from archspec.official_discovery.editions import bind_editions, source_editions, without_editions
from archspec.official_discovery.local_sources import _LocalPageParser
from archspec.official_discovery.plan import non_rule_notice_title
from archspec.official_discovery.provider import MAX_BYTES
from archspec.official_discovery.public_queries import (
    LOCAL_SUNLIGHT_TITLES,
    PUBLIC_CODE,
    PUBLIC_TITLES,
    extract_public_codes,
)
from archspec.official_documents.government_urls import (
    validate_government_pair,
    validate_government_url,
)
from archspec.official_http import official_http_client

_LOCAL_CODE = re.compile(r"(?:DB\d{2,6}(?:/T)?\s*\d+(?:\.\d+)?[-—]\d{4}|[\u4e00-\u9fff]{1,16}[〔\[［]\d{4}[〕\]］]\s*\d{1,6}\s*号)")


class _GovernmentPageParser(_LocalPageParser):
    """读取通用网页标题及显式文号标签，不从正文引用列表猜文号。"""
    def __init__(self):
        super().__init__()
        self.document_title = ""
        self.document_numbers = []
        self._in_title = False
        self._number_label = False

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if self.hidden:
            return
        self._in_title = self._in_title or tag == "title"
        values = dict(attrs)
        if values.get("title") in {"文号", "发文字号"}:
            self._number_label = True

    def handle_data(self, data):
        super().handle_data(data)
        if self.hidden:
            return
        if self._in_title:
            self.document_title += data
        # 标签和值可以位于相邻元素；只跟踪明确的文号字段，不从正文引用猜编号。
        label = re.sub(r"\s+", "", data)
        if re.fullmatch(r"(?:发文字号|文号)[:：]?", label):
            self._number_label = True
        if self._number_label or re.match(r"^(?:发文字号|文号)[:：]", label):
            self.document_numbers.extend(match.group() for match in _LOCAL_CODE.finditer(data))
        if self._number_label and _LOCAL_CODE.search(data):
            self._number_label = False

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in {"li", "tr", "div"}:
            self._number_label = False
        super().handle_endtag(tag)


def _embedded_attachment_links(html):
    """只解码页面明确声明的附件数据及字面量，不执行脚本或推测下载地址。"""
    for script in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL):
        # 政府发布模板也用相邻的文件题名/地址字面量；只读声明，不执行脚本。
        literal = r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
        pair = rf"\b(?:var|let|const)\s+_t\s*=\s*({literal})\s*;\s*(?:var|let|const)\s+_u\s*=\s*({literal})\s*;"
        for declaration in re.finditer(pair, script):
            try:
                name, target = (ast.literal_eval(declaration.group(i)) for i in (1, 2))
            except (ValueError, SyntaxError):
                continue
            if isinstance(name, str) and isinstance(target, str):
                yield {"title": name, "href": target}
        for declaration in re.finditer(
                r"\b(?:var|let|const)\s+(?:attachFiles|doc_rel_document_\d+)\s*=\s*", script):
            try:
                attachments, _ = json.JSONDecoder().raw_decode(script[declaration.end():])
            except ValueError:
                continue
            if not isinstance(attachments, list):
                continue
            # 公报的原件版本列表比附件多一层数组；仍只读取明确的文件字段。
            entries = [item for group in attachments
                       for item in (group if isinstance(group, list) else [group])]
            for item in entries:
                if not isinstance(item, dict):
                    continue
                name, path = item.get("name"), item.get("relative_path")
                if isinstance(name, str) and isinstance(path, str):
                    # 后续统一核主文件题名、同站地址、PDF签名和完整原件身份。
                    yield {"title": name, "href": path}


def supports_inspection(url):
    """只按明确路径后缀跳过尚不支持的附件；查询参数不证明响应文件类型。"""
    path = unquote(urlsplit(url).path).lower()
    return not path.endswith((".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                              ".zip", ".rar", ".7z", ".gz", ".jpg", ".jpeg", ".png", ".gif"))


def inspection_priority(clue, plan):
    """可核网页/PDF先于已知不支持附件；原主文件和地域排序继续复用。"""
    priority = candidate_priority(clue, plan)
    return (priority[0], not supports_inspection(clue["url"]), *priority[1:])


def candidate_priority(clue, plan):
    """摘要只排读取顺序；目标文件优先，无关网页不能挤掉目标官方PDF。"""
    title = clue["title"]
    direct_pdf = urlsplit(clue["url"]).path.lower().endswith(".pdf")
    # 正文身份核验会排除解读页；读取排序须另看其明确引用的书名，才能先找到原文入口。
    # 这里仅调整共享五页额度的读取顺序，不把书名引用或搜索摘要升级为正文证据。
    subjects = (title, *re.findall(r"《([^》]+)》", title))
    target = any(plan.match_source(code, subject) == "TARGET"
                 for subject in subjects for code in (extract_public_codes(subject) or ("",)))
    commentary = bool(re.search(r"解读|图解|问答|答疑", title))
    # 独立文书题名先于引用它的批复/新闻；这只决定读取顺序，不替代正文身份核验。
    standalone = bool(re.fullmatch(r"《[^》]+》(?:[（(][^（）()]+[）)])?(?:[.]pdf)?", title.strip(), re.IGNORECASE))
    # 计划明确含日照/间距专项时先读同题专项，综合规划文件仍作为补充保留。
    # 只影响共同五页额度的顺序；不改主文件、地域、正文及效力核验，也不采用搜索分数。
    sunlight = plan.region is not None and any(
        kind == "LOCAL_TOPIC" and value in LOCAL_SUNLIGHT_TITLES for kind, value in plan.targets)
    specific = sunlight and bool(re.search(r"日照|间距", title))
    return (not target, commentary, not specific, not standalone, direct_pdf)


def _navigation(page, url, plan, raw):
    """只跟随实际读到的同站目标原文锚点；解读和搜索摘要永不变成正文。"""
    links = []
    for link in page.links:
        label = link.get("text") or link.get("title", "")
        target = urljoin(url, link.get("href", ""))
        try:
            validate_government_url(target)
        except ValueError:
            continue
        priority = candidate_priority({"url": target, "title": label}, plan)
        if (target == url or urlsplit(target).hostname != urlsplit(url).hostname
                or priority[0] or priority[1] or urlsplit(target).path.lower().endswith(".pdf")):
            continue
        if target not in {row["url"] for row in links}:
            links.append({"url": target, "title": label})
    if not links:
        raise ValueError("尚未取得可核对的主文件或官方原文链接，保留线索")
    return {"navigation_links": links[:5], "url": url,
            "text_sha256": hashlib.sha256(raw).hexdigest(), "body_status": "NOT_ACQUIRED"}


def _primary_edition_title(code, title, heading):
    """只保留紧邻主编号/书名的明确年版，复用请求绑定合同，不从正文年份猜测。"""
    spans = [(m.start(), m.end(), (("CODE", code),)) for m in PUBLIC_CODE.finditer(heading)]
    spans.extend((m.start(), m.end(), (("TITLE", title),))
                 for m in re.finditer(re.escape(title), heading))
    years = {year for kind, value, year in bind_editions(heading, spans)
             if (kind, value) in {("CODE", code), ("TITLE", title)}}
    if len(years) > 1:
        raise ValueError("政府主标题年版声明冲突，未选择其中一个版本")
    return f"{title}（{next(iter(years))}年版）" if years else title


def _separate_national_declaration(raw, headings):
    """只核已定位全文开头的独立书名/完整编号声明，不扫摘要、引用或页脚。"""
    from archspec.official_documents.web_body import _dom_article_text

    compact = lambda value: re.sub(r"[\s《》〈〉（）()]+", "", value)
    labels = {compact(PUBLIC_CODE.sub("", heading)) for heading in headings if heading}
    titles = [title for title in PUBLIC_TITLES if compact(title) in labels]
    if len(titles) != 1:
        return None
    title = titles[0]
    try:
        article = _dom_article_text(raw, title, allow_unread_media=True)
    except ValueError:
        return None
    # 书名必须是正文开头；至多四行英文副题，不能跨过中文正文找编号。
    title_pattern = r"\s*".join(re.escape(letter) for letter in compact(title))
    pattern = (r"\A\s*[《〈]?\s*" + title_pattern + r"\s*[》〉]?\s*"
        + r"(?:[A-Za-z][A-Za-z \t/'(),:-]*\s*\n){0,4}\s*[（(]?\s*"
        + r"(?P<code>" + PUBLIC_CODE.pattern + r")\s*[）)]?(?=\s*(?:\n|$))")
    match = re.match(pattern, article[:512], re.IGNORECASE)
    if match is None or match["year"] is None:
        return None
    following = article[match.end():].lstrip().splitlines()
    if (re.search(r"\bdraft\b", match[0], re.IGNORECASE)
            or following and re.fullmatch(r"[（(]?(?:征求意见稿|草案|送审稿|报批稿)[）)]?", following[0].strip())):
        raise ValueError("网页开头标明非正式稿件，不能作为正式规范正文")
    code = extract_public_codes(match["code"])[0]
    return code, title, match[0].strip()


def _local_identity(page, headings, plan):
    """只核实际主标题和其文号字段，不用正文引用清单或第三方摘要确定主文件。"""
    for heading in headings:
        if non_rule_notice_title(heading) or re.search(
                r"解读|图解|问答|答疑|征求意见|草案|公示|局部修订|部分条文|修改单", heading):
            continue
        # 书名号已明确主文件题名时，通知外壳不再产生第二个题名。
        labels = re.findall(r"《([^》]+)》", heading)
        if not labels:
            # 通知本身可能就是正式主文件；只有印发/发布外壳才剥离通知后缀。
            labels = [re.split(r"的通知|的公告", re.split(r"关于印发|关于发布", heading)[-1])[0]
                      if re.search(r"关于印发|关于发布", heading) else heading]
        code_text = " ".join([heading, page.document_title, *(page.meta.get(name, "") for name in
                              ("filenumber", "docnumber", "documentnumber", "wh")), *page.document_numbers])
        codes = list(dict.fromkeys(match.group().strip() for match in _LOCAL_CODE.finditer(code_text)))
        if len(codes) > 1:
            continue
        code = codes[0] if codes else ""  # 文号缺失保留未知，不伪造登记编号。
        titles = list(dict.fromkeys(title.strip(" 《》") for title in labels
                                    if plan.match_source(code, title.strip(" 《》")) == "TARGET"))
        if len(titles) == 1:
            return code, titles[0], heading
    raise ValueError("地方主文件的题名、文号或地域尚未核清，保留官方线索")


def _inspect_government_pdf(url, plan, client, cache_dir):
    """政府PDF本身就是发布件；只核首页身份，全文计算仍由现有独立服务承担。"""
    from archspec.official_documents.downloader import download_pdf_file
    with tempfile.TemporaryDirectory(prefix=".inspect-official-", dir=cache_dir) as temporary:
        downloaded = download_pdf_file(url, Path(temporary), client=client, page_url=url)
        try:
            with pymupdf.open(downloaded.path) as pdf:
                lines = [line.strip() for line in pdf[0].get_text().splitlines() if line.strip()][:16]
        except Exception:  # noqa: BLE001 -- PDF库异常统一保留线索，不输出未核首页身份。
            raise ValueError("官方PDF首页暂无法辨认，保留原件线索") from None
        page = _GovernmentPageParser()
        # 只读封面独立文号行；正文引用的其他文件编号不能成为本文件文号。
        page.document_numbers = [line for line in lines[:6] if _LOCAL_CODE.fullmatch(line)]
        headings = ["".join(lines[start:start + width]) for width in (1, 2, 3) for start in range(len(lines))]
        if plan.region is not None:
            code, title, heading = _local_identity(page, headings, plan)
        else:
            matches = [(code, title) for heading in headings for code in extract_public_codes(heading)
                       for title in PUBLIC_TITLES if title in heading and plan.match_source(code, title) == "TARGET"]
            matches = list(dict.fromkeys(matches))
            if len(matches) != 1:
                raise ValueError("官方PDF主文件身份尚未核清，保留原件线索")
            code, title = matches[0]
            heading = title
        digest = downloaded.sha256
        return {"url": url, "title": heading, "standard_title": title, "standard_code": code,
                "authority": None, "scope": "LOCAL" if plan.region is not None else "NATIONAL" if code.startswith("GB") else "INDUSTRY",
                **({"region": plan.region} if plan.region is not None else {}),
                "notice_type": "OFFICIAL_COPY", "version_check": "OFFICIAL_COPY_ONLY",
                "status": "PENDING_REVIEW", "lifecycle_status": "UNKNOWN", "text_sha256": digest,
                "evidence_excerpt": "\n".join(lines), "attachments": [{"title": title + ".pdf", "url": url,
                    "expected_original_sha256": digest}], "full_text_available": False,
                "warnings": ["已读取政府PDF首页核题名；发布机关、文号、版本及现行性未核字段仍待核对"]}


def inspect_government_copy(clue, plan, *, client=None, capture_original=None, cache_dir=None):
    url = validate_government_url(clue["url"])
    owned = client is None
    client = client or official_http_client()
    try:
        if urlsplit(url).path.lower().endswith(".pdf"):
            return _inspect_government_pdf(url, plan, client, cache_dir)
        from archspec.official_documents.official_fetch import open_public_response
        with open_public_response(client, url, MAX_BYTES, timeout=8, follow_redirects=False,
                           headers={"User-Agent": "ArchSpec-Agent/0.1 official-copy"}) as response:
            response.raise_for_status()
            content = bytearray()
            for part in response.iter_bytes():
                content.extend(part)
                if len(content) > MAX_BYTES:
                    raise ValueError("政府发布页超出读取上限，正文尚未取得")
            raw = bytes(content)
        page = _GovernmentPageParser()
        html = raw.decode("utf-8")
        page.feed(html)
        page.links.extend(_embedded_attachment_links(html))
        headings = [page.meta.get("articletitle", ""), *page.subject_titles,
                    re.split(r"[-_|]政务公开", page.document_title)[0].strip()]
        # 非正式/解读页仅能沿明确正式原文链接继续，不能直接登记其附件或正文。
        if any(non_rule_notice_title(heading) or re.search(
                r"征求意见|草案|送审稿|报批稿|局部修订|部分条文|修改单|补充(?:规定|细则|通知|说明)|解读|图解|问答|答疑", heading)
                for heading in headings if heading):
            return _navigation(page, url, plan, raw)
        identity_excerpt = None
        # 主标题与正文开头独立声明可以共同核身份，摘要和引用清单始终不参与。
        if plan.region is not None:
            try:
                code, title, observed_heading = _local_identity(page, headings, plan)
            except ValueError:
                return _navigation(page, url, plan, raw)
        else:
            matches = []
            for heading in headings:
                codes = extract_public_codes(heading)
                if len(codes) != 1:
                    continue
                title = next((t for t in sorted(PUBLIC_TITLES, key=len, reverse=True) if t in heading), None)
                title = title or PUBLIC_CODE.sub("", heading).strip(" 《》()（）-—")
                if title:
                    title = _primary_edition_title(codes[0], title, heading)
                if title and plan.match_source(codes[0], title) == "TARGET":
                    matches.append((codes[0], title, heading))
            matches = list({(code, title): heading for code, title, heading in matches}.items())
            declaration = _separate_national_declaration(raw, headings)
            heading_codes = {code for heading in headings for code in extract_public_codes(heading)}
            if declaration and heading_codes and (heading_codes != {declaration[0]}):
                raise ValueError("网页主标题与正文开头编号声明冲突，未选择其中一个版本")
            if not matches and not heading_codes and declaration and plan.match_source(
                    declaration[0], declaration[1]) == "TARGET":
                code, title, identity_excerpt = declaration
                observed_heading = title
            elif len(matches) == 1:
                (code, title), observed_heading = matches[0]
            else:
                raise ValueError("政府发布页主规范暂无法核对，保留搜索线索")
        attachments = []
        compact = lambda text: re.sub(r"[\s《》〈〉（）()]+", "", text)
        for link in page.links:
            target = urljoin(url, link.get("href", ""))
            label = link.get("title") or link.get("text", "")
            if compact(without_editions(title)) not in compact(without_editions(label)):
                continue
            editions, attachment_editions = source_editions(code, title), source_editions(None, label)
            if editions and attachment_editions and editions != attachment_editions:
                continue
            other_codes = extract_public_codes(label)
            if other_codes and code not in other_codes:
                continue
            try:
                validate_government_pair(url, target)
            except ValueError:
                continue
            item = {"title": label, "url": target}
            if item not in attachments:
                attachments.append(item)
        if capture_original is not None:
            from archspec.official_documents.web_body import _dom_article_text, inspect_web_body
            readable = False
            try:
                inspect_web_body(raw, {"page_url": url, "title": title, "standard_code": code})
                readable = True
            except ValueError:
                try:
                    # 范围已定位但含表图：只保全原件，不把媒体文字当作可引用正文。
                    _dom_article_text(raw, title, allow_unread_media=True)
                    readable = True
                except ValueError:
                    pass
            if readable:
                descriptor = capture_original(raw)
                attachments.insert(0, {"title": title + "（官方网页原件）", "url": url, **descriptor})
        if not attachments:
            return _navigation(page, url, plan, raw)
        return {"url": url, "title": observed_heading, "standard_title": title,
                "standard_code": code, "authority": None,
                "scope": "LOCAL" if plan.region is not None else "NATIONAL" if code.startswith("GB") else "INDUSTRY",
                **({"region": plan.region} if plan.region is not None else {}),
                "notice_type": "OFFICIAL_COPY", "version_check": "OFFICIAL_COPY_ONLY",
                "status": "PENDING_REVIEW", "lifecycle_status": "UNKNOWN",
                "text_sha256": hashlib.sha256(raw).hexdigest(), "evidence_excerpt": identity_excerpt or observed_heading,
                "attachments": attachments[:10], "full_text_available": False,
                "warnings": ["政府公开副本；发布机关、版本、现行性及适用条件仍待核对"]}
    finally:
        if owned:
            client.close()
