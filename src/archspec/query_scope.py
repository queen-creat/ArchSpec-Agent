"""只提取用户明确的查询取舍，原问与项目事实仍由调用方保存。"""
import re

_CHOICE = re.compile(
    r"(?P<prefix>.*?)(?:不问|不查|不要查|不用查|不需要查)"
    r"[^。！？!?\n]{2,200}?[，,；;]\s*(?:只|仅)(?:问|查|找)"
    r"(?P<wanted>[^。！？!?\n].*)", re.DOTALL)


def requested_content(query):
    """仅识别明确配对指令；不删规范否定句，不推断同义词或新条件。"""
    # 多次改口没有唯一切分，不在不同调用点反复缩减原问。
    if len(re.findall(r"不问|不查|不要查|不用查|不需要查", query)) != 1:
        return query
    match = _CHOICE.fullmatch(query)
    if match is None or not match['wanted'].strip():
        return query
    return (match['prefix'].rstrip() + ' ' + match['wanted'].lstrip()).strip()
