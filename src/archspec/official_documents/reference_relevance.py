"""普通问题共同相关性：保留实质对象与属性，不把引用逐字正确当主题正确。"""

import re

from archspec.official_discovery.public_queries import PUBLIC_CODE, normalize_query
from archspec.official_documents.reading_scope import explicitly_excluded, scoped_object_match
from archspec.official_qa.fire_scope import fire_use_conflict

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
    r"从哪(?:里)?(?:开始)?量|怎么量|如何量|怎么处理|如何处理|怎样处理|怎么控制|如何控制|怎样控制|在哪里|在哪|哪里|怎样|如何|怎么|有什么|有哪些|同时|涉及|多条|里面|之间|能不能|不能太远|不能|请问|帮我|我想|查找|查询|多少|什么|哪些|是否|有关|相关|出处|[的呢吗啊？?和与从到沿时]"
)


def _compact(text):
    return re.sub(r"\s+", "", normalize_query(str(text or "")))


def phrases(query):
    """未知主题仍可查词组；双字窗口必须覆盖多个实质词，单字交集不够。"""
    content = QUESTION_FILLERS.sub(" ", normalize_query(str(query or "")))
    terms = set()
    for segment in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]{2,}", content):
        if re.fullmatch(r"[A-Za-z]+", segment):
            terms.add(segment.lower())
        else:
            terms.update(
                segment[i : i + 2]
                for i in range(len(segment) - 1)
                if segment[i : i + 2] not in COMMON
            )
    return terms


def phrase_match(terms, text):
    if not terms:
        return False
    found = {term for term in terms if term in text.lower()}
    # 这只是保守的候选展示规则，绝不表示60%正确率；原始结果仍可逐条阅读。
    return bool(found) if len(terms) == 1 else len(found) >= 2 and len(found) / len(terms) >= 0.6


def subject_query(query, targets=(), *, region=None):
    """只去掉服务器已绑定的书名/编号；不从候选反推问题或扩大来源。"""
    text = PUBLIC_CODE.sub(" ", query)
    for kind, value in targets:
        if kind in {"TITLE", "LOCAL_TOPIC"}:
            text = text.replace(value, " ")
    # 地域来自同次既有解析和过滤；只去背景词，不从文字推断另一个地域。
    if region and region != "地域待补充":
        text = text.replace(region + "市", " ").replace(region + "省", " ").replace(region, " ")
    return text


def use_background_pattern(use):
    """共享既有通用用途范围，不从候选正文推定新的工程用途。"""
    return re.escape(use) + r"(?:建筑|楼|室)?" if use in {"办公", "住宅", "商业", "公共建筑"} else None


def measurement_statement(text):
    """复用原量法谓词边界；对象归属由调用方的同陈述焦点核对，不据此推算值。"""
    return bool(re.search(r"按.{0,100}(?:计算|测量)|从.{0,60}(?:算起|量)|(?:测量|计算)方法",
                          re.sub(r"\s+", "", text)))


def reference_dimension_variants(value):
    """复用既有尺寸查原文口径；仅生成核对词，不改原问、原文或选取数值。"""
    return tuple(value.replace("尺寸", word) for word in ("宽度", "高度")) if "尺寸" in value else (value,)


def reference_matches(query, text, conditions, *, scope_context=()):
    """同一规则用于候选和完整核心；普通用途是背景，不能替代所问对象。"""
    original, text = query, _compact(text)
    use = conditions.get("building_use", "")
    if explicitly_excluded(original, text) or fire_use_conflict(use, text):
        return False
    # 这些通用用途仍由来源/主句核冲突；原文无需再连写“办公建筑屋面”。
    if background := use_background_pattern(use):
        query = re.sub(background, " ", query)
    # 只切断完整问句词，不删除“控制线/等级”等实质词里的单字。
    query = QUESTION_FILLERS.sub(" ", _compact(query))
    query = re.sub(r"要求|规定|规范|标准|限制", " ", query)
    # 只按实际词法核操作与否定谓词；不替用户推定项目条件或距离数值。
    if re.search(r"布置|设置", original) and not re.search(r"布置|设置", text):
        return False
    if re.search(r"(?:不能|不得|不应).{0,12}(?:布置|设置)", original) and not re.search(
            r"(?:不应|不得|不宜|禁止|严禁|不可).{0,6}(?:布置|设置)", text):
        return False
    if "贴邻" in original and "贴邻" not in text:
        return False
    # 量法需要明确的计量谓词；“有这个尺寸”或设置要求不能代答量法。
    if re.search(r"从哪(?:里)?(?:开始)?量|怎么量|如何量|计量", original) and not measurement_statement(text):
        return False
    # 尺寸是问法中的属性集合；宽/高任一实际规定可供查阅，不推算数值。
    variants = reference_dimension_variants(query)
    # 专用楼梯用途须直接绑定分类行；不能借表注排除名单或另一用途补齐。
    start = re.match(r"(.+?楼梯(?:踏步|平台)?)", query.strip())
    if start and scoped_object_match(start[1], text) is False:
        return False
    # 普通尺寸/等级问法仍须有对应属性，不能借办公/住宅或一个“等级”代答。
    attributes = {"内半径": r"内半径", "外半径": r"外半径", "净宽": r"净宽|净空宽", "宽度": r"宽",
                  "高度": r"高度|净高|层高|高不|高应|高宜",
                  "厚度": r"厚度|厚不|厚应|厚宜|厚为",
                  "坡度": r"坡度", "半径": r"半径", "等级": r"等级",
                  "长度": r"长度|长不|长应|长宜", "距离": r"距离|间距",
                  "面积": r"面积", "数量": r"数量|[个处]|至少|不应少于|不宜少于"}
    # 只把已独立核过的原字范围用于词组覆盖；排除、用途、动作和属性仍只核正文。
    # 不能把书名中的“布置/面积”等字借给未规定该动作或性质的条文。
    matching_text = text + "\n" + "\n".join(_compact(value) for value in scope_context)
    for value in variants:
        # 单一明确属性前的限定字不能被覆盖比例吞掉。
        # 允许正文在对象与部位之间插入谓词，不要求原问整词连写。
        # 多属性问法保留既有分段规则；这里只核正文，不借书名补对象。
        metric_pattern = "|".join(map(re.escape, sorted(attributes, key=len, reverse=True)))
        metrics = list(re.finditer(metric_pattern, value))
        if len(metrics) == 1:
            prefix = value[:metrics[0].start()].strip()
            owner = re.search(r"[\u4e00-\u9fff]{3,}$", prefix)
            remaining = iter(text)
            if owner and not all(character in remaining for character in owner[0]):
                continue
        if any(key in value and not re.search(pattern, text) for key, pattern in attributes.items()):
            continue
        if phrase_match(phrases(value), matching_text):
            return True
        # 长问法的每个实质词段都须对应，不能以背景或动作的四字命中
        # 替代另一个缺失对象。词段边界沿原问句分隔，不拼接成新词。
        segments = [segment for segment in re.findall(r"[\u4e00-\u9fff]+", value)
                    if phrases(segment)]
        if segments and all(phrase_match(phrases(segment), matching_text)
                            or any(segment[i:i+4] in matching_text for i in range(len(segment)-3))
                            for segment in segments):
            return True
    return False
