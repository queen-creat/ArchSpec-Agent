"""限量实时发现、查询快照与明确标注的过期缓存。"""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import httpx

from archspec.official_discovery.government_copies import (
    inspect_government_copy,
    inspection_priority,
    supports_inspection,
)
from archspec.official_discovery.local_sources import YichangClueProvider
from archspec.official_discovery.plan import MATCH_MESSAGES, build_discovery_plan
from archspec.official_discovery.portal_search import PORTAL_ROOTS, discover_portal
from archspec.official_discovery.portal_search import VERSION as PORTAL_VERSION
from archspec.official_discovery.provider import MohurdDiscoveryProvider
from archspec.official_discovery.public_queries import (  # noqa: F401 -- 保留旧公共导入接口
    PUBLIC_CODE,
    PUBLIC_TITLES,
    TOPICS,
    extract_public_codes,
    plan_queries,
)
from archspec.official_discovery.tavily import (
    TavilyUnavailable,
    reviewed_mohurd_publication,
    validate_clue,
    validate_public_term,
)
from archspec.official_documents.local_policy import LOCAL_POLICY_VERSION, reviewed_publication

NETWORK_ERRORS = (httpx.HTTPError, OSError, ValueError, TimeoutError)


def _network_failure(error):
    """只保留可行动的失败类别，不回显请求头、密钥或远端原文。"""
    from archspec.official_documents.official_fetch import FetchNodeUnavailable
    if isinstance(error, FetchNodeUnavailable):
        return error.public_message
    if isinstance(error, httpx.HTTPStatusError):
        return f"官方服务 HTTP {error.response.status_code}，本次未读取"
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "官方连接超时，本次未读取"
    if isinstance(error, (httpx.TransportError, OSError)):
        return "官方连接失败，本次未读取"
    return "官方数据暂无法解析，本次未读取"


class _NoticeBudget:
    """仅属于一次查找；在发请求前记账，错误不能释放上限或重复读同URL。"""
    def __init__(self):
        self._attempted = set()

    @property
    def count(self):
        return len(self._attempted)

    def reserve(self, url):
        if self.count >= 5 or url in self._attempted:
            return False
        self._attempted.add(url)
        return True


class DiscoveryService:
    _lock = Lock()

    def __init__(self, cache_dir: Path, provider=None, clock=None, local_provider=None,
                 external_provider=None, publication_catalog=None):
        self.cache_dir = cache_dir
        self.provider = provider or MohurdDiscoveryProvider()
        self.local_provider = local_provider or YichangClueProvider(cache_dir)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.external_provider = external_provider
        self.portal_roots = PORTAL_ROOTS
        if publication_catalog is None:
            from archspec.official_discovery.registered_publications import PublicationCatalog
            publication_catalog = PublicationCatalog()
        self.publication_catalog = publication_catalog

    def _read(self, path: Path) -> dict | None:
        from archspec.persistence.business_files import read_runtime_file
        managed, content = read_runtime_file(path, missing_ok=True)
        try:
            if managed:
                return json.loads(content) if content is not None else None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, path: Path, value: dict):
        from archspec.persistence.business_files import write_runtime_file
        if write_runtime_file(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid4()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _with_official_entries(self, result):
        """补当前已登记的官网导航，不改历史抓取内容，也不声称取得规范正文。"""
        from archspec.official_documents.government_urls import validate_government_url

        region = result.get("discovery_plan", {}).get("region")
        root = self.portal_roots.get(region)
        if root is None:
            return result
        try:
            url = validate_government_url(root)
        except ValueError:
            return result
        return {**result, "official_entries": [{"kind": "OFFICIAL_ENTRY",
            "name": f"{region}政府官网", "url": url, "region": region,
            "body_status": "NOT_ACQUIRED", "basis": "REGISTERED_PORTAL"}]}

    def get(self, run_id: str) -> dict:
        try:
            if str(UUID(run_id)) != run_id:
                raise ValueError("invalid id")
        except ValueError as error:
            raise LookupError("查询记录不存在") from error
        result = self._read(self.cache_dir / "runs" / f"{run_id}.json")
        if result is None:
            raise LookupError("查询记录不存在")
        return self._with_official_entries(result)

    def run(self, query: str, topics=(), refresh=False, *, region=None, public_titles=()) -> dict:
        if len(query.strip()) > 500:
            raise ValueError("问题最长为 500 个字")
        plan = build_discovery_plan(query, topics, region=region, public_titles=public_titles)
        public_plan = plan.public_dict()
        local = plan.route == "YICHANG"
        queries, omitted = public_plan["queries"], plan.omitted_query_count
        # 新共同目标及公平调度隔离全部旧查询缓存；原公告和正文快照完全保留。
        cache_identity = {"version": 8 if local else 21, "plan": public_plan}
        if plan.route == "UNSUPPORTED_LOCAL":
            # 只隔离地方候选新排序，旧查询档案和全国/已审核来源缓存保持。
            cache_identity["local_notice_priority"] = 1
        if plan.route in {"MOHURD", "REGISTERED_LOCAL"}:
            cache_identity["registered_publications"] = self.publication_catalog.version
        if local:
            cache_identity["reviewed_local_policy"] = LOCAL_POLICY_VERSION
        if plan.route == "UNSUPPORTED_LOCAL" and plan.region in self.portal_roots:
            cache_identity["portal_discovery"] = {"version": PORTAL_VERSION, "root": self.portal_roots[plan.region]}
        if self.external_provider is not None:
            cache_identity["external_provider"] = self.external_provider.version
        identity = json.dumps(cache_identity, ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()
        cache_path = self.cache_dir / "queries" / f"{key}.json"
        cached = self._read(cache_path)
        now = self.clock()
        if cached and not refresh and now - datetime.fromisoformat(cached["fetched_at"]) < timedelta(hours=1):
            return self._with_official_entries({**cached, "from_cache": True})
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("已有官方查询正在执行，请稍后再试")
        try:
            # 一次run中的原生/已审核入口/外部线索共用尝试账本，失败也占预算。
            notice_budget = _NoticeBudget()
            if local:
                local_result = (self.local_provider.discover(notice_budget=notice_budget)
                                if isinstance(self.local_provider, YichangClueProvider)
                                else self.local_provider.discover())
                result = {**local_result, "id": str(uuid4()), "queries": queries,
                          "fetched_at": now.isoformat(), "attempted_at": now.isoformat(),
                          "from_cache": False, "omitted_query_count": 0}
                result["results"] = [self._match_notice(item, plan) for item in result["results"]]
            elif plan.route == "UNSUPPORTED_LOCAL":
                result = {
                    "id": str(uuid4()), "queries": queries, "searches": [], "results": [], "errors": [],
                    "status": "PARTIAL", "local_coverage": "UNSUPPORTED_REGION",
                    "fetched_at": now.isoformat(), "attempted_at": now.isoformat(),
                    "from_cache": False, "omitted_query_count": omitted, "omitted_notice_count": 0,
                    "limitations": [f"尚未接入{plan.region}的官方正文发现路线；未以全国规范充当地方答案",
                                    "请核对所需地方正式文件或补充公开规范编号；未外发原问题"],
                }
            else:
                result = self._discover(queries, omitted, now, plan=plan, notice_budget=notice_budget)
            if self.external_provider is not None or (plan.route == "UNSUPPORTED_LOCAL" and plan.region in self.portal_roots):
                result = self._supplement_external(result, plan, notice_budget=notice_budget)
            result["notice_attempted_count"] = notice_budget.count
            result["discovery_plan"] = public_plan
            # 本次失败单独存记录，但不覆盖以前完整成功的查询结果。
            if (result["status"] == "FAILED" or result.get("external_status") == "FAILED") and cached:
                result.update({"status": "STALE_CACHE", "from_cache": True,
                               "fetched_at": cached["fetched_at"],
                               "results": copy.deepcopy(cached["results"]),
                               "cached_run_id": cached["id"]})
                if local or plan.route == "REGISTERED_LOCAL":
                    result.update(local_clues=copy.deepcopy(cached.get("local_clues", [])),
                                  local_coverage=cached.get("local_coverage", "ENTRY_ONLY"))
                if self.external_provider is not None:
                    result["external_clues"] = copy.deepcopy(cached.get("external_clues", []))
                    result["external_from_cache"] = True
            self._save(self.cache_dir / "runs" / f"{result['id']}.json", result)
            if result["status"] == "COMPLETE":
                self._save(cache_path, result)
            return self._with_official_entries(result)
        finally:
            self._lock.release()

    @staticmethod
    def _external_queries(plan):
        """地域仅取共同计划的登记地名，原问题和项目资料不进入第三方请求。"""
        if plan.region == "地域待补充":
            return []
        queries = []
        for term, _reason in plan.queries[:3]:
            public = f"{plan.region} {term}" if plan.region and plan.region not in term else term
            validate_public_term(public)
            if public not in queries:
                queries.append(public)
        return queries

    def _supplement_external(self, result, plan, *, notice_budget=None):
        """政府搜索另存线索，只有审核过的发布路径才能独立读取原公告。"""
        notice_budget = notice_budget if notice_budget is not None else _NoticeBudget()
        terms = self._external_queries(plan)
        provider_name = getattr(self.external_provider, "name", "TAVILY")
        result.update(external_search=[], external_clues=[], external_status="NOT_QUERIED",
                      external_provider_version=self.external_provider.version if self.external_provider is not None else None,
                      external_omitted_clue_count=0)
        if not terms:
            result["external_message"] = (
                "地域尚未补充，未发送地方问题或调用外部搜索" if plan.region == "地域待补充"
                else "未识别可发送的公共规范编号、题名或主题，未调用外部搜索")
            return result
        found, queues, successes, failures = {}, [], 0, 0
        if plan.route == "UNSUPPORTED_LOCAL" and plan.region in self.portal_roots:
            native = discover_portal(plan, self.portal_roots, client=getattr(self.provider, "client", None))
            result["searches"].extend(native["searches"])
            result["errors"].extend(native["errors"])
            result["portal_metadata_read_count"] = native["metadata_read_count"]
            if any(row["status"] == "COMPLETE" for row in native["searches"]):
                result["local_coverage"] = "CLUES_ONLY"
                result["limitations"] = ["已查询对应机关的官方门户；仅搜索线索，正文、版本和适用性分别核验",
                                        "每个公共词两提供方各一次，公告正文共享五页上限"]
            queues.extend(native["queues"])
            for clue in native["items"]:
                found.setdefault(clue["url"], clue)
        for term_index, term in enumerate(terms if self.external_provider is not None else []):
            try:
                response = self.external_provider.search(term)
                if (not isinstance(response, dict) or not isinstance(response.get("items"), list)
                        or len(response["items"]) > 5):
                    raise TavilyUnavailable("外部搜索线索合同变化")
                items = []
                for row in response["items"]:
                    if (not isinstance(row, dict) or row.get("kind") != "SEARCH_SNIPPET"
                            or row.get("body_status") != "NOT_ACQUIRED"):
                        raise TavilyUnavailable("外部搜索线索类别无法核对")
                    clue = validate_clue({"title": row.get("title"), "url": row.get("url"),
                        "content": row.get("excerpt"), "score": row.get("score")})
                    if clue is not None:
                        # 来源名来自受信提供方，不接受网页自称的身份或置信度。
                        clue["provider"] = provider_name
                        if row.get("score_kind") == "NOT_CALIBRATED":
                            clue["score_kind"] = "NOT_CALIBRATED"
                        items.append(clue)
            except NETWORK_ERRORS as error:
                failures += 1
                message = str(error) if isinstance(error, TavilyUnavailable) else "外部搜索未完成，未取得可核对线索"
                result["external_search"].append({"term": term, "provider": provider_name,
                    "status": "FAILED", "result_count": 0, "message": message})
                result["errors"].append({"term": term, "message": message})
                if isinstance(error, TavilyUnavailable) and error.stop_batch:
                    # 剩余词保留在进度中但绝不发请求，避免超额度或坏凭据反复调用。
                    result["external_search"].extend({"term": remaining, "provider": provider_name,
                        "status": "SKIPPED", "result_count": 0,
                        "message": "本批次遇到账户、额度或频率限制，该公共词未发送"}
                        for remaining in terms[term_index + 1:])
                    break
                continue
            successes += 1
            result["external_search"].append({"term": term, "provider": provider_name,
                "status": "COMPLETE", "result_count": len(items),
                "rejected_result_count": response.get("rejected_result_count", 0),
                # 保留计划词，同时展示提供方实际发送的公共查询，避免掩盖地域约束。
                "search_query": response.get("search_query", term),
                "exact_match": response.get("exact_match", False)})
            queues.append([item["url"] for item in items])
            for item in items:
                clue = found.setdefault(item["url"], {**item, "matched_queries": [],
                                                       "requested_region": plan.region})
                if term not in clue["matched_queries"]:
                    clue["matched_queries"].append(term)
        candidates = sorted(self._fair_candidates(queues, found, limit=15),
                            key=lambda clue: inspection_priority(clue, plan))
        for clue in candidates:
            if not supports_inspection(clue["url"]):
                clue.update(inspection_state="UNSUPPORTED_FORMAT",
                    inspection_message="此附件格式尚不支持自动核对，请打开官方链接；未获取正文")
        result["external_clues"] = candidates[:5]
        result["external_omitted_clue_count"] = max(0, len(found) - len(result["external_clues"]))
        # SKIPPED不是成功或失败的实际请求；全失败与已有成功须明确区分。
        result["external_status"] = ("NOT_CONFIGURED" if self.external_provider is None else
                                     "FAILED" if failures and not successes
                                     else "PARTIAL" if failures else "COMPLETE")
        existing = {item.get("url") for item in result["results"]}
        # 摘要只决定读取顺序；身份仍由下面实际网页的主标题和附件关系核验。
        ordered = list(candidates)
        result["external_navigation"] = []
        while ordered and notice_budget.count < 5:
            clue = ordered.pop(0)
            # 仍保留官方线索，但不把已知二进制附件当网页读取或占五页核对额度。
            if not supports_inspection(clue["url"]):
                continue
            # 已审核地方配对保持原规则；其他地方页交共同主文件身份核验，不能拿全国副本代替。
            local_source = reviewed_publication(clue["url"])
            local_target = (local_source is not None and plan.region is not None
                            and plan.match_source(local_source.code, local_source.title) == "TARGET")
            national_target = plan.region is None and reviewed_mohurd_publication(clue["url"])
            government_copy = local_source is None and not national_target and plan.region != "地域待补充"
            if clue["url"] in existing or not (local_target or national_target or government_copy):
                continue
            if not notice_budget.reserve(clue["url"]):
                continue
            try:
                if government_copy:
                    from archspec.official_documents.web_body import capture_web_body
                    notice = inspect_government_copy(clue, plan, client=getattr(self.provider, "client", None),
                        capture_original=lambda raw: capture_web_body(self.cache_dir, raw), cache_dir=self.cache_dir)
                else:
                    inspector = self.local_provider if local_target else self.provider
                    notice = inspector.inspect({"title": clue["title"], "url": clue["url"]})
                if notice.get("navigation_links"):
                    # 导航也消耗同一个五页预算；只将最终核清身份的发布页交给入库。
                    result["external_navigation"].append(notice)
                    ordered[0:0] = [{**row, "matched_queries": list(clue["matched_queries"])}
                                    for row in notice["navigation_links"]]
                    continue
                notice = {**notice, "matched_queries": list(clue["matched_queries"])}
                result["results"].append(self._match_notice(notice, plan))
                existing.add(clue["url"])
            except NETWORK_ERRORS as error:
                result["errors"].append({"url": clue["url"],
                    "message": _network_failure(error) + "；搜索摘要未当成正文"})
        result["notice_attempted_count"] = notice_budget.count
        if plan.region and any(item.get("scope") == "LOCAL" and item.get("target_match") == "TARGET"
                               and item.get("attachments") for item in result["results"]):
            result["local_coverage"] = "ATTACHMENTS_DISCOVERED"
            result["limitations"] = ["已核地方发布页和附件关系；正文、现行性和适用条件仍须分别核对"]
        native_status = result["status"]
        if result["external_status"] == "FAILED":
            result["status"] = "FAILED" if native_status == "FAILED" or (
                plan.route == "UNSUPPORTED_LOCAL" and not result["results"]) else "PARTIAL"
        elif result["external_status"] == "PARTIAL" or result["errors"] or native_status == "FAILED":
            result["status"] = "PARTIAL"
        elif native_status == "COMPLETE" or plan.route == "UNSUPPORTED_LOCAL":
            # COMPLETE仅指本次受限线索检索完成；地方正文仍明确UNSUPPORTED_REGION。
            result["status"] = "COMPLETE"
        result["limitations"].append("外部政府搜索摘要只供找出处；未登记正文、版本和适用性，不能用于数值回答")
        return result

    @staticmethod
    def _match_notice(item, plan):
        match = plan.match_source(item.get("standard_code"), item.get("standard_title"))
        if match != "TARGET" and plan.route == "MOHURD":
            from archspec.official_discovery.provider import renamed_attachment_title

            renamed = renamed_attachment_title(item)
            if renamed is not None and plan.match_source(item["identity_change"]["to_code"], renamed) == "TARGET":
                # 同一公告明确更名可对应新查询；旧主编号、修订角色及年份限制均不改写。
                return {**item, "target_match": "TARGET", "target_match_basis": "EXPLICIT_NAME_CODE_CHANGE",
                        "target_match_message": "公告明确记录所查名称、编号的变更；修订附件仍不代表完整规范正文"}
        return {**item, "target_match": match, "target_match_message": MATCH_MESSAGES[match]}

    @staticmethod
    def _fair_candidates(queues, found, *, limit=5):
        """每个公共词轮流取一个未读取链接，共享五份公告上限，先词不能占满预算。"""
        positions, selected, seen = [0] * len(queues), [], set()
        while len(selected) < limit:
            progress = False
            for index, urls in enumerate(queues):
                while positions[index] < len(urls):
                    url = urls[positions[index]]
                    positions[index] += 1
                    if url in seen:
                        continue
                    seen.add(url)
                    selected.append(found[url])
                    progress = True
                    break
                if len(selected) == limit:
                    break
            if not progress:
                break
        return selected

    def _discover(self, queries: list[dict], omitted: int, now: datetime, *, plan=None,
                  notice_budget=None) -> dict:
        found, searches, errors, queues = {}, [], [], []
        registered_local = plan is not None and plan.route == "REGISTERED_LOCAL"
        inspector = self.local_provider if registered_local else self.provider
        notice_budget = notice_budget if notice_budget is not None else _NoticeBudget()
        from archspec.persistence.runtime_bindings import current_runtime, runtime_scope
        bindings = current_runtime()

        def invoke(operation, *args, **kwargs):
            # 只传固定运行绑定；每个线程自建/释放资源，不复制请求的 SQL 连接或模型租约。
            with runtime_scope(bindings):
                return operation(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = ([None] * len(queries) if registered_local else
                       [executor.submit(invoke, self.provider.search, q["term"]) for q in queries])
            for query, future in zip(queries, futures, strict=True):
                # 登记只提供寻找入口，仍由下方实际公告解析核主规范和附件。
                registered = self.publication_catalog.for_query(plan, query["term"]) if plan else []
                items = list(registered)
                try:
                    search = (future.result() if future is not None else {
                        "total": len(registered), "items": [],
                        "search_url": registered[0]["url"] if registered else None,
                        "kind": "REVIEWED_PUBLICATION_ENTRY"})
                except NETWORK_ERRORS as error:
                    errors.append({"term": query["term"], "message": _network_failure(error)})
                else:
                    searches.append({**query, "total": search["total"], "search_url": search["search_url"]})
                    items.extend(search["items"])
                queues.append([item["url"] for item in items])
                for item in items:
                    old = found.setdefault(item["url"], {**item, "matched_queries": []})
                    if query["term"] not in old["matched_queries"]:
                        old["matched_queries"].append(query["term"])
            candidates = [item for item in self._fair_candidates(queues, found)
                          if notice_budget.reserve(item["url"])]
            inspected = [executor.submit(invoke, inspector.inspect, item) for item in candidates]
            results, read_successes = [], 0
            for item, future in zip(candidates, inspected, strict=True):
                try:
                    notice = {**future.result(), "matched_queries": item["matched_queries"]}
                    results.append(self._match_notice(notice, plan) if plan else notice)
                    read_successes += 1
                except NETWORK_ERRORS as error:
                    errors.append({"url": item["url"], "message": _network_failure(error) + "；不能核对版本字段"})
                    notice = {**item, "version_check": "UNREADABLE", "lifecycle_status": "UNKNOWN",
                              "status": "PENDING_REVIEW", "full_text_available": False}
                    results.append(self._match_notice(notice, plan) if plan else notice)
            # 公告没有目标完整附件时，复用已核政府全文入口；仍读实际首页再登记。
            # 与搜索和公告共用五次尝试，失败不释放，不把目录记录当正文。
            copies_for_query = getattr(self.publication_catalog, "copies_for_query", None)
            has_attachment = any(row.get("attachments") and row.get("target_match") == "TARGET"
                and row.get("notice_type") in {"PUBLICATION", "OFFICIAL_COPY"} for row in results)
            if plan is not None and not registered_local and not has_attachment and callable(copies_for_query):
                copy_queues, copy_found = [], {}
                for query in queries:
                    entries = copies_for_query(plan, query["term"])
                    copy_queues.append([entry["url"] for entry in entries])
                    for entry in entries:
                        current = copy_found.setdefault(entry["url"], {**entry, "matched_queries": []})
                        if query["term"] not in current["matched_queries"]:
                            current["matched_queries"].append(query["term"])
                for entry in self._fair_candidates(copy_queues, copy_found):
                    found.setdefault(entry["url"], entry)
                    if not notice_budget.reserve(entry["url"]):
                        continue
                    candidates.append(entry)
                    try:
                        self.cache_dir.mkdir(parents=True, exist_ok=True)
                        notice = invoke(inspect_government_copy, entry, plan,
                            client=getattr(self.provider, "client", None), cache_dir=self.cache_dir)
                        notice.update(matched_queries=entry["matched_queries"],
                                      entry_origin="REVIEWED_GOVERNMENT_COPY")
                        results.append(self._match_notice(notice, plan))
                        read_successes += 1
                    except NETWORK_ERRORS as error:
                        errors.append({"url": entry["url"],
                            "message": _network_failure(error) + "；已登记入口未当作正文"})

        return {
            "id": str(uuid4()), "queries": queries, "searches": searches, "results": results,
            "errors": errors, "status": "FAILED" if (
                not read_successes and (registered_local or not searches))
            else "PARTIAL" if errors else "COMPLETE",
            "notice_attempted_count": notice_budget.count,
            "fetched_at": now.isoformat(), "attempted_at": now.isoformat(), "from_cache": False,
            "omitted_query_count": omitted, "omitted_notice_count": max(0, len(found) - len(candidates)),
            "local_coverage": ("ATTACHMENTS_DISCOVERED" if any(
                item.get("full_text_available") for item in results) else "ENTRY_ONLY")
                if registered_local else "NOT_CONNECTED",
            "limitations": ["仅精确审核的地方发布入口可交接正文；其它地方线索未自动准入" if registered_local
                            else "目前只查住建部官网，地方规范、规划条件和地方审查口径未接入",
                            "每个词只读取第一页 20 条，最多核对 5 份公告，不保证规范齐全",
                            "日期与部分废止线索来自公告；未完成最新现行性和项目适用性复核",
                            "附件仅发现链接，没有自动下载正文，也没有调用大模型"],
        }
