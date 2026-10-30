"""明确查表请求的阅读顺序；表题只能定位，不能证明单元格或取值。"""
import re

from archspec.official_documents.evidence_relations import captions
from archspec.official_documents.reference_relevance import (
    QUESTION_FILLERS,
    phrase_match,
    phrases,
    reference_matches,
    use_background_pattern,
)


def table_lookup_subject(query):
    """只接受完整定位问句，不将混合取值/判断问题改成查表。"""
    match = re.fullmatch(r'(?:请问|请查|帮我查|帮我找)?\s*(.{2,100}?)的?表(?:格)?(?:在(?:哪里|哪)|在哪里|在哪儿)[？?。]?', query.strip())
    if match is None:
        return None
    subject = match[1].removesuffix('的')
    if re.search(r'多少|多大|是否|能否|能不能|取值|怎么算|如何计算|[，,；;。？！?表]', subject):
        return None
    return subject


def table_caption_indices(query, snapshot, conditions):
    """只选本固定资料的实际表题块；引用表号、目录与其他对象不被提升。"""
    subject = table_lookup_subject(query)
    if subject is None:
        return set()
    terms = subject
    if background := use_background_pattern(conditions.get('building_use', '')):
        terms = re.sub(background, ' ', terms)
    segments = [phrases(part) for part in QUESTION_FILLERS.sub(' ', terms).split()]
    segments = [part for part in segments if part]
    if not segments:
        return set()
    selected = set()
    for index, row in enumerate(snapshot['chunks']):
        for caption in captions(row['text']):
            literal = caption['caption']
            lines = row['text'].splitlines()
            starts = [i for i, line in enumerate(lines) if line.strip() == literal.strip()]
            # 只连接紧随表题、明确以连接词起首的一行原字；不吞表头/单元格或跨块猜题名。
            if len(starts) == 1 and starts[0] + 1 < len(lines):
                following = lines[starts[0] + 1].strip()
                if (re.match(r'^(?:与|及|和|之间|的)', following) and len(following) <= 100
                        and not re.search(r'[。；;！？!?]|[.…⋯·]{2,}', following)):
                    literal += '\n' + following
            text = re.sub(r'\s+', '', literal)
            if (reference_matches(subject, text, conditions)
                    and all(phrase_match(part, text) for part in segments)):
                selected.add(index)
    return selected
