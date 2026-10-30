"""有限问法识别：只用于追问和候选筛选，不改写检索、不推算规范数字。"""

import re


def net_height_question(text):
    """只识别明确的净高话题；楼层面积、噪声、建筑总高度不能代答。"""
    normalized = re.sub(r"\s+", "", text)
    return any(term in normalized for term in ("净高", "净空高度"))


def height_measurement_question(query):
    """统一识别明确高度量法；净高取值、高层定义和疏散要求不是量法。"""
    text = re.sub(r"\s+", "", query)
    return (not net_height_question(text) and not highrise_definition_question(text)
            and any(term in text for term in ("建筑高度", "檐口高度", "屋脊高度"))
            and any(term in text for term in ("计量", "测量", "计算", "量法", "怎么算", "怎么量",
                "如何算", "如何量", "从哪里量", "从哪量", "算不算", "计不计", "计入")))


def fire_area_question(query):
    """消防分区是常见口语；仅讨论分区大小时按防火分区意图处理。"""
    return any(term in query for term in ("防火分区", "消防分区")) and any(
        term in query for term in ("面积", "大小", "多大"))


def highrise_definition_question(query):
    """高层疏散、楼梯等问题不是高层定义；不按出现一个关键词就追问。"""
    return "高层" in query and any(term in query for term in (
        "多高", "多少米", "几米", "算高层", "判定", "界定", "定义", "划分标准"))


def height_use_category(value):
    """用途只是检索筛选线索；混合、否定和未知输入不能猜成单一类别。"""
    if any(word in value for word in ("未知", "不清楚", "不知道", "不确定", "未确定",
                                      "随便", "不限", "非住宅", "不是住宅")):
        return None
    groups = {"住宅": ("住宅",), "公共": ("公共", "办公", "商业"),
              "厂房": ("厂房",), "仓库": ("仓库",)}
    matched = [key for key, words in groups.items() if any(word in value for word in words)]
    return matched[0] if len(matched) == 1 else None


def highrise_definition_evidence(text, category):
    """用途与高度须对应肯定分类谓词，不借别句/尾句的词证明定义。"""
    terms = {"住宅": ("住宅",), "公共": ("公共", "办公", "商业"),
             "厂房": ("厂房",), "仓库": ("仓库",)}.get(category, ())
    if not terms:
        return False
    normalized = re.sub(r"\s+", "", text)
    subject = r"高层(?:民用|公共|住宅|工业)?(?:建筑|厂房|仓库)"
    predicate = r"属于|称为|划分为|判定为|定义为|界定为|为"
    negative = re.compile(r"(?:不|不得|不能|不应|未|并非|禁止)(?:被|直接|再)?$")

    def matching_label(label):
        # 明确的住宅/公共/工业子类不能靠邻接用途变成另一类；民用共列仍可读取。
        explicit = next((key for key in ("住宅", "公共", "厂房", "仓库")
                         if key in label), None)
        return explicit is None or explicit == category

    def height_condition(scope):
        # 高度比较须属于相同条件分句。另一分句的量法、楼梯或设备用途不能代答。
        return ("高度" in scope and any(term in scope for term in terms)
                and any(word in scope for word in ("大于", "小于", "超过", "高于", "低于", "以上", "以下"))
                and not any(word in scope for word in
                            ("安全出口", "疏散出口", "疏散距离", "面宽", "踏步", "设备房")))

    # 小数点、逗号、折行不结束分类项；真实共列阈值、上限和x原字不被改写。
    for sentence in re.split(r"[。；;！？!?]", normalized):
        if any(word in sentence[:180] for word in ("安全出口", "疏散出口", "疏散距离")):
            continue
        for match in re.finditer(r"(?<!作)(?:" + predicate + r")高层", sentence):
            before = sentence[:match.start()]
            if negative.search(before):
                continue
            label = sentence[match.end() - 2:]
            # 分类标签之后的逗号句只解释其它要求，不能给前置条件补用途。
            label = re.split(r"[，,]", label, maxsplit=1)[0]
            # 最近的带用途高度条件才是此谓词的主体；前面另一建筑的设备要求
            # 即使也写了高度比较，不能越过新主体给它补公共/住宅用途。
            condition = next((part for part in reversed(re.split(r"[，,]", before))
                if "高度" in part and any(use in part for use in
                    ("住宅", "公共", "办公", "商业", "厂房", "仓库"))), "")
            if matching_label(label) and height_condition(condition):
                return True
        for match in re.finditer(subject + r"(?:的定义)?(?:是|指|系|为)", sentence):
            # 主体先行的定义只看直接解释分句，不能借后面的独立高度量法。
            label = match.group()
            tail = re.split(r"[，,]", sentence[match.end():], maxsplit=1)[0]
            if matching_label(label) and height_condition(label + tail):
                return True
    return False
