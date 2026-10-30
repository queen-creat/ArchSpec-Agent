"""一份服务器条件登记；只登记用户字段，不解释规范或生成工程事实。"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ConditionField:
    key: str
    label: str
    prompt: str
    group: str
    legacy_model: bool = False
    project_label: str | None = None
    project_assignment_labels: tuple[str, ...] = ()
    project_value_pattern: str | None = None
    public_prompt: str | None = None


_FIELDS = (
    ConditionField("building_use", "建筑用途",
        "建筑用途是什么（如住宅、办公、汽车库、修车库）？", "fire", True,
        project_assignment_labels=("建筑用途", "项目用途"),
        project_value_pattern=r"(?:住宅|办公楼?|商业|厂房|仓库|车库|公共建筑|学校|医疗|文化|旅馆)"),
    ConditionField("fire_resistance", "耐火等级",
        "耐火等级是多少（如一级、二级；不清楚可写未知）？", "fire", True,
        project_assignment_labels=("耐火等级",), project_value_pattern=r"[一二三四1234]级"),
    ConditionField("fire_context", "地上地下及灭火情况",
        "是地上还是地下，是否设置自动灭火系统？", "fire", True, "所在层位与自动灭火配置",
        ("防火位置", "地上地下及灭火情况", "层位及灭火配置"), r".{1,100}"),
    ConditionField("public_fire_scope", "公共建筑范围",
        "是否木结构、有特殊要求，或附建汽车库？", "fire"),
    ConditionField("garage_form", "车库形式",
        "车库是普通封闭式、敞开式、错层式、斜楼板式，还是机械式？", "garage"),
    ConditionField("garage_storage", "停放或存放情况",
        "只停放普通车辆，还是存放危险物品？", "garage"),
    ConditionField("suppression_scope", "灭火系统覆盖范围",
        "本次防火分区的自动灭火系统是全部覆盖、局部覆盖，还是没有？", "fire",
        public_prompt="自动灭火系统是全部覆盖、局部覆盖，还是没有？"),
    ConditionField("daylight_space", "房间功能",
        "查哪个房间或空间（如普通办公室、阅览室、教室）？", "daylight", True),
    ConditionField("daylight_mode", "采光方式",
        "查侧面采光还是顶部采光？", "daylight", True),
    ConditionField("daylight_region", "地点或光气候区",
        "项目在哪里，或已确认属于哪个光气候区？", "daylight", True),
    ConditionField("spacing_building_pair", "双方建筑用途",
        "两栋建筑分别是什么用途（如双方住宅，或住宅与办公）？", "spacing", True),
    ConditionField("spacing_geometry", "双方高度与相对布置",
        "两栋各多高、朝向和相对布置怎样（正面相对还是侧面相对）？", "spacing", True),
    ConditionField("spacing_area", "所在范围",
        "位于新城区、旧城区还是其他规划范围？不清楚可写未知。", "spacing", True),
    ConditionField("height_purpose", "高度计量口径",
        "按消防、规划，还是一般民用建筑口径核对高度？不清楚可写未知。", "height",
        project_assignment_labels=("高度计量口径",), project_value_pattern=r".{1,100}"),
    ConditionField("roof_form", "屋面形式",
        "是平屋顶、坡屋顶，还是混合屋面？不清楚可写未知。", "height",
        project_assignment_labels=("屋面形式",), project_value_pattern=r".{1,100}"),
    ConditionField("height_control_scope", "高度控制范围",
        "是否位于特殊高度控制范围？请说明范围，或明确无特殊控制区。", "height",
        project_assignment_labels=("高度控制范围",), project_value_pattern=r".{1,100}"),
    ConditionField("roof_equipment_description", "屋顶设备或突出用房",
        "屋顶有哪些设备或突出用房？请说明实际用途，不清楚可写未知。", "height",
        project_assignment_labels=("屋顶设备或突出用房",), project_value_pattern=r".{1,100}"),
    ConditionField("roof_equipment_area", "突出用房与屋面面积",
        "突出用房及屋面面积已知多少？请保留单位，不清楚可写未知。", "height",
        project_assignment_labels=("突出用房与屋面面积",), project_value_pattern=r".{1,100}"),
)
CONDITION_FIELDS = MappingProxyType({field.key: field for field in _FIELDS})
CONDITION_KEYS = tuple(CONDITION_FIELDS)
# 旧V1模型只保留原9项能力；新语义V2不接受模型生成工程条件值。
LEGACY_MODEL_KEYS = tuple(field.key for field in _FIELDS if field.legacy_model)
QUESTION_LABELS = MappingProxyType({field.key: field.prompt for field in _FIELDS})


def condition_questions(issues):
    """只消费领域已判定缺项；最多三问，不在标签层推条件或规范结论。"""
    return [{"key": key, "label": CONDITION_FIELDS[key].prompt, "reason": reason}
            for key, reason in list(issues.items())[:3]]


def condition_prompt(key, *, public=False):
    """同一登记的有限语境措辞；保留已有公共建筑短标签。"""
    field = CONDITION_FIELDS[key]
    return field.public_prompt if public and field.public_prompt else field.prompt


def group_questions(group, *, keys=None):
    """兼容领域标签入口委托登记；不再保留第二份字段说明。"""
    return {key: field.prompt for key, field in CONDITION_FIELDS.items()
            if field.group == group or keys is not None and key in keys}
