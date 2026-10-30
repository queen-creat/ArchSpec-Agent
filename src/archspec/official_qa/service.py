"""有上限的官方证据闭环；默认本地原文整理，绝不调用外部模型。

生成器只能选择真实原文，不能自填规范数字。假模型可显式注入做故障验收；
正式模型需另行建立许可、费用及外传确认，这个服务不读取任何模型密钥。
"""

import re

from archspec.official_documents.clause_units import CLAUSE
from archspec.official_qa.condition_checks import context_issue, query_conditions
from archspec.official_qa.condition_fields import QUESTION_LABELS
from archspec.official_qa.context_selection import (
    select_context_evidence as _select_context_evidence,
)
from archspec.official_qa.daylight_conditions import (
    daylight_query_conditions,
    daylight_room_conflict,
    daylight_value_question,
)
from archspec.official_qa.garage_conditions import (
    garage_query_conditions,
)
from archspec.official_qa.garage_rules import (
    required_garage_evidence,
)
from archspec.official_qa.intents import (
    fire_area_question,
    height_measurement_question,
    height_use_category,
    highrise_definition_evidence,
    highrise_definition_question,
    net_height_question,
)
from archspec.official_qa.local_spacing import (
    evidence_key,
    required_spacing_evidence,
)
from archspec.official_qa.project_conditions import ProjectConditionChanged
from archspec.official_qa.schemas import QuestionRequest
from archspec.search.tokenizer import LexicalTokenizer
from archspec.standard_governance.compatibility import ContentUseDenied

QUESTIONS = QUESTION_LABELS


def is_body(text, clause_number):
    """条号不是正文证明；明确点线目录不作事实，原检索列表仍保留原文。"""
    return bool(CLAUSE.fullmatch(clause_number or "") and not re.search(
        r"[.…·]{3,}\s*\d+\s*(?:\n|$)", text))


def relevant(query, text, building_use=""):
    """过滤仅一字偶合，不重排前五、不把这个规则说成语义检索。"""
    if CLAUSE.fullmatch(query):
        return True
    if net_height_question(query) or height_measurement_question(query):
        # 命中可能在没有重复“净高”的续页；先读固定同条上下文，稍后按完整主条核对话题。
        return True
    if fire_area_question(query):
        normalized = re.sub(r"\s+", "", text)
        # 出口数量条款也常含面积数字；主句讨论出口，不能作为分区面积限值回答。
        if ("防火分区" not in normalized or "面积" not in normalized
                or any(word in normalized[:180] for word in ("安全出口", "疏散出口"))):
            return False
    if highrise_definition_question(query) and not highrise_definition_evidence(
            text, height_use_category(building_use)):
        return False
    tokenizer = LexicalTokenizer()
    terms = {term for term in tokenizer.tokenize(query) if len(term) >= 2}
    return bool(terms & set(tokenizer.tokenize(text)))


def fire_context_complete(value):
    """保留旧函数入口；具体解释与补填原因使用同一检查。"""
    return not context_issue(value)


def explicit_conditions(query, supplied):
    """只提取问题中直接写明的单一事实，字段中的用户明确值优先，不猜设计条件。"""
    extracted = query_conditions(query)
    if highrise_definition_question(query) and not height_use_category(query):
        extracted.pop("building_use", None)
    return {**extracted, **daylight_query_conditions(query), **garage_query_conditions(query), **supplied}


def select_context_evidence(query, contexts, evidence, *, priority_core_keys=()):
    """保留已有公开观测入口；选择实现仍只有一份。"""
    return _select_context_evidence(query, contexts, evidence, priority_core_keys=priority_core_keys)


def complete_clause_keys(contexts, evidence):
    """旧公开入口委托同一完整性合同。"""
    from archspec.official_qa.evidence_requirements import complete_clause_keys as shared

    return shared(contexts, evidence)


class LocalExtractiveComposer:
    """只选完整原文片段，不作自由改写；长条文留给阅读区，不截掉尾部条件。"""
    name = "LOCAL_EXTRACTIVE_V1"

    def compose(self, query, conditions, evidence):
        required = required_garage_evidence(conditions, evidence) if fire_area_question(query) else set()
        required |= required_spacing_evidence(query, evidence)
        return self._select(evidence, required, query=query, conditions=conditions)

    def compose_bundle(self, bundle):
        from archspec.official_qa.question_plan import thaw_value

        # 旧测试/兼容整理器的明确override仍接到副本；生产标准选择只消费共享必选集。
        if type(self).compose is not LocalExtractiveComposer.compose:
            return self.compose(bundle.plan.original_query, thaw_value(bundle.plan.conditions),
                                thaw_value(bundle.usable))
        from archspec.official_qa.evidence_requirements import complete_outgoing_dependency

        evidence = thaw_value(bundle.usable)
        ready = {row["chunk_id"] for row in evidence
                 if row.get("quote_ready") and row["role"] != "RELATED_CONTEXT"}
        priority = set()
        # 只对当前完整可摘录账本中、已证明的同书出向引用调整预览顺序。
        # 目标须独立读成完整核心；只读邻接、缺页或入向引用不因此升级。
        # 这是短答选择顺序，不修改V1检索排名，也不把引用目标变成工程适用结论。
        for context in thaw_value(bundle.contexts):
            dependency = complete_outgoing_dependency(context)
            if dependency is None:
                continue
            core, targets = dependency
            if not set(core["chunk_ids"]) <= ready:
                continue
            for target in targets:
                if set(target["chunk_ids"]) <= ready:
                    priority.update(target["chunk_ids"])
        evidence.sort(key=lambda row: row["chunk_id"] not in priority)
        # _select仍将政策必选例外置于最前，完整续文和原有软上限保持。
        return self._select(evidence, bundle.required_evidence_ids)

    @staticmethod
    def _select(evidence, required, *, query=None, conditions=None):
        selected, used = [], set()
        # 特殊条件先于普通相关摘录，整条保留；不因三条预览软上限省略已知例外。
        if required:
            evidence = sorted(evidence, key=lambda row: row["evidence_id"] not in required)
        for item in evidence:
            key = evidence_key(item)
            if (item["role"] == "RELATED_CONTEXT" or not item.get("quote_ready")
                    or key in used or len(item["text"]) > 1200):
                continue
            # 正文续文也要呈现，不能为了简短只显示开头而省略例外。
            parts = [part for part in evidence if
                     evidence_key(part) == key
                     and part["role"] != "RELATED_CONTEXT"]
            if sum(len(part["text"]) for part in parts) > 1800:
                continue
            if query is not None and daylight_value_question(query) and daylight_room_conflict(
                    conditions, "\n".join(part["text"] for part in parts)):
                continue
            # 命中可能发生在续页，摘录仍按固定片段位置排列，而不是先显示后页。
            parts.sort(key=lambda part: int(part["chunk_id"].rsplit(":", 1)[1]))
            if selected and len(selected) + len(parts) > (8 if required else 4):
                break
            selected.extend({"evidence_id": part["evidence_id"], "quote": part["text"]}
                            for part in parts)
            used.add(key)
            if len(selected) >= (8 if required else 3):
                break
        return selected


class OfficialQAService:
    def __init__(self, documents, *, composer=None, validate_project=None, project_conditions=None,
                 conditions_query=None, planner=None, legacy_missing_conditions=(), reading_only=False):
        self.documents = documents
        self.composer = composer or LocalExtractiveComposer()
        # 防止把未登记外部网关当普通整理器接上。公开路由只构造本地整理器。
        from archspec.official_qa.model_layer import ModelEvidenceComposer

        if (self.composer.name != "LOCAL_EXTRACTIVE_V1" and not self.composer.name.startswith(
                "EXPLICIT_FAKE_MODEL") and not isinstance(self.composer, ModelEvidenceComposer)):
            raise ValueError("本地问答只允许原文整理器或明确测试假模型")
        self.validate_project = validate_project
        self.project_conditions = project_conditions
        # 检索问法可由模型改写，设计条件只能取自用户原问法，不能从改写推定。
        self.conditions_query = conditions_query
        self.planner = planner
        self.legacy_missing_conditions = tuple(legacy_missing_conditions)
        # 只限制输出，不绕过项目、来源、条件或固定原件读取守卫。
        self.reading_only = reading_only

    def answer(self, request: QuestionRequest):
        from archspec.official_qa.executor import OfficialQAExecutor
        from archspec.official_qa.question_plan import LocalPlanner, build_question_plan

        # 语义只在计划边界提出；正文/条件/许可仍由同一有限执行器核对。
        planner = self.planner or LocalPlanner()
        proposal = planner.propose(request.query, request.conditions.model_dump(exclude_none=True))
        original = request.model_copy(update={
            "query": self.conditions_query if self.conditions_query is not None else request.query})
        source = getattr(planner, "name", "LOCAL_PLANNER_V1")
        if source.startswith("EXPLICIT_FAKE_PLANNER"):
            source = source.replace("EXPLICIT_FAKE_PLANNER", "EXPLICIT_FAKE_MODEL", 1)
        plan = build_question_plan(original, proposal, planner_source=source)
        executor = OfficialQAExecutor(self, request, proposal)
        with self.documents.read_phase():
            return executor.execute(plan)

    def _answer(self, request: QuestionRequest):
        # 保留旧内部入口给兼容调用；不再保存另一份业务流程。
        return self.answer(request)

    def _check_project(self, result, packet):
        """任何返回路径均复核项目条件，避免长检索/整理后输出失效资料的摘录。"""
        if packet is None:
            return result
        try:
            if self.project_conditions.inspect(packet["project_id"]) != packet:
                raise ProjectConditionChanged("项目条件在处理期间变化")
        except (ValueError, LookupError, OSError, ContentUseDenied):
            result.update(status="EVIDENCE_CHECK_FAILED", summary="项目条件已变化，请重新查询并确认。",
                          answer_items=[], evidence=[], context_groups=[], condition_provenance=[], questions=[])
            result["table_readings"] = []
            result["page_readings"] = []
            result.pop("condition_followups", None)
            result.pop("garage_condition_state", None)
            result.pop("garage_rule_checks", None)
            result.pop("public_fire_condition_state", None)
            result.pop("public_fire_scope_note", None)
            result.pop("local_spacing_condition_state", None)
            result.pop("height_condition_state", None)
            result.pop("answer_brief", None)
            # 不保留旧候选原文，既不输出受限摘录，也不改写历史资料。
            result["project_conditions"] = {"project_id": packet["project_id"], "candidates": [],
                                             "conflicts": [], "gaps": ["项目条件需要重新读取"]}
            result["gaps"] = ["项目资料、原件或读取条件发生变化，本次回答未输出。"]
            result["trace"].append({"step": "PROJECT_CONDITIONS", "state": "FAILED", "message": "条件复核未通过"})
        return result

    @staticmethod
    def _guard(draft, evidence):
        from archspec.official_qa.evidence_bundle import guard_draft

        return guard_draft(draft, evidence)
