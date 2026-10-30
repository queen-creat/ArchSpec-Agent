"""模型服务边界：业务层只依赖这个合同，不直接依赖 DeepSeek。"""

import json
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class LlmToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class LlmUsage:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None


@dataclass(frozen=True)
class LlmTurnResult:
    content: str | None
    tool_calls: list[LlmToolCall]
    usage: LlmUsage
    model: str
    request_id: str | None
    finish_reason: str | None


class LlmGateway(Protocol):
    """Agent 编排器使用的统一模型接口。"""

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> LlmTurnResult: ...


class LlmGatewayError(RuntimeError):
    def __init__(self, code: str, user_message: str, retryable: bool) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.retryable = retryable


_PROVIDER_ERRORS: dict[int, tuple[str, str, bool]] = {
    400: ("INVALID_REQUEST", "模型请求格式不正确", False),
    401: ("INVALID_API_KEY", "DeepSeek API Key 无效", False),
    402: ("INSUFFICIENT_BALANCE", "DeepSeek 账户余额不足", False),
    422: ("INVALID_PARAMETERS", "DeepSeek 请求参数不受支持", False),
    429: ("RATE_LIMITED", "DeepSeek 请求过于频繁，请稍后重试", True),
    500: ("PROVIDER_ERROR", "DeepSeek 服务暂时异常", True),
    503: ("PROVIDER_OVERLOADED", "DeepSeek 服务繁忙，请稍后重试", True),
}


class DeepSeekChatGateway:
    """通过 DeepSeek 的 OpenAI 兼容接口完成一轮模型调用。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        timeout_seconds: float = 45,
        transport: httpx.BaseTransport | None = None,
        max_tokens: int | None = None,
        json_output: bool = False,
        thinking_disabled: bool = False,
        trust_env: bool = True,
    ) -> None:
        if max_tokens is not None and (type(max_tokens) is not int or not 64 <= max_tokens <= 4096):
            raise ValueError("max_tokens must be an integer between 64 and 4096")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.max_tokens = max_tokens
        self.json_output = json_output
        self.thinking_disabled = thinking_disabled
        self.trust_env = trust_env

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> LlmTurnResult:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.json_output:
            payload["response_format"] = {"type": "json_object"}
        if self.thinking_disabled:
            payload["thinking"] = {"type": "disabled"}
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.timeout_seconds,
                trust_env=self.trust_env,
            ) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise LlmGatewayError(
                "NETWORK_ERROR", "无法连接 DeepSeek，请检查网络或代理", True
            ) from error

        if response.status_code >= 400:
            code, message, retryable = _PROVIDER_ERRORS.get(
                response.status_code,
                ("PROVIDER_ERROR", "DeepSeek 返回了无法处理的错误", True),
            )
            raise LlmGatewayError(code, message, retryable)

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            calls = [self._parse_tool_call(item) for item in message.get("tool_calls", [])]
            usage = body.get("usage", {})
            return LlmTurnResult(
                content=message.get("content"),
                tool_calls=calls,
                usage=LlmUsage(
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    cached_tokens=(
                        int(usage["prompt_cache_hit_tokens"])
                        if usage.get("prompt_cache_hit_tokens") is not None
                        else None
                    ),
                ),
                model=str(body.get("model", self.model)),
                request_id=body.get("id"),
                finish_reason=choice.get("finish_reason"),
            )
        except LlmGatewayError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise LlmGatewayError(
                "INVALID_MODEL_RESPONSE", "DeepSeek 返回内容格式异常", False
            ) from error

    @staticmethod
    def _parse_tool_call(item: dict[str, object]) -> LlmToolCall:
        try:
            function = item["function"]
            if not isinstance(function, dict):
                raise TypeError
            arguments = json.loads(str(function["arguments"]))
            if not isinstance(arguments, dict):
                raise TypeError
            return LlmToolCall(
                id=str(item["id"]),
                name=str(function["name"]),
                arguments=arguments,
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise LlmGatewayError(
                "INVALID_TOOL_ARGUMENTS", "模型返回了无法读取的工具参数", False
            ) from error
