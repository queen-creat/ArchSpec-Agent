"""工作台只读匹配判断：原结果完整保留，不把词面分数当正确率或规范答案。"""

import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from archspec.official_discovery.plan import (
    DiscoveryPlan,
    build_discovery_plan,
    resolve_query_region,
)
from archspec.official_discovery.public_queries import PUBLIC_CODE, normalize_query
from archspec.official_documents.clause_units import ARTICLE
from archspec.official_documents.evidence_relations import DIRECTORY
from archspec.official_documents.named_units import MARKER
from archspec.official_documents.reading_scope import explicitly_excluded, scoped_object_match
from archspec.official_documents.reference_relevance import phrase_match as _phrase_match
from archspec.official_documents.reference_relevance import phrases as _phrases
from archspec.official_qa.condition_policies import ConditionPolicy, policy_for
from archspec.official_qa.fire_scope import FAMILIES
from archspec.official_qa.intents import height_use_category
from archspec.official_qa.question_plan import LocalPlanner, QuestionPlan, build_question_plan
from archspec.official_qa.schemas import QuestionRequest

VERSION = "LOOKUP_CONTEXT_V1"
CLAUSE = re.compile(r"\d+(?:\.\d+){2,3}")
# 通用问句与检索动作不是工程主题，禁止靠这些公共字词自动展开条文。
COMMON = frozenset(
    (
        "建筑",
        "规范",
        "标准",
        "设计",
        "要求",
        "规定",
        "条文",
        "查找",
        "查询",
        "如何",
        "怎么",
        "多少",
        "什么",
        "哪些",
        "有关",
        "相关",
        "是否",
        "按照",
        "根据",
        "可以",
        "能否",
        "应当",
        "应为",
        "请问",
        "我想",
        "帮我",
        "给我",
        "需要",
        "限制",
    )
)
QUESTION_FILLERS = re.compile(
    r"请问|帮我|我想|查找|查询|多少|什么|哪些|怎么|如何|是否|有关|相关|[的呢吗啊？?]"
)



class _Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LookupSourceBinding(_Observation):
    source_id: Annotated[str, Field(min_length=1, max_length=100)]
    snapshot_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    metadata_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None


class LookupSourceObservation(_Observation):
    state: Literal["READABLE", "MISSING", "UNVERIFIED", "RESTRICTED"]
    scope: Literal["REGION", "EXPLICIT_SOURCE", "CACHE"]
    bindings: Annotated[list[LookupSourceBinding], Field(max_length=20)]

    @model_validator(mode="after")
    def bound_readability(self):
        identities = [(item.source_id, item.snapshot_id) for item in self.bindings]
        if len(set(identities)) != len(identities) or bool(self.bindings) != (self.state == "READABLE"):
            raise ValueError("可读正文观察必须绑定去重固定来源；未知/缺失不能借历史身份")
        return self


class LookupCandidateObservation(_Observation):
    state: Literal["MATCHED", "UNCONFIRMED", "NONE"]
    matched_count: Annotated[int, Field(ge=0, le=40)]
    unconfirmed_count: Annotated[int, Field(ge=0, le=40)]

    @model_validator(mode="after")
    def same_partition(self):
        state = "MATCHED" if self.matched_count else "UNCONFIRMED" if self.unconfirmed_count else "NONE"
        if self.state != state:
            raise ValueError("候选观察与对应/关联分区计数不一致")
        return self


class LookupUnderstandingObservation(_Observation):
    state: Literal["CLEAR", "UNCERTAIN"]
    topic: Annotated[str, Field(min_length=1, max_length=80)]
    operation: Annotated[str, Field(min_length=1, max_length=80)]


class LookupConditionsObservation(_Observation):
    state: Literal["NEEDS_DETAILS", "NOT_REQUESTED"]
    missing_fields: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]], Field(max_length=30)]

    @model_validator(mode="after")
    def needs_details(self):
        if bool(self.missing_fields) != (self.state == "NEEDS_DETAILS"):
            raise ValueError("条件观察仅消费原政策的实际缺项")
        return self


class LookupEvidenceObservation(_Observation):
    state: Literal["NOT_EVALUATED"]
    applicability_state: Literal["NOT_VERIFIED"]


class LookupValidityObservation(_Observation):
    state: Literal["VERIFIED", "UNVERIFIED", "MIXED"]
    verified_count: Annotated[int, Field(ge=0, le=20)]
    unverified_count: Annotated[int, Field(ge=0, le=20)]

    @model_validator(mode="after")
    def actual_counts(self):
        state = ("MIXED" if self.verified_count and self.unverified_count else
                 "VERIFIED" if self.verified_count else "UNVERIFIED")
        if state != self.state:
            raise ValueError("版本观察计数不一致；核验状态也不代表现行")
        return self


class LookupRecognitionObservation(_Observation):
    state: Literal["NO_RECORDED_DOUBTS", "HAS_DOUBTS", "UNVERIFIED"]
    low_count: Annotated[int, Field(ge=0, le=4_000_000)] | None

    @model_validator(mode="after")
    def scoped_count(self):
        state = ("UNVERIFIED" if self.low_count is None else
                 "HAS_DOUBTS" if self.low_count else "NO_RECORDED_DOUBTS")
        if self.state != state:
            raise ValueError("识别观察只统计本次候选片段；未知不能用零消除提醒")
        return self


class LookupObservations(_Observation):
    """查阅观察不是回答充分性或许可：七维分开，绝不改原文/条件/引用门。"""

    version: Literal["LOOKUP_OBSERVATIONS_V1"]
    source: LookupSourceObservation
    candidates: LookupCandidateObservation
    understanding: LookupUnderstandingObservation
    conditions: LookupConditionsObservation
    evidence: LookupEvidenceObservation
    validity: LookupValidityObservation
    recognition: LookupRecognitionObservation

    @model_validator(mode="after")
    def same_source_units(self):
        if self.validity.verified_count + self.validity.unverified_count != len(self.source.bindings):
            raise ValueError("版本观察按本次去重固定来源计数，不能拿候选数替代")
        return self


def checked_observations(value):
    """共享当前/历史边界的纯验证器；旧记录缺观察时保持未知，不推断成功。"""
    return LookupObservations.model_validate(value).model_dump()


def _snapshot_identity(row):
    source, pin = row.get("source_id"), row.get("snapshot_id")
    if (not isinstance(source, str) or not 0 < len(source) <= 100
            or not isinstance(pin, str) or not re.fullmatch(r"[a-f0-9]{64}", pin)):
        return None
    return source, pin


def _metadata_identity(metadata, row):
    return (isinstance(metadata, dict) and _snapshot_identity(row) is not None
            and all(metadata.get(key) == row.get(key) for key in ("source_id", "snapshot_id"))
            and all(key not in metadata or metadata[key] == row.get(key)
                    for key in ("title", "standard_code")))


def _source_observation(result, rows, *, explicit, region, discovery, require_main=False):
    """只消费同请求业务覆盖和固定档案；主题不认识不能推出正文缺失。"""
    scope = "REGION" if region else "EXPLICIT_SOURCE" if explicit else "CACHE"
    coverage = result.get("coverage")
    entries = coverage if isinstance(coverage, list) and coverage else [
        {**row, "state": "SEARCHED"} for row in rows]
    known_coverage = isinstance(coverage, list) and bool(coverage)
    bindings, observed, unknown, restricted = {}, {}, False, False
    for entry in entries:
        if not isinstance(entry, dict):
            unknown = True
            continue
        if explicit and discovery.match_source(entry.get("standard_code"), entry.get("title")) != "TARGET":
            continue
        state = entry.get("state")
        matches = [row for row in rows if row.get("source_id") == entry.get("source_id")]
        facts = entry
        if _snapshot_identity(facts) is None and matches:
            facts = {**entry, "snapshot_id": matches[0].get("snapshot_id")}
        fixed = entry.get("source_metadata")
        if fixed is None:
            fixed = next((row.get("source_metadata") for row in matches
                          if row.get("snapshot_id") == facts.get("snapshot_id")), None)
        if state == "RESTRICTED":
            restricted = True
            continue
        if state not in {"SEARCHED", "EXCLUDED_METADATA"}:
            unknown |= state not in {"NOT_ACQUIRED", "EXCLUDED_ATTACHMENT", "EXCLUDED_FIXED_SCOPE"}
            continue
        if region is not None:
            if not _metadata_identity(fixed, facts):
                unknown = True
                continue
            fields = fixed.get("fields") if isinstance(fixed.get("fields"), dict) else {}
            field = fields.get("region") if isinstance(fields.get("region"), dict) else {}
            level = fields.get("level") if isinstance(fields.get("level"), dict) else {}
            if not _region_matches({**facts, "source_metadata": fixed}, region):
                from archspec.official_documents.metadata_filter import SourceMetadataFilter
                expected_region = SourceMetadataFilter(region=region).resolved_region
                known_other = (field.get("state") == "NOTICE_OBSERVED"
                               and isinstance(field.get("value"), str) and field["value"] != expected_region
                               or level.get("state") in {"CODE_CLUE", "NOTICE_OBSERVED"}
                               and level.get("value") in {"NATIONAL", "INDUSTRY"}
                               and field.get("state") == "UNKNOWN" and field.get("value") is None)
                unknown |= not known_other
                continue
        # 先核固定地域档案，再核文书角色；全国正文不会被误记为未知地方资料。
        if require_main:
            role = discovery.match_source(entry.get("standard_code"), entry.get("title"))
            if role != "TARGET":
                unknown |= role == "UNKNOWN"
                continue
        if state == "EXCLUDED_METADATA":
            # 地域相符但因其它筛选未纳入，不把未知效力或强条筛选说成文件丢失。
            unknown = True
            continue
        identity = _snapshot_identity(facts)
        if identity is None:
            unknown = True
            continue
        metadata_id = fixed.get("metadata_id") if _metadata_identity(fixed, facts) else None
        if not isinstance(metadata_id, str) or not re.fullmatch(r"[a-f0-9]{64}", metadata_id):
            metadata_id = None
        bindings[identity] = {"source_id": identity[0], "snapshot_id": identity[1], "metadata_id": metadata_id}
        current = entry.get("source_validity")
        if current is None:
            current = next((row.get("source_validity") for row in matches
                            if row.get("snapshot_id") == identity[1]), None)
        observed[identity] = current
    state = ("READABLE" if bindings else "RESTRICTED" if restricted else
             "UNVERIFIED" if unknown or not entries else "MISSING"
             if known_coverage or explicit or rows else "UNVERIFIED")
    return {"state": state, "scope": scope, "bindings": list(bindings.values())}, observed


def _recognition_observation(rows):
    if not rows:
        return {"state": "UNVERIFIED", "low_count": None}
    total = 0
    for row in rows:
        risk = row.get("recognition")
        if (not isinstance(risk, dict) or risk.get("scope") != "CURRENT_FRAGMENT"
                or any(type(risk.get(key)) is not int or not 0 <= risk[key] <= 100_000
                       for key in ("low_count", "other_low_count", "unlocated_count", "omitted_count"))
                or not isinstance(risk.get("issues"), list) or len(risk["issues"]) > 20
                or risk["low_count"] != len(risk["issues"]) + risk["omitted_count"]
                or risk["unlocated_count"] > risk["low_count"]):
            return {"state": "UNVERIFIED", "low_count": None}
        total += risk["low_count"]
    return {"state": "HAS_DOUBTS" if total else "NO_RECORDED_DOUBTS", "low_count": total}


def _lookup_observations(prepared, result, rows, primary, related, *, explicit, related_only=False):
    # 主动关联只观察本次真实缓存读取，不能冒充原项目地域或指定书目已具备。
    source, current = _source_observation(result, rows, explicit=explicit and not related_only,
        region=None if related_only else prepared.region, discovery=prepared.discovery_plan,
        require_main=bool(not related_only and prepared.region and prepared.policy.local_spacing))
    verified = 0
    for binding in source["bindings"]:
        view = current.get((binding["source_id"], binding["snapshot_id"]))
        status = view.get("version_status") if isinstance(view, dict) else None
        if (isinstance(status, dict) and status.get("verification_state") == "VERIFIED"
                and status.get("value") in {"CURRENT", "UPCOMING", "SUPERSEDED", "WITHDRAWN"}
                and all(view.get(key) == binding[key] for key in ("source_id", "snapshot_id"))
                and (binding["metadata_id"] is None or view.get("metadata_id") == binding["metadata_id"])):
            verified += 1
    unverified = len(source["bindings"]) - verified
    issues = prepared.policy.pre_search_issues(prepared.question_plan.conditions)
    return checked_observations({"version": "LOOKUP_OBSERVATIONS_V1", "source": source,
        "candidates": {"state": "MATCHED" if primary else "UNCONFIRMED" if related else "NONE",
                       "matched_count": len(primary), "unconfirmed_count": len(related)},
        "understanding": {"state": prepared.question_plan.understanding,
                          "topic": prepared.question_plan.topic, "operation": prepared.question_plan.operation},
        "conditions": {"state": "NEEDS_DETAILS" if issues else "NOT_REQUESTED",
                       "missing_fields": list(issues)},
        "evidence": {"state": "NOT_EVALUATED", "applicability_state": "NOT_VERIFIED"},
        "validity": {"state": "MIXED" if verified and unverified else "VERIFIED" if verified else "UNVERIFIED",
                     "verified_count": verified, "unverified_count": unverified},
        "recognition": _recognition_observation(rows)})

def _compact(text):
    return re.sub(r"\s+", "", normalize_query(str(text or "")))



def _parallel_requirements(query, question):
    """只拆明确并列的2—4个要求；不拆用途枚举、标准题名或推断项目条件。"""
    pieces = re.split(r"、|以及|和|[，,]", query)
    if not 2 <= len(pieces) <= 4:
        return []
    subject, marker, first = pieces[0].partition("的")
    shared = subject if marker and len(subject) <= 16 else ""
    if shared:
        pieces[0] = first
    labels = [re.sub(r"分别|各自|查哪条规范|查哪里|有什么要求|有什么规定|怎么规定|如何规定|一样吗|有哪些要求", "", part).strip(" ？?")
              for part in pieces]
    dimensions = r"宽|高|坡度|长度|数量|面积|尺寸|踏步|平台|栏杆|出口|距离|间距"
    if any(len(label) < 2 or not re.search(dimensions, label) for label in labels):
        return []
    parts = []
    for label in labels:
        part_query = f"{shared}的{label}" if shared else label
        prepared = prepare_lookup_plan(part_query)
        # 子要求只消费同一原问已有条件；不回写工程、原问题或回答计划。
        child = replace(prepared.question_plan, conditions={**prepared.question_plan.conditions, **question.conditions})
        # 尺寸/多宽是问法词，不要求规范原文也逐字写“尺寸”；不补任何数值。
        words = label.replace("尺寸", "宽度 高度").replace("多宽", "宽度").replace("多高", "高度")
        words = re.sub(r"最大|最小|要", "", words)
        context = shared or question.conditions.get("building_use", "")
        dimension = re.search(r"净高|净宽|宽度|高度|坡度|长度|数量|面积|距离|间距", words)
        subject = words[:dimension.start()].strip() if dimension else (
            words if re.search(r"楼梯(?:踏步|平台)?$", words) else "")
        parts.append({"query": label, "object": subject, "words": words,
                      "terms": _phrases(words), "context": _phrases(shared),
                      "retrieval_query": f"{context} {words}".strip(),
                      "region": prepared.region,
                      "question": child, "policy": policy_for(child), "primary_indices": []})
    return parts


def _part_matches(part, text, title, use, number):
    from archspec.official_qa.fire_scope import fire_use_conflict
    if (fire_use_conflict(use, text) or
            explicitly_excluded(part["question"].original_query, text)):
        return False
    if part["question"].topic != "UNKNOWN":
        return _operation_matches(part["policy"], part["question"], text, number, source_title=title)
    # “尺寸”的宽/高是可选检索词，不能拼成必须同时覆盖的连续双字窗口。
    scoped = scoped_object_match(part["object"], text)
    if scoped is False:
        return False
    size_object = (scoped is True or "尺寸" in part["query"] and part["object"]
                   and _compact(part["object"]) in text)
    if not size_object and not _phrase_match(part["terms"], text):
        return False
    # 对象和属性分别核对；“通道宽度”不能只因双字重叠就替代“走道宽度”。
    if part["object"] and scoped is not True and _compact(part["object"]) not in text:
        return False
    attributes = {"宽度": r"宽", "高度": r"高度|净高|层高|高不|高应|高宜",
                  "坡度": r"坡度", "长度": r"长度|长不|长应|长宜", "面积": r"面积"}
    requested = [key for key in attributes if key in part["words"]]
    # “尺寸”可由宽/高规定对应，不要求规范原文出现问法中的“尺寸”。
    if requested and not any(re.search(attributes[key], text) for key in requested):
        return False
    # 问数量/上下限时需有相应规定语言；定义或仅提到该对象不冒称满足这一要求。
    if "数量" in part["query"] and not re.search(r"数量|[个处]|至少|不应少于|不宜少于", text):
        return False
    if "最大" in part["query"] and not re.search(r"最大|不[应宜得]?大于|不得超过|不超过|上限", text):
        return False
    if "最小" in part["query"] and not re.search(r"最小|不[应宜得]?小于|不应少于|至少|下限", text):
        return False
    context = text + _compact(title)
    # 已明确车库时，不能把地铁/其它设施的出口当作该子要求；未核工程适用性仍保持未知。
    if "车库" in use and "车库" not in context:
        return False
    requested = part["question"].conditions.get("fire_context", part["question"].original_query)
    if any(word in requested for word in ("汽车库", "机动车库")) and "非机动车库" not in requested:
        subject = re.split(r"[。；;：:]|不应|不得|应|宜", text, maxsplit=1)[0][:180]
        if "非机动车库" in subject and "机动车库" not in subject.replace("非机动车库", ""):
            return False
    # 未识别用途的明确共同对象仍须在正文或固定书名中对应，例如无障碍坡道。
    return not part["context"] or bool(use) or _phrase_match(part["context"], context)


def _region_matches(item, region):
    metadata = item.get("source_metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("source_id") != item.get("source_id")
        or metadata.get("snapshot_id") != item.get("snapshot_id")
    ):
        return False
    fields = metadata.get("fields")
    if not isinstance(fields, dict):
        return False
    field, level = fields.get("region"), fields.get("level")
    if not isinstance(field, dict) or not isinstance(level, dict):
        return False
    from archspec.official_documents.metadata_filter import SourceMetadataFilter

    return (
        field.get("state") == "NOTICE_OBSERVED"
        and level.get("state") == "NOTICE_OBSERVED"
        and level.get("value") == "LOCAL"
        and region != "地域待补充"
        and field.get("value") == SourceMetadataFilter(region=region).resolved_region
    )


def _operation_matches(policy, question, text, number, *, source_title=""):
    """读取候选复用问答政策的主语/操作检查，不把面宽、楼梯或标题当高度定义。"""
    named = (
        policy.local_spacing
        and number is None
        and MARKER.match(text.strip())
        and not DIRECTORY.search(text)
    )
    # 地方正式文件常用“第×条”；这里只承认阅读形态，不改原条号或QA完整单元。
    article = ((ARTICLE.fullmatch(number or "") or
                number is None and ARTICLE.match(text.strip()))
               and not DIRECTORY.search(text))
    if not policy.is_body(text, number) and not named and not article:
        return False
    conditions = question.conditions
    variants = [conditions]
    if policy.highrise_definition and not height_use_category(conditions.get("building_use", "")):
        # 用途未填仍可看到分类出处。这里只检查现有政策中至少一种分类是否出现，
        # 绝不将阅读分类回写为用户项目条件，也不输出一个通用高度数值。
        variants = [
            {**conditions, "building_use": words[0]}
            for words in FAMILIES.values()
            if height_use_category(words[0])
        ]
    return any(
        policy.matches_reading_question(text, scope, source_title=source_title) and policy.accept_full_clause(text, scope, source_title=source_title)
        for scope in variants
    )



def _reading_source_matches(prepared, item):
    """查阅标注与初读择取共用主文件角色；地域/许可仍由各自固定读取门核验。"""
    discovery, policy = prepared.discovery_plan, prepared.policy
    explicit = bool(discovery and discovery.explicit_targets(prepared.original_query))
    return prepared.question_plan.matches_explicit_source(item) and (not (explicit or prepared.region and policy.local_spacing)
            or discovery.match_source(item.get("standard_code"), item.get("title")) == "TARGET") and policy.matches_source_title(
                item.get("title", ""), prepared.question_plan.conditions)


def _preferred_reading_indices(prepared, snapshot):
    """只为读取择取完整同条主题信号；不签发完整性、适用性或引用资格。"""
    from archspec.official_documents.clause_units import clause_runs

    if not _reading_source_matches(prepared, snapshot):
        return set()
    from archspec.official_documents.table_lookup import table_caption_indices, table_lookup_subject

    if table_lookup_subject(prepared.question_plan.search_query) is not None:
        return table_caption_indices(prepared.question_plan.search_query, snapshot, prepared.question_plan.conditions)
    from archspec.official_documents.classification_lookup import (
        classification_indices,
        classification_subject,
    )

    if classification_subject(prepared.question_plan.search_query) is not None:
        return classification_indices(prepared.question_plan.search_query, snapshot)
    rows = snapshot["chunks"]
    runs = clause_runs(rows)
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[runs.get(index, (index,))[0]].append((index, row))
    preferred = set()
    from archspec.official_documents.reading_scope import direct_statement_pairs

    terms = prepared.question_plan.local_reading_terms
    for members in groups.values():
        # 术语只在同一陈述内共同出现才影响阅读顺序，不能跨对象借词。
        original = "\n".join(row["text"] for _, row in members)
        if terms and any(
                all(word in _compact(statement) for word in term.split())
                for _, statement in direct_statement_pairs(original) for term in terms):
            preferred.update(index for index, _ in members)
        text = _compact("\n".join(row["text"] for _, row in members))
        if _operation_matches(prepared.policy, prepared.question_plan, text, members[0][1].get("clause_number"), source_title=snapshot.get("title", "")):
            preferred.update(index for index, _ in members)
    return preferred


@dataclass(frozen=True)
class LookupPlan:
    """仅供后端传递的一次准备结果；嵌套合同亦不可变，不接受浏览器计划。"""

    original_query: str
    normalized_query: str
    question_plan: QuestionPlan
    policy: ConditionPolicy
    discovery_plan: DiscoveryPlan | None

    def __post_init__(self):
        # 原问逐字绑定，归一化相同的两种输入也不能交叉借用同一计划。
        if (
            not isinstance(self.original_query, str)
            or not isinstance(self.normalized_query, str)
            or not 1 <= len(self.normalized_query) <= 500
            or normalize_query(self.original_query) != self.normalized_query
            or not isinstance(self.question_plan, QuestionPlan)
            or self.question_plan.original_query != self.normalized_query
            or not isinstance(self.policy, ConditionPolicy)
            or self.policy.original_query != self.normalized_query
            or (self.policy.topic, self.policy.operation, self.policy.focus)
            != (self.question_plan.topic, self.question_plan.operation, self.question_plan.focus)
            or (
                self.discovery_plan is not None
                and not isinstance(self.discovery_plan, DiscoveryPlan)
            )
        ):
            raise ValueError("查阅计划与本次原问题或共同条件政策不一致")

    @property
    def query(self) -> str:
        return self.original_query

    @property
    def region(self) -> str | None:
        return self.discovery_plan.region if self.discovery_plan is not None else None

    @property
    def parallel_parts(self) -> tuple:
        if self.question_plan.explicit_clause_numbers:
            return ()
        return tuple(_parallel_requirements(self.question_plan.search_query, self.question_plan))

    @property
    def parallel_queries(self) -> tuple:
        """有界子检索仅由同一后端计划生成；条号继续沿原精确选择合同。"""
        return tuple(part["retrieval_query"] for part in self.parallel_parts)

    @property
    def retrieval_query(self) -> str:
        """只消费共同计划已确认的唯一条号，不从多条号中另猜一个目标。"""
        return self.question_plan.explicit_clause_number or self.legacy_retrieval_query

    @property
    def legacy_retrieval_query(self) -> str:
        """保留原RAW项目查阅的概念/原词合同；明确条号改写仅由PLANNED选择。"""
        if (self.region is not None and self.policy.local_spacing
                and not CLAUSE.search(self.question_plan.search_query)
                and not PUBLIC_CODE.search(self.question_plan.search_query)):
            return self.policy.concept_query(self.question_plan.conditions) or self.question_plan.search_query
        return self.question_plan.search_query

    def public_dict(self) -> dict:
        """公开副本沿用旧发现合同，不公开原问、条件对象或内部解释器。"""
        if self.discovery_plan is not None:
            public = deepcopy(self.discovery_plan.public_dict())
            if len(self.question_plan.explicit_clause_numbers) > 1:
                public["explicit_clause_numbers"] = list(self.question_plan.explicit_clause_numbers)
            return public
        return {
            "version": "DISCOVERY_PLAN_V1",
            "route": "PUBLIC_WORDS_NOT_IDENTIFIED",
            "region": None,
            "targets": [],
            "queries": [],
            "omitted_query_count": 0,
        }


def prepare_lookup_plan(query: str, *, project_region=None, region=None, source_ids=None,
                        public_titles=()) -> LookupPlan:
    """只准备一次既有解释与发现计划，不增建地域词表、不调用外部模型。"""
    if not isinstance(query, str):
        raise ValueError("查阅计划的问题必须是文字")  # noqa: TRY004 - 统一输入校验合同
    normalized = normalize_query(query)
    semantic = LocalPlanner().propose(normalized, {})
    question = build_question_plan(QuestionRequest(query=normalized), semantic)
    if source_ids is not None:
        # 范围由原检索入口校验；计划只保留原选择，不提前改写404等既有输入行为。
        question = replace(question, requested_source_ids=tuple(source_ids))
    try:
        # 项目公开省市仅补充地方主题；原问的明确地域仍优先。
        if region is None and question.topic in {"SUNLIGHT_DURATION", "SUNLIGHT_SPACING"}:
            region = resolve_query_region(normalized, project_region=project_region)
        discovery = build_discovery_plan(normalized, region=region, public_titles=public_titles)
    except ValueError:
        # 无公共发现词仍可本地查词组；不补出一个虚构的地方或规范目标。
        discovery = None
    policy = policy_for(question, reference_region=discovery.region if discovery else None)
    return LookupPlan(query, normalized, question, policy, discovery)


def lookup_context(query, result, *, plan=None, source_ids=None, related_only=False):
    """消费同一发现目标及问题主题，返回原结果索引分区；不下载、识别或调用模型。"""
    if plan is None:
        # 单书响应本身已绑定source_id，不能当作不限书目的全库请求。
        selected = source_ids
        if selected is None and isinstance(result.get("source_id"), str):
            selected = [result["source_id"]]
        prepared = prepare_lookup_plan(query, source_ids=selected)
    elif not isinstance(plan, LookupPlan) or plan.original_query != query:
        raise ValueError("查阅计划不属于本次原问题")
    else:
        prepared = plan
    normalized = prepared.normalized_query
    question, policy = prepared.question_plan, prepared.policy
    topic = question.topic
    discovery = prepared.discovery_plan
    public = prepared.public_dict()
    # 查阅和问答共用明确来源判定，不能把推导出的题名当用户指定书目。
    explicit = bool(discovery and discovery.explicit_targets(query))
    region = public["region"]
    rows = result.get("results", [])
    # 来源身份和正文主题是两种约束；正文不必把自己的书名/编号再写一次。
    subject_query = PUBLIC_CODE.sub(" ", normalized) if explicit else normalized
    if explicit:
        for target in public["targets"]:
            if target["kind"] in {"TITLE", "LOCAL_TOPIC"}:
                subject_query = subject_query.replace(target["value"], " ")
    terms = _phrases(subject_query)
    exact = CLAUSE.fullmatch(normalized.strip()) or ARTICLE.fullmatch(normalized.strip())
    # 只消费共同计划已绑定的条号，中文与数字同用来源交集和精确比较。
    numbered = question.explicit_clause_numbers if explicit else ()
    parts = [] if exact or numbered else _parallel_requirements(subject_query, question)
    # 同号续文只在同书同快照内共享主题；没有完整条号的片段不能跨条合并。
    groups = defaultdict(list)
    for index, item in enumerate(rows):
        number = item.get("clause_number")
        key = (
            item.get("source_id"),
            item.get("snapshot_id"),
            number if isinstance(number, str) and CLAUSE.fullmatch(number) else index,
        )
        fragments = item.get("fragments")
        text = (
            "\n".join(
                [
                    str(item.get("text", "")),
                    *(
                        part["text"]
                        for part in fragments
                        if isinstance(part, dict)
                        and part.get("source_id") == item.get("source_id")
                        and part.get("snapshot_id") == item.get("snapshot_id")
                        and part.get("clause_number") == number
                        and isinstance(part.get("text"), str)
                    ),
                ]
            )
            if isinstance(fragments, list)
            else str(item.get("text", ""))
        )
        groups[key].append((index, item, text))
    primary = []
    for members in groups.values():
        text = _compact("\n".join(row[2] for row in members))
        for index, item, _ in members:
            # 地方计算题的主文件角色与发现入口相同；监督结果仍可关联阅读，
            # 但不能借同地域档案成为规范正文可读或对应候选的证明。
            source_ok = _reading_source_matches(prepared, item)
            local_ok = region is None or _region_matches(item, region)
            if exact:
                subject_ok = item.get("clause_number") == exact.group()
            elif numbered:
                subject_ok = item.get("clause_number") in numbered
            elif parts:
                subject_ok = False
                if source_ok and local_ok:
                    for part in parts:
                        if _part_matches(part, text, item.get("title", ""), question.conditions.get("building_use", ""),
                                         item.get("clause_number")):
                            part["primary_indices"].append(index)
                            subject_ok = True
            elif topic != "UNKNOWN" or question.query_focus is not None:
                # 已明确对象属性时，附注提到对象不能把错主语标成对应候选。
                # 同号片段沿本组共同核对；此处仍不签发完整性或工程适用证明。
                subject_ok = _operation_matches(policy, question, text, item.get("clause_number"), source_title=item.get("title", ""))
            elif explicit and not terms:
                subject_ok = True
            else:
                subject_ok = policy.matches_reading_question(text, question.conditions)
            if source_ok and local_ok and subject_ok:
                primary.append(index)
    primary.sort()
    if related_only:
        primary = []  # 用户主动查看的跨范围原文只作关联，不成为对应或充分证据。
        for part in parts:
            part["primary_indices"] = []
    primary_set = set(primary)
    related = [index for index in range(len(rows)) if index not in primary_set]
    coverage = result.get("coverage", [])
    missing = (
        explicit
        and not any(
            row.get("state") == "SEARCHED"
            and discovery.match_source(row.get("standard_code"), row.get("title")) == "TARGET"
            for row in coverage
        )
        if coverage
        else explicit
        and not any(
            discovery.match_source(row.get("standard_code"), row.get("title")) == "TARGET"
            for row in rows
        )
    )
    from archspec.official_qa.evidence_requirements import (
        explicit_clause_requirements,
        explicit_source_requirements,
    )

    # 原覆盖可保留尚未准备的登记项；无coverage的旧单书结果只绑定实际响应来源。
    entries = coverage or [{**row, "state": "SEARCHED"} for row in rows]
    source_requirements = () if related_only else explicit_source_requirements(question, entries)
    source_gaps = [row for row in source_requirements if row.state != "FOUND_COMPLETE"]
    clause_requirements = () if related_only else explicit_clause_requirements(question, results=rows,
        clause_selection=result.get("clause_selection"))
    observations = _lookup_observations(prepared, result, rows, primary, related,
                                        explicit=explicit, related_only=related_only)
    source_state = observations["source"]["state"]
    state = (
        "NO_MATCH"
        if related_only
        else "MISSING_SOURCE"
        if any(row.source_state in {"MISSING_SOURCE", "NOT_ACQUIRED", "EXCLUDED_FIXED_SCOPE"}
               for row in source_gaps)
        else "NO_MATCH"
        if source_gaps
        else "MATCHED"
        if primary
        else "REGION_GAP"
        if region and source_state == "MISSING"
        else "MISSING_SOURCE"
        if missing and source_state == "MISSING"
        else "NO_MATCH"
    )
    notes = [
        "候选只表示主题或词组对应；版本、适用条件、效力和识别文字仍须对照固定出处核验。",
        "相关度不是正确率；未列为对应候选的原文仍保留，可在关联候选中查阅。",
    ]
    if policy.highrise_definition and policy.pre_search_issues(question.conditions):
        notes.insert(
            0,
            "用途尚未明确；这里只列分类出处，确定住宅、公共建筑或工业用途后再读取相应条件，未给出统一高度数值。",
        )
    if state == "REGION_GAP":
        notes.insert(0, f"当前未找到能核对到{region}的对应地方正文，全国相似条文不能替代地方要求。")
    elif state == "MISSING_SOURCE":
        notes.insert(0, "指定规范的对应正文尚未进入此次可读范围，不能用其他书的相同条号替代。")
    elif state == "NO_MATCH":
        notes.insert(0, ("已取得本次范围的可读正文，候选与问题的对应关系尚待核对；不能据此作规范事实回答。"
                        if source_state == "READABLE" else
                        "本次正文可读范围尚未核对；候选原文仍保留，不能据此认定缺少规范要求。"))
    elif topic == "UNKNOWN":
        notes.insert(0, "本次按实质词组查找，尚未识别专门问答主题；不推断具体限值。")
    if source_gaps:
        notes.insert(0, "；".join(row.reason for row in source_gaps))
    if clause_requirements:
        notes.insert(0, "；".join(row.reason for row in clause_requirements))
    if related_only:
        notes.insert(0, "本次为用户主动查看的缓存关联参考；不替代项目地域或原问指定规范，不据此认定证据已齐。")
    if parts:
        found = sum(bool(part["primary_indices"]) for part in parts)
        notes.insert(0, f"并列要求：{found}/{len(parts)}部分已有候选，"
            + ("其余部分未查齐；" if found < len(parts) else "各部分均有候选；")
            + "候选对应不表示证据充分或工程适用性已核验。")
    return {
        "version": VERSION,
        "state": state,
        **({"question_parts": [{"query": part["query"], "primary_indices": part["primary_indices"]}
                               for part in parts]} if parts else {}),
        **({"scope": "RELATED"} if related_only else {}),
        **({"evidence_requirements": [row.model_dump(mode="json", exclude_none=True)
                                     for row in (*source_requirements, *clause_requirements)]}
           if source_requirements or clause_requirements else {}),
        "topic": topic,
        "operation": question.operation,
        "primary_indices": primary,
        "related_indices": related,
        "public_plan": deepcopy(public),
        "observations": observations,
        "notes": notes,
    }


def attach_lookup(query, result, *, plan=None, source_ids=None, related_only=False):
    return {**result, "lookup": lookup_context(query, result, plan=plan, source_ids=source_ids,
                                            related_only=related_only)}
