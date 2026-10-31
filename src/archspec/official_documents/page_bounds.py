"""页码显示边界取已核快照实际页数；旧位置不因此取得额外读取权限。"""
MAX_PAGES = 500
LEGACY_PAGES = 200


def page_bound(row, owner=None):
    """父记录有页数时必须一致；缺页数的旧记录沿原200页边界。"""
    owner = owner or {}
    if "page_count" in owner and "page_count" in row and owner["page_count"] != row["page_count"]:
        return 0
    value = owner.get("page_count", row.get("page_count", LEGACY_PAGES))
    return value if type(value) is int and 1 <= value <= MAX_PAGES else 0


def valid_page_range(row, owner=None):
    start, end = row.get("page_start"), row.get("page_end")
    return type(start) is int and type(end) is int and 1 <= start <= end <= page_bound(row, owner)
