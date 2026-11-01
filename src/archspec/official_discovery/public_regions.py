"""共同公开地域目录：只绑定已知名称和已审核官网入口，不推断项目适用范围。"""

from types import MappingProxyType

_BASE_REGION_NAMES = (
    "宜昌", "北京", "天津", "上海", "重庆", "武汉", "杭州", "南京", "广州",
    "深圳", "成都", "西安", "长沙", "合肥", "济南", "郑州", "福州", "南昌",
    "南宁", "昆明", "贵阳", "海口", "太原", "沈阳", "长春", "哈尔滨",
    "石家庄", "呼和浩特", "乌鲁木齐", "兰州", "西宁", "银川", "拉萨",
    "苏州", "宁波", "厦门", "青岛", "大连", "无锡", "佛山", "东莞",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽",
    "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古",
    "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)
# 既有福建省官网地市目录观察；保留原来源与指纹，不把门户当作规范正文。
DIRECTORY_SOURCE = "https://www.fujian.gov.cn/"
DIRECTORY_SHA256 = "754a476e8f4b213855bccd0c0b0c2bac943e52111cc001f6567a0028b13945ed"
_FUJIAN_PORTAL_ROOTS = MappingProxyType({'福建': 'https://www.fujian.gov.cn/', '福州': 'https://www.fuzhou.gov.cn/', '厦门': 'https://www.xm.gov.cn/', '漳州': 'https://www.zhangzhou.gov.cn/', '泉州': 'https://www.quanzhou.gov.cn/', '三明': 'https://www.sm.gov.cn/', '莆田': 'https://www.putian.gov.cn/', '南平': 'https://www.np.gov.cn/', '龙岩': 'https://www.longyan.gov.cn/', '宁德': 'https://www.ningde.gov.cn/'})
# 武汉市黄陂区政府链接目录原始内容，记载来源而不把目录当作正文。
NATIONAL_DIRECTORY_SOURCE = "https://www.huangpi.gov.cn/fbjd_33/dfhbmptlj/"
NATIONAL_DIRECTORY_SHA256 = "d9288deb45096d08deeb2888b197dbe92c7d9a2eb6664fd427f1e2f585602a57"
# 目录原始链接为HTTP；仅保留同一已列主机、经实测HTTPS成功的根网址。
# 其他站点（含403/超时）跳过；可访问入口不代表本站支持自动搜索合同。
_NATIONWIDE_HTTPS_PORTAL_ROOTS = MappingProxyType({
    '天津': 'https://www.tj.gov.cn/',
    '河北': 'https://www.hebei.gov.cn/',
    '山西': 'https://www.shanxi.gov.cn/',
    '内蒙古': 'https://www.nmg.gov.cn/',
    '辽宁': 'https://www.ln.gov.cn/',
    '吉林': 'https://www.jl.gov.cn/',
    '浙江': 'https://www.zj.gov.cn/',
    '安徽': 'https://www.ah.gov.cn/',
    '河南': 'https://www.henan.gov.cn/',
    '海南': 'https://www.hainan.gov.cn/',
    '重庆': 'https://www.cq.gov.cn/',
    '四川': 'https://www.sc.gov.cn/',
    '贵州': 'https://www.guizhou.gov.cn/',
    '云南': 'https://www.yn.gov.cn/',
    '陕西': 'https://www.shaanxi.gov.cn/',
    '宁夏': 'https://www.nx.gov.cn/',
    '新疆': 'https://www.xinjiang.gov.cn/',
    '武汉': 'https://www.wuhan.gov.cn/',
})
PORTAL_ROOTS = MappingProxyType({**_NATIONWIDE_HTTPS_PORTAL_ROOTS, **_FUJIAN_PORTAL_ROOTS})
# 只有原福建城市是福建的子级，省级入口和武汉不能继承福建身份。
REGION_PARENTS = MappingProxyType({**{city: "福建" for city in _FUJIAN_PORTAL_ROOTS if city != "福建"},
                                  "武汉": "湖北"})
# 同一目录供问题识别和门户发现消费，不能登记了搜索入口却丢掉原问地域。
REGION_NAMES = tuple(dict.fromkeys((*_BASE_REGION_NAMES, *PORTAL_ROOTS)))
