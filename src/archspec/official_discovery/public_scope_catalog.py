"""公开章节目录只作找书线索；不包含条文、数值、版本现行性或适用结论。"""

# 已实际取得并目视核对的公开目录标签；正文仍须独立取得、匹配和核验。
# (公开题名, 章节题名, 官方发布页, 原PDF SHA256, PDF页码)
PUBLIC_SECTION_CANDIDATES = (
    ('民用建筑通用规范', ('出入口', '台阶、人行坡道', '楼梯、走廊'),
     'https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2022/art_17339_767703.html',
     '0c792fac5b2524f0326dcb750d89ed90c23cd2768f49dcbdfbd3679ca911bf8e', (14,)),
)


def public_section_subjects():
    """仅由已核章节名拆出目录对象，不从问题、候选正文或模型增补词表。"""
    return tuple(dict.fromkeys(subject for _title, headings, _url, _sha, _pages in PUBLIC_SECTION_CANDIDATES
                               for heading in headings for subject in heading.split('、')))
