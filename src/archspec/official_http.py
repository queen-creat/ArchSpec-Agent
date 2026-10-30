"""官方搜索、公告与附件共用网络模式；不改变URL、TLS、重定向或读取预算。"""

import os

import httpx


def official_http_mode():
    """只读取程序自身配置；默认沿用系统，显式直连不修改电脑代理。"""
    mode = os.getenv("ARCHSPEC_OFFICIAL_HTTP_MODE", "SYSTEM")
    if mode not in {"SYSTEM", "DIRECT"}:
        raise ValueError("官方网络模式须为SYSTEM或DIRECT")
    return mode


def official_http_client(**options):
    """所有默认官方客户端共用配置，显式注入的测试或服务端客户端仍由调用方持有。

    DIRECT仅忽略代理等环境设置，证书校验仍用HTTP客户端默认的可信证书；
    不关闭TLS、不访问其它来源、不自动重试失败请求。
    """
    return httpx.Client(trust_env=official_http_mode() == "SYSTEM", **options)
