"""公开章节目录只作找书线索；不包含条文、数值、版本现行性或适用结论。"""

# 已实际取得并目视核对的公开目录标签；正文仍须独立取得、匹配和核验。
# (公开题名, 章节题名, 官方发布页, 原PDF SHA256, PDF页码)
import re

PUBLIC_SECTION_CANDIDATES = (
    ('民用建筑通用规范', ('出入口', '台阶、人行坡道', '楼梯、走廊'),
     'https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2022/art_17339_767703.html',
     '0c792fac5b2524f0326dcb750d89ed90c23cd2768f49dcbdfbd3679ca911bf8e', (14,)),
    ('民用建筑设计统一标准', ('台阶、坡道和栏杆',),
     'https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2019/art_17339_240715.html',
     'b604bcf33beb7eafe63ab2cc5759c06d0929019c70a076e8b128a18c888a4b59', (35,)),
)


def public_section_subjects():
    """仅由已核章节名拆出目录对象，不从问题、候选正文或模型增补词表。"""
    return tuple(dict.fromkeys(subject for _title, headings, _url, _sha, _pages in PUBLIC_SECTION_CANDIDATES
                               for subject in section_subjects(headings)))


def section_subjects(headings):
    """只拆已核目录标题里的并列分隔符，不处理任意正文或用户句子。"""
    return tuple(subject for heading in headings for subject in re.split('、|和', heading) if subject)
