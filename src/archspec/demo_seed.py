"""免费演示部署适配：构建阶段准备公开正文，运行期只导入已校验种子。

不改下载、识别或检索算法；不使用本机原资料、数据库或密钥。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

SEED_SCHEMA = "ARCHSPEC_PUBLIC_DEMO_SEED_V1"
BUILD_DIRECTORY = "/opt/archspec-demo-seed"
SEED_MAX_BYTES = 512 * 1024 * 1024
SOURCE_TIMEOUT_SECONDS = 1800
_PAGE_IMAGE_ASSET = re.compile(
    r"official-cache/page-images/(?:gb55031|official-[a-f0-9]{32})/"
    r"[a-f0-9]{64}/[a-f0-9]{64}/[a-f0-9]{64}/[1-9][0-9]{0,3}\.(?:png|json)"
)
# 这里只扩大公开资料准备覆盖，不预设问题答案或声明这些版本现行。
PUBLIC_SEED_QUERIES = (
    "GB 55031-2022", "GB 55038-2025", "GB 55037-2022", "GB 50067-2014",
    "JGJ 100-2015", "GB 55016-2021", "GB 50033-2013",
    "GB 50352-2019", "GB 50016-2014", "宜昌市建筑日照",
)


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bootstrap_demo_seed(seed_dir, data_dir):
    """首次空缓存导入；已有任何缓存均保留，不覆盖/合并旧快照。"""
    source, target = Path(seed_dir).resolve(), Path(data_dir).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("演示种子和运行资料目录不能重叠")
    existing = target / "official-cache"
    if existing.exists() and any(existing.iterdir()):
        return False
    manifest = json.loads((source / "demo-seed-manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files")
    if (manifest.get("schema") != SEED_SCHEMA or not isinstance(files, dict)
            or not 1 <= len(files) <= 20000):
        raise ValueError("演示资料清单无效")
    selected, total = [], 0
    for relative, digest in files.items():
        name = PurePosixPath(relative)
        if (name.is_absolute() or ".." in name.parts or "\\" in relative
                or not name.parts or name.parts[0] not in {"official-cache", "official-discovery"}
                or (name.suffix not in {".json", ".pdf"}
                    and _PAGE_IMAGE_ASSET.fullmatch(relative) is None)
                or not re.fullmatch(r"[a-f0-9]{64}", str(digest))):
            raise ValueError("演示种子只接受公开正文和发现记录")
        path = source / Path(relative)
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source)
                or _digest(path) != digest):
            raise ValueError("演示资料指纹不符或路径不安全")
        total += path.stat().st_size
        if total > SEED_MAX_BYTES:
            raise ValueError("演示种子超出512MiB资料上限")
        destination = target / Path(relative)
        if not destination.resolve().is_relative_to(target):
            raise ValueError("演示导入目标不能跳出运行资料目录")
        if destination.exists():
            raise ValueError("演示导入不会覆盖已有记录")
        selected.append((path, destination))
    _verify_seed_page_images(source, files, manifest.get("sources", []),
                             schema=manifest.get("page_image_schema"))
    # 完成全部预检后才建立目标。独占创建避免意外覆盖，流式复制不把PDF读进内存。
    for path, destination in selected:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as original, destination.open("xb") as output:
            shutil.copyfileobj(original, output, length=1024 * 1024)
    return True


def _verify_seed_page_images(root, files, sources, *, schema=None):
    """原页属于同份成功正文且完整配对；所有预检在建立导入目标之前完成。"""
    page_assets = {name for name in files if name.startswith("official-cache/page-images/")}
    if schema not in {None, "OFFICIAL_PAGE_IMAGE_V1"}:
        raise ValueError("演示原页格式版本不受支持")
    if not page_assets and schema is None:
        return  # 旧无原页种子只保留旧导入行为；正式REMOTE不得据此声明完整。
    if (not isinstance(sources, list) or not sources
            or any(_PAGE_IMAGE_ASSET.fullmatch(name) is None for name in page_assets)):
        raise ValueError("演示原页目录或来源清单无效")
    from archspec.official_documents.page_images import PageImageRepository
    repository = PageImageRepository(root / "official-cache")
    expected = set()
    with _build_documents(root, page_images=True) as documents:
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("演示原页来源清单无效")  # noqa: TRY004 -- 清单校验统一按无效资料拒绝
            snapshot = documents.get(source["source_id"])
            if any(snapshot[key] != source.get(key) for key in ("snapshot_id", "sha256", "page_count")):
                raise ValueError("演示原页不属于清单正文快照")
            for page_number in range(1, snapshot["page_count"] + 1):
                # 原件/权限与PNG的身份、完整指纹都由既有实际读取入口核验。
                documents.page_image(source["source_id"], source["snapshot_id"], page_number)
                identity = repository._paths({"source_id": source["source_id"],
                    "snapshot_id": source["snapshot_id"], "document_sha256": source["sha256"],
                    "page_number": page_number})
                expected.update("official-cache/" + path.relative_to(repository.cache_dir).as_posix()
                                for path in identity)
    if expected != page_assets:
        raise ValueError("演示原页未完整配对或包含未准备来源")


def additional_demo_source(registry, discovery):
    """使用原发现服务results字段，经原登记器分类，只选一份完整正文。"""
    for notice in discovery.get("results", []):
        for entry in registry.register_notice(notice, discovery["id"]):
            if entry["kind"] == "FULL_TEXT" and entry["auto_acquire"]:
                return entry["source_id"]
    return None


def _build_root(destination):
    """内部构建命令只接受固定Linux目录，拒绝借参数处理本机已有资料。"""
    if sys.platform != "linux" or str(destination) != BUILD_DIRECTORY:
        raise ValueError("仅允许在Linux构建阶段的固定演示种子目录准备")
    root = Path(destination)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError("构建种子目录不能是符号链接或跳转路径")
    return root


def _allowed_source(source):
    """全国只准备完整正文；精确登记的宜昌修订附件保持REVISION独立准备。"""
    if source.get("auto_acquire") is not True:
        return False
    if source.get("kind") == "FULL_TEXT":
        return True
    from archspec.official_documents.local_policy import PUBLICATIONS
    registered = PUBLICATIONS.get(source.get("page_url"))
    return (source.get("kind") == "REVISION" and registered is not None
            and registered["kind"] == "REVISION"
            and source.get("pdf_url") == registered["pdf_url"])


def _validate_build_source(root, source):
    """额外附件必须对应父进程保存的本次TARGET公告，不接受仅有关联字样的登记。"""
    if not _allowed_source(source):
        raise ValueError("构建只准备完整正文或精确登记的宜昌独立修订附件")
    if source["source_id"] == "gb55031":
        return
    from archspec.official_discovery.service import DiscoveryService
    discovery = DiscoveryService(root / "official-discovery").get(source["discovery_run_id"])
    if discovery.get("status") not in {"COMPLETE", "PARTIAL"}:
        raise ValueError("本次官方发现未取得可准备目标，不复用失败或旧线索")
    for notice in discovery.get("results", []):
        if (notice.get("target_match") == "TARGET"
                and notice.get("url") == source["page_url"]
                and notice.get("standard_code") == source["standard_code"]
                and any(item.get("url") == source["pdf_url"]
                        for item in notice.get("attachments", []))):
            return
    raise ValueError("附件没有对应本次TARGET公告，未准备关联或未知正文")


def _snapshot_summary(snapshot, source):
    """成功进程仍须有原服务可读的固定快照；退出码不替代真实正文和出处。"""
    if (not isinstance(snapshot, dict) or snapshot.get("source_id") != source["source_id"]
            or snapshot.get("kind") != source["kind"]
            or snapshot.get("title") != source["title"]
            or any(snapshot.get(key) != source.get(key) for key in (
                "standard_code", "page_url", "pdf_url", "discovery_run_id"))
            or not re.fullmatch(r"[a-f0-9]{64}", str(snapshot.get("snapshot_id")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(snapshot.get("sha256")))
            or type(snapshot.get("page_count")) is not int or snapshot["page_count"] < 1):
        raise ValueError("准备进程没有发布可核对的真实正文快照")
    return {key: snapshot[key] for key in (
        "source_id", "snapshot_id", "sha256", "page_count", "title", "standard_code", "kind")}


@contextmanager
def _build_documents(root, *, registry=None, page_images=False):
    """原许可适配器绑定全新内存库，构建读取不接触进程已加载的本机数据库。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from archspec.official_documents.page_images import PageImageRepository
    from archspec.official_documents.service import OfficialDocumentService
    from archspec.standard_governance.compatibility import LegacyAdmissionAdapter
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with Session(engine) as session:
            yield OfficialDocumentService(root / "official-cache", registry=registry,
                                          governance=LegacyAdmissionAdapter(session, root),
                                          page_image_repository=(PageImageRepository(root / "official-cache")
                                                                 if page_images else None))
    finally:
        engine.dispose()


def _child_environment(root):
    """只改构建子进程副本；外部模型/追踪凭据不继承，运行期OCR开关保持原值。"""
    environment = os.environ.copy()
    for name in tuple(environment):
        upper = name.upper()
        if (upper.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_KEY", "_PASSWORD", "_PRIVATE_KEY"))
                or upper in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "ARCHSPEC_DATABASE_URL"}
                or upper.startswith(("LANGSMITH_", "LANGCHAIN_", "OTEL_", "WANDB_", "SENTRY_"))):
            environment.pop(name)
    environment.update({"ARCHSPEC_DATA_DIR": str(root), "DEEPSEEK_API_KEY": "",
                        "ARCHSPEC_RUNTIME_OCR_ENABLED": "true",
                        "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
    return environment


def prepare_source(destination, source_id):
    """每份资料在新进程执行原获取/识别，完成退出释放引擎；不使用本机缓存。"""
    root = _build_root(destination)
    if not root.is_dir() or not isinstance(source_id, str) or not re.fullmatch(
            r"gb55031|official-[a-f0-9]{32}", source_id):
        raise ValueError("构建资料目录或来源编号无效")
    # 子进程明确禁止生成模型；OCR是本机文字识别，不把原件发给外部模型。
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from archspec.official_documents.registry import OfficialSourceRegistry
    registry = OfficialSourceRegistry(root / "official-cache")
    source = registry.get(source_id)
    _validate_build_source(root, source)

    def progress(state, detail):
        page = detail.get("pdf_page") or detail.get("page_number") or ""
        print(json.dumps({"demo_seed": state, "source_id": source_id, "page": page},
                         ensure_ascii=False), flush=True)

    with _build_documents(root, registry=registry, page_images=True) as documents:
        snapshot = documents.acquire(source_id, progress=progress)
        summary = _snapshot_summary(snapshot, source)
        # 原件/即时许可每页沿用原读取门；逐页释放像素，不在网页进程处理整书。
        for page_number in range(1, summary["page_count"] + 1):
            documents.prepare_page_image(source_id, summary["snapshot_id"], page_number)
            print(json.dumps({"demo_seed": "PAGE_PREPARED", "source_id": source_id,
                              "page": page_number}, ensure_ascii=False), flush=True)
    print(json.dumps({"demo_seed": "PREPARED", "source_id": source_id,
                      "pages": summary["page_count"], "kind": source["kind"]},
                     ensure_ascii=False), flush=True)
    return summary


def _prepare_fresh(root, source):
    """同步等待一个有时限的独立进程；不并发、重试或在父进程识别。"""
    environment = _child_environment(root)
    subprocess.run([sys.executable, "-m", "archspec.demo_seed", "--prepare-source", source["source_id"]],
                   check=True, timeout=SOURCE_TIMEOUT_SECONDS, env=environment)
    with _build_documents(root, page_images=True) as documents:
        snapshot = documents.get(source["source_id"])
        summary = _snapshot_summary(snapshot, source)
        # get保留原读取门；再走原件指纹/PDF结构核验，退出成功不能替代完整附件。
        documents._check_cached_file(snapshot, source["source_id"])
        for page_number in range(1, summary["page_count"] + 1):
            documents.page_image(source["source_id"], summary["snapshot_id"], page_number)
        return summary


def _preparation(root, source, prepared):
    """同一来源无论成功或失败都只尝试一次，后续公告仅引用这份准备记录。"""
    identity = source["source_id"]
    if identity in prepared:
        return {**prepared[identity], "reused": True}
    value = {"source_id": identity, "kind": source["kind"], "reused": False}
    from archspec.standard_governance.compatibility import ContentUseDenied
    try:
        summary = _prepare_fresh(root, source)
        value.update(summary, status="ATTACHMENT_PREPARED" if source["kind"] == "REVISION" else "PREPARED")
    except (OSError, ValueError, RuntimeError, LookupError, KeyError, TypeError,
            ContentUseDenied, subprocess.SubprocessError) as error:
        # 不打印完整异常串/子进程环境，避免密钥或代理信息进入构建日志和公开清单。
        value.update(status="FAILED", failure_type=type(error).__name__,
                     message="本次公开附件准备未完成，保留缺口；未生成替代正文或自动重试")
    prepared[identity] = value
    return value


def _discover_coverage(root, registry, query, prepared):
    """使用共同发现目标，保留关联/未知/失败线索但不把它们加入可查正文。"""
    from archspec.official_discovery.service import DiscoveryService
    row = {"query": query, "status": "MISSING", "preparations": [], "notices": [],
           "skipped_attachments": []}
    try:
        discovery = DiscoveryService(root / "official-discovery").run(query, topics=[])
        row.update(discovery_run_id=discovery["id"], discovery_status=discovery["status"],
                   errors=discovery.get("errors", []))
        for notice in discovery.get("results", []):
            match = notice.get("target_match", "UNKNOWN")
            row["notices"].append({key: notice.get(key) for key in (
                "url", "standard_title", "standard_code", "target_match")})
            if match != "TARGET" or discovery["status"] not in {"COMPLETE", "PARTIAL"}:
                continue
            for source in registry.register_notice(notice, discovery["id"]):
                if not _allowed_source(source):
                    row["skipped_attachments"].append({
                        "source_id": source["source_id"], "kind": source["kind"],
                        "reason": "本附件不是已核对完整正文或精确登记宜昌修订，未准备"})
                    continue
                row["preparations"].append(_preparation(root, source, prepared))
        states = [item["status"] for item in row["preparations"]]
        if states:
            if all(state == "FAILED" for state in states):
                row["status"] = "FAILED"
            elif "FAILED" in states or discovery["status"] == "PARTIAL":
                row["status"] = "PARTIAL"
            else:
                row["status"] = "PREPARED" if "PREPARED" in states else "ATTACHMENT_PREPARED"
        elif row["notices"] and all(item["target_match"] == "ASSOCIATED" for item in row["notices"]):
            row["status"] = "ASSOCIATED"
        elif discovery["status"] not in {"COMPLETE", "PARTIAL"}:
            row["status"] = "FAILED"
    except (OSError, ValueError, RuntimeError, LookupError, KeyError, TypeError) as error:
        row.update(status="FAILED", failure_type=type(error).__name__,
                   message="官方资料发现或登记未完成；没有把公告摘要当成正文")
    return row


def _public_seed_files(root):
    """摘要逐文件流式计算；总资料预算独立于运行实例内存，越界不发布清单。"""
    files, total = {}, 0
    for folder in ("official-cache", "official-discovery"):
        for path in (root / folder).rglob("*"):
            if path.is_file() and (path.suffix in {".json", ".pdf"}
                                   or _PAGE_IMAGE_ASSET.fullmatch(path.relative_to(root).as_posix())):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError("演示资料路径不安全")
                total += path.stat().st_size
                if total > SEED_MAX_BYTES or len(files) >= 20000:
                    raise ValueError("演示种子超出512MiB资料或文件数量上限")
                files[path.relative_to(root).as_posix()] = _digest(path)
    if not files:
        raise ValueError("构建没有取得任何可校验的公开资料")
    return files, total


def build_public_seed(destination):
    """仅Linux Docker构建：基础规范必须成功，额外公开查询与缺口逐一记录。"""
    root = _build_root(destination)
    if root.exists():
        raise ValueError("构建种子目录已存在，不覆盖旧资料")
    root.mkdir(parents=True)
    # 构建阶段不采用任何个人/项目输入或外部生成模型。
    os.environ["DEEPSEEK_API_KEY"] = ""
    from archspec.official_documents.registry import OfficialSourceRegistry
    registry = OfficialSourceRegistry(root / "official-cache")
    prepared = {}
    basic = _preparation(root, registry.get("gb55031"), prepared)
    if basic["status"] != "PREPARED":
        raise RuntimeError("基础公开规范准备失败，本次构建未发布演示种子")
    coverage = [{"query": PUBLIC_SEED_QUERIES[0], "status": "PREPARED",
                 "origin": "REGISTERED_OFFICIAL_ENTRY", "discovery_status": "NOT_QUERIED",
                 "preparations": [basic]}]
    for query in PUBLIC_SEED_QUERIES[1:]:
        coverage.append(_discover_coverage(root, registry, query, prepared))
        print(json.dumps({"demo_seed": "COVERAGE", "query": query,
                          "status": coverage[-1]["status"]}, ensure_ascii=False), flush=True)
    sources = [{key: value[key] for key in (
        "source_id", "snapshot_id", "sha256", "page_count", "title", "standard_code", "kind")}
        for value in prepared.values() if value["status"] in {"PREPARED", "ATTACHMENT_PREPARED"}]
    files, total = _public_seed_files(root)
    ready_sources = {source["source_id"] for source in sources}
    # 失败现场保留，但失败来源的半套图片不得被包装为完整原页种子。
    for relative in tuple(files):
        if (relative.startswith("official-cache/page-images/")
                and relative.split("/")[2] not in ready_sources):
            total -= (root / relative).stat().st_size
            del files[relative]
    _verify_seed_page_images(root, files, sources, schema="OFFICIAL_PAGE_IMAGE_V1")
    manifest = {"schema": SEED_SCHEMA, "page_image_schema": "OFFICIAL_PAGE_IMAGE_V1",
                "files": files, "sources": sources,
                "coverage": coverage, "total_file_bytes": total,
                "full_text_source_count": sum(item["kind"] == "FULL_TEXT" for item in sources),
                "attachment_source_count": sum(item["kind"] == "REVISION" for item in sources),
                "local_materials_copied": 0, "model_calls": 0,
                "notice": "构建时从公开官网新获取；修订附件独立保留，版本、许可及工程适用性仍待核"}
    with (root / "demo-seed-manifest.json").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    print(json.dumps({"demo_seed": "COMPLETE", "sources": len(sources),
                      "files": len(files), "data_bytes": total}, ensure_ascii=False), flush=True)



def _processing_peak_rss():
    """记录计算子进程的内核峰值；不把Python对象分配量冒充进程内存。"""
    if sys.platform.startswith("linux"):
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise OSError("无法读取计算进程内核峰值")
        return int(value.PeakWorkingSetSize)
    return None


def prepare_processing_cache(input_pdf, destination, asset, *, publication=False, source_page_url=None, spec=None, source_policy=None):
    """复用原处理器，仅发布两类计算缓存；不登记来源、latest、治理或云端指针。"""
    from dataclasses import asdict
    from time import perf_counter

    from archspec.documents.chunker import DocumentChunker, SourceBlock
    from archspec.official_documents.policy import AcquisitionPolicy
    from archspec.official_documents.processing_cache import (
        PREPARED_MANIFEST,
        PREPARED_SCHEMA,
        ProcessingCache,
        checked_processing_asset,
        file_hash,
        publish_json,
    )
    from archspec.official_documents.service import OfficialDocumentService
    started = perf_counter()
    asset = checked_processing_asset(asset, page_url=source_page_url, source_policy=source_policy)
    input_pdf, root = Path(input_pdf).absolute(), Path(destination).absolute()
    if (any(path.is_symlink() for path in (input_pdf, *input_pdf.parents, root, *root.parents))
            or not input_pdf.is_file() or input_pdf.stat().st_size != asset["size_bytes"]
            or file_hash(input_pdf) != asset["sha256"]):
        raise ValueError("预备计算缓存输入原件指纹或大小不符")
    if root.exists():
        raise FileExistsError("预备计算缓存目录已存在，拒绝覆盖")
    count = OfficialDocumentService._check_pdf_budget(input_pdf,
        AcquisitionPolicy("PUBLIC_PROCESSING_CACHE_V1", asset["size_bytes"], asset["max_pages"]))
    root.mkdir(parents=True)
    documents = OfficialDocumentService(root)
    spec = spec or documents._processing_spec()
    def progress(state, detail):
        print(json.dumps({"processing_cache":state,"page":detail.get("pdf_page") or detail.get("page_number")}),flush=True)
    pages = documents._extract(input_pdf, max_pages=asset["max_pages"], spec=spec, progress=progress)
    if len(pages) != count:
        raise ValueError("预备计算缓存识别页数与完整原件不一致")
    chunks = [asdict(chunk) for chunk in DocumentChunker(max_characters=spec.chunk_config["max_characters"]).chunk(
        tuple(SourceBlock(page["text"], page["pdf_page"], index) for index, page in enumerate(pages)))]
    cache = ProcessingCache(root)
    digest = asset["sha256"]
    cache.save_extraction(spec, digest, pages)
    cache.save_snapshot(spec, digest, pages, chunks)
    paths = [cache._path("extractions", spec.extraction_id(digest)), cache._path("snapshots", spec.snapshot_id(digest))]
    files = {path.relative_to(root).as_posix():{"sha256":file_hash(path),"size_bytes":path.stat().st_size} for path in paths}
    manifest = {"schema_version":PREPARED_SCHEMA,"asset":dict(asset),"identity":spec.identity(digest),
        "extraction_id":spec.extraction_id(digest),"snapshot_id":spec.snapshot_id(digest),"page_count":count,"files":files}
    if publication or os.getenv("ARCHSPEC_BUILD_PUBLICATION_CACHE") == "1":
        manifest["schema_version"] = "ARCHSPEC_PREPARED_PUBLICATION_V1"
        manifest["processing_id"] = manifest.pop("snapshot_id")
        manifest["publication"] = _prepare_publication_computations(
            input_pdf, root, pages, chunks)
    publish_json(root / PREPARED_MANIFEST, manifest)
    return {"processing_cache":"COMPLETE","sha256":digest,"page_count":count,
        "ocr_page_count":sum(page["method"] in {"OCR","HYBRID"} for page in pages),
         "snapshot_id":spec.snapshot_id(digest),"extraction_id":manifest["extraction_id"],
        "manifest_sha256":file_hash(root/PREPARED_MANIFEST),"files":files,
        "elapsed_seconds":round(perf_counter()-started,3),"kernel_peak_rss_bytes":_processing_peak_rss(),
        "source_records":0,"model_calls":0}



def _prepare_publication_computations(input_pdf, root, pages, chunks):
    """构建纯内容向量与PNG；不生成source、治理或正式快照描述。"""
    import pymupdf

    from archspec.embeddings.provider import PinnedFastEmbedProvider
    from archspec.official_documents.page_images import (
        PageImageRepository,
        page_image_renderer,
        validate_page_image,
    )
    from archspec.official_documents.processing_cache import file_hash, stable_hash
    from archspec.official_documents.semantic_experiment import (
        _canonical_parts,
        _digest,
        checked_content_vectors,
    )
    directory = os.getenv("ARCHSPEC_EMBEDDING_MODEL_DIR")
    if not directory:
        raise ValueError("构建正文计算缓存需要已核固定模型目录")
    provider = PinnedFastEmbedProvider(Path(directory))
    fingerprint = provider.model_fingerprint
    if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
        raise ValueError("构建固定模型指纹无效")
    texts = [chunk["text"] for chunk in chunks]
    generate = getattr(provider, "iter_documents", None)
    raw = generate(texts) if callable(generate) else provider.embed_documents(texts)
    vectors = checked_content_vectors(raw, len(chunks), normalize=True)
    folder = root / "publication"
    folder.mkdir()
    vector_file = folder / "vectors.json"
    with vector_file.open("x", encoding="utf-8", newline="\n") as stream:
        stream.writelines(_canonical_parts({"vectors": vectors}))
    def entry(path):
        return {"sha256": file_hash(path), "size_bytes": path.stat().st_size}
    image_entries = []
    with pymupdf.open(input_pdf) as document:
        if len(document) != len(pages):
            raise ValueError("构建原页数量与完整提取不符")
        for number, page in enumerate(document, 1):
            raster = (page.rect * pymupdf.Matrix(1.5, 1.5)).irect
            if raster.width * raster.height > 10_000_000:
                raise ValueError("构建原页超过既有像素预算")
            content = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")
            validate_page_image(content)
            path = folder / "pages" / f"{number}.png"
            PageImageRepository._publish(path, content)
            image_entries.append(entry(path))
    return {"model_fingerprint": fingerprint, "dimension": 512,
        "chunks_sha256": stable_hash(chunks), "text_sequence_sha256": _digest(texts),
        "vector_payload_sha256": _digest(vectors), "renderer": page_image_renderer(),
        "vectors_file": entry(vector_file), "pages": image_entries}


def publication_inputs(path):
    """多原件只接受代码中已审精确附件及审阅SHA，不把官方域名当资料授权。"""
    from archspec.official_documents.local_policy import reviewed_attachment
    from archspec.official_documents.processing_cache import checked_processing_asset
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 65536:
        raise ValueError("公开计算输入清单缺失或超限")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (set(value) != {"schema_version", "assets"}
            or value["schema_version"] != "ARCHSPEC_PUBLIC_PROCESSING_INPUT_V1"
            or not isinstance(value["assets"], list) or not 1 <= len(value["assets"]) <= 20):
        raise ValueError("公开计算输入清单无效")
    assets, seen = [], set()
    for raw in value["assets"]:
        asset = checked_processing_asset(raw)
        reviewed = reviewed_attachment(asset["url"])
        if (reviewed is None or reviewed.kind != "FULL_TEXT"
                or dict(reviewed.review_evidence).get("pdf_sha256") != asset["sha256"]
                or asset["sha256"] in seen):
            raise ValueError("公开计算原件不在已核精确附件清单或身份重复")
        assets.append(asset)
        seen.add(asset["sha256"])
    return tuple(assets)


def build_publication_cache(manifest_path="/app/prepared-processing-input.json"):
    """显式完整计算模式；同一个多原件构建器不预登记任何来源。"""
    return _build_computation_cache(manifest_path, publication=True)


def _build_computation_cache(manifest_path, *, publication):
    """沿既有有界下载器逐份构建；索引最后排他发布。"""
    from tempfile import TemporaryDirectory

    from archspec.official_documents.downloader import download_pdf_file
    from archspec.official_documents.processing_cache import (
        PREPARED_MANIFEST,
        PUBLICATION_INDEX_SCHEMA,
        file_hash,
        publish_json,
    )
    if sys.platform != "linux":
        raise ValueError("仅Linux构建可从公开清单准备完整计算缓存")
    assets = publication_inputs(manifest_path)
    root = Path("/opt/archspec-prepared-cache")
    if root.exists():
        raise FileExistsError("预备计算缓存目录已存在，拒绝覆盖")
    root.mkdir(parents=True)
    index, results = {}, []
    for asset in assets:
        with TemporaryDirectory(prefix="archspec-public-computations-") as temporary:
            downloaded = download_pdf_file(asset["url"], Path(temporary))
            result = prepare_processing_cache(downloaded.path, root / asset["sha256"],
                                              asset, publication=publication)
        path = root / asset["sha256"] / PREPARED_MANIFEST
        index[asset["sha256"]] = {"sha256": file_hash(path), "size_bytes": path.stat().st_size}
        results.append(result)
    publish_json(root / PREPARED_MANIFEST,
                 {"schema_version": PUBLICATION_INDEX_SCHEMA, "assets": index})
    return {"processing_cache": "COMPLETE", "assets": results,
            "source_records": 0, "model_calls": 0}

def processing_input(path):
    """只读取随代码审核的公开身份清单，不接受资料内容或凭据。"""
    from archspec.official_documents.processing_cache import checked_processing_asset
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 65536:
        raise ValueError("公开计算输入清单缺失或超限")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (set(value) != {"schema_version","assets"} or value["schema_version"] != "ARCHSPEC_PUBLIC_PROCESSING_INPUT_V1"
            or not isinstance(value["assets"],list) or len(value["assets"]) != 1):
        raise ValueError("本构建只允许一份明确审核的公开原件")
    return checked_processing_asset(value["assets"][0])


def build_processing_cache(manifest_path="/app/prepared-processing-input.json"):
    """仅Linux镜像构建使用；下载固定官方原件，临时PDF不会进入输出或源码仓库。"""
    from tempfile import TemporaryDirectory

    from archspec.official_documents.downloader import download_pdf_file
    output = "/opt/archspec-prepared-cache"
    if sys.platform != "linux":
        raise ValueError("仅Linux构建可从公开清单准备计算缓存")
    path = Path(manifest_path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 65536:
        raise ValueError("公开计算输入清单缺失或超限")
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("assets"), list) and len(value["assets"]) > 1:
        # 旧命令仍完整提取每份已审核原件，不加载向量模型或丢掉第二份资料。
        return _build_computation_cache(manifest_path, publication=False)
    asset = processing_input(manifest_path)
    with TemporaryDirectory(prefix="archspec-public-processing-") as folder:
        # 下载预算沿用服务端默认30MiB档位；精确长度和SHA由下方计算入口再次核验，不能混为预算参数。
        downloaded = download_pdf_file(asset["url"], Path(folder))
        return prepare_processing_cache(downloaded.path,output,asset)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--build", action="store_true")
    operation.add_argument("--prepare-source")
    operation.add_argument("--build-processing-cache", action="store_true")
    operation.add_argument("--build-publication-cache", action="store_true")
    operation.add_argument("--prepare-processing-cache", action="store_true")
    parser.add_argument("--input-pdf", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--processing-manifest", type=Path, default=Path("/app/prepared-processing-input.json"))
    args = parser.parse_args()
    from archspec.standard_governance.compatibility import ContentUseDenied
    try:
        if args.build_processing_cache or args.prepare_processing_cache or args.build_publication_cache:
            # 计算子进程不使用模型、追踪、数据库或存储凭据；运行服务的OCR开关不变。
            environment = _child_environment(Path(args.output or "/opt/archspec-prepared-cache"))
            os.environ.clear()
            os.environ.update(environment)
            if args.build_publication_cache:
                result = build_publication_cache(args.processing_manifest)
            elif args.prepare_processing_cache:
                if args.input_pdf is None or args.output is None:
                    raise ValueError("离线计算需要明确的新输入和输出目录")
                result = prepare_processing_cache(args.input_pdf,args.output,processing_input(args.processing_manifest))
            else:
                result = build_processing_cache(args.processing_manifest)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        elif args.build:
            build_public_seed(BUILD_DIRECTORY)
        else:
            prepare_source(BUILD_DIRECTORY, args.prepare_source)
    except (OSError, ValueError, RuntimeError, LookupError, KeyError, TypeError,
            ContentUseDenied, subprocess.SubprocessError) as error:
        # CLI作为构建边界只输出失败类型；网址异常可能含代理凭据，禁止原异常串/traceback。
        print(json.dumps({"demo_seed": "FAILED", "failure_type": type(error).__name__},
                         ensure_ascii=False), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
