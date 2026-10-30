"""只核明确原字范围与分类行；不是工程适用性、数字或表格完整性证明。"""

import re


def explicitly_excluded(query, text):
    """本条/本表明确排除的对象仍可关联阅读，不能靠排除名单当对应证据。"""
    query, text = (re.sub(r"\s+", "", value) for value in (query, text))
    pattern = (r"(?:本(?:条|章|节|标准|规范|规定|表)|表中|该表)"
               r"[^。；;！？!?]{0,80}?(?:不包括|不适用于)([^。；;！？!?]{1,180})")
    for match in re.finditer(pattern, text):
        for subject in re.split(r"以及|及|和|与|[、，,]", match[1]):
            # 只接受名单中的完整原字，不从否定句猜另一种用途或改写要求。
            if len(subject) >= 2 and subject in query:
                return True
    return False


def scoped_object_match(value, text):
    """分类表可将楼梯和踏步分写；范围仍须直接连着对象，不借宿舍/邻接用途。

    返回None表示没有支持的拆分结构，调用方继续原词组规则。中小学须由
    原文的共同类别或小学、中学两种分类都见到；不能由一个类别代替两者。
    """
    value, text = (re.sub(r"\s+", "", item) for item in (value, text))
    match = re.fullmatch(r"(.+?)(楼梯)(踏步|平台)?", value)
    if not match:
        return None
    scope, facility, detail = match.groups()
    if detail and detail not in text:
        return False
    if scope in {"中小学", "中小学校"}:
        scopes = (r"(?:中小学(?:校)?|小学(?:校)?)", r"(?:中小学(?:校)?|中学(?:校)?)")
    elif scope in {"小学", "小学校", "中学", "中学校"}:
        scopes = (re.escape(scope.removesuffix("校")) + r"(?:校)?",)
    else:
        scopes = (re.escape(scope),)
    return all(re.search(subject + r"的?" + facility, text) for subject in scopes)


def focus_prefix_context(query, subjects, *, background_use=None, clause=False):
    """保留首个对象前未被角色覆盖的原字，不推断它是用途、等级或工程事实。

    明确书名/编号仍由来源范围核对；提问语气沿用现有词表。条文模式只去开头
    的数字条号，不能把条号误当范围。没有此结构时返回空，不猜后置修饰关系。
    """
    from archspec.official_discovery.public_queries import PUBLIC_CODE, extract_public_titles
    from archspec.official_documents.reference_relevance import (
        COMMON,
        QUESTION_FILLERS,
        use_background_pattern,
    )
    from archspec.query_scope import requested_content

    content = re.sub(r"\s+", "", requested_content(query))
    if clause:
        content = re.sub(r"^\d+(?:\.\d+){1,3}", "", content)
    starts = [content.find(re.sub(r"\s+", "", subject)) for subject in subjects]
    starts = [start for start in starts if start >= 0]
    if not starts:
        return ()
    prefix = PUBLIC_CODE.sub(" ", content[:min(starts)])
    for title in extract_public_titles(prefix):
        prefix = prefix.replace(title, " ")
    if background_use and (pattern := use_background_pattern(background_use)):
        prefix = re.sub(pattern, " ", prefix)
    prefix = QUESTION_FILLERS.sub(" ", prefix)
    # 空间修饰的结构助词不是位置名称；只去明确内/外后的句末“的”。
    # 位置本身仍为原问连续原字，不删除范围或生成同义改写。
    prefix = re.sub(r"(?<=[内外])的$", "", prefix)
    values = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+(?:\.[0-9]+)?", prefix)
    return tuple(dict.fromkeys(value for value in values if len(value) >= 2 and value not in COMMON))


# 只识别一个明确的后置确定谓词；遇到并列或多个谓词不推测语法边界。
_DETERMINATION = r"(?:确定|计算|选定|选择|验算|核定|评定)"
_BASIS_CLAUSE = re.compile(
    r"(?:应当|必须|不应|不宜|不得|应|宜|可|须)"
    r"(?P<basis>(?:根据|依据|按照|按)(?:(?!" + _DETERMINATION
    + r"|并|且|同时)[^。；;，,！？!?]){1,300})"
    + _DETERMINATION + r"(?=\s*(?:[。；;！？!?]|$))")


def direct_statement_text(text):
    """仅供角色核对的视图：依据条件不冒充主规定，原条文和引用完全不改。

    根据/依据/按照/按之后、句末单一确定谓词之前是输入范围；复杂并列、多谓词
    不猜。用非文字分隔符保持位置且不拼成新词，不生成或保存替代正文。
    """
    parts, start = [], 0
    for match in _BASIS_CLAUSE.finditer(text):
        left, right = match.span("basis")
        parts.extend((text[start:left], "\ufffc" * (right - left)))
        start = right
    return "".join((*parts, text[start:])) if parts else text


# 这里只划分明确模态分句，不宣称完成中文语法解析或工程适用性判断。
_STATEMENT_MODAL = re.compile(r"应当|必须|不得|不应|不宜|严禁|禁止|应|宜|可|须")


def _statement_pairs(role_text, original):
    parts = re.split(r"[，,]", role_text)
    originals = re.split(r"[，,]", original)
    if len(role_text) != len(original) or len(parts) != len(originals):
        raise ValueError("陈述角色与原句位置不一致")
    pairs = tuple(zip(parts, originals, strict=True))
    if not any(_STATEMENT_MODAL.search(part) for part in parts):
        return pairs
    scopes, pending, owner = [], [], ("", "")
    for part, raw in pairs:
        if not _STATEMENT_MODAL.search(part):
            if re.search(r"分为|分成|包括|由[^，,]{1,80}组成", part):
                scopes.append((part, raw))
            pending.append((part, raw))
            continue
        stripped = part.lstrip()
        condition = re.match(r"^(?:当|在).{1,120}(?:时|下)(?:的情况下)?", stripped)
        pronoun = re.match(r"其(?!中|余|他)", stripped) is not None
        inherited = (_STATEMENT_MODAL.match(stripped) is not None or pronoun
                     or condition is not None and _STATEMENT_MODAL.match(stripped[condition.end():]) is not None)
        prefix = pending or ([owner] if owner[0] else [])
        scope = tuple("，".join(p[index] for p in (*prefix, (part, raw)))
                      for index in (0, 1)) if inherited else (part, raw)
        scopes.append(scope)
        end = _STATEMENT_MODAL.search(scope[0]).start()
        owner = (scope[0][:end], scope[1][:end])
        pending = []
    return tuple(scopes)


def direct_statement_scopes(text):
    """同句另一对象不借谓词；维持原分句/继承规则。"""
    return tuple(role for role, _ in _statement_pairs(text, text))


def direct_statement_pairs(text, *, role_text=None):
    """同一陈述的角色视图与原字成对；依据遮蔽不丢掉原量法。"""
    return _statement_pairs(direct_statement_text(text) if role_text is None else role_text, text)


def main_subject_match(text, subject):
    """仅连接主名称两侧的原字；括注仍完整引用，括号里的别称不冒充主名称。"""
    compact = lambda value: re.sub(r"\s+", "", value)
    text, subject = compact(text), compact(subject)
    if subject in text:
        return True
    # 纯文字短括注不含限制/指引时，可核主名称的连续原字；不解释括注同义关系。
    def annotation(match):
        return match[0] if re.search(r"不|非|除|含|仅|见|注|参|说明", match[1]) else ""
    return subject in re.sub(r"[（(]([\u3400-\u9fff]{2,12})[）)]", annotation, text)


def numbered_statement_pairs(text):
    """读取首组完整连续编号；各项各句独立核角色，不以句号结束整组。"""
    from archspec.documents.chunker import clause_start

    role = direct_statement_text(text)
    colon = re.search(r"[：:]", role)
    if colon is None or re.search(r"[。；;]", role[:colon.start()]):
        return ()
    start, stop = colon.end(), len(text)
    for line in re.finditer(r"(?m)^.*$", text[start:]):
        value = line[0].strip()
        if (clause_start(value) is not None
                or re.match(r"^(?:[（(]\s*)?(?:注(?:释)?|附注|条文说明)(?:[：:\d０-９]|\s|$)"
                            r"|^(?:表|图)\s*\d", value)):
            stop = start + line.start()
            break
    body, raw_body = role[start:stop], text[start:stop]
    markers = list(re.finditer(r"(?m)^[ \t]*([1-9]\d{0,2})[、.)．]?[ \t]*(?=[\u4e00-\u9fff])", body))
    # 不能跨缺号推断仍为完整分项，也不能把正文后的另一组编号并入。
    if not markers or [int(marker[1]) for marker in markers] != list(range(1, len(markers) + 1)):
        return ()
    pairs = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        item, original = body[marker.end():end], raw_body[marker.end():end]
        # 原字/角色视图同长，按真实句界切片；每句都重新要求主语，不继承别项对象。
        offset = 0
        for sentence in re.finditer(r"[^。]+(?:。|$)", item):
            if len(pairs) >= 64:
                return tuple(pairs)
            begin, finish = sentence.span()
            role_sentence = item[begin:finish].removesuffix("。")
            raw_sentence = original[begin:finish].removesuffix("。")
            terminal = re.search(r"[；;：:]", role_sentence)
            if terminal:
                role_sentence, raw_sentence = role_sentence[:terminal.start()], raw_sentence[:terminal.start()]
            pairs.extend(direct_statement_pairs(raw_sentence, role_text=role_sentence))
            offset = finish
        # offset仅为上方真实切片的终点，不从空白或未识别区域补正文。
        if offset > len(item):
            raise ValueError("编号项文字边界不一致")
    return tuple(pairs)


def subsequent_subject_pairs(text, subjects):
    """连续后句必须在规范谓词前明确重复首句对象；遇附注/新条号/换对象即止。

    不解决代词、复合对象或同义改写；不从依据里的名词推导主语。
    """
    from archspec.documents.chunker import clause_start

    role_text = direct_statement_text(text)
    boundary = re.search(r"[。；;：:]", role_text)
    if not subjects or boundary is None or boundary[0] != "。":
        return ()

    def owns(role):
        modal = _STATEMENT_MODAL.search(role)
        prefix = re.sub(r"\s+", "", role[:modal.start()]) if modal else ""
        return modal is not None and all(re.sub(r"\s+", "", value) in prefix for value in subjects)

    first = direct_statement_pairs(text[:boundary.start()], role_text=role_text[:boundary.start()])
    if not any(owns(role) for role, _ in first):
        return ()
    found = []
    for sentence in text[boundary.end():].split("。")[:8]:
        sentence = sentence.strip()
        if (not sentence or clause_start(sentence) is not None
                or re.match(r"^(?:[（(]\s*)?(?:注(?:释)?|附注|条文说明)(?:[：:\d０-９]|\s|$)"
                            r"|^(?:表|图)\s*\d", sentence)):
            break
        pairs = [pair for pair in direct_statement_pairs(sentence) if owns(pair[0])]
        if not pairs:
            break
        found.extend(pairs)
    return tuple(found)


def conditional_subject_match(text, subject):
    """只认“当对象…时，属性应…”的原字复合归属，不靠全句共同出现推断。"""
    from archspec.documents.chunker import clause_start

    text, subject = (re.sub(r"\s+", "", value) for value in (text, subject))
    anchor = clause_start(text)
    if anchor:
        text = anchor["body"]
    condition = re.fullmatch(r"当([^，,。；;]{2,120})时[，,]([^，,。；;]{2,400})", text)
    if not condition:
        return False
    modal = _STATEMENT_MODAL.search(condition[2])
    head = condition[2][:modal.start()] if modal else ""
    # 主句须以完整属性短语起头；不能跳过门洞等异类主语来取后面的“净宽”。
    if not re.fullmatch(r"[\u3400-\u9fff]{2,30}", head):
        return False
    if subject in head:
        return True
    if not subject.endswith(head) or len(subject) - len(head) < 2:
        return False
    owner = subject[:-len(head)]
    # 依据、排除和多对象并列均不能签发单一归属；条件和量法仍在原引用中保留。
    position = condition[1].find(owner)
    leading = condition[1][:position] if position >= 0 else ""
    return (0 <= position <= 4 and condition[1].count(owner) == 1
            and not re.search(r"有|设|装|含|放|存|用|在|内|外|中|上|下|对|将|由|邻|距", leading)
            and not re.search(r"与|和|及|或|、|根据|依据|按照|按|参照|计算|确定|非|不包括|不适用|\ufffc", condition[1]))


def conditional_subject_pairs(text, subjects, *, role_text=None):
    """补充首句明确条件结构的成对视图；不改原字，不跨句或补造主语。"""
    role = direct_statement_text(text) if role_text is None else role_text
    return ((role, text),) if subjects and all(conditional_subject_match(role, value) for value in subjects) else ()
