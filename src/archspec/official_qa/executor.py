"""官方QA共同有限执行器：计划驱动同一读取/预算/选择流程，不叠加循环。"""

import json
import re
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from archspec.official_discovery.local_sources import yichang_hints
from archspec.official_documents.table_context import TABLE
from archspec.official_qa import parallel_requirements as parallel
from archspec.official_qa import service as qa_service
from archspec.official_qa.answer_brief import build_answer_brief
from archspec.official_qa.answer_sufficiency import describe_answer_sufficiency
from archspec.official_qa.clause_groups import build_clause_groups
from archspec.official_qa.condition_policies import policy_for
from archspec.official_qa.context_groups import build_context_groups
from archspec.official_qa.evidence_bundle import (
    compile_evidence_bundle,
    guard_draft,
    guard_selection,
)
from archspec.official_qa.evidence_requirements import (
    complete_clause_keys,
    complete_outgoing_dependency,
    requirements_for,
    requirements_from_contexts,
)
from archspec.official_qa.fire_page_readings import build_page_readings
from archspec.official_qa.fire_supplements import read_fire_supplements
from archspec.official_qa.garage_rules import (
    build_garage_rule_checks,
    read_garage_supplements,
)
from archspec.official_qa.height_measurement import (
    read_height_supplements,
)
from archspec.official_qa.intents import (
    fire_area_question,
    net_height_question,
)
from archspec.official_qa.local_scope import (
    is_yichang_context,
    select_yichang_sources,
    spacing_evidence,
    sunlight_spacing_question,
)
from archspec.official_qa.local_spacing import (
    evidence_key,
    read_spacing_supplements,
    unit_key,
)
from archspec.official_qa.project_conditions import ProjectConditionChanged
from archspec.official_qa.public_fire_conditions import (
    public_area_question,
    public_scope_located,
    public_scope_note,
)
from archspec.official_qa.question_plan import thaw_value
from archspec.official_qa.service import QUESTIONS, is_body
from archspec.official_qa.table_groups import build_table_groups
from archspec.official_qa.table_readings import build_table_readings, table_ink_issues
from archspec.standard_governance.compatibility import ContentUseDenied


class _BudgetedReader:
    """领域读器消费本轮固定初读预算；拒绝/失败也先计数，不拥有搜索权。"""

    def __init__(self, documents, budget, category, limit):
        self.documents, self.budget = documents, budget
        self.category, self.limit = category, limit

    def __getattr__(self, name):
        return getattr(self.documents, name)

    def search_all(self, *args, **kwargs):
        raise ValueError("领域补读不能自行发起概念搜索")

    def read_context(self, *args, **kwargs):
        if self.budget[self.category] >= self.limit:
            raise ValueError("本轮领域固定初读预算已用完")
        self.budget[self.category] += 1
        return self.documents.read_context(*args, **kwargs)


class OfficialQAExecutor:
    def __init__(self, owner, request, proposal):
        self.owner, self.request, self.proposal = owner, request.model_copy(deep=True), proposal
        self.documents, self.composer = owner.documents, owner.composer
        self.validate_project, self.project_conditions = owner.validate_project, owner.project_conditions
        self.conditions_query = owner.conditions_query
        self.bundle = None
        self.reading_contexts = None
        self.table_ink_input, self.table_ink_findings = None, []
        self.parallel_lookup, self.parallel_parts = None, ()
        self.candidate_lookup = None
        self.registered_sources = None
        self.search_options = {}
        self.local_source_plan = None
        self.budget = {"initial_queries": 0, "concept_queries": 0, "concept_read_attempts": 0,
                       "fire_read_attempts": 0, "garage_read_attempts": 0, "height_read_attempts": 0,
                       "dependency_read_attempts": 0}

    def _check_project(self, result, packet):
        # 沿用所有早退路径的当前项目复核；合同详情不能让失败后的旧事实重新外显。
        result = self.recheck_current_project(result, packet)
        plan = replace(self.plan, condition_provenance=tuple(
            result.get("condition_provenance", ()))).model_dump(mode="json")
        if result["status"] == "EVIDENCE_CHECK_FAILED":
            plan["conditions"], plan["condition_provenance"] = {}, []
        result["question_plan"] = plan
        result["execution_budget"] = dict(self.budget)
        result["evidence_requirements"] = [
            row.model_dump(mode="json", exclude_none=True) for row in
            (self.bundle.requirements if self.bundle is not None else requirements_for(self.plan))]
        result["bundle_coverage"] = (thaw_value(self.bundle.coverage) if self.bundle is not None
                                     else {"core_body": "UNRESOLVED"})
        if result["status"] == "EVIDENCE_CHECK_FAILED":
            result["bundle_coverage"] = {key: "CHECK_FAILED" for key in result["bundle_coverage"]}
            for requirement in result["evidence_requirements"]:
                requirement["state"], requirement["evidence_ids"] = "CHECK_FAILED", []
        # 共同详情在原件和项目最后复核之后产生，不能把旧包缺口重新外显为有效来源。
        recorded = any(result.get(key) == "DETAILS_RECORDED" for key in (
            "height_condition_state", "local_spacing_condition_state",
            "garage_condition_state", "public_fire_condition_state"))
        result["requirement_details"], result["sufficiency"] = describe_answer_sufficiency(
            self.bundle, status=result["status"],
            condition_followups=result.get("questions", ()) or result.get("condition_followups", ()),
            condition_state="DETAILS_RECORDED" if recorded else None)
        return parallel.describe_result(result, self.bundle)

    def recheck_current_project(self, result, packet):
        return self.owner._check_project(result, packet)

    @staticmethod
    def _guard(draft, evidence):
        return guard_draft(draft, evidence)

    def _source_allowed(self, source):
        """补查、固定补读和最终重读都消费本轮同一来源交集。"""
        from archspec.official_discovery.plan import non_rule_notice_title
        if non_rule_notice_title(source.get("title") or ""):
            return False
        if not self.policy.matches_source_title(source.get("title", ""), self.plan.conditions):
            return False
        if (self.local_source_plan is not None and self.local_source_plan.match_source(
                source.get("standard_code"), source.get("title")) != "TARGET"):
            return False
        return (self.plan.effective_source_ids is None
                or source.get("source_id") in self.plan.effective_source_ids) and self.plan.matches_explicit_source(source)

    def _complete_core(self, context):
        coverage = context["selected_clause_coverage"]
        number = coverage["clause_number"]
        expected = coverage["expected_fragment_count"]
        prefix = f"{context['source_id']}:{context['snapshot_id']}:"
        core = [part for part in context["fragments"]
                if part["clause_number"] == number and part["role"] != "RELATED_CONTEXT"]
        if (not self.policy.is_body("", number) or type(expected) is not int or expected < 1
                or not coverage.get("complete") or coverage.get("included_fragment_count") != expected
                or len({part["id"] for part in core}) != expected
                or not any(part["role"] == "MATCH" and part["id"] == context["selected_chunk_id"]
                           for part in core)
                or any(not part["id"].startswith(prefix) or not part["id"][len(prefix):].isdecimal()
                       for part in core)):
            return False
        core.sort(key=lambda part: int(part["id"][len(prefix):]))
        text = "\n".join(part["text"] for part in core)
        return (parallel.matches(self.parallel_parts, text, number, context, self.plan.conditions)
                if self.parallel_parts else self.policy.accept_full_clause(text, self.plan.conditions, source_title=context.get("title", "")))

    def _read_dependencies(self, contexts):
        """明确同书引用最多补读两条；不搜网、不取latest、不递归扩展引用。"""
        existing = {(row["source_id"], row["snapshot_id"], unit_key(row["selected_clause_coverage"]))
                    for row in contexts}
        found, gaps = [], []
        for context in contexts:
            # 共用真实原字、身份、完整覆盖证明；普通邻接和传入的裸条号无资格触发。
            dependency = complete_outgoing_dependency(context)
            if dependency is None or not self._complete_core(context):
                continue
            _, targets = dependency
            for target in targets:
                key = target["source_id"], target["snapshot_id"], target["unit_key"]
                if key in existing:
                    continue
                # 完整引用文字已在父上下文核对；仅其明确提表且主题吻合时再取原表合同。
                parts = {part["id"]: part for part in context["fragments"]}
                text = "\n".join(parts[chunk]["text"] for chunk in target["chunk_ids"])
                if (not TABLE.search(text)
                        or not self.policy.relevant_text(text, self.plan.conditions, source_title=context.get("title", ""))
                        or not self.policy.accept_full_clause(text, self.plan.conditions, source_title=context.get("title", ""))):
                    continue
                existing.add(key)
                if self.budget["dependency_read_attempts"] >= 2:
                    gaps.append("明确引用的条文达到两条补读上限，其余仍须对照原页。")
                    continue
                self.budget["dependency_read_attempts"] += 1
                try:
                    read = self.documents.read_context(*key[:2], target["chunk_ids"][0])
                    if ((read["source_id"], read["snapshot_id"],
                         unit_key(read["selected_clause_coverage"])) != key
                            or read["snapshot_fingerprint"] != context["snapshot_fingerprint"]
                            or not self._complete_core(read)):
                        raise ValueError("引用目标的固定身份或完整正文不一致")
                    core = [part["text"] for part in read["fragments"]
                            if part["role"] != "RELATED_CONTEXT"]
                    if self.policy.relevant_text("\n".join(core), self.plan.conditions, source_title=read.get("title", "")):
                        found.append(read)
                except (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied):
                    gaps.append("明确引用条文的固定原件或读取条件无法核对，未作为回答。")
        return found, list(dict.fromkeys(gaps))

    def _read_probe_candidates(self, contexts, source_ids, *, local):
        """所有语义主题共享一次概念查询和两次固定初读，失败也先记预算。"""
        query = self.policy.concept_query(self.plan.conditions)
        if query is None or self.budget["concept_queries"]:
            return [], [], None
        for context in contexts:
            if (not local or is_yichang_context(context)) and self._complete_core(context):
                return [], [], None
        self.budget["concept_queries"] += 1
        receipt = {"query": query, "method": "BM25_UNIFIED_V1", "top_k": 5,
                   "hits": [], "state": "SEARCHED"}
        errors = (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied)
        try:
            searched = self.documents.search_all(query, 5, source_ids, ranking_version="V1",
                                                 **self.search_options)
            receipt["method"] = searched["method"]
            receipt["hits"] = [{key: hit.get(key) for key in
                               ("id", "source_id", "snapshot_id", "clause_number", "page_start", "page_end")}
                              for hit in searched["results"][:5]]
        except errors:
            receipt["state"] = "FAILED"
            return [], ["概念补检索的固定正文或使用条件无法核对，未扩大资料范围。"], receipt
        gaps, attempted, partial = [], set(), []
        for hit in searched["results"][:5]:
            number = hit.get("clause_number")
            if (not self._source_allowed(hit) or not self.policy.is_body(hit["text"], number)
                    or source_ids is not None and hit["source_id"] not in source_ids):
                continue
            key = hit["source_id"], hit["snapshot_id"], number
            if key in attempted:
                continue
            if self.budget["concept_read_attempts"] >= 2:
                gaps.append("概念补查达到两条固定初读上限，其余须对照原页。")
                break
            attempted.add(key)
            self.budget["concept_read_attempts"] += 1
            try:
                context = self.documents.read_context(hit["source_id"], hit["snapshot_id"], hit["id"])
                if (context["source_id"] != hit["source_id"]
                        or context["snapshot_id"] != hit["snapshot_id"]
                        or context["selected_chunk_id"] != hit["id"]
                        or context["selected_clause_coverage"]["clause_number"] != number):
                    raise ValueError("概念补查固定身份不一致")
                if local and not is_yichang_context(context):
                    gaps.append("概念补查来源档案无法核对，未作为地方证据。")
                    continue
                core = [part for part in context["fragments"] if part["clause_number"] == number
                        and part["role"] != "RELATED_CONTEXT"]
                core_text = "\n".join(part["text"] for part in core)
                # 补查也核共同主题用途；同号文字完整不代表与本题相关。
                # 按已读完整本条核对，避免命中续页时只因单片段未重复主题而误丢。
                if not self._source_allowed(context) or not self.policy.relevant_text(core_text, self.plan.conditions, source_title=context.get("title", "")):
                    continue
                if self._complete_core(context):
                    receipt["state"] = "FOUND"
                    return [context], gaps, receipt
                if self.policy.accept_full_clause(core_text, self.plan.conditions, source_title=context.get("title", "")):
                    partial.append(context)
                    gaps.append("概念补查相关条文续文未完整读齐，只保留阅读出处。")
            except errors:
                gaps.append("概念补查的固定出处或读取条件无法核对，未纳入回答。")
        receipt["state"] = "PARTIAL" if partial else "NO_DEFINITION"
        gaps.append("当前有限补检索未读到完整的对应核心；没有命中不等于没有要求。")
        return partial, list(dict.fromkeys(gaps)), receipt

    def check_current_project_and_scope(self, result):
        """当前本地条件与资料范围先核对；不由语义提案生成工程事实。"""
        request = self.request
        packet = None
        if request.project_id and self.project_conditions is not None:
            packet = self.project_conditions.inspect(request.project_id)
            result["project_conditions"] = packet
        trace, gaps = result["trace"], result["gaps"]
        trace.append({"step": "UNDERSTAND", "state": "DONE", "message": "识别问题与明确条件"})
        conditions = dict(self.plan.conditions)
        if request.project_condition_ids:
            if self.project_conditions is None:
                raise ProjectConditionChanged("当前项目条件无法核对，请重新查询")
            confirmed, provenance = self.project_conditions.resolve(request.project_id, request.project_condition_ids)
            # 用户本轮手填或问题明确写出的条件优先；被覆盖的候选不谎称是采用依据。
            result["condition_provenance"] = [row for row in provenance if row["field"] not in conditions]
            conditions = {**confirmed, **conditions}
        self.plan = replace(self.plan, conditions=conditions,
                            condition_provenance=tuple(result["condition_provenance"]),
                            project_condition_fingerprint=(sha256(json.dumps(packet, sort_keys=True,
                                ensure_ascii=False).encode("utf-8")).hexdigest() if packet else None))
        if packet:
            gaps.extend(packet["gaps"])
        # V1旧调用的缺字段提示只在同一条件阶段消费；不得夹带事实或重复核对项目。
        advisory = [key for key in self.owner.legacy_missing_conditions if key not in conditions]
        if advisory:
            result.update(status="NEEDS_CONDITIONS", summary="请补充本轮问题所需条件后继续。",
                          questions=[{"key": key, "label": QUESTIONS[key]} for key in advisory])
            trace.append({"step": "CONDITIONS", "state": "WAITING",
                          "message": "旧版理解提示的字段尚未由原问或当前条件确认"})
            return self._check_project(result, packet), None
        daylight_value = self.policy.daylight_value
        issues = self.policy.pre_search_issues(conditions)
        if daylight_value and issues:
            result.update(status="NEEDS_CONDITIONS", summary="采光具体取值需要区分房间、采光方式及地区，请补充缺少的条件。",
                questions=[{"key": key, "label": QUESTIONS[key], "reason": reason}
                           for key, reason in issues.items()])
            trace.append({"step": "CONDITIONS", "state": "WAITING", "message": "采光取值条件未齐，不提供统一系数"})
            return self._check_project(result, packet), None
        from archspec.official_discovery.plan import resolve_query_region
        from archspec.official_documents.metadata_filter import SourceMetadataFilter

        # 问句和项目地域只解析一次，初检与补检索消费同一个固定范围。
        project_region = (packet["region"] if packet and self.plan.topic in {
            "SUNLIGHT_DURATION", "SUNLIGHT_SPACING"} else None)
        region = resolve_query_region(request.query, project_region=project_region)
        if region is not None:
            # 复用同次已解析地域，不让背景地名变成正文必须重复的主题词。
            self.policy = policy_for(self.plan, reference_region=region)
            self.search_options["metadata_filter"] = SourceMetadataFilter(region=region)
            if self.policy.local_spacing:
                from archspec.official_discovery.plan import build_discovery_plan
                self.local_source_plan = build_discovery_plan(request.query, region=region)
        local_question = region == "宜昌"
        local_spacing = local_question and self.policy.local_spacing
        source_ids = request.source_ids
        if self.plan.explicit_source_scope is not None:
            # 登记身份只用于限定候选；真正正文和最终输出仍核固定原件、档案及许可。
            self.registered_sources = self.documents.registry.list()
            targets = [row["source_id"] for row in self.registered_sources
                       if row["kind"] == "FULL_TEXT" and self.plan.matches_explicit_source(row)]
            selected = sorted(set(targets) if source_ids is None else set(targets).intersection(source_ids))
            self.plan = replace(self.plan, effective_source_ids=tuple(selected))
            if not selected:
                result.update(status="MISSING_SOURCE" if not targets else "NO_EVIDENCE",
                    summary=("本次登记范围没有原问指定规范的完整正文，未用其他书的相同条号代答。"
                             if not targets else "本次选择与原问指定规范不一致，请核对书目范围后再查。"))
                gaps.append("原问指定的规范与显式选书、地域及读取许可分别核对；没有自动扩大到全部正文。")
                trace.append({"step": "SOURCE_SCOPE", "state": "MISSING" if not targets else "SCOPE_MISMATCH",
                              "message": "保留原问指定规范与本次显式选择的交集"})
                return self._check_project(result, packet), None
            source_ids = selected
        if local_question:
            scope = select_yichang_sources(self.documents, source_ids)
            if scope["state"] == "SCOPE_MISMATCH":
                result.update(status="NO_EVIDENCE", summary="本次选择未包含宜昌地方正文，请切换到对应正文或全部已准备正文。")
                gaps.append("本次选择的资料范围与宜昌问题不一致，未自动扩大范围或用全国结果代答。")
                trace.append({"step": "LOCAL_SOURCE", "state": "SCOPE_MISMATCH", "message": "保持用户显式选择的范围"})
                return self._check_project(result, packet), None
            if scope["state"] != "READY":
                summary = ("宜昌地方正文缓存无法读取，请检查资料状态后重试。" if scope["state"] == "CACHE_ERROR"
                           else "未取得宜昌地方正式正文，暂不能给出宜昌系数或公式。")
                result.update(status="MISSING_SOURCE", summary=summary, source_hints=yichang_hints())
                gaps.append("缺少可读的宜昌相关正式文件、固定版本和完整正文；请核对《宜昌市城乡规划管理技术规定》等可能文件。")
                gaps.append("官方入口仅供继续查找，不能代替地方规定；全国相似结果未充当地方答案。")
                trace.append({"step": "LOCAL_SOURCE", "state": scope["state"], "message": "地方完整正文尚不可用"})
                return self._check_project(result, packet), None
            source_ids = scope["source_ids"]
            trace.append({"step": "LOCAL_SOURCE", "state": "READY", "message": "只查对应宜昌登记完整正文，细则不混作整本"})
        if self.policy.highrise_definition and issues:
            result.update(status="NEEDS_CONDITIONS", summary="高层判定需先区分建筑用途，请补充用途后查对应出处。",
                          questions=[{"key": "building_use", "label": "是住宅、公共建筑（如办公商业），还是厂房、仓库？混合用途请分别查询。"}])
            trace.append({"step": "CONDITIONS", "state": "WAITING", "message": "用途未明确，不返回统一高度数值"})
            return self._check_project(result, packet), None
        if self.policy.fire_area and issues:
            # 勾选仅代表用户选择了候选，歧义未解决的资料不能显示成已经采用。
            result["condition_provenance"] = [row for row in result["condition_provenance"]
                                              if row["field"] not in issues]
            result.update(status="NEEDS_CONDITIONS", summary="防火面积随用途和条件变化，请先补充以下条件。",
                          questions=[{"key": key, "label": QUESTIONS[key], "reason": reason}
                                     for key, reason in issues.items()])
            trace.append({"step": "CONDITIONS", "state": "WAITING", "message": "缺少必要条件，不返回通用数"})
            return self._check_project(result, packet), None
        return None, (packet, conditions, source_ids, local_question, local_spacing, daylight_value)

    def read_initial_candidates(self, result, conditions, source_ids, local_question, local_spacing):
        """按共同计划择取最多五条初读；原排名记录与实际择取分别保留。"""
        request = self.request
        trace, gaps = result["trace"], result["gaps"]
        # 不用条件自动改写查询，保留V1排序的可比基线。条件只限制回答，不推定适用。
        self.budget["initial_queries"] += 1
        self.plan = replace(self.plan, effective_source_ids=(tuple(source_ids) if source_ids is not None else None))
        # 地方问法与查阅入口使用同一地域发现器和固定元数据过滤；
        # 地名只限制来源，不能靠别城正文里的相似词回答本地问题。
        criteria = self.search_options.get("metadata_filter")
        region = criteria.region if criteria is not None else None
        # 与已有PLANNED查阅一样，地域走固定档案，间距主题走既有概念；
        # 用户原问、明确选书和V1排序保留，后续一次补查继承同一地域约束。
        from archspec.official_discovery.public_queries import PUBLIC_CODE
        retrieval_query = self.plan.explicit_clause_number or (self.policy.concept_query(conditions)
                           if region is not None and self.policy.local_spacing
                           and not re.search(r"\d+(?:\.\d+){2,3}", request.query)
                           and not PUBLIC_CODE.search(self.plan.search_query) else None) or self.plan.search_query
        # 多号只在本批初读使用共同计划；概念补查仍保留原预算与查询合同。
        planned = {"question_plan": self.plan} if len(self.plan.bound_clause_numbers()) > 1 else {}
        if self.parallel_lookup is not None:
            # 共用查阅入口的有限子检索和同批授权快照，总结果仍只有原前五。
            retrieval_query = self.parallel_lookup.retrieval_query
            planned["lookup_plan"] = self.parallel_lookup
            self.budget["parallel_queries"] = len(self.parallel_parts)
        from archspec.official_documents.table_lookup import table_lookup_subject

        if self.parallel_lookup is None and (self.policy.local_spacing or self.plan.query_focus is not None
                or self.plan.local_reading_terms or table_lookup_subject(self.plan.search_query) is not None):
            from archspec.official_documents.lookup import prepare_lookup_plan

            lookup = prepare_lookup_plan(request.query, region=region, source_ids=source_ids)
            # 地方间距与通用焦点共用完整条文择取，不让高词频错对象挤掉正文。
            # 只共享原问仍一致的计划；模型改写继续走既有读取链，不放宽引用门。
            if self.plan.original_query == lookup.normalized_query:
                self.candidate_lookup = replace(lookup, question_plan=self.plan, policy=self.policy)
                planned["lookup_plan"] = self.candidate_lookup
        searched = self.documents.search_all(retrieval_query, 5, source_ids,
                                             ranking_version="V1", **self.search_options, **planned)
        result["coverage"] = searched["coverage"]
        # 已有正文被来源范围拒绝，与没有正文分开；沿共享筛选器的静态原因展示。
        for source in searched["coverage"]:
            gaps.extend(reason["message"] for reason in source.get("metadata_exclusions", ()))
        result["retrieval"] = {"method": searched["method"], "top_k": 5,
            "hits": [{key: hit.get(key) for key in
                     ("id", "source_id", "snapshot_id", "clause_number", "page_start", "page_end")}
                     for hit in searched["results"]]}
        if searched.get("source_reading_priority") is not None:
            result["retrieval"]["source_reading_priority"] = searched["source_reading_priority"]
        if self.plan.search_query != request.query:
            result["retrieval"].update(query=request.query, retrieval_query=retrieval_query)
        selection = searched.get("candidate_selection")
        if selection is not None:
            result["retrieval"]["selected_hits"] = result["retrieval"]["hits"]
            result["retrieval"]["hits"] = selection["raw_hits"]
            result["retrieval"]["candidate_selection"] = selection
        if "clause_selection" in searched:
            result["retrieval"]["clause_selection"] = searched["clause_selection"]
        if region is not None:
            result["retrieval"].update(region=region, query=request.query,
                                        retrieval_query=retrieval_query)
        if local_question:
            result["retrieval"].update(scope="YICHANG_LOCAL", query=request.query)
        trace.append({"step": "RETRIEVE", "state": "DONE", "message": ("同次检索按共同正文主题择取最多五条；保留原关键词前五记录" if selection is not None
                                                                    else f"前五命中{len(searched['results'])}条；使用原关键词排序")})
        contexts, evidence, seen, successful_reads = [], [], set(), set()
        for hit in searched["results"]:
            if not self._source_allowed(hit):
                gaps.append("检索候选与本轮指定规范范围不同，未作为回答。")
                continue
            # 中文分项是固定正文结构，不取决于某一城市专用补读流程。
            # 这里只允许进入原read_context，后续仍须核named身份、完整性与引用。
            named_candidate = (self.policy.local_spacing and hit.get("clause_number") is None
                               and re.match(r"^[（(][一二三四五六七八九十百]+[）)]", hit["text"].strip()))
            relevant = (parallel.matches(self.parallel_parts, hit["text"], hit.get("clause_number"), hit, conditions)
                        if self.parallel_parts else self.policy.relevant_text(hit["text"], conditions, source_title=hit.get("title", "")))
            if selection is not None:
                relevant = relevant or hit["id"] in selection["preferred_ids"]
            body = (is_body(hit["text"], hit.get("clause_number")) if self.parallel_parts
                    else self.policy.is_body(hit["text"], hit.get("clause_number")))
            if not (body or named_candidate) or not relevant:
                continue  # 目录仍留在原检索列表，只是不作规范事实回答。
            if local_spacing and not self.parallel_parts and not spacing_evidence(hit["text"]):
                continue  # 日照时长与间距公式分别讨论，不改原检索排名。
            key = (hit["source_id"], hit["snapshot_id"], hit["clause_number"] or hit["id"])
            if key in successful_reads:
                # 同条真实重复命中仍记MATCH，但只初读一次。预算外未读到的片段不造证据。
                existing = next((row for row in evidence if row["chunk_id"] == hit["id"]), None)
                if existing is not None:
                    existing["role"] = "MATCH"
                else:
                    gaps.append("同条另一个命中片段未在有限上下文中读到，请继续对照固定原页。")
                continue
            try:
                context = self.documents.read_context(hit["source_id"], hit["snapshot_id"], hit["id"])
            except (OSError, ValueError, LookupError, TypeError, KeyError, ContentUseDenied):
                gaps.append("某个命中的固定正文或读取条件无法核对，未纳入回答。")
                continue
            if not self._source_allowed(context):
                gaps.append("命中正文的固定档案与原问指定规范不同，未作为回答。")
                continue
            if local_question and not is_yichang_context(context):
                gaps.append("命中正文的固定地方来源档案不完整或不一致，未作为宜昌证据。")
                continue
            if named_candidate:
                coverage = context["selected_clause_coverage"]
                if not str(unit_key(coverage) or "").startswith("named:"):
                    continue
                key = (hit["source_id"], hit["snapshot_id"], unit_key(coverage))
                if key in successful_reads:
                    existing = next((row for row in evidence if row["chunk_id"] == hit["id"]), None)
                    if existing is not None:
                        existing["role"] = "MATCH"
                    continue
            if self.policy.net_height and not self.parallel_parts and not any(
                    net_height_question(fragment["text"])
                    for fragment in context["fragments"]
                    if fragment["clause_number"] == hit["clause_number"]):
                # 只用同一固定条款的主文/续文判定；邻接条款出现净高不能替命中面积条款背书。
                # 原始前五未改，相关原文仍可从阅读入口查看，不能因筛选而删除原件。
                continue
            # 车库专书的面积不能充当办公/住宅等用途的答案；只排除明确冲突，
            # 其余候选仍需人工核对适用条件，不把这一窄规则称作合规判断。
            use = conditions.get("building_use", "")
            if (self.policy.fire_area and not self.parallel_parts and "车库" in context["title"]
                    and "车库" not in use and any(word in use for word in
                    ("办公", "住宅", "商业", "厂房", "仓库"))):
                gaps.append("车库专用正文与本次明确用途不同，未作为面积回答；原检索候选仍保留。")
                continue
            contexts.append(context)
            successful_reads.add(key)
            # 高度先按完整核心确定本题阅读范围，再登记对应原表/缺口。
            if not self.policy.height_measurement or self.parallel_parts:
                gaps.extend(context["gaps"])
                if context["table_pages"]:
                    gaps.append("相关文字引用了表格；须核对原表行列，未将识别单元格推算成数字结论。")
            for fragment in context["fragments"]:
                if fragment["id"] in seen:
                    # 同一片段原先是邻接上下文、后来被检索命中时，升级角色而不是复制文字。
                    if fragment["role"] != "RELATED_CONTEXT":
                        # 已读邻接后来被独立核为核心时，同时接回实际单元归属和续文。
                        # 同一固定片段不复制；已是MATCH不因另一次续读降级。
                        existing = next(row for row in evidence if row["chunk_id"] == fragment["id"])
                        if existing["role"] != "MATCH":
                            existing["role"] = fragment["role"]
                        existing.update({key: fragment[key] for key in
                                         ("reading_unit_key", "reading_unit_label") if key in fragment})
                    continue
                seen.add(fragment["id"])
                evidence.append({
                    "evidence_id": f"oq_{len(evidence) + 1}", "chunk_id": fragment["id"],
                    **{key: context[key] for key in ("source_id", "snapshot_id", "title", "standard_code", "source_url", "snapshot_fingerprint")},
                    # 来源信息与被引用的固定正文一起走，不在回答阶段查最新版本替换。
                    "source_metadata": context.get("source_metadata"),
                    "source_validity": context.get("source_validity"),
                    **{key: fragment[key] for key in ("text", "clause_number", "page_start", "page_end", "role", "warnings")},
                    **{key: fragment[key] for key in ("reading_unit_key", "reading_unit_label") if key in fragment},
                    "evidence_mode": "CANDIDATE_ONLY", "is_citable": False,
                })
        return contexts, evidence, seen

    def read_requirement_supplements(self, result, conditions, source_ids, contexts, evidence,
                                    seen, local_question, local_spacing):
        """已有领域固定读器与唯一概念探查共享本轮预算，不递归读邻接。"""
        trace, gaps = result["trace"], result["gaps"]
        # 灭火条件的邻接片段不能直接当答案；按原身份补读全部主文和续文。
        supplements, supplement_gaps = (read_fire_supplements(
            _BudgetedReader(self.documents, self.budget, "fire_read_attempts", 2),
            self.policy.legacy_query, conditions, evidence)
            if self.policy.fire_area else ([], []))
        gaps.extend(supplement_gaps)
        garage_supplements, garage_gaps = (read_garage_supplements(_BudgetedReader(self.documents, self.budget, "garage_read_attempts", 2), conditions, evidence)
            if self.policy.fire_area else ([], []))
        gaps.extend(garage_gaps)
        # 两类线索可指向同一条，只读取既有同身份结果，不递归扩大邻接。
        supplemental_keys = {(row["source_id"], row["snapshot_id"],
                              row["selected_clause_coverage"]["clause_number"]) for row in supplements}
        supplements.extend(row for row in garage_supplements if (row["source_id"], row["snapshot_id"],
            row["selected_clause_coverage"]["clause_number"]) not in supplemental_keys)
        height_supplements, height_gaps = (read_height_supplements(
            _BudgetedReader(self.documents, self.budget, "height_read_attempts", 2),
            self.policy.legacy_query, evidence, focus=self.plan.focus)
            if self.policy.height_measurement else ([], []))
        gaps.extend(height_gaps)
        supplements.extend(height_supplements)
        # 同一executor管理全部语义补查；原问法和原前五独立保留。
        probe_contexts, probe_gaps, probe_receipt = self._read_probe_candidates(
            contexts + supplements, source_ids, local=local_question)
        gaps.extend(probe_gaps)
        if probe_receipt is not None:
            result["supplemental_retrieval"] = probe_receipt
            trace.append({"step": "SUPPLEMENT_RETRIEVE", "state": probe_receipt["state"],
                          "message": ("高层分类有限同义补查；候选仍须完整固定读取与引用核对"
                                      if self.policy.highrise_definition else
                                      "有限概念补查；候选仍须完整固定读取与引用核对")})
        supplements.extend(probe_contexts)
        spacing_contexts, spacing_gaps = (read_spacing_supplements(self.documents, self.policy.legacy_query, contexts)
                                         if local_spacing else ([], []))
        gaps.extend(spacing_gaps)
        supplements.extend(spacing_contexts)
        dependency_contexts, dependency_gaps = self._read_dependencies(contexts + supplements)
        supplements.extend(dependency_contexts)
        gaps.extend(dependency_gaps)
        dependency_ids = {id(context) for context in dependency_contexts}
        for context in supplements:
            if not self._source_allowed(context):
                gaps.append("补读正文与本轮指定规范范围不同，未纳入证据。")
                continue
            if local_question and not is_yichang_context(context):
                gaps.append("补读正文的固定地方来源档案无法核对，未作为宜昌证据。")
                continue
            contexts.append(context)
            # 独立核读引用目标后，只纳入其完整核心和明确原表，不纳入新的普通邻接。
            table_ids = ({chunk for ref in context.get("table_references", ()) for page in ref["pages"]
                          for chunk in [page["chunk_id"], *(part["chunk_id"] for part in page.get("reading_parts", ()))]}
                         if id(context) in dependency_ids else set())
            for fragment in context["fragments"]:
                core = (unit_key(fragment) == unit_key(context["selected_clause_coverage"])
                        and fragment["role"] != "RELATED_CONTEXT")
                if not core and fragment["id"] not in table_ids:
                    continue  # 补读不递归纳入新的邻接线索。
                if fragment["id"] in seen:
                    existing = next(row for row in evidence if row["chunk_id"] == fragment["id"])
                    if core:
                        existing["role"] = fragment["role"]
                    existing.update({key: fragment[key] for key in ("reading_unit_key", "reading_unit_label") if key in fragment})
                    continue
                seen.add(fragment["id"])
                evidence.append({
                    "evidence_id": f"oq_{len(evidence) + 1}", "chunk_id": fragment["id"],
                    **{key: context[key] for key in ("source_id", "snapshot_id", "title", "standard_code", "source_url", "snapshot_fingerprint")},
                    "source_metadata": context.get("source_metadata"),
                    "source_validity": context.get("source_validity"),
                    **{key: fragment[key] for key in ("text", "clause_number", "page_start", "page_end", "role", "warnings")},
                    **{key: fragment[key] for key in ("reading_unit_key", "reading_unit_label") if key in fragment},
                    "evidence_mode": "CANDIDATE_ONLY", "is_citable": False,
                })
        if supplements:
            trace.append({"step": "SUPPLEMENT", "state": "DONE",
                          "message": f"补读{len(supplements)}条固定相关条文；引用仍需完整性核对"})
        return spacing_contexts

    def _accepted_unit_keys(self, rows, conditions):
        """预算前后共用完整单元主题政策；不为预算引入第二份识别词表。"""
        grouped = {}
        for row in rows:
            grouped.setdefault(evidence_key(row), []).append(row)
        accepted, rejected = set(), set()
        for key, parts in grouped.items():
            parts.sort(key=lambda row: int(row["chunk_id"].rsplit(":", 1)[1]))
            core = [row for row in parts if row["role"] != "RELATED_CONTEXT"]
            text = "\n".join(row["text"] for row in (core or parts))
            relevant = (parallel.matches(self.parallel_parts, text, parts[0]["clause_number"], parts[0], conditions)
                        if self.parallel_parts else self.policy.accept_full_clause(text, conditions, source_title=parts[0].get("title", "")))
            # 中文政府条文新接入摘录时仍核所问操作；一般规则留作上下文，
            # 不能把“按现状控制”等文字当作系数。宜昌命名单元的完整补读保持。
            from archspec.official_documents.clause_units import ARTICLE
            if self.policy.local_spacing and ARTICLE.fullmatch(parts[0].get("clause_number") or ""):
                relevant = relevant and self.policy.matches_reading_question(text, conditions)
            (accepted if relevant else rejected).add(key)
        return accepted, rejected

    def _contexts_for_reading(self, contexts, evidence, conditions):
        """高度的阅读需求只取主题核心及同书明确依赖；保留全部初读供独立复核。"""
        if not self.policy.height_measurement or self.parallel_parts:
            return contexts
        accepted, _ = self._accepted_unit_keys(
            [row for row in evidence if is_body(row["text"], row["clause_number"])], conditions)
        def key(context):
            return context["source_id"], context["snapshot_id"], unit_key(context["selected_clause_coverage"])
        # 尚未读齐不能按残片判为无关，继续保留它的缺口和固定阅读入口。
        selected = [context for context in contexts if key(context) in accepted
                    or context["selected_clause_coverage"].get("complete") is not True]
        dependencies = set()
        for context in selected:
            dependency = complete_outgoing_dependency(context)
            if dependency is not None:
                dependencies.update((target["source_id"], target["snapshot_id"], target["unit_key"])
                                    for target in dependency[1])
        # 仅已有的一跳固定依赖，不递归读取或从另一版本补上下文。
        return [context for context in contexts if context in selected or key(context) in dependencies]

    def prepare_evidence(self, result, contexts, evidence, spacing_contexts,
                         conditions, local_spacing, daylight_value):
        """统一预算与完整核心分类；只读邻接不成为核心证明。"""
        trace, gaps = result["trace"], result["gaps"]
        self.reading_contexts = self._contexts_for_reading(contexts, evidence, conditions)
        if self.policy.height_measurement and not self.parallel_parts:
            for context in self.reading_contexts:
                gaps.extend(context["gaps"])
                # table_pages还可能收集邻页表格；提示沿当前核心实际引用的原表合同。
                if context.get("table_references"):
                    gaps.append("相关文字引用了表格；须核对原表行列，未将识别单元格推算成数字结论。")
        # 同条续文优先，其次是被条文明确引用且已定位的原表；普通邻接最后补。
        # 否则第一本的邻接片段会占满总预算，挤掉第二本已经找到的原表。
        table_chunks = {identity for context in contexts
                        for ref in context.get("table_references", []) for page in ref["pages"]
                        for identity in [page["chunk_id"],
                                         *(part["chunk_id"] for part in page.get("reading_parts", []))]}
        linked_chunks = {identity for context in contexts
                         for ref in context.get("clause_references", []) for identity in ref["chunk_ids"]}
        evidence.sort(key=lambda row: -1 if self.policy.local_spacing and row.get("reading_unit_key") and row["role"] != "RELATED_CONTEXT" else
                      0 if row["role"] == "MATCH" else
                      1 if row["role"] == "CONTINUATION" else
                      2 if row["chunk_id"] in table_chunks else
                      3 if row["chunk_id"] in linked_chunks else 4)
        # 只改变已读证据的预算分配；V1前五不变，计量包中的邻接不能升级为摘录。
        priority_core_keys = set()
        if (self.parallel_parts or self.policy.height_measurement and not self.policy.fire_area and not local_spacing
                and not fire_area_question(self.plan.original_query)
                and not sunlight_spacing_question(self.plan.original_query)):
            preview, _ = self._accepted_unit_keys(
                [row for row in evidence if is_body(row["text"], row["clause_number"])], conditions)
            priority_core_keys = (preview & complete_clause_keys(contexts, evidence)
                                  & {evidence_key(row) for row in evidence if row["role"] == "MATCH"})
        evidence, selection_gaps = qa_service.select_context_evidence(
            self.policy.legacy_query, self.reading_contexts, evidence, priority_core_keys=priority_core_keys)
        gaps.extend(selection_gaps)
        complete_keys = complete_clause_keys(contexts, evidence)
        for row in evidence:
            key = evidence_key(row)
            row["quote_ready"] = (row["role"] != "RELATED_CONTEXT" and key in complete_keys)
        # 原表位置齐全不代表OCR文字齐全；有可见字迹却无识别行的格不能进入摘录。
        self.table_ink_input = (deepcopy(self.reading_contexts), deepcopy(evidence))
        self.table_ink_findings = table_ink_issues(self.documents, *self.table_ink_input)
        if self.table_ink_findings:
            blocked_ids = {item["anchor_evidence_id"] for item in self.table_ink_findings}
            blocked_keys = {evidence_key(row) for row in evidence if row["evidence_id"] in blocked_ids}
            for row in evidence:
                if evidence_key(row) in blocked_keys:
                    row["quote_ready"] = False
            result["table_integrity_issues"] = deepcopy(self.table_ink_findings)
            gaps.append("原表部分单元格有可见字迹但未被识别或定位，本次不将该表作为完整摘录，请对照原页。")
        clause_issues = [{"source_id": context["source_id"], "snapshot_id": context["snapshot_id"],
                          **issue} for context in contexts
                         for issue in context.get("selected_clause_coverage", {}).get("boundary_issues", [])]
        if clause_issues:
            result["clause_integrity_issues"] = clause_issues
        if any(row["role"] == "MATCH" and evidence_key(row) not in complete_keys for row in evidence):
            if any(context.get("selected_clause_coverage", {}).get("boundary_issues") for context in contexts):
                gaps.append("部分片段的条文完整性或原表归属尚未核对，本次仅供阅读；请对照原页。")
            else:
                gaps.append("同一条文的续文未完整纳入，本次仅保留阅读出处，不将开头当完整摘录。")
        trace.append({"step": "CONTEXT", "state": "DONE", "message": f"读取{len(evidence)}个固定片段，保留续文和邻接条件"})
        # 不给整理器章节/目录当事实，也不让未知片段中的表格数字单独变成答案。
        spacing_keys = {(context["source_id"], context["snapshot_id"], unit_key(context["selected_clause_coverage"]))
                        for context in spacing_contexts}
        # 命名单元的依据来自实际固定读取，不以城市补读开关或前端标签授权。
        named_keys = {(context["source_id"], context["snapshot_id"], unit_key(context["selected_clause_coverage"]))
                      for context in contexts
                      if self.policy.local_spacing and str(unit_key(context["selected_clause_coverage"]) or "").startswith("named:")}
        usable = [row for row in evidence if (is_body(row["text"], row["clause_number"])
                  or evidence_key(row) in named_keys
                  or (local_spacing and evidence_key(row) in spacing_keys))]
        # 同一固定阅读单元只拼接一次；所有领域策略共享核心与只读上下文。
        accepted, rejected = self._accepted_unit_keys(usable, conditions)
        # 完整性不等于回答资格；语义角色已拒绝的单元仍可读，但不能标成可摘录。
        # 同步公开账本与整理器的实际准入，原文字和固定身份均不变。
        for row in evidence:
            if evidence_key(row) in rejected:
                row["quote_ready"] = False
        if rejected and self.policy.fire_area:
            gaps.append("明确属于其他用途或防烟面积的条文未作为本次防火面积回答；原检索和阅读出处仍保留。")
        if rejected and daylight_value:
            gaps.append("普通办公室查询未采用医疗、教育等专用空间表；原检索候选仍保留。")
        usable = [row for row in usable if evidence_key(row) in accepted
                  and (row["quote_ready"] or row["role"] == "RELATED_CONTEXT")]
        if self.policy.height_measurement and not any(row["quote_ready"] for row in usable):
            topic = "屋顶设备用房" if self.plan.focus == "ROOF_EQUIPMENT" else "高度计量"
            gaps.append(f"当前有限证据未读到完整的{topic}相关条文；不能用面宽或其他屋顶要求代答。")
        has_complete_match = any(row["role"] == "MATCH" for row in usable)
        return evidence, usable, complete_keys, spacing_keys, has_complete_match, accepted

    def compose_bundle(self, result, ready, local_spacing):
        """模型/本地/明确假整理器只选同一包的完整固定证据。"""
        request, bundle = self.request, self.bundle
        conditions, usable, readings = (thaw_value(bundle.plan.conditions), thaw_value(bundle.usable),
                                        thaw_value(bundle.table_readings))
        trace = result["trace"]
        ready_to_compose = ready
        # 整理器只能看证据副本，不能通过修改输入污染服务端核对账本。
        if ready_to_compose:
            compose_bundle = getattr(self.composer, "compose_bundle", None)
            compose_tables = getattr(self.composer, "compose_with_table_readings", None)
            draft = (compose_bundle(self.bundle) if compose_bundle else
                     compose_tables(request.query, deepcopy(conditions), deepcopy(usable),
                                    deepcopy(readings)) if compose_tables and readings else
                     self.composer.compose(request.query, deepcopy(conditions), deepcopy(usable)))
            trace.append({"step": "COMPOSE", "state": "DONE", "message": "仅选择完整条文的原文引用，不自由生成规范事实"})
        else:
            draft = []
            message = ("本次只读原文，不选择答案或调用整理器" if self.owner.reading_only else
                       "地方间距仍需区分实际条件，只组织固定阅读出处，不生成通用系数" if local_spacing
                       else "当前证据尚不足以选择短摘录；保留候选原文，未调用整理器")
            trace.append({"step": "COMPOSE", "state": "SKIPPED", "message": message})
        return draft

    def recheck_fixed_bundle(self, contexts, conditions, table_groups, usable,
                             readings, page_readings, daylight_value):
        """输出前开启全新阶段核对原件/许可/固定正文，不能继承初读确认。"""
        # 整理可能耗时，输出前再次核对全部固定身份、原件和读取权限。
        with self.documents.read_phase(sql=True):
            # 重新读取的阶段绝不继承初读校验；无模型的阅读回答也必须经过这里。
            for old in contexts:
                current = self.documents.read_context(old["source_id"], old["snapshot_id"], old["selected_chunk_id"])
                if not self._source_allowed(current):
                    raise ValueError("固定证据已不属于本轮指定规范范围")
                # 核对日期只供展示，跨UTC日不代表固定正文发生变化。
                # 新阶段仍重新读取原件并按真实时刻核权限/效力；只排除这个
                # 观察字段，其他正文、身份和治理字段必须完全一致。
                def comparable(context):
                    validity = context["source_validity"]
                    return {**context, "source_validity": {
                        key: value for key, value in validity.items() if key != "evaluated_at"}}
                if comparable(current) != comparable(old):
                    raise ValueError("固定证据在整理期间变化")
            if readings != (build_table_readings(self.documents, conditions, table_groups, usable)
                            if daylight_value or self.policy.fire_area else []):
                raise ValueError("原表行列在整理期间变化")
            if (self.table_ink_input is not None
                    and self.table_ink_findings != table_ink_issues(self.documents, *self.table_ink_input)):
                raise ValueError("原表单元格覆盖在整理期间变化")
            if page_readings != build_page_readings(self.documents, self.policy.legacy_query, conditions, usable):
                raise ValueError("原页核读记录在整理期间变化")

    def render_response(self, result, packet, conditions, contexts, evidence, usable, items,
                        local_spacing, daylight_value, original_query):
        """已核对摘录、原表/原页和缺口复用既有呈现；不推定工程适用结论。"""
        trace, gaps = result["trace"], result["gaps"]
        # 完整初读仍在bundle中接受fresh复核；这里只登记当前问题的阅读关系。
        contexts = self.reading_contexts if self.reading_contexts is not None else contexts
        if not items:
            result.update(status="READING_ONLY", evidence=evidence,
                          summary="已找到阅读出处，但续文未齐或条文过长，本次不生成短摘录。")
            if self.owner.reading_only:
                result["summary"] = "本次仅提供已核对身份的候选原文和原页，未自动选择答案。"
                gaps.append("只读模式未选择摘录；候选原文不等于已确认回答。")
            elif self.table_ink_findings:
                result["summary"] = "已找到条文和原表，但部分单元格尚未识别完整；请对照原页，本次未生成该表摘录。"
            elif local_spacing:
                result["summary"] = "已找到宜昌地方间距阅读出处；不同条件不能合成一个通用系数，请按对应条款和续页核对。"
                gaps.append("本次未确认建筑用途、双方高度与朝向、新旧城区等条件，也未自动选定公式或计算数值。")
            elif any(context.get("selected_clause_coverage", {}).get("boundary_issues")
                     for context in self.reading_contexts or ()):
                result["summary"] = "已找到候选原文，但条文完整性或原表归属尚未核对；请对照原页，本次未生成摘录。"
            elif self.plan.topic == "UNKNOWN" and self.plan.understanding == "CLEAR" and self.plan.query_focus is None:
                result["summary"] = "已找到候选原文，但本题对象与属性尚未绑定；请先阅读出处，未自动生成答案。"
                gaps.append("候选词组相关不等于对象已核对；本次仅供阅读和找资料，不选择自动摘录。")
            elif self.plan.query_focus is not None and not any(row.get("quote_ready") for row in usable):
                result["summary"] = "已找到候选原文，但尚未核对到问题焦点；请对照正文和原页，未自动生成摘录。"
                gaps.append("原问召回与焦点核对分开；这些候选只供阅读，不作为已确认答案。")
            elif self.plan.understanding == "CLEAR":
                gaps.append("条款过长或上下文尚不完整，本次未生成简短摘录。")
            if self.plan.understanding != "CLEAR":
                # 未开始选择时，不把能力缺口误报为缺页/长条文或用户缺条件。
                if self.plan.understanding_reason == "LOCAL_TOPIC_NOT_REGISTERED":
                    result["summary"] = "本地自动理解暂未覆盖这类问题；已保留候选原文和原页供查阅。"
                    gaps.append("本地主题识别未覆盖，本次未自动选择规范摘录；候选不等于已确认答案。")
                else:
                    result["summary"] = "问题理解尚待确认，先保留候选出处供阅读；请明确所问对象和要求。"
                    gaps.append("理解仍不确定，本次未选择规范摘录或调用模型选择。")
        else:
            result.update(status="REFERENCE_BRIEF", summary="以下为相关原文摘录，按对应出处及完整条件对照。",
                          answer_items=items, evidence=evidence)
            # 使用用户原问法，而不是模型改写中的推测条件。既有完整引用与
            # 原件复核已经通过，新增短答只定位原文段落，不新增规范事实。
            result["answer_brief"] = build_answer_brief(self.policy.legacy_query, conditions, items)
            if local_spacing:
                result.update(answer_mode="LOCAL_SPACING_RULES",
                    summary="已找到宜昌间距计算规则和对应原页；按实际条件核对分支，不合成通用系数。")
                gaps.append("本次只定位并引用计算规则，未选定项目公式或计算实际间距；版本与适用范围仍须核对。")
            if self.policy.height_measurement:
                result.update(answer_mode="HEIGHT_MEASUREMENT_READING",
                    summary="已找到对应高度计量原文；请按屋面形式、特殊范围及例外对照原页，不作统一量法。")
            if daylight_value:
                # 条件齐全仍不是表格行列已核验；保留完整原文和身份，不猜哪个数字适用。
                result.update(answer_mode="TABLE_READING_REQUIRED",
                    summary="已找到采光相关出处；请对照原表等级、采光方式和地区条件，本次尚未确定单一系数。")
            # 逐字引用与输出前原件/许可复核均成功后，才组织已有出处；不增搜或改角色。
            result["context_groups"] = build_context_groups(self.policy.legacy_query, contexts, evidence)
            gaps.append("来源版本、项目适用性和识别文字尚未完成专业核验。")
            if any("LOW_CONFIDENCE_OCR" in row["warnings"] for row in usable):
                gaps.append("命中页有低置信度识别文字，数字、单位和否定词需核对原页。")
        # 已完成输出前的原件/权限复核；原表组仅提供当前账本里的固定阅读出处。
        result["context_groups"].extend(build_table_groups(contexts, evidence))
        result["context_groups"].extend(build_clause_groups(contexts, evidence))
        if result["status"] in {"REFERENCE_BRIEF", "READING_ONLY"}:
            # 补问消费同一最终完整包，长条文不生成短摘录也不能丢已读例外。
            usable_ids = {row["evidence_id"] for row in self.bundle.usable}
            units = [unit for unit in self.bundle.units if unit["complete"]
                     and set(unit["evidence_ids"]) <= usable_ids]
            followups = self.policy.followups(conditions, items=items, units=units)
            if self.policy.height_measurement:
                result["condition_followups"] = followups
                result["height_condition_state"] = (
                    "NEEDS_DETAILS" if result["condition_followups"] else "DETAILS_RECORDED")
            if self.policy.local_spacing:
                # 补问按本轮日照间距主题分派；local_spacing仅是宜昌命名附录读取开关。
                # 读取预算不足仍保留原页和缺项；不让补问只依赖短摘录成功。
                result["condition_followups"] = followups
                result["local_spacing_condition_state"] = ("NEEDS_DETAILS"
                    if result["condition_followups"] else "DETAILS_RECORDED")
            # 基础读数可先读；形式和覆盖未齐时不能把摘录成功解释成最终面积已确定。
            if ("车库" in conditions.get("building_use", "")
                    and conditions.get("building_use") != "修车库" and self.policy.fire_area):
                result["condition_followups"] = followups
                result["garage_condition_state"] = "NEEDS_DETAILS" if followups else "DETAILS_RECORDED"
                result["garage_rule_checks"] = build_garage_rule_checks(conditions, usable)
                gaps.extend(row["message"] for row in result["garage_rule_checks"] if row["state"] != "LOCATED")
                if followups:
                    gaps.append("已找到面积阅读出处；仍需补充车库条件，基础读数不是最终适用面积。")
                else:
                    gaps.append("已记录车库补充条件；特殊形式、危险物品及覆盖修正仍需核对完整对应条文，未计算最终面积。")
            elif public_area_question(original_query, conditions):
                # 条文已经读到后再问范围与覆盖，不让用户重复填写用途和等级。
                result["condition_followups"] = followups
                note = public_scope_note(conditions, items)
                result["public_fire_condition_state"] = (("SCOPE_EXCLUDED" if public_scope_located(items)
                    else "SCOPE_UNLOCATED") if note else
                    "NEEDS_DETAILS" if result["condition_followups"] else "DETAILS_RECORDED")
                if note:
                    result["public_fire_scope_note"] = note
                    gaps.append(note)
                elif result["condition_followups"]:
                    gaps.append("已找到公共建筑面积出处；仍需核对排除范围或灭火覆盖，基准读数不是最终项目面积。")
        trace.append({"step": "CHECK", "state": "DONE", "message": "每段摘录逐字对应固定证据，原件与读取条件复核"})
        result["gaps"] = list(dict.fromkeys(gaps))
        return self._check_project(result, packet)

    def execute(self, plan):
        request = self.request
        self.plan = plan
        if plan.public_search_titles:
            # 同一语义及恢复计划的公开题名参与有限读取顺序，不扩大选书/地域/许可。
            self.search_options["reading_priority_titles"] = plan.public_search_titles
        self.policy = policy_for(plan)
        if request.project_id and self.validate_project is not None:
            self.validate_project(request.project_id)
        result = {
            "request_id": str(uuid4()), "query": request.query, "project_id": request.project_id,
            "status": "NO_EVIDENCE", "summary": "", "questions": [], "answer_items": [],
            "evidence": [], "context_groups": [], "coverage": [], "gaps": [], "trace": [],
            "composer": self.composer.name, "retrieval": None,
            "evidence_mode": "CANDIDATE_ONLY", "is_citable": False,
            "condition_provenance": [],
        }
        early, scope = self.check_current_project_and_scope(result)
        if early is not None:
            return early
        packet, conditions, source_ids, local_question, local_spacing, daylight_value = scope
        trace, gaps = result["trace"], result["gaps"]
        self.parallel_lookup = parallel.lookup_for_answer(self.plan, source_ids, conditions)
        self.parallel_parts = self.parallel_lookup.parallel_parts if self.parallel_lookup is not None else ()
        original_query = self.plan.original_query
        # 初读只包原有有界检索/固定上下文/预算补读/证据包；项目Session与整理器在块外。
        # 整理可能调用真实模型，不能把SQL锁扩展到compose或复用初读代替最终fresh守卫。
        initial_check_failed = False
        with self.documents.read_phase(sql=True):
            contexts, evidence, seen = self.read_initial_candidates(
                result, conditions, source_ids, local_question, local_spacing)
            spacing_contexts = self.read_requirement_supplements(
                result, conditions, source_ids, contexts, evidence, seen, local_question, local_spacing)
            evidence, usable, complete_keys, spacing_keys, has_complete_match, accepted = self.prepare_evidence(
                result, contexts, evidence, spacing_contexts, conditions, local_spacing, daylight_value)
            context_keys = {(context["source_id"], context["snapshot_id"],
                             unit_key(context["selected_clause_coverage"])) for context in contexts}
            core_keys = accepted & context_keys
            if not evidence:
                self.bundle = compile_evidence_bundle(
                    self.plan, requirements_from_contexts(self.plan, self.reading_contexts, core_keys=core_keys,
                        source_coverage=result["coverage"], sources=self.registered_sources,
                        clause_selection=result["retrieval"].get("clause_selection"))
                    + parallel.requirements(self.parallel_parts, contexts, conditions),
                    contexts, {"original": result["retrieval"],
                               "concept": result.get("supplemental_retrieval")}, self.budget,
                    evidence=[], usable=[])
                result["summary"] = ("已有缓存正文未通过本次来源档案筛选，请核对档案或范围。"
                    if result["coverage"] and all(row["state"] == "EXCLUDED_METADATA"
                                                  for row in result["coverage"]) else
                    "当前正文没有找到可用于回答的条款，请换问法或补充资料。")
                gaps.append("前五没有可核对的条款正文；这不是设计要求不存在的结论。")
                result["gaps"] = list(dict.fromkeys(gaps))
            else:
                try:
                    table_groups = build_table_groups(self.reading_contexts, evidence)
                    readings = (build_table_readings(self.documents, conditions, table_groups, usable)
                                if daylight_value or self.policy.fire_area else [])
                    page_readings = build_page_readings(self.documents, self.policy.legacy_query, conditions, usable)
                    spacing_ready = bool(spacing_keys) and spacing_keys.issubset(complete_keys)
                    # 语义不确定只提供固定候选阅读，不能把主题未确认当已答对。
                    ready_to_compose = (not self.owner.reading_only
                                        and self.plan.understanding == "CLEAR" and has_complete_match
                                        and (not local_spacing or spacing_ready))
                    required = self.policy.required_evidence_ids(conditions, usable)
                    requirements = requirements_from_contexts(
                        self.plan, self.reading_contexts, core_keys=core_keys,
                        required_evidence_ids=required if self.policy.fire_area else (),
                        source_coverage=result["coverage"], sources=self.registered_sources,
                        clause_selection=result["retrieval"].get("clause_selection"))
                    requirements += parallel.requirements(self.parallel_parts, contexts, conditions)
                    self.bundle = compile_evidence_bundle(
                        self.plan, requirements, contexts,
                        {"original": result["retrieval"],
                         "concept": result.get("supplemental_retrieval")}, self.budget,
                        evidence=evidence, usable=usable, table_readings=readings,
                        page_readings=page_readings, required_evidence_ids=required,
                        require_empty_selection=(ready_to_compose and self.policy.empty_selection_requires_required))
                    # 已核齐且能短摘录的每个子要求都必须进入选择，模型不能静默漏掉次主题。
                    self.bundle = replace(self.bundle, required_evidence_ids=parallel.required_short_quotes(self.bundle))
                except (ValueError, LookupError, TypeError, KeyError, OSError, ContentUseDenied):
                    # 先归还初读连接，再走原错误响应/当前项目复核；不回显底层异常。
                    initial_check_failed = True
        if not evidence:
            return self._check_project(result, packet)
        try:
            if initial_check_failed:
                raise ValueError("初读固定证据包无法核对")
            draft = self.compose_bundle(result, ready_to_compose, local_spacing)
            selected_readings = getattr(self.composer, "selected_table_reading_ids", None)
            items = guard_selection(self.bundle, {
                "choices": draft, "table_reading_ids": selected_readings or []})
            self.recheck_fixed_bundle(contexts, conditions, table_groups, usable,
                                      readings, page_readings, daylight_value)
            chosen = {item["citation"]["evidence_id"] for item in items}
            # 仅附到已通过逐字核对的完整条文；独立核读不进入外传模型输入。
            result["page_readings"] = [row for row in page_readings if row["evidence_id"] in chosen]
            allowed = {row["id"]: row for row in readings if row["anchor_evidence_id"] in chosen}
            selected = getattr(self.composer, "selected_table_reading_ids", None)
            if selected is not None and (len(selected) != len(set(selected))
                                         or any(identity not in allowed for identity in selected)):
                raise ValueError("读数未对应已选条文的固定原表")
            result["table_readings"] = list(allowed.values()) if selected is None else [
                allowed[identity] for identity in selected]
            if any(row.get("reading_kind") == "GARAGE_AREA_BASELINE" for row in result["table_readings"]):
                gaps.append("车库卡片只读原表基础面积；敞开、错层、机械式、危险品及灭火修正需分别对照完整条文，未计算最终面积。")
        except (ValueError, LookupError, TypeError, KeyError, OSError, ContentUseDenied):
            result.update(status="EVIDENCE_CHECK_FAILED", summary="引用核对未通过，本次未显示回答。")
            gaps.append("原文、数字、引用身份或读取条件无法确认；请重查并对照固定原页。")
            trace.append({"step": "CHECK", "state": "FAILED", "message": "不一致的引用未输出"})
            result["gaps"] = list(dict.fromkeys(gaps))
            return self._check_project(result, packet)
        return self.render_response(result, packet, conditions, contexts, evidence, usable, items,
                                    local_spacing, daylight_value, original_query)
