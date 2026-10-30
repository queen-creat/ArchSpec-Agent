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
