"""官方问答专用配置；只读本机文件，不全局启用其他模型功能。"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from archspec.agent_runs.llm import DeepSeekChatGateway
from archspec.official_qa.model_budget import DevelopmentBudgetGateway

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ModelConfigurationError(ValueError):
    """只向调用方提供安全描述，不把配置值、路径或密钥放进异常。"""


@dataclass(frozen=True)
class ModelConfiguration:
    api_key: str = field(repr=False)
    timeout_seconds: float = 45


def load_model_configuration(*, project_root=None, environ=None):
    """环境变量按存在优先；显式空密钥可以关闭文件配置，不覆盖进程环境。"""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    environment = os.environ if environ is None else environ
    # 显式关闭先于任何文件访问；状态查询也不能为了确认关闭去读私有配置。
    if "DEEPSEEK_API_KEY" in environment and not str(environment["DEEPSEEK_API_KEY"]).strip():
        return None
    try:
        config_path = root / ".env.local"
        if "ARCHSPEC_QA_MODEL_CONFIG_FILE" in environment:
            selected = environment["ARCHSPEC_QA_MODEL_CONFIG_FILE"]
            if not isinstance(selected, str) or not selected.strip():
                raise ValueError("专用模型配置路径为空")
            config_path = Path(selected)
            if not config_path.is_absolute():
                config_path = root / config_path
            # 专用入口不能通过旧文件名或符号链接读取受保护的旧配置。
            protected = {".env.local", "live-access.local.env"}
            if config_path.name in protected or config_path.resolve().name in protected:
                raise ValueError("专用模型配置不能指向旧私有文件")
        # 不展开${变量}，不调用load_dotenv；只读所选文件，且不回退旧配置。
        values = dotenv_values(config_path, interpolate=False)
    except (OSError, ValueError, UnicodeError, RuntimeError):
        raise ModelConfigurationError("本机模型配置无法安全读取") from None

    def read(name, default=None):
        return environment[name] if name in environment else values.get(name, default)

    key = read("DEEPSEEK_API_KEY")
    if key is None or not str(key).strip():
        return None
    base_url = str(read("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).strip().rstrip("/")
    model = str(read("DEEPSEEK_MODEL", "deepseek-flash")).strip()
    if base_url != "https://api.deepseek.com" or model != "deepseek-flash":
        raise ModelConfigurationError("本入口仅支持已批准的官方DeepSeek Flash配置")
    try:
        timeout = float(read("DEEPSEEK_TIMEOUT_SECONDS", "45"))
    except (TypeError, ValueError) as error:
        raise ModelConfigurationError("模型等待时长配置无效") from error
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ModelConfigurationError("模型等待时长配置无效")
    return ModelConfiguration(str(key).strip(), timeout)


def create_budget_gateway(configuration, data_dir, *, transport=None):
    """只在同意后的模型请求调用；固定端点、输出限额与非思考模式。"""
    gateway = DeepSeekChatGateway(
        configuration.api_key, base_url="https://api.deepseek.com", model="deepseek-flash",
        timeout_seconds=configuration.timeout_seconds, transport=transport,
        # 本入口直连官方HTTPS，不继承失效的系统代理；仍校验证书，失败后不换线路重发。
        max_tokens=1024, json_output=True, thinking_disabled=True, trust_env=False)
    from archspec.persistence.runtime_bindings import current_runtime
    selected = current_runtime()
    if selected is not None:
        return selected.wrap_gateway(gateway)
    return DevelopmentBudgetGateway(gateway, Path(data_dir) / "model-budget.sqlite3")
