"""分类问句的阅读定位；不证明类别完整、适用条件或答案资格。"""
import re

from archspec.official_documents.clause_units import CLAUSE
from archspec.official_documents.reading_scope import direct_statement_pairs


def classification_subject(query):
    match = re.fullmatch(
        r'(.{2,80}?)(?:的)?(?:划分要求|如何划分|怎么划分|如何分类|怎么分类)[？?。]?',
        query.strip())
    if match is None:
        return None
    subject = match[1].removesuffix('的')
    if re.search(r'[《》、，,；;。？！?]|以及|和|与|是否|多少|能否', subject):
        return None
    return subject

def classification_indices(query, snapshot):
    subject = classification_subject(query)
    if subject is None:
        return set()
    # Only an explicit output subject followed by a classification predicate.
    # Basis text cannot introduce another object with 将/把 or an extra predicate.
    pattern = re.compile(re.escape(subject) + r'(?:应当|应|可|宜)?'
        r'(?:(?:按|根据|依据)(?:(?!将|把|确定|计算|选定|分为)[^。；;，,：:]){1,150})?分为')
    result = set()
    for index, row in enumerate(snapshot['chunks']):
        if not CLAUSE.fullmatch(row.get('clause_number') or ''):
            continue
        text = row['text']
        if re.search(r'[.…⋯·]{2,}\s*\d+\s*(?:\n|$)', text):
            continue
        first = re.split(r'[。；;：:]', text, maxsplit=1)[0]
        for role, _ in direct_statement_pairs(first):
            role = re.sub(r'\s+', '', role)
            match = pattern.search(role)
            if match is None:
                continue
            prefix = re.sub(r'^\d+(?:\.\d+){1,3}', '', role[:match.start()])
            if not re.search(r'应|宜|可|须|按|根据|依据|将|把|分为|不', prefix):
                result.add(index)
                break
    return result
