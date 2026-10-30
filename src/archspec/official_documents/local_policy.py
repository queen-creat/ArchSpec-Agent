"""已审核地方公告—附件的唯一准入合同；审核来源不批准内容使用或现行性。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from types import MappingProxyType

ROOT = "https://zrzy.yichang.gov.cn/"
TITLE = "宜昌市城乡规划管理技术规定（2025版）"


@dataclass(frozen=True)
class ReviewedLocalPublication:
    """只存服务端审阅的公开事实与精确入口，不接受浏览器新增网址或授权。"""

    notice_url: str
    title: str
    code: str
    kind: str
    pdf_url: str
    authority: str
    province: str
    city: str | None
    document_type: str
    site_name: str
    public_term: str
    query_keywords: tuple[str, ...] = ()
    attachment_titles: tuple[str, ...] = ()
    review_evidence: tuple[tuple[str, str], ...] = ()
    # 准入仅允许获取这一配对；正文读写及MODEL_SEND仍走实时治理门。
    fetch_policy: str = "EXACT_PAIR_ONLY"
    use_permission: str = "UNKNOWN"
    redirect_targets: tuple[str, ...] = ()

    @property
    def region(self):
        return "·".join(part for part in (self.province, self.city) if part)

    @property
    def notice_title(self):
        # 公告与完整附件可有不同题名；角色证据不新增字段，旧asdict及review_id保持原值。
        return dict(self.review_evidence).get("notice_title", self.title)

    @property
    def review_id(self):
        content = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


_REVIEWED = (
    ReviewedLocalPublication(
        notice_url=ROOT + "content-42395-986231-1.html", title=TITLE,
        code="宜昌城乡规划·2025版", kind="FULL_TEXT",
        pdf_url="https://img.yichang.gov.cn/upload2022/2025/1029/20251029034657227.pdf",
        authority="宜昌市自然资源和城乡建设局", province="湖北", city="宜昌",
        document_type="PLANNING_REGULATION", site_name="宜昌市自然资源和城乡建设局官网",
        public_term="宜昌市城乡规划管理技术规定"),
    ReviewedLocalPublication(
        notice_url=ROOT + "content-42395-987164-1.html", title=TITLE + "补充细则",
        code="宜昌城乡规划·2025版补充细则", kind="REVISION",
        pdf_url="https://img.yichang.gov.cn/upload2022/2026/0626/20260626061011241.pdf",
        authority="宜昌市自然资源和城乡建设局", province="湖北", city="宜昌",
        document_type="PLANNING_REVISION", site_name="宜昌市自然资源和城乡建设局官网",
        public_term="宜昌市城乡规划管理技术规定"),
    ReviewedLocalPublication(
        notice_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202411/t20241106_5402600.shtml",
        title="湖北省绿色建筑设计与施工图审查指南（试行）",
        code="湖北绿色建筑指南·试行", kind="FULL_TEXT",
        pdf_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202411/P020241106529262111543.pdf",
        authority="湖北省住房和城乡建设厅", province="湖北", city=None,
        document_type="GUIDANCE", site_name="湖北省住房和城乡建设厅官网",
        public_term="湖北省绿色建筑设计与施工图审查指南（试行）", query_keywords=("绿色建筑",),
        attachment_titles=("湖北省地方标准绿色建筑设计指南.pdf",),
        # 仅为已完成审阅的证据，不以旧SHA替代本次下载与处理的完整指纹。
        review_evidence=(("notice_sha256", "1f454f5a668dbf52835129c69368fb18f0a0a49e2933c392823515a75b1dea75"),
                         ("pdf_sha256", "ccd78162e4f1c4f8e6a3761490a6f7ae229b8dbab94af9d468f74d7cf35aa3e2"))),
    ReviewedLocalPublication(
        notice_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202604/t20260423_5920779.shtml",
        title="湖北省工程建设管理BIM云平台建筑工程模型交付指南（试行）",
        code="湖北BIM模型交付指南·试行", kind="FULL_TEXT",
        pdf_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202604/P020260423333990789167.pdf",
        authority="湖北省住房和城乡建设厅", province="湖北", city=None,
        document_type="GUIDANCE", site_name="湖北省住房和城乡建设厅官网",
        public_term="湖北省工程建设管理BIM云平台建筑工程模型交付指南（试行）",
        query_keywords=("BIM模型交付",),
        attachment_titles=("湖北省工程建设管理BIM云平台建筑工程模型交付指南（试行）.pdf",),
        # 2026-04-20正式通知与56页文字型附件已逐字节核对；不因此批准现行性或MODEL_SEND。
        review_evidence=(("notice_sha256", "30e8611905a68f253f65fefeb799ebba6b5e9569238d67221bc3b0cec0a9de83"),
                         ("pdf_sha256", "e3abe66562751e25daea9d5bfce1bf5a052471aee34a63aa733f88e469b5c01b"))),
    ReviewedLocalPublication(
        notice_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202412/t20241225_5474005.shtml",
        title="关于实行施工许可网上办理的通知（修订版）",
        code="鄂建办〔2015〕222号", kind="FULL_TEXT",
        pdf_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202412/P020241225384774531003.pdf",
        authority="湖北省住房和城乡建设厅", province="湖北", city=None,
        document_type="MANAGEMENT_RULE", site_name="湖北省住房和城乡建设厅官网",
        public_term="关于实行施工许可网上办理的通知（修订版）",
        query_keywords=("施工许可网上办理",),
        attachment_titles=("关于实行施工许可网上办理的通知(修订版).pdf",),
        # 公告〔2024〕24号发布完整修订正本；222号是原通知文号，均不认证现行性或使用许可。
        review_evidence=(("notice_title", "省住建厅关于鄂建办〔2015〕222号文件修订的公告"),
                         ("notice_sha256", "5c95d5b139fb7a8c9db7582c602deefec260e4f44246c111d47aa19dcef29213"),
                         ("pdf_sha256", "2c3d331928f3f81ab0dce09e77c3ed192471e9a35eb763c5caef4b89710675cb"))),
    ReviewedLocalPublication(
        notice_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202408/t20240821_5311048.shtml",
        title="湖北省建设工程消防设计审查验收疑难问题技术指南（2024年版）",
        code="鄂建函〔2024〕96号", kind="FULL_TEXT",
        pdf_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202408/P020240821587519855631.pdf",
        authority="湖北省住房和城乡建设厅", province="湖北", city=None,
        document_type="GUIDANCE", site_name="湖北省住房和城乡建设厅官网",
        public_term="湖北省建设工程消防设计审查验收疑难问题技术指南（2024年版）",
        attachment_titles=("《湖北省建设工程消防设计审查验收疑难问题技术指南（2024年版）》.pdf",
                           "湖北省建设工程消防设计审查验收疑难问题技术指南（2024年版）.pdf"),
        # 原公告及完整121页附件已核；仅准入这一配对，不授予现行性、强条或MODEL_SEND。
        review_evidence=(("notice_title", "省住建厅 省消防救援总队 关于印发《湖北省建设工程消防设计审查验收疑难问题技术指南（2024年版）》的通知"),
                         ("notice_sha256", "3d6d4d1659503023ed28fb34048633782a27a578b0ce6e39db434f9fc2cbf6c2"),
                         ("pdf_sha256", "bbb3b623c46e5c9824680a96322ca1671b95c0002ca0392687db4b9bda1f6a70"))),
    ReviewedLocalPublication(
        notice_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202401/t20240104_5029160.shtml",
        title="湖北省住宅建筑模数化设计导则",
        code="厅头〔2024〕12号", kind="FULL_TEXT",
        pdf_url="https://zjt.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202401/P020240108326768163000.pdf",
        authority="湖北省住房和城乡建设厅", province="湖北", city=None,
        document_type="GUIDANCE", site_name="湖北省住房和城乡建设厅官网",
        public_term="湖北省住宅建筑模数化设计导则", query_keywords=("住宅建筑模数化",),
        attachment_titles=("湖北省住宅建筑模数化设计导则.pdf",),
        # 已核公告、完整52页及原页；仅准入精确配对，不认证效力、强条或MODEL_SEND。
        review_evidence=(("notice_title", "关于印发《湖北省住宅建筑模数化设计导则》的通知"),
                         ("notice_sha256", "660425f97d2142b0d6dbffa98ebc65935821dab4766065f1df2f56f0fd1cb46a"),
                         ("pdf_sha256", "49f99a1e9e57094e427c4cd2b9391797a1e4dff7ee4047d8a8435697191269ce"))),
)
REVIEWED_PUBLICATIONS = MappingProxyType({entry.notice_url: entry for entry in _REVIEWED})
# 原PUBLICATIONS是两份宜昌文件的兼容视图；旧枚举、来源ID和HTTP降级不扩大。
PUBLICATIONS = MappingProxyType({entry.notice_url: MappingProxyType({
    "title": entry.title, "code": entry.code, "kind": entry.kind, "pdf_url": entry.pdf_url,
}) for entry in _REVIEWED if entry.city == "宜昌"})
ATTACHMENTS = frozenset(entry.pdf_url for entry in _REVIEWED)
LOCAL_POLICY_VERSION = "REVIEWED_LOCAL_V1:" + hashlib.sha256(
    "\n".join(entry.review_id for entry in _REVIEWED).encode("ascii")).hexdigest()


def reviewed_publication(url):
    """精确公告身份查找；政府域名、相似路径或URL参数都不能扩大权限。"""
    return REVIEWED_PUBLICATIONS.get(url) if isinstance(url, str) else None


def reviewed_attachment(url):
    return next((entry for entry in _REVIEWED if entry.pdf_url == url), None)


def reviewed_public_terms():
    return tuple(dict.fromkeys(entry.public_term for entry in _REVIEWED))


def region_matches(entry, region):
    # 省级指南不是市级规定；此处不猜测省规是否满足某市专项问题。
    return region == (entry.city or entry.province)


def local_query_entries(query, region):
    return tuple(entry for entry in _REVIEWED if region_matches(entry, region)
                 and (entry.public_term in query or any(word in query for word in entry.query_keywords)))


def canonical_local_page(url):
    """旧宜昌HTTP页面仅转为服务端HTTPS身份；新省级入口不允许HTTP降级。"""
    if not isinstance(url, str):
        return None
    canonical = "https:" + url[5:] if url.startswith("http:") else url
    return canonical if canonical in PUBLICATIONS or canonical == ROOT else None


def permitted_notice_redirect(origin, destination):
    # 旧宜昌仅一次同路径降级；SSL失败不降级，湖北新入口与全部PDF仍仅HTTPS。
    return (canonical_local_page(origin) == origin and origin.startswith("https:")
            and destination == "http:" + origin[6:])
