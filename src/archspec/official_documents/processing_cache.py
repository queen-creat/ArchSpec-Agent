"""共享计算结果，不共享来源的许可；只有完整、指纹匹配的产物才能复用。"""

import hashlib
import json
import math
import os
import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

PREPARED_ENV = "ARCHSPEC_PREPARED_CACHE_DIR"
PREPARED_SCHEMA = "ARCHSPEC_PREPARED_PROCESSING_V1"
PREPARED_MANIFEST = "prepared-processing.json"
PREPARED_MAX_FILE_BYTES = 32 * 1024 * 1024
PUBLICATION_SCHEMA = "ARCHSPEC_PREPARED_PUBLICATION_V1"
PUBLICATION_INDEX_SCHEMA = "ARCHSPEC_PREPARED_PUBLICATION_INDEX_V1"
PUBLICATION_MODE_ENV = "ARCHSPEC_RUNTIME_PUBLICATION_COMPUTE"
_PREPARED_PACKAGE = ContextVar("archspec_request_prepared_package", default=None)


@contextmanager
def prepared_package(root, *, source_page_url, source_policy=None):
    """只在当前已取得原件的流程借用内容包，不改变全局镜像或其它查询。"""
    token = _PREPARED_PACKAGE.set((str(Path(root).absolute()), source_page_url, source_policy))
    try:
        yield
    finally:
        _PREPARED_PACKAGE.reset(token)


def _prepared_location():
    return _PREPARED_PACKAGE.get() or (os.getenv(PREPARED_ENV), None, None)


class ProcessingLimited(ValueError):
    """小内存策略明确拒绝现场重计算，不生成替代正文或向量。"""

    def __init__(self):
        super().__init__("当前实例仅允许已预备的完整计算产物；这份资料尚未预备，"
                         "正文未发布、旧资料保留。请在构建期准备后显式重试原任务。")


def publication_compute_mode():
    """本机默认保留原处理能力；部署须显式选择小内存策略。"""
    mode = os.getenv(PUBLICATION_MODE_ENV, "native")
    if mode not in {"native", "prepared_only"}:
        raise ValueError("正文计算策略配置无效")
    return mode


def _read_prepared(root, relative, maximum, entry=None):
    """仅读取固定包内有界普通文件；入口、目录和叶子均不接受链接。"""
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("预备计算缓存路径无效")
    path = root / relative
    if (any(item.is_symlink() for item in (path, *path.parents))
            or not path.is_file() or not path.resolve().is_relative_to(root)
            or not 0 < path.stat().st_size <= maximum):
        raise ValueError("预备计算缓存文件缺失或超出预算")
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if not 0 < len(content) <= maximum:
        raise ValueError("预备计算缓存读取超出预算")
    if entry is not None and (
            not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
            or type(entry["size_bytes"]) is not int
            or len(content) != entry["size_bytes"]
            or checked_digest(entry["sha256"]) != hashlib.sha256(content).hexdigest()):
        raise ValueError("预备计算缓存文件指纹不符")
    return content


@dataclass(frozen=True)
class PreparedPublication:
    """仅本次已核内容计算包；逐页读取，不把全书PNG载入内存。"""

    root: Path
    vectors: object
    pages: tuple

    def page(self, number):
        from archspec.official_documents.page_images import validate_page_image
        if type(number) is not int or not 1 <= number <= len(self.pages):
            raise ValueError("预备原页页码无效")
        content = _read_prepared(self.root, f"publication/pages/{number}.png",
                                 20 * 1024 * 1024, self.pages[number - 1])
        validate_page_image(content)
        return content


def stable_hash(value):
    """顺序不影响字典指纹，但文字、页码或配置改变会产生新编号。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("缓存指纹无效")
    return value


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class ProcessingSpec:
    """分开记录提取与切分版本：改切分不使已完成的 OCR 失效。"""

    parser_version: str
    ocr_version: str
    chunker_version: str
    extraction_config: dict
    chunk_config: dict

    @property
    def processor_version(self):
        return (f"{self.parser_version}:{self.ocr_version}:"
                f"processing-{stable_hash(self.identity_fields())}")

    def identity_fields(self):
        return {
            "parser_version": self.parser_version, "ocr_version": self.ocr_version,
            "chunker_version": self.chunker_version,
            "extraction_config_hash": stable_hash(self.extraction_config),
            "chunk_config_hash": stable_hash(self.chunk_config),
            "config_hash": stable_hash({"extraction": self.extraction_config,
                                        "chunk": self.chunk_config}),
        }

    def identity(self, blob_id):
        return {"blob_id": checked_digest(blob_id), **self.identity_fields()}

    def extraction_identity(self, blob_id):
        return {"blob_id": checked_digest(blob_id), "parser_version": self.parser_version,
                "ocr_version": self.ocr_version,
                "extraction_config_hash": stable_hash(self.extraction_config)}

    def extraction_id(self, blob_id):
        return stable_hash(self.extraction_identity(blob_id))

    def snapshot_id(self, blob_id):
        return stable_hash(self.identity(blob_id))


def publish_json(path, value):
    """独占发布已写完整的 JSON。旧编号内容冲突时拒绝覆盖。

    临时文件和目的文件在同一目录；硬链接只用于发布我们自己的临时产物，
    不把用户原件或其他来源目录互相链接。读者不会看到写到一半的 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if stable_hash(existing) != stable_hash(value):
                raise ValueError("缓存编号对应内容冲突，未覆盖旧快照") from None
    finally:
        temporary.unlink(missing_ok=True)


def validate_pages(pages):
    if not isinstance(pages, list) or not pages:
        raise ValueError("缓存识别页面不完整")
    for number, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or type(page.get("pdf_page")) is not int
                or page["pdf_page"] != number or page.get("method") not in {"TEXT", "OCR", "HYBRID", "BLANK", "UNRECOGNIZED"}
                or not isinstance(page.get("text"), str)
                or not isinstance(page.get("raw_lines"), list)
                or not isinstance(page.get("warnings"), list)
                or not all(isinstance(item, str) for item in page["warnings"])):
            raise ValueError("缓存识别页面格式无效")
        if page["method"] == "BLANK":
            proof = page.get("blank_render")
            if (page["text"] != "" or page["raw_lines"] != []
                    or "VISIBLE_BLANK_PAGE" not in page["warnings"]
                    or not isinstance(proof, dict)
                    or set(proof) != {"kind", "width", "height", "scale"}
                    or proof["kind"] != "ALL_WHITE_RGB_V1"
                    or type(proof["width"]) is not int or proof["width"] <= 0
                    or type(proof["height"]) is not int or proof["height"] <= 0
                    or proof["width"] * proof["height"] > 16_000_000
                    or type(proof["scale"]) not in {int, float}
                    or not math.isfinite(proof["scale"]) or proof["scale"] <= 0):
                raise ValueError("缓存可见空白页证明无效")
        elif page["method"] == "UNRECOGNIZED":
            if (page["text"] != "" or "blank_render" in page
                    or "UNRECOGNIZED_PAGE" not in page["warnings"] or not page.get("unrecognized_images")
                    or any(row.get("included_in_text", True) for row in page["raw_lines"])):
                raise ValueError("待辨认整页不能伪装成已提取正文或空白页")
        elif not page["text"].strip() or "blank_render" in page:
            raise ValueError("缓存识别页面正文无效")
        images = page.get("unrecognized_images", [])
        if (not isinstance(images, list) or bool(images) != ("UNRECOGNIZED_IMAGES" in page["warnings"])
                or any(not isinstance(image, dict) or image.get("status") != "UNRECOGNIZED"
                       or image.get("coordinate_system") != "PDF_POINTS"
                       or any(not isinstance(image.get(key), list) or len(image[key]) != 4
                              or not all(type(v) in {int, float} and math.isfinite(v) for v in image[key])
                              or not 0 <= image[key][0] < image[key][2]
                              or not 0 <= image[key][1] < image[key][3]
                              for key in ("pdf_box", "layout_box")) for image in images)):
            raise ValueError("待辨认图片缺少可核对的原页位置")
        for line in page["raw_lines"]:
            if (not isinstance(line, dict) or not isinstance(line.get("text"), str)
                    or not isinstance(line.get("box"), list)
                    or (line.get("score") is not None and
                        (type(line["score"]) not in {int, float} or not 0 <= line["score"] <= 1))):
                raise ValueError("缓存原始识别行格式无效")
    if not any(page["text"].strip() for page in pages):
        raise ValueError("缓存全部为空白页，缺少正文")


def validate_chunks(chunks, page_count):
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("缓存切分结果不完整")
    for chunk in chunks:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str)
                or not chunk["text"].strip()):
            raise ValueError("缓存片段文字无效")
        start, end = chunk.get("page_start"), chunk.get("page_end")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= page_count:
            raise ValueError("缓存片段页码无效")
        for key in ("clause_number", "heading_path"):
            if key not in chunk or (chunk[key] is not None and not isinstance(chunk[key], str)):
                raise ValueError("缓存片段条款信息无效")


class ProcessingCache:
    def __init__(self, cache_dir: Path):
        self.root = cache_dir / "content-store"

    def blob_path(self, digest, media_type="application/pdf"):
        if media_type not in {"application/pdf", "text/html"}:
            raise ValueError("原件格式不受支持")
        extension = "html" if media_type == "text/html" else "pdf"
        return self.root / "blobs" / f"{checked_digest(digest)}.{extension}"

    def publish_blob(self, path, digest, media_type="application/pdf"):
        destination = self.blob_path(digest, media_type)
        if file_hash(path) != digest:
            raise ValueError("下载文件指纹不一致，未发布原文件")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and file_hash(destination) == digest:
            return destination
        # 复制到专属临时文件再替换，旧布局的原文件本身不会被移动或修改。
        temporary = destination.with_name(f".{digest}.{uuid4().hex}.part")
        try:
            with path.open("rb") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if file_hash(temporary) != digest:
                raise ValueError("缓存原件复制校验失败")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _path(self, layer, identity):
        return self.root / layer / f"{checked_digest(identity)}.json"

    def _load(self, layer, identity, expected):
        path = self._path(layer, identity)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["state"] != "READY" or record["identity"] != expected
                    or record["payload_sha256"] != stable_hash(record["payload"])):
                raise ValueError("缓存身份或完整性不一致")
            return record["payload"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("共享处理缓存损坏，未覆盖或重新识别") from error

    def _save(self, layer, identity, expected, payload):
        record = {"state": "READY", "identity": expected, "payload": payload,
                  "payload_sha256": stable_hash(payload)}
        publish_json(self._path(layer, identity), record)

    def load_extraction(self, spec, digest):
        payload = self._load("extractions", spec.extraction_id(digest),
                             spec.extraction_identity(digest))
        if payload is None:
            return None
        try:
            validate_pages(payload["pages"])
            return payload["pages"]
        except (TypeError, KeyError) as error:
            raise ValueError("缓存识别页面不完整") from error

    def save_extraction(self, spec, digest, pages):
        validate_pages(pages)
        self._save("extractions", spec.extraction_id(digest), spec.extraction_identity(digest),
                   {"pages": pages})

    def load_snapshot(self, spec, digest):
        payload = self._load("snapshots", spec.snapshot_id(digest), spec.identity(digest))
        if payload is None:
            if not self._import_prepared(spec, digest):
                if publication_compute_mode() == "prepared_only":
                    raise ProcessingLimited()
                return None
            payload = self._load("snapshots", spec.snapshot_id(digest), spec.identity(digest))
        pages = self.load_extraction(spec, digest)
        if pages is None:
            raise ValueError("完整切分快照缺少对应提取缓存")
        try:
            if payload["extraction_id"] != spec.extraction_id(digest):
                raise ValueError("缓存提取与切分身份不一致")
            validate_chunks(payload["chunks"], len(pages))
            return {"pages": pages, "chunks": payload["chunks"],
                    **({"rendering": payload["rendering"]} if "rendering" in payload else {})}
        except (TypeError, KeyError) as error:
            raise ValueError("缓存切分结果不完整") from error

    def save_snapshot(self, spec, digest, pages, chunks, *, rendering=None):
        validate_pages(pages)
        validate_chunks(chunks, len(pages))
        extra = {"rendering": rendering} if rendering is not None else {}
        self._save("snapshots", spec.snapshot_id(digest), spec.identity(digest),
                   {"extraction_id": spec.extraction_id(digest), "chunks": chunks, **extra})
        return {"pages": pages, "chunks": chunks, **extra}


    @staticmethod
    def _prepared_manifest(root, digest):
        manifest = json.loads(_read_prepared(root, PREPARED_MANIFEST, 2 * 1024 * 1024))
        if not isinstance(manifest, dict):
            raise ValueError("预备计算缓存清单无效")  # noqa: TRY004 -- 载荷格式不符按完整性失败处理。
        if manifest.get("schema_version") == PUBLICATION_INDEX_SCHEMA:
            assets = manifest.get("assets")
            if (set(manifest) != {"schema_version", "assets"} or not isinstance(assets, dict)
                    or not 1 <= len(assets) <= 20):
                raise ValueError("多原件计算缓存索引无效")
            for key, entry in assets.items():
                checked_digest(key)
                if not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}:
                    raise ValueError("多原件计算缓存索引无效")
            if digest not in assets:
                return root, {"schema_version": PUBLICATION_INDEX_SCHEMA}
            content = _read_prepared(root, digest + "/" + PREPARED_MANIFEST,
                                     2 * 1024 * 1024, assets[digest])
            root = root / digest
            manifest = json.loads(content)
            if (not isinstance(manifest, dict)
                    or manifest.get("schema_version") not in {PUBLICATION_SCHEMA, PREPARED_SCHEMA}
                    or manifest.get("asset", {}).get("sha256") != digest):
                raise ValueError("多原件内容包身份无效")
        return root, manifest

    def load_publication(self, spec, digest, chunks, model_fingerprint, *, page_count=None):
        """下载原件和当前许可核验后才用；预备产物不提供来源、权限或元信息。"""
        from archspec.official_documents.page_images import page_image_renderer
        from archspec.official_documents.semantic_experiment import (
            PreparedContentVectors,
            _digest,
            checked_content_vectors,
        )
        configured, source_page_url, source_policy = _prepared_location()
        if configured is None:
            return None
        root = Path(configured).absolute()
        if (not configured or not Path(configured).is_absolute()
                or any(part.is_symlink() for part in (root, *root.parents)) or not root.is_dir()):
            raise ValueError("预备计算缓存目录配置无效")
        root, manifest = self._prepared_manifest(root.resolve(), digest)
        if manifest.get("schema_version") != PUBLICATION_SCHEMA:
            return None
        if manifest.get("asset", {}).get("sha256") != digest:
            return None
        asset = checked_processing_asset(manifest["asset"], page_url=source_page_url, source_policy=source_policy)
        original = self.blob_path(digest, "text/html" if asset.get("source_format") == "HTML" else "application/pdf")
        if (manifest.get("identity") != spec.identity(digest)
                or manifest.get("processing_id") != spec.snapshot_id(digest)
                or original.is_symlink() or not original.is_file()
                or original.stat().st_size != asset["size_bytes"] or file_hash(original) != digest):
            raise ValueError("预备计算缓存缺少本次完整原件或处理身份不符")
        value = manifest.get("publication")
        required = {"model_fingerprint", "dimension", "chunks_sha256", "text_sequence_sha256",
                    "vector_payload_sha256", "renderer", "vectors_file", "pages"}
        count = manifest.get("page_count")
        if (not isinstance(value, dict) or set(value) != required
                or value["model_fingerprint"] != model_fingerprint
                or checked_digest(model_fingerprint) != model_fingerprint
                or type(value["dimension"]) is not int or value["dimension"] != 512
                or value["chunks_sha256"] != stable_hash(chunks)
                or value["text_sequence_sha256"] != _digest([chunk["text"] for chunk in chunks])
                or value["renderer"] != page_image_renderer()
                or type(count) is not int or not 1 <= count <= asset["max_pages"]
                or (page_count is not None and count != page_count)
                or not isinstance(value["pages"], list) or len(value["pages"]) != count):
            raise ValueError("预备正文向量、原页或完整条文身份不符")
        raw = json.loads(_read_prepared(root, "publication/vectors.json",
                         PREPARED_MAX_FILE_BYTES, value["vectors_file"]))
        if not isinstance(raw, dict) or set(raw) != {"vectors"}:
            raise ValueError("预备向量载荷无效")
        vectors = checked_content_vectors(raw["vectors"], len(chunks))
        del raw
        if value["vector_payload_sha256"] != _digest(vectors):
            raise ValueError("预备向量完整载荷指纹不符")
        prepared = PreparedContentVectors(model_fingerprint, value["text_sequence_sha256"], vectors)
        return PreparedPublication(root, prepared, tuple(value["pages"]))

    def _import_prepared(self, spec, digest):
        """只导入已核计算缓存；真实原件必须已由acquire下载并通过来源预算。

        配置目录是镜像内不可变计算产物，不是来源库。未列入的原件仍是缓存未命中；
        配置损坏或同原件处理版本不符则明确失败，绝不悄悄运行OCR或审批许可。
        """
        configured, source_page_url, source_policy = _prepared_location()
        if configured is None:
            return False
        try:
            if not configured or not Path(configured).is_absolute():
                raise ValueError("预备计算缓存目录配置无效")
            root = Path(configured).absolute()
            if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
                raise ValueError("预备计算缓存目录不存在或包含链接")
            root = root.resolve()
            root, manifest = self._prepared_manifest(root, digest)

            def read_file(relative, maximum):
                return _read_prepared(root, relative, maximum)

            if manifest.get("schema_version") == PUBLICATION_INDEX_SCHEMA:
                return False
            publication = manifest.get("schema_version") == PUBLICATION_SCHEMA
            if publication:
                if set(manifest) != {"schema_version", "asset", "identity", "extraction_id",
                                     "processing_id", "page_count", "files", "publication"}:
                    raise ValueError("预备计算缓存清单无效")
                manifest = dict(manifest, snapshot_id=manifest["processing_id"])
            elif (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "asset", "identity",
                    "extraction_id", "snapshot_id", "page_count", "files"}
                    or manifest["schema_version"] != PREPARED_SCHEMA):
                raise ValueError("预备计算缓存清单无效")
            asset = checked_processing_asset(manifest["asset"], page_url=source_page_url, source_policy=source_policy)
            if asset["sha256"] != digest:
                return False
            if (manifest["identity"] != spec.identity(digest)
                    or manifest["extraction_id"] != spec.extraction_id(digest)
                    or manifest["snapshot_id"] != spec.snapshot_id(digest)):
                raise ValueError("预备计算缓存处理身份不符")
            # 计算结果不能单凭一个SHA凭空登记；当前运行目录必须已有同份完整下载字节。
            original = self.blob_path(digest, "text/html" if asset.get("source_format") == "HTML" else "application/pdf")
            if (original.is_symlink() or not original.is_file()
                    or original.stat().st_size != asset["size_bytes"] or file_hash(original) != digest):
                raise ValueError("预备计算缓存缺少本次核对的完整原件")
            extraction = "content-store/extractions/" + manifest["extraction_id"] + ".json"
            snapshot = "content-store/snapshots/" + manifest["snapshot_id"] + ".json"
            files = manifest["files"]
            if not isinstance(files, dict) or set(files) != {extraction, snapshot}:
                raise ValueError("预备计算缓存只能包含提取与切分两类固定文件")
            payloads = {}
            for relative, expected in ((extraction, spec.extraction_identity(digest)), (snapshot, spec.identity(digest))):
                entry = files[relative]
                if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                        or type(entry["size_bytes"]) is not int
                        or not 0 < entry["size_bytes"] <= PREPARED_MAX_FILE_BYTES):
                    raise ValueError("预备计算缓存文件清单无效")
                checked_digest(entry["sha256"])
                content = read_file(relative, PREPARED_MAX_FILE_BYTES)
                if len(content) != entry["size_bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise ValueError("预备计算缓存文件指纹不符")
                record = json.loads(content)
                if (set(record) != {"state", "identity", "payload", "payload_sha256"}
                        or record["state"] != "READY" or record["identity"] != expected
                        or stable_hash(record["payload"]) != record["payload_sha256"]):
                    raise ValueError("预备计算缓存身份或载荷指纹不符")
                payloads[relative] = record["payload"]
            pages = payloads[extraction]["pages"]
            chunks = payloads[snapshot]["chunks"]
            validate_pages(pages)
            validate_chunks(chunks, len(pages))
            if (type(manifest["page_count"]) is not int
                    or not 1 <= manifest["page_count"] == len(pages) <= asset["max_pages"]
                    or payloads[snapshot]["extraction_id"] != manifest["extraction_id"]):
                raise ValueError("预备计算缓存完整页数或提取身份不符")
            if asset.get("source_format") == "HTML":
                rendering = {"sha256": asset["rendering_sha256"], "size_bytes": asset["rendering_size_bytes"]}
                _read_prepared(root, "publication/rendering.pdf", PREPARED_MAX_FILE_BYTES, rendering)
                self.publish_blob(root / "publication/rendering.pdf", asset["rendering_sha256"])
            # 全部输入核齐后才独占写入；沿原缓存冲突规则，旧产物绝不被覆盖。
            self.save_extraction(spec, digest, pages)
            self.save_snapshot(spec, digest, pages, chunks, rendering=(
                {key: asset[key] for key in ("rendering_sha256", "rendering_size_bytes")}
                if asset.get("source_format") == "HTML" else None))
            return True
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("预备计算缓存无法完整核对，未发布来源或重新识别") from error


def checked_processing_asset(asset, *, page_url=None, source_policy=None):
    """计算与导入使用当前已核来源的同一资源策略，不接受浏览器自定预算。"""
    from archspec.official_documents.downloader import validate_attachment_url
    from archspec.official_documents.local_policy import reviewed_attachment
    from archspec.official_documents.policy import (
        DEFAULT_POLICY,
        FIXED_POLICIES,
        LARGE_POLICY,
        policy_for_source,
    )

    expected = {"url", "sha256", "size_bytes", "max_pages"}
    web = isinstance(asset, dict) and asset.get("source_format") == "HTML"
    if web:
        expected |= {"source_format", "rendering_sha256", "rendering_size_bytes"}
    if (not isinstance(asset, dict) or set(asset) != expected
            or not isinstance(asset["url"], str) or type(asset["size_bytes"]) is not int
            or not 0 < asset["size_bytes"] <= LARGE_POLICY.max_bytes
            or type(asset["max_pages"]) is not int or asset["max_pages"] < 1):
        raise ValueError("预备计算缓存公开原件清单无效")
    checked_digest(asset["sha256"])
    if web:
        from archspec.official_documents.government_urls import validate_government_url
        validate_government_url(asset["url"])
        if (asset["url"] != page_url or asset["size_bytes"] > 2 * 1024 * 1024
                or type(asset["rendering_size_bytes"]) is not int
                or not 0 < asset["rendering_size_bytes"] <= PREPARED_MAX_FILE_BYTES):
            raise ValueError("网页原件和派生排版未绑定同一已捕获发布页")
        checked_digest(asset["rendering_sha256"])
    else:
        validate_attachment_url(asset["url"], page_url=page_url)
    selected = DEFAULT_POLICY
    reviewed = reviewed_attachment(asset["url"])
    if reviewed is not None and dict(reviewed.review_evidence).get("pdf_sha256") == asset["sha256"]:
        # URL和原件指纹先匹配；不能拿已审公告给别的PDF借大预算。
        identity = f"{reviewed.code}\n{reviewed.pdf_url}\n{reviewed.kind}"
        selected = policy_for_source({
            "source_id": "official-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            "page_url": reviewed.notice_url, "pdf_url": reviewed.pdf_url,
            "kind": reviewed.kind, "title": reviewed.title, "standard_code": reviewed.code,
        })
    if source_policy is not None:
        # 这里只接收服务端从来源登记链选出的固定策略；原件SHA、URL和完整包身份仍逐项核验。
        if source_policy not in FIXED_POLICIES:
            raise ValueError("计算资源策略不是服务端支持的固定范围")
        selected = source_policy
    # 字节和页数都服从同一服务端策略；不能固定30MiB拒绝已核大文件，
    # 也不能让普通来源借用结构校验的最大80MiB上限。
    if asset["size_bytes"] > selected.max_bytes or asset["max_pages"] > selected.max_pages:
        raise ValueError("预备计算缓存公开原件清单无效")
    return asset
