"""运行期重处理开关：小内存实例只允许原生文本页，扫描件识别必须放在构建期。

免费 Render 实例只有 0.1 CPU / 512MiB。本机复现一次真实扫描页 OCR 的进程峰值是
750MB—1GB（data/test-runs/render-memory-probe/results-acquire.jsonl），运行期识别会
触发平台内存终止并重启站点。

开关只决定“运行实例能否构造本机识别引擎”，不改变固定快照、原页、许可或证据规则：
关闭时只会明确失败并如实说明缺口，绝不生成替代正文，也不影响原生文本页与构建期准备。
"""

import os

ENVIRONMENT_KEY = "ARCHSPEC_RUNTIME_OCR_ENABLED"
ENABLED_VALUES = {"true", "1", "yes", "on"}
DISABLED_VALUES = {"false", "0", "no", "off"}

DISABLED_MESSAGE = (
    "本实例未开启运行期文字识别：该附件是扫描件，识别需要约750MB以上内存，"
    "超过当前实例的内存上限。正文没有加入资料库，也没有生成任何替代文字；"
    "请查询已预备正文的规范，或在构建期准备好这份规范后再检索。"
)


class RuntimeOcrDisabled(ValueError):
    """禁用运行期识别时明确失败，保留旧缓存且不生成替代正文。"""


def runtime_ocr_enabled(environ=None) -> bool:
    """未配置时保持既有行为（本机与构建期默认可识别）。"""
    raw = (os.environ if environ is None else environ).get(ENVIRONMENT_KEY, "")
    value = str(raw).strip().lower()
    if not value:
        return True
    if value in ENABLED_VALUES:
        return True
    if value in DISABLED_VALUES:
        return False
    raise ValueError(f"{ENVIRONMENT_KEY}开关配置无效，只能使用 true 或 false")


def runtime_ocr_state() -> str:
    """只用于对外如实展示的布尔状态，不泄露任何配置值。"""
    return "enabled" if runtime_ocr_enabled() else "disabled"


def require_runtime_ocr() -> None:
    """在构造识别引擎之前调用；关闭时明确拒绝，不加载任何模型。"""
    if not runtime_ocr_enabled():
        raise RuntimeOcrDisabled(DISABLED_MESSAGE)
