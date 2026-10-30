"""把纯内容计算交给独立服务；来源、权限、租约和正式发布仍归原网页流程。"""
import base64
import json
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager, ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from archspec.official_documents.processing_cache import ProcessingLimited, prepared_package

Client = httpx.Client
MAX_PACKAGE = 128 * 1024 * 1024
# 现有正文队列负责等待；真实低CPU实例计算约五分钟，不能仍在三分钟断开。
# 只延长计算首字节等待，连接/上传仍限时，不自动重发POST或扩大内容资源范围。
COMPUTE_RESULT_READ_SECONDS = 15 * 60


class ComputeUnavailable(ValueError):
    """只传固定状态说明；外部错误、凭据和响应正文不进入任务记录。"""
    MESSAGES = {
        "OCR_REQUIRED": "部分页面含需辨认的图像，当前计算实例未开启图片文字识别；官方原件和预览已保留。",
        "DOWNLOAD_UNAVAILABLE": "独立计算服务暂未取得同一官方原件；已保存网页端取得的原件和预览。",
        "IDENTITY_UNAVAILABLE": "独立计算产物身份尚未核验通过；已保留原件，未发布可检索正文。",
        "PROCESSING_UNAVAILABLE": "独立计算暂无法完成；已保留原件和预览，未发布可检索正文。",
    }

    def __init__(self, code):
        self.code = code if code in self.MESSAGES else "PROCESSING_UNAVAILABLE"
        self.public_message = self.MESSAGES[self.code]
        super().__init__(self.public_message)


@contextmanager
def remote_publication(documents, source, digest, size_bytes, spec):
    endpoint = os.getenv("ARCHSPEC_COMPUTE_URL", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (not endpoint or parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in {None, 443} or parsed.path
            or parsed.query or parsed.fragment):
        raise ProcessingLimited()
    password = os.getenv("ARCHSPEC_SERVER_PASSWORD", "")
    if not password:
        raise ProcessingLimited()
    # 只传已登记公开来源，不传用户问题、项目材料、权限记录或模型密钥。
    public = {key: source[key] for key in
              ("source_id", "title", "standard_code", "page_url", "pdf_url", "kind")}
    public["source_metadata"] = {"provenance": (source.get("source_metadata") or {}).get("provenance")}
    # 发布页字节指纹是公开来源身份；不传用户问题、任务记录或读取许可。
    public["announcement_hash"] = source.get("announcement_hash")
    payload = {"source": public, "sha256": digest, "size_bytes": size_bytes,
               "processing_identity": spec.identity(digest)}
    if source.get("source_format") == "HTML":
        from archspec.official_documents.web_body import read_captured_body
        public.update({key: source[key] for key in ("source_format", "body_sha256", "body_size_bytes")})
        payload["original_html"] = base64.b64encode(read_captured_body(documents, source)).decode("ascii")
    with tempfile.TemporaryDirectory(prefix=".remote-compute-", dir=documents.cache_dir) as temporary:
        work = Path(temporary)
        assert work.resolve().parent == documents.cache_dir.resolve()
        archive = work / "publication.zip"
        with Client(auth=(os.getenv("ARCHSPEC_SERVER_USER", "archspec"), password),
                    timeout=httpx.Timeout(COMPUTE_RESULT_READ_SECONDS, connect=30, write=120, pool=30),
                    follow_redirects=False, trust_env=False) as client:
            # 免费计算实例可能休眠；先等就绪，再唯一提交计算，避免唤醒占用计算请求。
            ready = client.get(endpoint + "/api/health", timeout=90)
            ready.raise_for_status()
            state = ready.json()
            if (state.get("status") != "ok" or state.get("role") != "PURE_CONTENT_COMPUTE"
                    or state.get("runtime_ocr") != "disabled"):
                raise ComputeUnavailable("PROCESSING_UNAVAILABLE")
            with ExitStack() as transfer:
                if source.get("source_format") == "HTML":
                    response = transfer.enter_context(client.stream("POST", endpoint + "/compute", json=payload))
                else:
                    from archspec.official_documents.processing_cache import ProcessingCache, file_hash
                    original = ProcessingCache(documents.cache_dir).blob_path(digest)
                    if original.is_symlink() or not original.is_file() or original.stat().st_size != size_bytes or file_hash(original) != digest:
                        raise ProcessingLimited()
                    file = transfer.enter_context(original.open("rb"))
                    response = transfer.enter_context(client.stream("POST", endpoint + "/compute-captured",
                        data={"metadata": json.dumps(payload, ensure_ascii=False)},
                        files={"original": ("original.pdf", file, "application/pdf")}))
                if response.status_code == 409:
                    try:
                        detail = json.loads(response.read()).get("detail")
                    except (ValueError, AttributeError):
                        detail = None
                    raise ComputeUnavailable(detail.get("code") if isinstance(detail, dict) else None)
                response.raise_for_status()
                total = 0
                with archive.open("xb") as output:
                    for block in response.iter_bytes(1024 * 1024):
                        total += len(block)
                        if total > MAX_PACKAGE:
                            raise ProcessingLimited()
                        output.write(block)
        root = work / "prepared"
        root.mkdir()
        with zipfile.ZipFile(archive) as package:
            total = 0
            for entry in package.infolist():
                name = entry.filename
                if not re.fullmatch(r"prepared-processing[.]json|content-store/(?:extractions|snapshots)/[a-f0-9]{64}[.]json|publication/vectors[.]json|publication/rendering[.]pdf|publication/pages/[0-9]{1,3}[.]png", name):
                    raise ProcessingLimited()
                total += entry.file_size
                if total > MAX_PACKAGE:
                    raise ProcessingLimited()
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source_file, target.open("xb") as output:
                    shutil.copyfileobj(source_file, output, 1024 * 1024)
        # 现有缓存读取器继续核原件SHA、处理身份、全部文字/向量/原页，不信远端成功声明。
        from archspec.official_documents.policy import policy_for_source
        with prepared_package(root, source_page_url=source["page_url"], source_policy=policy_for_source(source)):
            yield root
