"""独立纯内容计算服务；没有来源库、模型API密钥或正式发布权。"""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Form, UploadFile, File
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from archspec.deployment import AccessGate, load_server_settings
from archspec.official_documents.downloader import download_pdf_file
from archspec.official_documents.policy import policy_for_source
from archspec.official_documents.processing_cache import PREPARED_MANIFEST
from archspec.official_documents.remote_compute import ComputeUnavailable
from archspec.official_documents.runtime_ocr import RuntimeOcrDisabled
from archspec.official_documents.registry import SOURCES, validate_source_pair


def _unavailable(error, stage):
    failure = ComputeUnavailable("OCR_REQUIRED" if isinstance(error, RuntimeOcrDisabled) else stage)
    from archspec.persistence.migration_operator import safe_diagnostic
    import re
    import ssl
    diagnostic = safe_diagnostic(error)
    current = error
    # 只保留固定TLS符号、校验编号及错误类型；不输出异常文本、URL或请求头。
    for row in diagnostic["chain"]:
        if isinstance(current, ssl.SSLError):
            reason = current.reason or ""
            if re.fullmatch(r"[A-Z0-9_]{1,80}", reason):
                row["tls_reason"] = reason
        if isinstance(current, ssl.SSLCertVerificationError):
            row["tls_verify_code"] = current.verify_code
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    print(json.dumps({"event": "COMPUTE_UNAVAILABLE", "code": failure.code,
                      "diagnostic": diagnostic}), flush=True)
    return HTTPException(409, {"code": failure.code, "message": failure.public_message,
                              "diagnostic": diagnostic})

def create_compute_app(config=None, *, download_client=None):
    from archspec.official_documents.runtime_ocr import runtime_ocr_enabled
    if runtime_ocr_enabled():
        raise ValueError("独立免费计算必须明确关闭OCR，扫描件继续原件兜底")
    config = config or load_server_settings()
    root = config.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    busy = Lock()

    @app.get("/api/health")
    def health():
        return {"status": "ok", "role": "PURE_CONTENT_COMPUTE", "runtime_ocr": "disabled"}

    def _compute(payload: dict, captured_original=None):
        if not busy.acquire(blocking=False):
            raise HTTPException(429, "独立计算正在处理另一份资料")
        work = Path(tempfile.mkdtemp(prefix="job-", dir=root))
        def cleanup():
            if work.resolve().parent != root or not work.name.startswith("job-"):
                raise ValueError("计算临时目录归属不一致")
            shutil.rmtree(work)
        stage = "PROCESSING_UNAVAILABLE"
        try:
            source = payload["source"]
            role = (source.get("source_metadata") or {}).get("provenance")
            legacy = source["source_id"] == "gb55031" and all(
                source[key] == SOURCES["gb55031"][key] for key in ("page_url", "pdf_url"))
            if not legacy:
                validate_source_pair(source["page_url"], source["pdf_url"], publication_kind=role)
            policy = policy_for_source(source)
            destination = work / "prepared"
            if source.get("source_format") == "HTML":
                from archspec.official_documents.web_body import (
                    MAX_HTML_BYTES,
                    prepare_web_body_cache,
                )
                raw = base64.b64decode(payload["original_html"], validate=True)
                if (not 0 < len(raw) <= MAX_HTML_BYTES or len(raw) != payload["size_bytes"]
                        or hashlib.sha256(raw).hexdigest() != payload["sha256"]):
                    raise ValueError("网页原件与已捕获字节身份不同")
                original = work / "original.html"
                original.write_bytes(raw)
                prepare_web_body_cache(original, destination, source, policy.max_pages)
            else:
                context = {"page_url": source["page_url"]} if role == "OFFICIAL_COPY" else {}
                stage = "DOWNLOAD_UNAVAILABLE"
                if captured_original is None:
                    downloaded = download_pdf_file(source["pdf_url"], work, max_bytes=policy.max_bytes,
                                                   client=download_client, **context)
                else:
                    from archspec.official_documents.downloader import DownloadedPDF
                    from archspec.official_documents.processing_cache import file_hash
                    downloaded = DownloadedPDF(captured_original, captured_original.stat().st_size,
                                               file_hash(captured_original))
                if downloaded.sha256 != payload["sha256"] or downloaded.size_bytes != payload["size_bytes"]:
                    raise ValueError("独立下载原件与网页已取得原件不一致")
                stage = "PROCESSING_UNAVAILABLE"
                from archspec.demo_seed import prepare_processing_cache
                from archspec.official_documents.native_text import native_processing_spec
                from archspec.official_documents.service import OfficialDocumentService
                base = OfficialDocumentService._processing_spec()
                native = native_processing_spec(base)
                selected = native if payload["processing_identity"] == native.identity(downloaded.sha256) else base
                destination = work / "prepared"
                prepare_processing_cache(downloaded.path, destination,
                    {"url": source["pdf_url"], "sha256": downloaded.sha256,
                     "size_bytes": downloaded.size_bytes, "max_pages": policy.max_pages},
                    publication=True, source_page_url=source["page_url"] if role == "OFFICIAL_COPY" else None, spec=selected, source_policy=policy)
            stage = "IDENTITY_UNAVAILABLE"
            manifest = json.loads((destination / PREPARED_MANIFEST).read_bytes())
            if manifest["identity"] != payload["processing_identity"]:
                raise ValueError("独立处理身份不同，网页不可复用")
            archive = work / "publication.zip"
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
                for file in destination.rglob("*"):
                    if file.is_file():
                        package.write(file, file.relative_to(destination).as_posix())
            return FileResponse(archive, media_type="application/zip",
                                background=BackgroundTask(cleanup))
        except Exception as error:
            cleanup()
            # 正文不可用仍交回既有原件兜底，明确区分需要OCR和下载/身份失败。
            raise _unavailable(error, stage) from None
        finally:
            busy.release()

    @app.post("/compute")
    def compute(payload: dict):
        return _compute(payload)

    @app.post("/compute-captured")
    def compute_captured(metadata: str = Form(), original: UploadFile = File()):
        # 与HTML相同：只消费主站已核原件，不再二次访问同一官网；SHA/大小/来源/处理身份仍逐项核对。
        try:
            payload = json.loads(metadata)
            policy = policy_for_source(payload["source"])
            if (payload["source"].get("source_format") == "HTML" or type(payload["size_bytes"]) is not int
                    or not 0 < payload["size_bytes"] <= policy.max_bytes):
                raise ValueError("已捕获原件不在既有范围内")
            with tempfile.TemporaryDirectory(prefix="captured-", dir=root) as folder:
                path = Path(folder) / "original.pdf"
                total = 0
                with path.open("xb") as output:
                    while block := original.file.read(64 * 1024):
                        total += len(block)
                        if total > payload["size_bytes"]:raise ValueError("原件传输超过固定大小")
                        output.write(block)
                if total != payload["size_bytes"]:raise ValueError("原件传输未完成")
                return _compute(payload, captured_original=path)
        except HTTPException:
            raise
        except Exception as error:
            raise _unavailable(error, "IDENTITY_UNAVAILABLE") from None

    return AccessGate(app, config.username, config.password)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_compute_app(), host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
