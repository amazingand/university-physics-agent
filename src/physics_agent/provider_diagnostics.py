"""仅从受控类型和白名单字段构造公开诊断，不读取异常文本或响应正文。"""

import httpx

from physics_agent.providers.deepseek import (
    AttemptObserverError, CredentialError, IncompleteResponseError,
    ProviderConfigurationError, ProviderHTTPError, ResponseFormatError,
    RetryExhaustedError,
)


_NON_STOP_REASONS = frozenset({
    "length", "content_filter", "tool_calls", "insufficient_system_resource",
})


def provider_error_details(error: Exception | None) -> dict[str, object]:
    """None 表示提供商未抛异常但显式声明响应未完成。"""
    if isinstance(error, RetryExhaustedError):
        # 不递归跟随任意异常链，避免循环或把非结构化文本猜作根因。
        root = error.root_error
        details = _root_details(root) if not isinstance(root, RetryExhaustedError) else {"category": "unknown"}
        details["retry_exhausted"] = True
        if type(error.attempts) is int and error.attempts > 0:
            details["attempts"] = error.attempts
        return details
    return {"category": "incomplete"} if error is None else _root_details(error)


def _root_details(error: Exception | None) -> dict[str, object]:
    if isinstance(error, ProviderHTTPError):
        status = error.status_code
        if type(status) is int and 100 <= status <= 599:
            return {"category": "http", "http_status": status}
        return {"category": "unknown"}
    if isinstance(error, httpx.TimeoutException):
        return {"category": "timeout"}
    if isinstance(error, httpx.TransportError):
        return {"category": "transport"}
    if isinstance(error, ResponseFormatError):
        return {"category": "response_format"}
    if isinstance(error, IncompleteResponseError):
        reason = getattr(error, "finish_reason", None)
        if type(reason) is str and reason in _NON_STOP_REASONS:
            return {"category": "non_stop", "finish_reason": reason}
        return {"category": "incomplete"}
    for error_type, category in (
        (CredentialError, "credential"),
        (ProviderConfigurationError, "configuration"),
        (AttemptObserverError, "attempt_observer"),
    ):
        if isinstance(error, error_type):
            return {"category": category}
    return {"category": "unknown"}


def provider_error_text(details: dict[str, object]) -> str:
    """诊断只在本模块生成，类别之外的信息一律不展示。"""
    if details.get("finish_reason") == "length":
        label = "输出被截断"
    else:
        label = {
            "http": "HTTP 状态错误", "timeout": "请求超时", "transport": "传输失败",
            "response_format": "响应格式错误", "non_stop": "模型未正常结束",
            "incomplete": "响应未完成", "credential": "凭据不可用",
            "configuration": "提供商配置错误", "attempt_observer": "请求记录门禁拒绝",
        }.get(details.get("category"), "未知提供商错误")
    suffix = "，重试耗尽" if details.get("retry_exhausted") is True else ""
    return f"模型调用未完成：{label}{suffix}。本轮没有可发布的教学结论。"
