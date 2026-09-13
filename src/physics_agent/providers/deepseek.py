"""DeepSeek Chat Completions 同步适配器。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import os
import random
import time
from typing import Any

import httpx

from physics_agent.config import ProviderConfig
from physics_agent.core import (
    CompletionRequest,
    CompletionResult,
    HttpAttempt,
    ProviderCapabilities,
)


RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
KNOWN_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "insufficient_system_resource",
}


class ProviderError(RuntimeError):
    """可安全显示给 CLI 用户的提供商错误。"""


class ProviderConfigurationError(ProviderError):
    """提供商配置与当前适配器不兼容。"""


class CredentialError(ProviderError):
    """指定环境变量中没有可用凭据。"""


class ProviderHTTPError(ProviderError):
    """不可重试的 HTTP 状态。"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"DeepSeek API 返回 HTTP {status_code}，请求未完成")


class RetryExhaustedError(ProviderError):
    """可重试错误已达到尝试上限。"""

    def __init__(self, message: str, *, cause: Exception | None = None,
                 attempts: int | None = None) -> None:
        self.root_error = cause
        self.attempts = attempts
        super().__init__(message)


class AttemptObserverError(ProviderError):
    """HTTP 尝试观察器拒绝或未能记录本次发送。"""


class ResponseFormatError(ProviderError):
    """响应 JSON 或字段结构不符合预期。"""


class IncompleteResponseError(ProviderError):
    """模型明确或实际未正常完成响应。"""


class IncompleteCompletionError(IncompleteResponseError):
    """普通响应未正常结束。"""

    def __init__(self, finish_reason: str, partial_text: str) -> None:
        self.finish_reason = finish_reason
        self.partial_text = partial_text
        super().__init__(
            f"DeepSeek 普通响应因 finish_reason={finish_reason!r} 未正常结束，结果未完成"
        )


class IncompleteStreamError(IncompleteResponseError):
    """流未收到唯一有效完成标记。"""

    def __init__(self, message: str, *, finish_reason: str | None = None) -> None:
        self.finish_reason = finish_reason
        super().__init__(message)


class DeepSeekProvider:
    """只实现 M2 所需文本 Chat Completions，不启用思考或工具。"""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        now: Callable[[], float] = time.time,
        attempt_observer: Callable[[HttpAttempt], None] | None = None,
    ) -> None:
        if config.provider != "deepseek":
            raise ProviderConfigurationError(
                f"当前适配器不支持 provider={config.provider!r}"
            )
        source_environ = os.environ if environ is None else environ
        api_key = source_environ.get(config.api_key_env)
        if not api_key:
            raise CredentialError(f"环境变量 {config.api_key_env} 未设置或为空")

        self._config = config
        self._endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
        self._sleep = sleep
        self._random_value = random_value
        self._now = now
        if attempt_observer is not None and not callable(attempt_observer):
            raise TypeError("attempt_observer 必须可调用或为 None")
        self._attempt_observer = attempt_observer
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=config.timeout_seconds,
            verify=True,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_calls=False,
            structured_output=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DeepSeekProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = self._payload(request, stream=False)
        total_delay = 0.0
        for attempt in range(1, self._config.retry.max_attempts + 1):
            try:
                self._observe_attempt(attempt, streaming=False)
                response = self._client.post(
                    self._endpoint,
                    json=payload,
                    timeout=request.timeout_seconds,
                )
            except httpx.TransportError as exc:
                if attempt >= self._config.retry.max_attempts:
                    raise self._retry_exhausted(attempt, exc) from exc
                total_delay = self._wait_before_retry(attempt, total_delay, None)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt >= self._config.retry.max_attempts:
                    raise self._retry_exhausted(
                        attempt, ProviderHTTPError(response.status_code)
                    )
                total_delay = self._wait_before_retry(
                    attempt, total_delay, response.headers.get("Retry-After")
                )
                continue
            if not 200 <= response.status_code < 300:
                raise ProviderHTTPError(response.status_code)
            return self._parse_completion(response)

        raise AssertionError("重试循环不应执行到此处")

    def stream(self, request: CompletionRequest) -> Iterator[str]:
        payload = self._payload(request, stream=True)
        total_delay = 0.0
        produced_content = False

        for attempt in range(1, self._config.retry.max_attempts + 1):
            terminal_reason: str | None = None
            try:
                self._observe_attempt(attempt, streaming=True)
                with self._client.stream(
                    "POST",
                    self._endpoint,
                    json=payload,
                    timeout=request.timeout_seconds,
                ) as response:
                    if response.status_code in RETRYABLE_STATUS_CODES:
                        if attempt >= self._config.retry.max_attempts:
                            raise self._retry_exhausted(
                                attempt, ProviderHTTPError(response.status_code)
                            )
                        total_delay = self._wait_before_retry(
                            attempt,
                            total_delay,
                            response.headers.get("Retry-After"),
                        )
                        continue
                    if not 200 <= response.status_code < 300:
                        raise ProviderHTTPError(response.status_code)

                    for line in response.iter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if terminal_reason is None:
                                raise IncompleteStreamError(
                                    "DeepSeek 流式响应收到 [DONE]，但缺少终止原因，结果未完成"
                                )
                            if terminal_reason != "stop":
                                raise IncompleteStreamError(
                                    "DeepSeek 流式响应因 "
                                    f"finish_reason={terminal_reason!r} 未正常结束，结果未完成",
                                    finish_reason=terminal_reason,
                                )
                            if not produced_content:
                                raise IncompleteStreamError(
                                    "DeepSeek 流式响应没有输出文本，结果未完成"
                                )
                            return
                        if not data:
                            continue
                        if terminal_reason is not None:
                            raise ResponseFormatError(
                                "DeepSeek 流式响应在终止原因后仍包含 data JSON，结果未完成"
                            )
                        contents, finish_reason = self._parse_stream_data(data)
                        if finish_reason is not None:
                            if (
                                terminal_reason is not None
                                and terminal_reason != finish_reason
                            ):
                                raise ResponseFormatError(
                                    "DeepSeek 流式响应包含冲突的终止原因，结果未完成"
                                )
                            terminal_reason = finish_reason
                        for content in contents:
                            produced_content = True
                            yield content

                    raise IncompleteStreamError(
                        "DeepSeek 流式响应提前结束，未收到 [DONE]，结果未完成"
                    )
            except httpx.TransportError as exc:
                if produced_content:
                    raise IncompleteStreamError(
                        "DeepSeek 流式响应在输出内容后中断，结果未完成且未重试"
                    ) from exc
                if attempt >= self._config.retry.max_attempts:
                    raise self._retry_exhausted(attempt, exc) from exc
                total_delay = self._wait_before_retry(attempt, total_delay, None)

        raise AssertionError("重试循环不应执行到此处")

    def _payload(self, request: CompletionRequest, *, stream: bool) -> dict[str, Any]:
        if not request.messages:
            raise ProviderConfigurationError("请求至少需要一条消息")
        if not 1 <= request.timeout_seconds <= 300:
            raise ProviderConfigurationError("请求超时必须在 1 到 300 秒之间")
        messages: list[dict[str, str]] = []
        for message in request.messages:
            if message.role not in {"system", "user", "assistant"}:
                raise ProviderConfigurationError(f"M2 不支持消息角色 {message.role!r}")
            if not message.content:
                raise ProviderConfigurationError("消息内容不得为空")
            messages.append({"role": message.role, "content": message.content})
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": stream,
            "thinking": {"type": "disabled"},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload

    def _observe_attempt(self, attempt: int, *, streaming: bool) -> None:
        if self._attempt_observer is None:
            return
        event = HttpAttempt(
            provider="deepseek",
            model=self._config.model,
            attempt=attempt,
            streaming=streaming,
        )
        try:
            self._attempt_observer(event)
        except Exception as exc:
            raise AttemptObserverError(
                "HTTP 尝试观察器未能记录或拒绝了本次请求；请求尚未发送"
            ) from exc

    def _parse_completion(self, response: httpx.Response) -> CompletionResult:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResponseFormatError("DeepSeek 普通响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ResponseFormatError("DeepSeek 普通响应根节点必须是对象")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ResponseFormatError("DeepSeek 普通响应缺少 choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ResponseFormatError("DeepSeek 普通响应的 choice 结构无效")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ResponseFormatError("DeepSeek 普通响应缺少 message")
        finish_reason = first_choice.get("finish_reason")
        if not isinstance(finish_reason, str) or finish_reason not in KNOWN_FINISH_REASONS:
            raise ResponseFormatError(
                "DeepSeek 普通响应缺少有效 finish_reason，结果未完成"
            )
        content = message.get("content")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            raise ResponseFormatError("DeepSeek 普通响应缺少文本 content")
        if finish_reason != "stop":
            raise IncompleteCompletionError(finish_reason, content)
        if not content:
            raise IncompleteCompletionError("stop-without-content", content)
        return CompletionResult(
            text=content,
            completed=True,
            provider="deepseek",
            model=self._config.model,
        )

    def _parse_stream_data(self, data: str) -> tuple[tuple[str, ...], str | None]:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ResponseFormatError("DeepSeek 流式响应包含畸形 JSON，结果未完成") from exc
        if not isinstance(payload, dict):
            raise ResponseFormatError("DeepSeek 流式响应根节点必须是对象，结果未完成")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ResponseFormatError("DeepSeek 流式响应缺少 choices，结果未完成")
        if len(choices) != 1:
            raise ResponseFormatError("M2 流式响应必须恰好包含一个 choice，结果未完成")

        contents: list[str] = []
        finish_reason: str | None = None
        for choice in choices:
            if not isinstance(choice, dict):
                raise ResponseFormatError("DeepSeek 流式响应 choice 结构无效，结果未完成")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ResponseFormatError("DeepSeek 流式响应缺少 delta，结果未完成")
            raw_finish_reason = choice.get("finish_reason")
            if raw_finish_reason is not None:
                if (
                    not isinstance(raw_finish_reason, str)
                    or raw_finish_reason not in KNOWN_FINISH_REASONS
                ):
                    raise ResponseFormatError(
                        "DeepSeek 流式响应包含未知 finish_reason，结果未完成"
                    )
                finish_reason = raw_finish_reason
            content = delta.get("content")
            if content is None:
                continue
            if not isinstance(content, str):
                raise ResponseFormatError(
                    "DeepSeek 流式响应 content 不是字符串，结果未完成"
                )
            if content:
                contents.append(content)
        return tuple(contents), finish_reason

    def _wait_before_retry(
        self, attempt: int, total_delay: float, retry_after: str | None
    ) -> float:
        configured = self._config.retry
        header_delay = self._retry_after_seconds(retry_after)
        if header_delay is None:
            exponential = configured.base_delay_seconds * (2 ** (attempt - 1))
            jitter_value = min(1.0, max(0.0, self._random_value()))
            requested = exponential * (1 + configured.jitter_ratio * jitter_value)
        else:
            requested = header_delay

        requested = min(requested, configured.max_delay_seconds)
        remaining = max(0.0, configured.max_total_delay_seconds - total_delay)
        delay = min(requested, remaining)
        if delay > 0:
            self._sleep(delay)
        return total_delay + delay

    def _retry_after_seconds(self, value: str | None) -> float | None:
        if value is None:
            return None
        stripped = value.strip()
        if stripped.isdigit():
            return float(stripped)
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - self._now())

    @staticmethod
    def _retry_exhausted(attempts: int, reason: Exception) -> RetryExhaustedError:
        if isinstance(reason, ProviderHTTPError):
            label = f"HTTP {reason.status_code}"
        elif isinstance(reason, httpx.TimeoutException):
            label = "timeout"
        else:
            label = "transport"
        return RetryExhaustedError(
            f"DeepSeek 请求在 {attempts} 次尝试后仍失败（{label}），请求未完成",
            cause=reason,
            attempts=attempts,
        )
