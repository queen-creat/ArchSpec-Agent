"""给网页提供具体但不泄露网址、凭据或响应内容的下载失败原因。"""

import httpx


def download_failure_message(error: httpx.HTTPError) -> str:
    # 只读异常类型和状态码：异常原文可能包含代理凭据、完整网址或服务响应。
    # 这些提示描述本次失败，不自动判定附件永久失效，也不发起额外重试。
    if isinstance(error, httpx.TimeoutException):
        reason = "官方附件下载等待超时"
    elif isinstance(error, httpx.ConnectError):
        reason = "官方附件连接未能建立"
    elif isinstance(error, httpx.HTTPStatusError):
        reason = f"官方下载服务返回 HTTP {error.response.status_code}"
    else:
        reason = "官方附件下载通信失败"
    return f"{reason}，本次未取得完整文件；可稍后重试，已有成功缓存保留"


class AttachmentInputRejected(ValueError):
    """仅由下载边界签发的固定原因；不保存网址、响应正文或凭据。"""

    def __init__(self, code, *, max_bytes=None):
        if code == "SIZE_LIMIT" and type(max_bytes) is int and max_bytes in (
                30 * 1024 * 1024, 80 * 1024 * 1024):
            message = f"规范附件超过{max_bytes // (1024 * 1024)}MB限制"
        elif code == "INVALID_PDF":
            message = "官方附件响应不是有效PDF"
        else:
            raise ValueError("附件下载原因未登记")
        self.code = code
        super().__init__(message)
