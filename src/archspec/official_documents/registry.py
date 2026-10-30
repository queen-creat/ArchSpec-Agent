"""从服务端保存的公告登记附件；题名匹配不等于版本、适用性或许可核验。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from archspec.official_documents.downloader import (
    LEGACY_PDF,
    MOHURD_HOSTS,
    validate_attachment_url,
)
from archspec.official_documents.government_urls import (
    validate_government_pair,
    validate_government_url,
)
from archspec.official_documents.local_policy import ATTACHMENTS, reviewed_publication
from archspec.standard_catalog.provider import validate_official_url

SOURCES = {
    "gb55031": {
        "title": "民用建筑通用规范",
        "standard_code": "GB 55031-2022",
        "page_url": "https://zjj.sm.gov.cn/xxgk/fgwj/jsbz/202209/t20220909_1827378.htm",
        "pdf_url": LEGACY_PDF,
        "kind": "FULL_TEXT",
        "auto_acquire": True,
        "reason": "原有已登记正文入口；仍待核验版本、适用范围及使用许可",
    },
}
_REGISTRY_LOCK = threading.RLock()


def validate_source_pair(page_url, pdf_url, *, publication_kind=None):
    """地方公告和附件精确成对；不能拿审核过的A附件给B页面背书。"""
    if publication_kind == "OFFICIAL_WEB_BODY":
        validate_government_url(page_url)
        if pdf_url != page_url:
            raise ValueError("网页正文须对应实际捕获的同一发布页")
        return
    local = reviewed_publication(page_url)
    if local is not None:
        if pdf_url != local.pdf_url:
            raise ValueError("地方附件不属于该登记公告")
        return
    # 新政府副本必须携带服务器实际读取后保存的角色；旧的二参数合同保持严格。
    if publication_kind == "OFFICIAL_COPY" and pdf_url not in ATTACHMENTS:
        validate_government_pair(page_url, pdf_url)
        return
    validate_official_url(page_url, MOHURD_HOSTS)
    validate_official_url(pdf_url, MOHURD_HOSTS)
    validate_attachment_url(pdf_url)
    if pdf_url in ATTACHMENTS:
        raise ValueError("地方附件不能挂在国家公告下")


def _compact(text: str) -> str:
    return re.sub(r"[\s《》〈〉（）()\[\]]+", "", unicodedata.normalize("NFKC", text))


def _classify(title: str, standard_title: str, notice_type: str, *, renamed_title=None) -> tuple[str, bool, str]:
    label = _compact(title)
    if "废止" in label or "强制性条文清单" in label:
        return "WITHDRAWAL", False, "废止条文或旧规范清单，不是完整规范正文"
    matched = bool(standard_title and _compact(standard_title) in label)
    # 更名只帮助已核修订附件对应原公告，不把修订资料升级成完整规范。
    if notice_type == "REVISION" and renamed_title:
        matched = matched or _compact(renamed_title) in label
    if "修订" in label or "修改单" in label or notice_type == "REVISION":
        return "REVISION", matched, "修订附件须与完整规范区分，不自动合并或替换正文"
    if matched:
        return "FULL_TEXT", True, "附件题名与公告主规范匹配；处理完成后仍待核验"
    return "UNCONFIRMED", False, "附件题名未能与公告主规范可靠对应，需人工确认"


class OfficialSourceRegistry:
    def __init__(self, cache_dir: Path, *, backend=None):
        from archspec.official_documents.source_context import current_registry
        from archspec.persistence.source_registry import SourceRegistryUnavailable
        selected = current_registry(cache_dir)
        if selected is not None:
            if backend is not None and backend is not selected.backend:
                raise SourceRegistryUnavailable("来源后端与当前请求绑定不同，未回退本地")
            backend = selected.backend
        self.backend = backend
        if backend is not None and not all(callable(getattr(backend, name, None)) for name in ("load", "save", "mutation")):
            raise RuntimeError("来源登记后端未提供完整事务接口")
        self.path = Path(cache_dir) / "sources.json"

    def _read(self) -> dict:
        entries = {key: {"source_id": key, **value} for key, value in SOURCES.items()}
        if self.backend is not None or self.path.exists():
            payload = (self.backend.load() if self.backend is not None
                       else json.loads(self.path.read_text(encoding="utf-8")))
            for item in payload["sources"]:
                source_id = item["source_id"]
                if not re.fullmatch(r"official-[a-f0-9]{32}", source_id):
                    raise ValueError("正文来源登记记录格式无效")
                validate_source_pair(item["page_url"], item["pdf_url"],
                    publication_kind=(item.get("source_metadata") or {}).get("provenance"))
                entries[source_id] = item
        return entries

    def _access_lock(self):
        # SQL已有同库共享/独占事务与CAS；再套全局mutex会与短读的数据库锁反转。
        # LOCAL JSON以及非SQL兼容后端仍保留原RLock，避免并发读改写丢失登记。
        from archspec.persistence.source_registry import SQLSourceRegistryBackend
        return nullcontext() if isinstance(self.backend, SQLSourceRegistryBackend) else _REGISTRY_LOCK

    def list(self) -> list[dict]:
        with self._access_lock():
            return list(self._read().values())

    def get(self, source_id: str) -> dict:
        with self._access_lock():
            entries = self._read()
            if source_id not in entries:
                raise LookupError("未登记的官方正文来源")
            return entries[source_id]

    def register_notice(self, notice: dict, run_id: str) -> list[dict]:
        # 调用方必须从已保存的服务端发现记录取公告；浏览器不提供附件网址。
        local = reviewed_publication(notice["url"])
        if local is None:
            validate_government_url(notice["url"])
        elif (notice.get("standard_title") != local.title
              or notice.get("standard_code") != local.code):
            raise ValueError("地方公告题名或资料标识不符合登记来源")
        if local and local.attachment_titles and (
                notice.get("review_id") != local.review_id or notice.get("region") != local.region
                or notice.get("authority") != local.authority
                or notice.get("document_type") != local.document_type):
            raise ValueError("地方公告的审阅身份、机关或地域不符合固定准入记录")
        registered = []
        with self._access_lock(), (self.backend.mutation() if self.backend is not None else nullcontext()):
            entries = self._read()
            for attachment in notice.get("attachments", [])[:10]:
                url = attachment.get("url", "")
                try:
                    validate_source_pair(notice["url"], url, publication_kind=("OFFICIAL_WEB_BODY"
                        if attachment.get("source_format") == "HTML" else notice.get("notice_type")))
                except ValueError:
                    continue
                title = str(attachment.get("title") or "官方附件")[:500]
                names = parse_qs(urlsplit(url).query).get("fileName", [])
                # 官方链接文字偶尔是“附件”，文件名同样来自已保存的公告链接。
                name = unquote(names[0]) if names else unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                from archspec.official_discovery.provider import renamed_attachment_title

                kind, auto, reason = _classify(
                    title + " " + name, str(notice.get("standard_title", "")),
                    str(notice.get("notice_type", "")), renamed_title=renamed_attachment_title(notice),
                )
                if local and local.attachment_titles:
                    if _compact(title) not in {_compact(alias) for alias in local.attachment_titles}:
                        continue
                    kind, auto, reason = local.kind, True, "已核精确公告主标题和附件别名配对；处理完成后仍待核验"
                if local and kind != local.kind:
                    # 名称含“补充细则”的附件只能成为修订资料，不能覆盖正文。
                    continue
                identity = f"{notice['standard_code']}\n{url}\n{kind}"
                source_id = "official-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                item = {
                    "source_id": source_id,
                    "title": notice["standard_title"] if kind == "FULL_TEXT" else title,
                    "attachment_title": title,
                    "standard_code": notice["standard_code"],
                    "page_url": notice["url"], "pdf_url": url,
                    "kind": kind, "auto_acquire": auto, "reason": reason,
                    "discovery_run_id": run_id,
                    "announcement_hash": notice.get("text_sha256"),
                }
                if attachment.get("expected_original_sha256"):
                    # PDF首页身份绑定完整字节，正式获取不能悄悄换成已更新的另一份文件。
                    item["expected_original_sha256"] = attachment["expected_original_sha256"]
                if attachment.get("source_format") == "HTML":
                    item.update(source_format="HTML", body_sha256=attachment["body_sha256"],
                                body_size_bytes=attachment["body_size_bytes"], original_url=url)
                if notice.get("notice_type") == "OFFICIAL_COPY":
                    item["source_metadata"] = {"level": notice["scope"],
                        "region": "全国" if notice["scope"] == "NATIONAL" else notice.get("region"),
                        "publisher": None, "provenance": ("OFFICIAL_WEB_BODY"
                        if attachment.get("source_format") == "HTML" else "OFFICIAL_COPY")}
                entries[source_id] = item
                if not any(x["source_id"] == source_id for x in registered):
                    registered.append(item)
            if registered:
                payload = {"sources": [item for key, item in entries.items() if key not in SOURCES]}
                if self.backend is not None:
                    # 保留导入时的额外登记字段，全部修改仍在同一事务内。
                    self.backend.save({**self.backend.load(), **payload})
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
                    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    temporary.replace(self.path)
        return registered
