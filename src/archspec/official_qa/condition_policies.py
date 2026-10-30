"""服务器计划的统一条件政策；不检索、不读取原件、不推断工程适用性。"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from archspec.official_documents.clause_links import BODY
from archspec.official_documents.reference_relevance import (
    measurement_statement,
    reference_matches,
    subject_query,
)
from archspec.official_qa.condition_fields import condition_questions
from archspec.official_qa.daylight_conditions import (
    daylight_condition_issues,
    daylight_text_matches,
)
from archspec.official_qa.fire_scope import fire_use_conflict, smoke_area_only
from archspec.official_qa.garage_conditions import garage_followups
from archspec.official_qa.garage_rules import required_garage_evidence
from archspec.official_qa.height_conditions import height_condition_issues
from archspec.official_qa.height_measurement import measurement_text
from archspec.official_qa.intents import (
    height_use_category,
    highrise_definition_evidence,
    net_height_question,
)
from archspec.official_qa.local_scope import spacing_evidence
from archspec.official_qa.local_spacing import required_spacing_evidence, spacing_followups
from archspec.official_qa.public_fire_conditions import (
    fire_initial_issues,
    public_fire_followups,
)
from archspec.official_qa.question_plan import TOPIC_OPERATIONS
from archspec.search.tokenizer import LexicalTokenizer

# 主题词只为既有领域helper提供兼容入口，不增加原问句分类或条件值。
LEGACY_CONCEPTS = {
    "FIRE_COMPARTMENT_AREA": "防火分区面积",
    "SUNLIGHT_DURATION": "日照",
    "SUNLIGHT_SPACING": "日照间距计算公式",
    "BUILDING_HEIGHT": "建筑高度 计算方法",
    "NET_HEIGHT": "净高",
    "DAYLIGHT_COEFFICIENT": "采光系数",
}


def _fire_subject(text):
    compact = re.sub(r"\s+", "", text)
    return ("防火分区" in compact and "面积" in compact
            and not any(word in compact[:180] for word in ("安全出口", "疏散出口")))


def _measurement_subject(text):
    # 动词讨论高度本身；前置的面宽/间距主语不能借后面的高度词代答。
    # 按完整同条逐句核对，真正高度量法的其它解释句仍整体保留原字。
    compact = re.sub(r"\s+", "", text)
    for sentence in re.split(r"[。；;！？!?]", compact):
        if not measurement_text(sentence):
            continue
        introduction = re.split(r"应按|应根据|按|根据", sentence, maxsplit=1)[0]
        if ("高度" not in introduction and any(term in introduction for term in
                ("面宽", "面积", "间距", "停机坪", "疏散", "净高"))):
            continue
        return True
    return False


@dataclass(frozen=True)
class ConditionPolicy:
    """主题/操作只由固定计划决定；全文判断只消费已核对的完整核心文字。"""

    topic: str
    operation: str
    focus: str | None
    legacy_query: str
    original_query: str
    reference_query: str | None = None
    query_focus: Mapping | None = None
    fire_area: bool = False
    highrise_definition: bool = False
    height_measurement: bool = False
    net_height: bool = False
    daylight_value: bool = False
    local_spacing: bool = False
    empty_selection_requires_required: bool = False

    def matches_query_focus(self, text, *, source_title=""):
        """已绑定原问的对象与属性逐项核对，不以总词覆盖替代缺失焦点。

        动作原字保留在计划供诊断；同义动作继续由既有操作政策判断，
        不把“放在”和“布置”等不同原字强行视为相同或冲突。
        """
        if self.query_focus is None:
            return True
        from archspec.official_documents.reading_scope import (
            conditional_subject_match,
            conditional_subject_pairs,
            direct_statement_pairs,
            direct_statement_text,
            main_subject_match,
            numbered_statement_pairs,
            subsequent_subject_pairs,
        )
        from archspec.official_documents.reference_relevance import reference_dimension_variants

        def attribute_matches(value, part, *, starts=False):
            value = re.sub(r"\s+", "", value)
            variants = (value,)
            # “尺寸”查出处沿用候选的宽/高口径，不宣称已回答全部尺寸或测量方法。
            # 只扩展句末泛属性，尺寸精度等实质限定与明确宽度/高度保持原字。
            if self.operation == "REFERENCE_LOOKUP" and value.endswith("尺寸"):
                variants += reference_dimension_variants(value)
            return any(part.startswith(term) if starts else term in part for term in variants)

        # 依据条件仍完整保留在原引用中；只在核角色时不借输入属性回答输出问题。
        role_text = direct_statement_text(text)
        compact = lambda value: re.sub(r"\s+", "", value)
        # 角色与原句同长；在原位置切片后再去空白，不能靠同词在别句借量法。
        ending = re.search(r"[。；;：:]", role_text)
        end = ending.start() if ending else len(role_text)
        introduction = compact(role_text[:end])
        statements = list(direct_statement_pairs(text[:end], role_text=role_text[:end]))
        statements.extend(conditional_subject_pairs(text[:end], self.query_focus["subjects"],
                                                    role_text=role_text[:end]))
        initial_statements = tuple(statements)
        colon = re.search(r"[：:]", role_text)
        for role, raw in numbered_statement_pairs(text):
            statements.append((role, raw))
            if (colon and all(value in introduction for value in self.query_focus["subjects"])
                    and any(attribute_matches(value, compact(role), starts=True) for value in self.query_focus["attributes"])):
                statements.append((role_text[:colon.start()] + "：" + role,
                                   text[:colon.start()] + "：" + raw))
        statements = [(compact(role), compact(raw)) for role, raw in statements]
        from archspec.official_documents.reading_scope import focus_prefix_context

        contexts = self.query_focus.get("prefix_context", ())
        title = re.sub(r"\s+", "", source_title)
        def direct_match(part):
            if not all(main_subject_match(part, value) or conditional_subject_match(part, value)
                       for value in self.query_focus["subjects"]):
                return False
            if all(value in part for value in contexts):
                return True
            if not all(value in part or value in title for value in contexts):
                return False
            # 来源标题可为中性主句提供原字范围；异类前置主语不能借书名背书。
            prefixes = focus_prefix_context(part, self.query_focus["subjects"], clause=True)
            # 分句不能丢掉原引导句的异类范围，再借来源题名把它补成当前范围。
            original_prefixes = focus_prefix_context(introduction, self.query_focus["subjects"], clause=True)
            return all(any(prefix in value for value in contexts) for prefix in (*prefixes, *original_prefixes))
        # 首句已核对象范围后，连续后句明确重复该对象才可补充新属性。
        # 原句与角色仍成对；后句输入依据、附注或新条号不借来作答案。
        if any(direct_match(compact(role)) for role, _ in initial_statements):
            statements.extend((compact(role), compact(raw)) for role, raw in
                              subsequent_subject_pairs(text, self.query_focus["subjects"]))
        # 对象/范围与所问属性必须由同一分句或同一继承列表行支持，不能全条拼词。
        return any(direct_match(part) and all(attribute_matches(value, part)
                                             for value in self.query_focus["attributes"])
                   and (self.topic != "UNKNOWN" or self.operation != "MEASUREMENT_METHOD"
                        or measurement_statement(original)) for part, original in statements)

    def pre_search_issues(self, conditions: Mapping) -> dict:
        """出处/量法可先读；受条件影响的限值继续使用已有拒绝歧义检查。"""
        if self.fire_area and self.operation == "CONDITIONED_LIMIT":
            return fire_initial_issues(conditions)
        if self.highrise_definition and not height_use_category(conditions.get("building_use", "")):
            return {"building_use": "用途未明确，不返回统一高度数值"}
        if self.daylight_value:
            return daylight_condition_issues(conditions)
        return {}

    def concept_query(self, conditions: Mapping) -> str | None:
        """只返回登记概念；是否缺核心及一次/两身份预算由executor统一控制。"""
        if self.highrise_definition:
            return ("高层建筑高度分类"
                    if height_use_category(conditions.get("building_use", "")) else None)
        if self.height_measurement:
            return "建筑高度 计算方法"
        if self.local_spacing:
            return "间距 系数"
        return None

    def matches_source_title(self, title, conditions):
        """明确用途冲突只转关联；固定书名不能绕过正文主语核对。"""
        return self.topic != "UNKNOWN" or not fire_use_conflict(conditions.get("building_use", ""), title)

    def is_body(self, text: str, clause_number: str | None) -> bool:
        """共用实际完整条号检查；点线目录仍只保留阅读入口。"""
        return bool(BODY.fullmatch(clause_number or "") and not re.search(
            r"[.…·]{3,}\s*\d+\s*(?:\n|$)", text))

    def relevant_text(self, text: str, conditions: Mapping, *, source_title="") -> bool:
        """候选筛选不是完整性证明；量法/净高先读，之后按完整核心核对。"""
        if self.topic == "EXACT_REFERENCE" or self.height_measurement or self.net_height:
            return True
        if self.fire_area:
            return _fire_subject(text)
        if self.highrise_definition:
            return highrise_definition_evidence(text, height_use_category(conditions.get("building_use", "")))
        if self.local_spacing:
            return spacing_evidence(text)
        # 查出处和具体取值共用主题边界；只有取值操作才触发必要条件。
        if self.topic == "DAYLIGHT_COEFFICIENT":
            return daylight_text_matches(conditions, text)
        if self.topic == "UNKNOWN":
            # 阅读召回沿原问核对，不因模型过长/角色错误的焦点隐藏已有正文。
            # 自动摘录仍必须通过accept_full_clause中的原问焦点检查。
            return self._reference_matches(text, conditions, source_title=source_title)
        tokenizer = LexicalTokenizer()
        query = self.original_query if self.topic == "UNKNOWN" else self.legacy_query
        terms = {term for term in tokenizer.tokenize(query) if len(term) >= 2}
        return bool(terms & set(tokenizer.tokenize(text)))

    def matches_reading_question(self, text: str, conditions: Mapping, *, source_title="") -> bool:
        """对应候选须体现所问操作；一般规则仍由原补读保留，不升级为系数答案。"""
        return (self.relevant_text(text, conditions, source_title=source_title) and (not self.local_spacing
                or spacing_evidence(text, self.original_query)))

    def _reference_matches(self, text, conditions, *, source_title=""):
        """初读与引用共用范围支持；标题只补已经逐项核过的原问范围。"""
        title = re.sub(r"\s+", "", source_title)
        contexts = self.query_focus.get("prefix_context", ()) if self.query_focus else ()
        supported = (tuple(value for value in contexts if value in title)
                     if self.matches_query_focus(text, source_title=source_title) else ())
        return reference_matches(self.reference_query or self.original_query, text, conditions,
                                 scope_context=supported)

    def accept_full_clause(self, text: str, conditions: Mapping, *, source_title="") -> bool:
        """完整文字由bundle拼接一次；不借邻接用途、否定词或半片谓词证明。"""
        # 专题规则与已绑定原问焦点取交集；净高/高度等主题名不等于对象已核对。
        if self.query_focus is not None and not self.matches_query_focus(text, source_title=source_title):
            return False
        # 查出处和具体取值共用主题边界；只有取值操作才触发必要条件。
        if self.topic == "DAYLIGHT_COEFFICIENT":
            return daylight_text_matches(conditions, text)
        if self.topic == "UNKNOWN":
            # 普通词组召回只证明可读相关性；没有绑定对象不能自动引用成答案。
            return (self.query_focus is not None and self.matches_query_focus(text, source_title=source_title)
                    and self._reference_matches(text, conditions, source_title=source_title))
        if self.highrise_definition:
            return highrise_definition_evidence(text, height_use_category(conditions.get("building_use", "")))
        if self.height_measurement:
            if not _measurement_subject(text):
                return False
            compact = re.sub(r"\s+", "", text)
            return self.focus != "ROOF_EQUIPMENT" or any(term in compact for term in (
                "设备用房", "机房", "局部突出屋面", "突出屋面用房"))
        if self.net_height:
            return net_height_question(text)
        if self.fire_area:
            # 初始候选需要面积主语；已完整补读的设施/分隔修正不强制重复该词。
            # 这些条文仍必须核对主语用途和防烟冲突，不能借另一用途或半片代答。
            return (not smoke_area_only(text)
                    and not fire_use_conflict(conditions.get("building_use", ""), text))
        # 宜昌命名附录有高度计算子项，不能因该子项未重复间距词删掉必选正文。
        # 地方来源身份及实际附录完整性仍由executor/bundle的旧守卫核对。
        return True

    def required_evidence_ids(self, conditions: Mapping, evidence: Sequence) -> set[str]:
        """预算后只计算一次特殊必选集合；不读取额外正文或改变证据输入。"""
        if self.local_spacing:
            return required_spacing_evidence(self.legacy_query, evidence)
        if self.fire_area:
            return required_garage_evidence(conditions, evidence)
        return set()

    def followups(self, conditions: Mapping, *, items=(), units=()) -> list[dict]:
        """按实际已核全条判断缺项；标签来自共用登记，不生成工程值。"""
        if self.height_measurement:
            # 只在已核全条明确讨论设备/突出用房的面积例外后触发后两问。
            # 当前真实原字决定需求；问题里的“机房”不是设备性质证明。
            texts = [re.sub(r"\s+", "", item["quote"]) for item in items]
            texts.extend(re.sub(r"\s+", "", unit["text"]) for unit in units
                         if unit.get("complete") is True)
            observed = any("面积" in text
                and any(term in text for term in ("设备用房", "突出屋面用房"))
                and any(term in text for term in ("不计入建筑高度", "不应计入建筑高度"))
                for text in texts)
            return condition_questions(height_condition_issues(
                conditions, equipment_rule_observed=observed))
        if self.local_spacing:
            questions = spacing_followups(conditions)
            # 地方计算补问共享同一主题政策，不再由某一本命名附录的入口决定。
            # 城区分支只在实际完整文字出现时才问，不能把别处的分区条件套过来。
            texts = [item["quote"] for item in items]
            texts.extend(unit["text"] for unit in units if unit.get("complete") is True)
            named_rules = any(str(unit.get("unit_key", "")).startswith("named:") for unit in units)
            named_rules |= any(str(item["citation"].get("reading_unit_key", "")).startswith("named:")
                               for item in items)
            if texts and not named_rules and not any(term in re.sub(r"\s+", "", text)
                                                    for text in texts for term in ("城区", "规划范围")):
                questions = [row for row in questions if row["key"] != "spacing_area"]
            return questions
        if self.fire_area:
            return (garage_followups(self.legacy_query, conditions)
                    + public_fire_followups(self.legacy_query, conditions))[:3]
        return []


def policy_for(plan, *, reference_region=None) -> ConditionPolicy:
    """按唯一登记组合分派；模型或调用方不能自造操作、关注值和条件政策。"""
    if plan.operation not in TOPIC_OPERATIONS.get(plan.topic, ()):
        raise ValueError("问题主题与操作组合未登记")
    if plan.focus not in {None, "GENERAL", "ROOF_EQUIPMENT"}:
        raise ValueError("问题关注值未登记")
    if plan.focus == "ROOF_EQUIPMENT" and (
            plan.topic != "BUILDING_HEIGHT" or plan.operation != "MEASUREMENT_METHOD"):
        raise ValueError("设备用房关注只用于高度计量规则")
    highrise = plan.topic == "BUILDING_HEIGHT" and plan.operation == "DEFINITION"
    measurement = plan.topic == "BUILDING_HEIGHT" and plan.operation == "MEASUREMENT_METHOD"
    content = getattr(plan, "search_query", plan.original_query)
    legacy = content
    # LocalPlanner已识别原句，保持其兼容行为。模型语义只补统一主题词，不猜层位。
    if not plan.planner_source.startswith("LOCAL"):
        concept = "高层建筑高度分类" if highrise else LEGACY_CONCEPTS.get(plan.topic)
        if concept:
            legacy = f"{legacy} {concept}"
    return ConditionPolicy(
        topic=plan.topic, operation=plan.operation, focus=plan.focus, query_focus=getattr(plan, "query_focus", None),
        legacy_query=legacy, original_query=plan.original_query,
        reference_query=subject_query(content,
            (getattr(plan, "explicit_source_scope", None) or {}).get("targets", ()), region=reference_region),
        fire_area=plan.topic == "FIRE_COMPARTMENT_AREA", highrise_definition=highrise,
        height_measurement=measurement, net_height=plan.topic == "NET_HEIGHT",
        daylight_value=plan.topic == "DAYLIGHT_COEFFICIENT" and plan.operation == "CONDITIONED_LIMIT",
        local_spacing=plan.topic == "SUNLIGHT_SPACING",
        empty_selection_requires_required=plan.topic == "SUNLIGHT_SPACING")
