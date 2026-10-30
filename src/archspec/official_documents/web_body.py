"""官方网页正文的原件与派生排版；不把网页预览冒称官方PDF原页。"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, replace
from html.parser import HTMLParser
from pathlib import Path

from archspec.official_documents.government_urls import validate_government_url
from archspec.official_documents.processing_cache import checked_digest, file_hash

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip

MAX_HTML_BYTES = 2 * 1024 * 1024
CSS = "body{font-family:sans-serif;font-size:10pt;}p{margin:0 0 6pt;}"


class _BodyParser(HTMLParser):
    """只取公报明确的正文容器；未知页面保留线索，不猜模板。"""
    def __init__(self):
        super().__init__()
        self.depth = self.hidden = self.containers = 0
        self.parts = []
        self.has_unread_media = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if not self.depth and "html_content" in values.get("class", "").split():
            self.containers += 1
            self.depth = 1
        elif self.depth and tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.depth += 1
        if self.depth and tag in {"img", "iframe", "object", "video", "table"}:
            self.has_unread_media = True
        if self.depth and tag in {"p", "li", "tr", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.depth and not self.hidden:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1


def _dom_article_text(raw, title, *, allow_unread_media=False):
    """按条款段落的共同父节点定位全文，不依赖城市、域名或CSS类名。"""
    from lxml import etree
    from lxml import html as dom

    tree = dom.document_fromstring(raw.decode("utf-8"), parser=dom.HTMLParser(no_network=True))
    for node in tree.xpath("//script | //style | //noscript | //nav | //header | //footer | //aside"):
        node.drop_tree()
    compact = lambda value: re.sub(r"[\s《》〈〉（）()]+", "", value)
    headings = [node.text_content() for node in tree.xpath("//title | //h1 | //h2 | //h3 | //h4 | //h5 | //h6")]
    headings += tree.xpath("//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='articletitle']/@content")
    if not any(compact(title) in compact(heading) for heading in headings):
        raise ValueError("网页主标题与固定来源不一致")
    clause = re.compile(r"^(?:第[一二三四五六七八九十百零〇0-9]+条|[0-9]+(?:[.][0-9]+){1,3}|[一二三四五六七八九十]+、)")
    counts = {}
    for paragraph in tree.xpath("//p | //li"):
        if clause.match(paragraph.text_content().strip()):
            for node in paragraph.iterancestors():
                if node.tag in {"div", "section", "article", "main", "td", "body"}:
                    counts[node] = counts.get(node, 0) + 1
    if not counts or max(counts.values()) < 2:
        raise ValueError("尚未定位连贯的官方条款正文")
    # 保留最多条款，取包含它们的最小正文范围；引言、章节和续文一起保留。
    body = max(counts, key=lambda node: (counts[node], -len(node.text_content())))
    if not allow_unread_media and body.xpath(".//img | .//iframe | .//object | .//video | .//table"):
        raise ValueError("正文包含待核媒体或原表，保留官方原件线索")
    parts = []
    blocks = {"p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "br"}
    for event, node in etree.iterwalk(body, events=("start", "end")):
        if node.tag in blocks:
            parts.append("\n")
        value = node.text if event == "start" else node.tail if node is not body else None
        if value:
            parts.append(value)
    return "".join(parts).strip()


def inspect_web_body(raw: bytes, source: dict) -> dict:
    validate_government_url(source["page_url"])
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_HTML_BYTES:
        raise ValueError("网页原件大小受限，未取得可核正文")
    parser = _BodyParser()
    parser.feed(raw.decode("utf8"))
    text = "".join(parser.parts).strip()
    compact = lambda value: re.sub(r"[\s《》〈〉（）()]+", "", value)
    title_bound = compact(source["title"]) in compact(text)
    if parser.containers == 0:
        try:
            text = _dom_article_text(raw, source["title"])
        except Exception:
            raise ValueError("官方网页正文暂无法解析，保留原件线索") from None
        parser.containers, title_bound = 1, True  # 主标题已独立核验，不把标题强塞进原文。
    if (parser.containers != 1 or parser.has_unread_media or len(text) < 80
            or not title_bound
            or not re.search(r"总则|第[一二三四五六七八九十百0-9]+条|[0-9]+[.][0-9]+|[一二三四五六七八九十]+、", text)):
        raise ValueError("网页正文范围或媒体尚未核清，保留官方原件线索")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "body_text": text,
            "body_text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def capture_web_body(cache_dir: Path, raw: bytes) -> dict:
    """保存本轮已读取的字节，不为HTML正文再读取一次公告。"""
    from archspec.persistence.business_files import write_runtime_file
    digest = hashlib.sha256(raw).hexdigest()
    path = cache_dir / "web-bodies" / (digest + ".html")
    if not write_runtime_file(path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(raw)
        except FileExistsError:
            if file_hash(path) != digest:
                raise ValueError("已保存网页原件身份不同，未覆盖") from None
    return {"source_format": "HTML", "body_sha256": digest, "body_size_bytes": len(raw)}


def read_captured_body(documents, source):
    from archspec.official_documents.originals import _publisher
    digest = checked_digest(source["body_sha256"])
    path = documents.cache_dir.parent / "official-discovery" / "web-bodies" / (digest + ".html")
    # 后台任务沿documents捕获的同一云端绑定读账本，不依赖已退出HTTP的上下文。
    publisher = _publisher(documents)
    raw = publisher.read(path, missing_ok=False) if publisher is not None else path.read_bytes()
    if raw is None or len(raw) != source["body_size_bytes"] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("官方网页原件尚未完整保存")
    return raw  # 原件身份先核；正文可读性由获取入口核验并负责原件降级。


def html_processing_spec(documents):
    import pymupdf
    from importlib.metadata import version
    base = documents._processing_spec()
    return replace(base, parser_version="official-web-v1:pymupdf-" + pymupdf.VersionBind
                   + ":lxml-" + version("lxml") + ":" + LOADED_SOURCE_IDENTITY.sha256, ocr_version="no-ocr-html-v1",
                   extraction_config={**base.extraction_config, "watermark_lines": [],
                                      "source_format": "HTML", "css": CSS})


def render_web_body(original: Path, destination: Path, source: dict, max_pages: int) -> dict:
    import pymupdf
    body = inspect_web_body(original.read_bytes(), source)
    markup = "<article>" + "".join("<p>" + html.escape(line) + "</p>"
        for line in body["body_text"].splitlines() if line.strip()) + "</article>"
    story = pymupdf.Story(html=markup, user_css=CSS)
    count, more = 0, True
    temporary = destination.with_suffix(".draft.pdf")
    with pymupdf.DocumentWriter(str(temporary)) as writer:
        while more:
            if count >= max_pages:
                raise ValueError("网页排版超过来源页数预算")
            device = writer.begin_page(pymupdf.paper_rect("a4"))
            more, _ = story.place(pymupdf.Rect(30, 30, 565, 812))
            story.draw(device)
            writer.end_page()
            count += 1
    with pymupdf.open(temporary) as pdf:
        actual = "".join(page.get_text() for page in pdf)
        if "".join(actual.split()) != "".join(body["body_text"].split()):
            raise ValueError("网页排版文字不能完整核对，未发布正文")
        pdf.set_metadata({})
        identity = body["sha256"][:32]
        pdf.xref_set_key(-1, "ID", "[<" + identity + "><" + identity + ">]")
        pdf.save(destination, garbage=4, deflate=True, no_new_id=True)
    # 释放排版设备引用后才移除本次临时件，Windows同样可结束计算。
    device = writer = story = None
    temporary.unlink()
    return {"rendering_sha256": file_hash(destination), "rendering_size_bytes": destination.stat().st_size,
            "page_count": count, "page_semantics": "HTML_RENDERED"}


def prepare_web_body_cache(input_html, destination, source, max_pages):
    """只在独立计算中复用原提取/切分/编码/PNG，不登记来源或审批许可。"""
    from archspec.demo_seed import _prepare_publication_computations
    from archspec.documents.chunker import DocumentChunker, SourceBlock
    from archspec.official_documents.processing_cache import (
        PREPARED_MANIFEST,
        PUBLICATION_SCHEMA,
        ProcessingCache,
        publish_json,
    )
    from archspec.official_documents.service import OfficialDocumentService
    root = Path(destination)
    root.mkdir()
    rendering = root / "rendering.pdf"
    rendered = render_web_body(input_html, rendering, source, max_pages)
    documents = OfficialDocumentService(root)
    spec = html_processing_spec(documents)
    digest = file_hash(input_html)
    pages = documents._process(rendering, max_pages=max_pages, spec=spec)
    chunks = [asdict(chunk) for chunk in DocumentChunker(
        max_characters=spec.chunk_config["max_characters"]).chunk(tuple(
            SourceBlock(page["text"], page["pdf_page"], i) for i, page in enumerate(pages)))]
    cache = ProcessingCache(root)
    cache.save_extraction(spec, digest, pages)
    cache.save_snapshot(spec, digest, pages, chunks, rendering={
        key: rendered[key] for key in ("rendering_sha256", "rendering_size_bytes")})
    paths = [cache._path("extractions", spec.extraction_id(digest)),
             cache._path("snapshots", spec.snapshot_id(digest))]
    files = {p.relative_to(root).as_posix(): {"sha256": file_hash(p), "size_bytes": p.stat().st_size} for p in paths}
    asset = {"url": source["page_url"], "sha256": digest, "size_bytes": input_html.stat().st_size,
             "max_pages": max_pages, "source_format": "HTML",
             **{k: rendered[k] for k in ("rendering_sha256", "rendering_size_bytes")}}
    publication = _prepare_publication_computations(rendering, root, pages, chunks)
    rendering.rename(root / "publication" / "rendering.pdf")
    manifest = {"schema_version": PUBLICATION_SCHEMA, "asset": asset, "identity": spec.identity(digest),
                "extraction_id": spec.extraction_id(digest), "processing_id": spec.snapshot_id(digest),
                "page_count": len(pages), "files": files,
                "publication": publication}
    publish_json(root / PREPARED_MANIFEST, manifest)
    return manifest


def snapshot_web_spec(record):
    """重启后按固定快照的真实配置读回，不拿本次PDF身份冒充HTML提取。"""
    from archspec.official_documents.processing_cache import ProcessingSpec
    spec = ProcessingSpec(**record["source_processing_spec"])
    if (spec.identity(record["sha256"]) != record["processing_identity"]
            or spec.extraction_config.get("source_format") != "HTML"
            or spec.extraction_id(record["sha256"]) != record["extraction_id"]):
        raise ValueError("网页正文提取身份与固定快照不一致")
    return spec


def checked_rendering_path(documents, record):
    """派生PDF与原HTML分开寻址；只读快照绑定的完整字节。"""
    from archspec.official_documents.processing_cache import (
        PREPARED_MAX_FILE_BYTES,
        ProcessingCache,
    )
    path = ProcessingCache(documents.cache_dir).blob_path(checked_digest(record["rendering_sha256"]))
    size = record["rendering_size_bytes"]
    if (type(size) is not int or not 0 < size <= PREPARED_MAX_FILE_BYTES
            or path.is_symlink() or not path.is_file() or path.stat().st_size != size
            or file_hash(path) != record["rendering_sha256"]):
        raise ValueError("网页排版资料尚未完整恢复，请查看官方原件")
    return path


def acquire_web_body(documents, source, refresh, progress, computation, *, saved_extraction_only=False):
    """接回同一获取任务；不再访问公告，主网页进程不加载排版/编码器。"""
    import tempfile
    from datetime import UTC, datetime

    from archspec.official_documents.originals import save_original
    from archspec.official_documents.processing_cache import (
        ProcessingCache,
        ProcessingLimited,
        prepared_package,
    )
    from archspec.official_documents.remote_compute import remote_publication
    from archspec.persistence.acquisition_state import AcquisitionLeaseLost
    from archspec.standard_governance.compatibility import ContentUseDenied
    source_id = source["source_id"]
    catalog = documents._selected_snapshot_catalog()
    old_pointer = catalog.get(source_id) if catalog is not None else None
    old = documents.get(source_id)
    spec = documents._saved_extraction_spec(old) if saved_extraction_only else html_processing_spec(documents)
    if old and not refresh and old["processor_version"] == spec.processor_version:
        documents._check_cached_file(old, source_id)
        return {**old, "cache_hit": True}
    folder = documents.cache_dir / source_id
    folder.mkdir(parents=True, exist_ok=True)
    store = ProcessingCache(documents.cache_dir)
    if old and not refresh:
        documents._check_cached_file(old, source_id)
        raw = documents.raw_file_path(old, source_id).read_bytes()
    else:
        raw = read_captured_body(documents, source)
    digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
    # 原件先保全；接下来的计算失败只会进入原件兜底，不抹去已取得资料。
    temporary = folder / (digest + ".html")
    if not temporary.exists():
        with temporary.open("xb") as output:
            output.write(raw)
    raw_path = store.publish_blob(temporary, digest, media_type="text/html")
    checked_at = datetime.now(UTC).isoformat()
    if progress:
        progress("PROCESSING", {"source_id": source_id})
    try:
        body = inspect_web_body(raw, source)
        try:
            cached = store.load_snapshot(spec, digest)
        except ProcessingLimited:
            package = computation.enter_context(remote_publication(documents, source, digest, size, spec))
            cached = store.load_snapshot(spec, digest)
        else:
            package = None
        cache_hit = cached is not None
        if cached is None:
            if saved_extraction_only:
                raise ValueError("已保存网页提取缓存缺失，未重新获取")
            work = Path(computation.enter_context(tempfile.TemporaryDirectory(
                prefix=".web-body-", dir=documents.cache_dir)))
            package = work / "prepared"
            from archspec.official_documents.policy import policy_for_source
            prepare_web_body_cache(raw_path, package, source, policy_for_source(source).max_pages)
            computation.enter_context(prepared_package(package, source_page_url=source["page_url"]))
            cached = store.load_snapshot(spec, digest)
        if cached is None:
            raise ProcessingLimited()
        # 派生资料描述随同一不可变切分载荷保存，共享命中和重启不依赖临时包。
        rendered = cached["rendering"]
        pages, chunks = cached["pages"], cached["chunks"]
        if "".join(body["body_text"].split()) != "".join("".join(p["text"] for p in pages).split()):
            raise ValueError("网页正文与提取文字不同，保留原件")
        return documents._publish_processed(source, old, old_pointer, spec, digest, size, raw_path,
            pages, chunks, checked_at, cache_hit, cache_hit, saved_extraction_only=saved_extraction_only,
            format_fields={**rendered, "page_semantics": "HTML_RENDERED",
                "source_processing_spec": asdict(spec),
                "notice": "网页排版视图（非官方PDF页码）；版本、现行性与适用性待核。"})
    except (AcquisitionLeaseLost, ContentUseDenied):
        raise
    except Exception:  # noqa: BLE001 -- 按用户要求统一降级，原件及当前权限仍由原入口保全。
        return save_original(documents, source_id, raw_path, digest, size)


LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
