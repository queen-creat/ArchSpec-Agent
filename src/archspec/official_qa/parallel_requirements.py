"""并列要求共用一次有限执行和固定证据包；不另开子任务、模型或读取预算。"""
import re
from dataclasses import replace

from archspec.official_qa.condition_policies import policy_for
from archspec.official_qa.evidence_requirements import (
    EvidenceRequirement,
    _bound_fragments,
    _core_target,
)

COMPLETENESS = 'PARALLEL_CORE_UNITS'


def lookup_for_answer(plan, source_ids, conditions):
    from archspec.official_documents.lookup import prepare_lookup_plan
    lookup = prepare_lookup_plan(plan.original_query, source_ids=source_ids)
    child = replace(lookup.question_plan, conditions=conditions,
                    public_conditions=plan.public_conditions)
    lookup = replace(lookup, question_plan=child, policy=policy_for(child, reference_region=lookup.region))
    return lookup if lookup.parallel_queries else None


def matches(parts, text, number, owner, conditions):
    from archspec.official_documents.lookup import _part_matches, _region_matches
    compact = re.sub(r'\s+', '', text)
    return any((part.get('region') is None or _region_matches(owner, part['region']))
               and _part_matches(part, compact, owner.get('title', ''),
                                 conditions.get('building_use', ''), number) for part in parts)


def requirements(parts, contexts, conditions):
    """目标只来自本次实际固定核心；预算遗漏会由证据包再判，不能删除缺失要求。"""
    rows = []
    for index, part in enumerate(parts, 1):
        targets, seen = [], set()
        for context in contexts:
            fragments = _bound_fragments(context)
            core = _core_target(context, fragments)
            if core is None:
                continue
            text = '\n'.join(fragments[key]['text'] for key in core['chunk_ids'])
            number = context['selected_clause_coverage']['clause_number']
            if matches((part,), text, number, context, conditions):
                identity = core['source_id'], core['snapshot_id'], core['unit_key']
                if identity not in seen:
                    seen.add(identity)
                    targets.append(core)
        rows.append(EvidenceRequirement(f'parallel-part:{index}', kind='CORE_BODY',
            subject=part['query'], reason='此并列要求须有本轮实际核读的完整固定正文，不能借其他要求代答。',
            completeness=COMPLETENESS, fixed_targets=tuple(targets)))
    return tuple(rows)


def required_short_quotes(bundle):
    """每个已核齐且可短摘录的要求至少保留一整条；超长条文继续保留阅读入口。"""
    ledger = {row['chunk_id']: row for row in bundle.usable if row['quote_ready']}
    required = set(bundle.required_evidence_ids)
    for row in bundle.requirements:
        if row.completeness != COMPLETENESS or row.state != 'FOUND_COMPLETE':
            continue
        for target in row.fixed_targets:
            ids = target.get('chunk_ids', ())
            parts = [ledger[key] for key in ids if key in ledger]
            if (target.get('read_complete') and ids and len(parts) == len(ids)
                    and all(len(part['text']) <= 1200 for part in parts)
                    and sum(len(part['text']) for part in parts) <= 1800):
                required.update(part['evidence_id'] for part in parts)
                break
    return frozenset(required)


def describe_result(result, bundle):
    """摘录覆盖和读到证据分开；最终守卫失败时不重新显示旧来源或部分成功。"""
    if bundle is None or result['status'] == 'EVIDENCE_CHECK_FAILED':
        return result
    parts = [row for row in bundle.requirements if row.completeness == COMPLETENESS]
    if not parts:
        return result
    chosen = {item['citation']['evidence_id'] for item in result['answer_items']}
    selected = []
    for row in parts:
        complete = any(target.get('read_complete') and target.get('chunk_ids')
            and {e['evidence_id'] for e in bundle.evidence if e['chunk_id'] in target['chunk_ids']} <= chosen
            and len({e['chunk_id'] for e in bundle.evidence if e['chunk_id'] in target['chunk_ids']})
                == len(target['chunk_ids']) for target in row.fixed_targets)
        selected.append({'query': row.subject, 'evidence_state': row.state,
                         'reference_state': 'REFERENCED' if complete else
                            'READING_ONLY' if row.state == 'FOUND_COMPLETE' else 'UNRESOLVED'})
    result['answer_parts'] = selected
    referenced = sum(row['reference_state'] == 'REFERENCED' for row in selected)
    result['summary'] = (f'并列问题：{referenced}/{len(parts)}部分已列原文摘录；'
        + ('其余部分仍需核对出处或完整原文。' if referenced < len(parts)
           else '各项仍须按完整条件、版本及适用范围核对。'))
    if referenced < len(parts):
        missing = '、'.join(row['query'] for row in selected if row['reference_state'] != 'REFERENCED')
        result['gaps'] = list(dict.fromkeys([*result['gaps'], f'未列完整短摘录的要求：{missing}；不代表要求不存在。']))
    return result
