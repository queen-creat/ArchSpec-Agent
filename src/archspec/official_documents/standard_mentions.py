"""固定原文中的公共规范编号导航；编号提及不是条款依赖或效力证明。"""
from archspec.official_discovery.public_queries import PUBLIC_CODE, normalize_standard_code


def mentions(text, own_code, sources):
    """沿唯一编号规则保留原字位置；缺年份只列候选，不默认选最新版本。"""
    own = normalize_standard_code(own_code)
    result, seen = [], set()
    for match in PUBLIC_CODE.finditer(text):
        code = normalize_standard_code(match.group())
        has_year = match['year'] is not None
        if code in seen or (own and (code == own or not has_year and code == own.split('-')[0])):
            continue
        seen.add(code)
        targets = []
        for source in sources:
            target_code = normalize_standard_code(source.get('standard_code'))
            if (source.get('kind') != 'FULL_TEXT' or not target_code
                    or (target_code != code if has_year else target_code.split('-')[0] != code)):
                continue
            targets.append({key: source.get(key) for key in (
                'source_id', 'title', 'standard_code', 'kind', 'acquired', 'snapshot_id', 'cache_error')})
        result.append({'code': code, 'original': match.group(), 'start': match.start(), 'end': match.end(),
                       'edition_state': 'EXPLICIT_IN_TEXT' if has_year else 'NOT_SPECIFIED',
                       'targets': targets[:5], 'targets_truncated': len(targets) > 5})
    return result[:6], len(result) > 6
