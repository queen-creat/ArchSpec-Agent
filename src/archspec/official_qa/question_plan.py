"""共同问题计划：语义提案无资料权威，原请求和条件由服务器绑定。"""

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from archspec.official_documents.clause_units import ARTICLE, CLAUSE
from archspec.official_qa.condition_checks import query_conditions
from archspec.official_qa.daylight_conditions import (
    daylight_query_conditions,
    daylight_value_question,
)
from archspec.official_qa.garage_conditions import garage_query_conditions
from archspec.official_qa.height_conditions import height_query_conditions
from archspec.official_qa.height_measurement import height_topic
from archspec.official_qa.intents import (
    explicit_reference_lookup,
    fire_area_question,
    height_measurement_question,
    height_use_category,
    highrise_definition_question,
    net_height_question,
)
from archspec.official_qa.local_scope import sunlight_spacing_question
from archspec.official_qa.schemas import QuestionConditions, QuestionRequest
from archspec.query_scope import requested_content

Topic = Literal["EXACT_REFERENCE", "FIRE_COMPARTMENT_AREA", "SUNLIGHT_DURATION",
                "SUNLIGHT_SPACING", "BUILDING_HEIGHT", "NET_HEIGHT",
                "DAYLIGHT_COEFFICIENT", "UNKNOWN"]
Operation = Literal["REFERENCE_LOOKUP", "DEFINITION", "MEASUREMENT_METHOD",
                    "CONDITIONED_LIMIT", "CALCULATION_RULES"]
Focus = Literal["GENERAL", "ROOF_EQUIPMENT"] | None
TOPIC_OPERATIONS = {
    "EXACT_REFERENCE": frozenset({"REFERENCE_LOOKUP"}),
    "FIRE_COMPARTMENT_AREA": frozenset({"REFERENCE_LOOKUP", "CONDITIONED_LIMIT"}),
    "SUNLIGHT_DURATION": frozenset({"REFERENCE_LOOKUP", "CONDITIONED_LIMIT"}),
    "SUNLIGHT_SPACING": frozenset({"REFERENCE_LOOKUP", "CALCULATION_RULES"}),
    "BUILDING_HEIGHT": frozenset({"DEFINITION", "MEASUREMENT_METHOD"}),
    "NET_HEIGHT": frozenset({"REFERENCE_LOOKUP", "CONDITIONED_LIMIT"}),
    "DAYLIGHT_COEFFICIENT": frozenset({"REFERENCE_LOOKUP", "CONDITIONED_LIMIT"}),
    "UNKNOWN": frozenset({"REFERENCE_LOOKUP", "MEASUREMENT_METHOD"}),
}


class FrozenDict(dict):
    """保留普通映射读接口；递归冻结账本，外部写入立即拒绝。"""

    def _deny(self, *args, **kwargs):
        raise TypeError("固定合同不可修改，请使用独立序列化副本")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        return self


def freeze_value(value):
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze_value(part) for key, part in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(part) for part in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_value(part) for part in value)
    return deepcopy(value)


def thaw_value(value):
    """所有对外字典/列表均新建，不能让整理器回写内部合同。"""
    if isinstance(value, Mapping):
        return {key: thaw_value(part) for key, part in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_value(part) for part in value]
    if isinstance(value, (set, frozenset)):
        return [thaw_value(part) for part in sorted(value)]
    if isinstance(value, SnapshotRecord):
        return value.model_dump(mode="json")
    return deepcopy(value)


class SnapshotRecord:
    def model_dump(self, *, mode="python", exclude_none=False):
        return {field.name: thaw_value(getattr(self, field.name)) for field in fields(self)
                if (not exclude_none or getattr(self, field.name) is not None)
                and not (field.metadata.get("omit_empty") and not getattr(self, field.name))}


FocusPhrase = Annotated[str, Field(min_length=2, max_length=32)]
# 动作不参与对象/属性相关性门槛，中文“放、装、开”等一字动词仍须绑定原问。
FocusAction = Annotated[str, Field(min_length=1, max_length=32)]


class QueryFocusProposal(BaseModel):
    """只抽取原问词组，不允许模型给出同义改写、事实或资料身份。"""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    subjects: tuple[FocusPhrase, ...] = Field(default=(), max_length=3)
    attributes: tuple[FocusPhrase, ...] = Field(default=(), max_length=3)
    actions: tuple[FocusAction, ...] = Field(default=(), max_length=3)

    def bind(self, query, *, background_use=None):
        compact = re.sub(r"\s+", "", query)
        values = self.model_dump()
        if not self.subjects or any(re.sub(r"\s+", "", value) not in compact
                                   for parts in values.values() for value in parts):
            raise ValueError("问题焦点无法绑定原问，不作为查询约束")
        from archspec.official_documents.reference_relevance import COMMON

        # 提案原字保持在模型记录；绑定计划不把“要求/规定”等通用提问词当成性质。
        # 只复用原相关性词表的完整词，不删数字、不拆实质属性，也不放宽对象核对。
        attributes = []
        for value in values["attributes"]:
            phrase = re.sub(r"\s+", "", value)
            if phrase in COMMON:
                continue
            # 仅明确“有什么…要求/规定”的句末提问框架，不截内部术语或书名。
            # 原模型提案保持；绑定值仍是同一原问中的连续子串。
            if (value.endswith(("要求", "规定"))
                    and re.search(r"(?:有什么|有哪些|什么|哪些)" + re.escape(phrase) + r"[？?。]?$", compact)):
                base = value[:-2].rstrip()
                if len(re.sub(r"\s+", "", base)) >= 2 and base not in COMMON:
                    value = base
            attributes.append(value)
        values["attributes"] = tuple(dict.fromkeys(attributes))
        from archspec.official_documents.reading_scope import focus_prefix_context

        # 对象前的遗漏范围仍绑定原问；不写入用户条件，也不由模型/候选反推用途。
        context = focus_prefix_context(query, self.subjects, background_use=background_use)
        if context:
            values["prefix_context"] = context
        if background_use:
            from archspec.official_documents.reference_relevance import use_background_pattern

            pattern = use_background_pattern(background_use)
            subjects = []
            for subject in values["subjects"]:
                prefix = re.match(pattern, subject) if pattern else None
                remainder = subject[prefix.end():] if prefix else subject
                # 仅明确的“用途建筑/楼”前缀是背景；办公设施等对象原字保留。
                # 原提案未变，所得对象仍是原问连续子串，不生成同义词或条件。
                if prefix and prefix[0].endswith(("建筑", "楼")) and len(remainder) >= 2:
                    subjects.append(remainder)
                elif (pattern is None and query_conditions(query).get("building_use") == background_use
                      and subject.startswith(background_use) and len(subject) - len(background_use) >= 2):
                    # 其余已由原问识别的用途不能丢掉：拆成设施原字与显式范围，
                    # 后续仍须由正文/固定书名支持范围；用户另填用途或候选不得反推此拆分。
                    subjects.append(subject[len(background_use):])
                    values["prefix_context"] = tuple(dict.fromkeys((*context, background_use)))
                else:
                    subjects.append(subject)
            values["subjects"] = tuple(subjects)
        return values


class ModelSemanticProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    version: Literal["MODEL_SEMANTIC_PLAN_V2"] = "MODEL_SEMANTIC_PLAN_V2"
    topic: Topic
    operation: Operation
    focus: Focus = None
    understanding: Literal["CLEAR", "UNCERTAIN"] = "CLEAR"
    query_focus: QueryFocusProposal | None = None
    public_search_titles: tuple[Annotated[str, Field(min_length=2, max_length=100)], ...] = Field(
        default=(), max_length=3)

    @field_validator("public_search_titles")
    @classmethod
    def verified_title_hints(cls, values):
        from archspec.official_discovery.public_queries import PUBLIC_TITLES

        # 书名只是可选补查建议；未知名称丢弃，不能因附属建议破坏已确认理解。
        return tuple(dict.fromkeys(value for value in values if value in PUBLIC_TITLES))

    def semantic_payload(self):
        """保存/恢复共用格式；未启用焦点时保持旧V2字段，不写新增空值。"""
        excluded = {"query_focus"} if self.query_focus is None else set()
        if not self.public_search_titles:
            excluded.add("public_search_titles")
        return self.model_dump(exclude=excluded)

    @model_validator(mode="after")
    def registered_combination(self):
        if self.public_search_titles and (self.topic != "UNKNOWN" or self.understanding != "CLEAR"):
            object.__setattr__(self, "public_search_titles", ())
        # 焦点是原问的额外约束，不替代专题政策；已登记主题也不能丢掉对象。
        if self.query_focus is not None and self.understanding != "CLEAR":
            raise ValueError("当前原问焦点只用于明确理解的问题")
        if self.operation not in TOPIC_OPERATIONS[self.topic]:
            raise ValueError("问题主题与操作组合未登记")
        if self.focus == "ROOF_EQUIPMENT" and (
                self.topic != "BUILDING_HEIGHT" or self.operation != "MEASUREMENT_METHOD"):
            raise ValueError("设备用房关注只用于高度计量规则")
        return self


@dataclass(frozen=True)
class QuestionPlan(SnapshotRecord):
    original_query: str
    project_id: str | None
    requested_source_ids: tuple[str, ...] | None
    effective_source_ids: tuple[str, ...] | None
    project_condition_ids: tuple[str, ...]
    topic: str
    operation: str
    focus: str | None
    understanding: str
    conditions: Mapping
    public_conditions: Mapping
    condition_provenance: tuple
    project_condition_fingerprint: str | None
    explicit_source_scope: Mapping | None = None
    explicit_clause_number: str | None = None
    explicit_clause_numbers: tuple[str, ...] = ()
    planner_source: str = "LOCAL_PLANNER_V1"
    version: str = "QUESTION_PLAN_V1"
    query_focus: Mapping | None = None
    understanding_reason: str | None = None
    public_search_titles: tuple[str, ...] = field(default=(), metadata={"omit_empty": True})

    requested_editions: tuple[tuple[str, str, str], ...] = field(default=(), metadata={"omit_empty": True})

    def __post_init__(self):
        from archspec.official_discovery.public_queries import validated_public_titles

        # 模型建议只作读取顺序提示，不变成用户已指定的规范或工程适用性。
        object.__setattr__(self, "public_search_titles", validated_public_titles(self.public_search_titles))
        for name in ("conditions", "public_conditions", "condition_provenance", "explicit_source_scope",
                     "explicit_clause_numbers", "query_focus", "requested_editions"):
            object.__setattr__(self, name, freeze_value(getattr(self, name)))

    @property
    def search_query(self):
        """原问保留；共同检索与主题政策只去除明确不查的主题。"""
        return requested_content(self.original_query)

    def bound_clause_numbers(self):
        """只把多条号绑定到唯一明确书目；多书多号不猜条号与书的配对。"""
        return self.explicit_clause_numbers if len(self.explicit_source_targets()) == 1 else ()

    def matches_explicit_source(self, source):
        """同一明确目标核登记、固定上下文及最终证据；不推断版本现行或使用许可。"""
        if self.explicit_source_scope is None:
            return not self.requested_editions
        from archspec.official_discovery.plan import DiscoveryPlan

        scope = self.explicit_source_scope
        target = DiscoveryPlan(scope["route"], scope["region"], tuple(scope["targets"]), (), 0, requested_editions=self.requested_editions)
        return target.match_source(source.get("standard_code"), source.get("title")) == "TARGET"


    def explicit_source_targets(self):
        """只返回服务器从原问绑定的公共目标，不从已找到的一本书反推全部需求。"""
        return tuple(self.explicit_source_scope["targets"]) if self.explicit_source_scope else ()

    def matches_source_target(self, target, source):
        """分项核目标前仍核整个范围，防题名绕过同编号家族的明确年份。"""
        if target not in self.explicit_source_targets() or not self.matches_explicit_source(source):
            return False
        from archspec.official_discovery.plan import DiscoveryPlan

        scope = self.explicit_source_scope
        one = DiscoveryPlan(scope["route"], scope["region"], (tuple(target),), (), 0, requested_editions=self.requested_editions)
        return one.match_source(source.get("standard_code"), source.get("title")) == "TARGET"


def _literal_reference_focus(query):
    """只识别单一“对象的属性+查原文”结构，不从词表推对象或改写同义词。"""
    if query.count("的") != 1:
        return None
    match = re.fullmatch(
        r"(?:请问|请查|帮我查|帮我找)?\s*(?P<subject>[^的，,。；;？！?\s]{2,32})的"
        r"(?P<attribute>[^的，,。；;？！?\s]{2,32}?)"
        # 同一对象/属性的句末提问框架；不把“有哪些/有何/有什么”绑定成属性。
        r"(?:(?:怎么|如何)(?:要求|规定)|(?:有哪些|有何|有什么)(?:要求|规定)|"
        r"(?:要求|规定|原文|出处)(?:是什么|有哪些|在哪里|在哪)?)"
        r"[？?。]?", query.strip())
    if match is None:
        return None
    subject, attribute = match["subject"], match["attribute"]
    if (subject.endswith(("规范", "标准", "规定", "要求"))
            or re.search(r"[和与及或、]|多少|计算|怎么算|是否|能否|可以|允许|不得|不应|不能", subject + attribute)):
        return None
    # 明确内/外方位修饰的是后面的对象，不把位置当主体或把对象当属性。
    # 位置原字由共同prefix_context绑定，后续仍须在同一陈述中核对。
    if subject.endswith(("内", "外")):
        return {"subjects": [attribute], "attributes": [], "actions": []}
    return {"subjects": [subject], "attributes": [attribute], "actions": []}


def _literal_indexed_reference_focus(query):
    """单一公开目录对象+原字属性的省略结构；范围与疑问操作不猜。"""
    if "的" in query or re.search(r"[《》和与及或、]|多少|多大|计算|是否|能否|可以|允许|不得|不应|不能", query):
        return None
    match = re.fullmatch(
        r"(?:请问|请查|帮我查|帮我找)?(?P<phrase>[^，,。；;？！?]{4,96}?)"
        r"(?:(?:怎么|如何)(?:要求|规定)|(?:有哪些|有何|有什么)(?:要求|规定)|"
        r"(?:要求|规定|原文|出处)(?:是什么|有哪些|在哪里|在哪)?)"
        r"[？?。]?", re.sub(r"\s+", "", query))
    if match is None:
        return None
    from archspec.official_discovery.public_scope_catalog import public_section_subjects
    from archspec.official_documents.reference_relevance import COMMON

    phrase = match["phrase"]
    anchors = [(subject, found.start(), found.end()) for subject in public_section_subjects()
               for found in re.finditer(re.escape(subject), phrase)]
    if len(anchors) != 1:
        return None
    subject, start, end = anchors[0]
    prefix, attribute = phrase[:start], phrase[end:]
    # 未分清的复合对象仍保留原流程；这里只承接已有内/外空间修饰结构。
    if prefix and (len(prefix) > 32 or not prefix.endswith(("内", "外"))):
        return None
    if not 2 <= len(attribute) <= 32 or attribute in COMMON:
        return None
    return {"subjects": [subject], "attributes": [attribute], "actions": []}


def _literal_measurement_focus(query):
    """单对象明确量法可本地理解；只绑定原问连续原字，不推导尺寸、数字或同义词。"""
    compact = re.sub(r"\s+", "", query)
    match = re.fullmatch(
        r"(?:请问|请查|帮我查)?(?P<subject>[\u4e00-\u9fffA-Za-z0-9·]{2,32}?)的?"
        r"(?P<action>从(?:哪里|哪儿|哪)(?:开始)?量|怎么(?:测量|量取|量)|如何(?:测量|量取)|量法(?:是什么)?)"
        r"[？?。]?", compact)
    if match is None or re.search(r"[和与及或、]|是否|能否|多少|几米", match["subject"]):
        return None
    return {"subjects": [match["subject"]], "attributes": [], "actions": [match["action"]]}


class LocalPlanner:
    """旧问法只在此处识别一次；后续政策消费topic/operation/focus。"""

    def propose(self, query: str, public_conditions: dict[str, str], *, allow_reference_focus=True) -> dict:
        query = requested_content(query)
        topic, operation, focus = "UNKNOWN", "REFERENCE_LOOKUP", None
        if CLAUSE.fullmatch(query.strip()):
            topic = "EXACT_REFERENCE"
        elif net_height_question(query):
            topic = "NET_HEIGHT"
        elif highrise_definition_question(query):
            topic, operation = "BUILDING_HEIGHT", "DEFINITION"
        elif height_measurement_question(query):
            topic, operation, focus = "BUILDING_HEIGHT", "MEASUREMENT_METHOD", height_topic(query)
        elif fire_area_question(query):
            topic, operation = "FIRE_COMPARTMENT_AREA", "CONDITIONED_LIMIT"
        elif sunlight_spacing_question(query):
            topic, operation = "SUNLIGHT_SPACING", "CALCULATION_RULES"
        elif "日照" in query:
            topic = "SUNLIGHT_DURATION"
        elif "采光" in query:
            topic = "DAYLIGHT_COEFFICIENT"
            if daylight_value_question(query):
                operation = "CONDITIONED_LIMIT"
        # 查原文和取值共享主题，操作由原问的明确请求决定，不猜条件或换来源。
        if (topic != "UNKNOWN" and "REFERENCE_LOOKUP" in TOPIC_OPERATIONS[topic]
                and explicit_reference_lookup(query)):
            operation = "REFERENCE_LOOKUP"
        literal = (_literal_measurement_focus(query)
                   if topic == "UNKNOWN" and allow_reference_focus else None)
        if literal is not None:
            operation = "MEASUREMENT_METHOD"
        elif topic == "UNKNOWN" and allow_reference_focus:
            literal = _literal_reference_focus(query) or _literal_indexed_reference_focus(query)
        return ModelSemanticProposal(topic=topic, operation=operation, focus=focus,
            understanding="UNCERTAIN" if topic == "UNKNOWN" and literal is None else "CLEAR",
            query_focus=QueryFocusProposal.model_validate(literal) if literal is not None else None).model_dump(
                exclude={"public_search_titles"})


def bind_literal_model_intent(proposal, query, public_conditions):
    """合并原问明确结构时同时绑定对象与操作；原模型提案和恢复记录不回写。"""
    if proposal.understanding != "CLEAR":
        return proposal, {}
    if (proposal.topic != "UNKNOWN" and proposal.operation in {"CONDITIONED_LIMIT", "CALCULATION_RULES"}
            and "REFERENCE_LOOKUP" in TOPIC_OPERATIONS[proposal.topic]
            and explicit_reference_lookup(query)):
        payload = proposal.semantic_payload()
        payload["operation"] = "REFERENCE_LOOKUP"
        bound = ModelSemanticProposal.model_validate(payload)
        return bound, {"plan_resolution": {"rule": "EXPLICIT_REFERENCE_LOOKUP_V1",
            "proposed_topic": proposal.topic, "effective_topic": bound.topic,
            "proposed_operation": proposal.operation, "operation": bound.operation}}
    literal = LocalPlanner().propose(query, public_conditions)
    focus = literal.get("query_focus")
    if literal["topic"] != "UNKNOWN" or focus is None:
        return proposal, {}
    payload, notes = proposal.semantic_payload(), {}
    if proposal.topic == "UNKNOWN" and proposal.query_focus is None:
        payload.update(query_focus=focus, operation=literal["operation"])
        notes["focus_reused_from_local"] = True
        rule = "LITERAL_FOCUS_AND_OPERATION_V1"
    elif (literal["operation"] == "MEASUREMENT_METHOD" and proposal.focus is None
            and proposal.query_focus is not None and not proposal.query_focus.attributes
            and tuple(focus["subjects"]) == proposal.query_focus.subjects
            and (proposal.topic != "UNKNOWN" or proposal.operation != literal["operation"])):
        # 同一对象的原问量法优先于附加专题或遗漏的操作，也兼容旧的已补焦点恢复记录。
        payload.update(topic="UNKNOWN", operation="MEASUREMENT_METHOD")
        rule = "LITERAL_OBJECT_MEASUREMENT_V1"
    else:
        return proposal, {}
    bound = ModelSemanticProposal.model_validate(payload)
    notes["plan_resolution"] = {"rule": rule, "proposed_topic": proposal.topic,
        "effective_topic": bound.topic, "proposed_operation": proposal.operation,
        "operation": bound.operation}
    return bound, notes


def _explicit_reference_numbers(content):
    """中文条号仅认明确引用边界；不把第一条道路等物理对象量词当查条。"""
    article = (ARTICLE.pattern + r"(?=\s*(?:的?(?:内容|原文|规定|要求|出处)"
               r"|[、，,；;和与及或]|[？?。]?$))")
    pattern = re.compile(r"\d+(?:\.\d+){2,3}|" + article)
    return tuple(dict.fromkeys(match[0] for match in pattern.finditer(content)))


def build_question_plan(request: QuestionRequest, proposal, project_provider=None, *,
                        confirmed_conditions=None, condition_provenance=(),
                        project_condition_fingerprint=None, effective_source_ids=None,
                        planner_source="LOCAL_PLANNER_V1") -> QuestionPlan:
    """只绑定受信当前检查结果；factory不重复inspect/resolve或扩大显式范围。"""
    semantic = (proposal if isinstance(proposal, ModelSemanticProposal)
                else ModelSemanticProposal.model_validate(proposal))
    if (planner_source not in {"LOCAL_PLANNER_V1", "MODEL_PLANNER_V2",
                              "MODEL_SEMANTIC_PLAN_V2", "MODEL_LEGACY_ADAPTER_V1"}
            and not re.fullmatch(r"EXPLICIT_FAKE_MODEL(?:_[A-Z0-9]+)*", planner_source)):
        raise ValueError("计划生成器标签未登记")
    # 目标只取用户原问，模型提案不能新增书名、版本或条号；与普通查阅共用解析。
    from archspec.official_discovery.plan import build_discovery_plan
    from archspec.official_discovery.public_queries import PUBLIC_CODE

    content = requested_content(request.query)
    scope, clause_number, numbers = None, None, ()
    try:
        discovery = build_discovery_plan(content)
    except ValueError:
        discovery = None
    if discovery is not None and (targets := discovery.explicit_targets(content)):
        scope = {"route": discovery.route, "region": discovery.region, "targets": targets}
        numbers = _explicit_reference_numbers(PUBLIC_CODE.sub(" ", content))
        if len(numbers) == 1:
            clause_number = numbers[0]
        # 明确书目和条号是出处查找；多号仍保留唯一条号字段为空，不猜选其中一个。
        if numbers and semantic.topic == "UNKNOWN":
            semantic = ModelSemanticProposal(topic="EXACT_REFERENCE", operation="REFERENCE_LOOKUP")
    supplied = request.conditions.model_dump(exclude_none=True)
    extracted = query_conditions(content)
    if semantic.topic == "BUILDING_HEIGHT" and semantic.operation == "DEFINITION" and not height_use_category(content):
        extracted.pop("building_use", None)
    height = (height_query_conditions(content)
        if semantic.topic == "BUILDING_HEIGHT" and semantic.operation == "MEASUREMENT_METHOD"
        else {})
    explicit = {**extracted, **daylight_query_conditions(content),
                **garage_query_conditions(content), **height, **supplied}
    conditions = QuestionConditions.model_validate(
        {**(confirmed_conditions or {}), **explicit}).model_dump(exclude_none=True)
    query_focus = (semantic.query_focus.bind(request.query, background_use=conditions.get("building_use"))
                   if semantic.query_focus is not None else None)
    requested = tuple(request.source_ids) if request.source_ids is not None else None
    effective = (requested if effective_source_ids is None else tuple(effective_source_ids))
    if effective is not None:
        for source_id in effective:
            if not re.fullmatch(r"(?:gb55031|official-[a-f0-9]{32})", source_id):
                raise ValueError("有效来源身份不合法")
        if requested is not None and not set(effective) <= set(requested):
            raise ValueError("有效来源不能扩大用户显式范围")
    provenance = tuple(row for row in condition_provenance if row["field"] not in explicit)
    return QuestionPlan(
        original_query=request.query, project_id=request.project_id,
        requested_source_ids=requested, effective_source_ids=effective,
        project_condition_ids=tuple(request.project_condition_ids or ()),
        topic=semantic.topic, operation=semantic.operation, focus=semantic.focus,
        understanding=semantic.understanding, conditions=conditions, public_conditions=supplied,
        condition_provenance=provenance, project_condition_fingerprint=project_condition_fingerprint,
        explicit_source_scope=scope, explicit_clause_number=clause_number,
        explicit_clause_numbers=numbers,
        requested_editions=discovery.requested_editions if discovery else (),
        planner_source=planner_source, query_focus=query_focus,
        public_search_titles=semantic.public_search_titles,
        # 未登记本地主题是能力缺口，不能据此断言用户表达含糊。
        # 仍保留原有非CLEAR拦截；原因不授权扩大证据或开始模型调用。
        understanding_reason=(None if semantic.understanding == "CLEAR" else
            "LOCAL_TOPIC_NOT_REGISTERED" if planner_source == "LOCAL_PLANNER_V1"
            and semantic.topic == "UNKNOWN" else "QUESTION_UNCERTAIN"))
