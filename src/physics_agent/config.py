"""只使用标准库读取并检查非敏感 TOML 配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit

from physics_agent.resources import default_config_resource


class ConfigError(ValueError):
    """配置缺失、类型错误或疑似直接包含凭据。"""


@dataclass(frozen=True)
class RetryConfig:
    """有界重试参数。"""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    max_total_delay_seconds: float
    jitter_ratio: float


@dataclass(frozen=True)
class ProviderConfig:
    """聊天或嵌入提供商的非敏感连接参数。"""

    provider: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: int
    retry: RetryConfig


@dataclass(frozen=True)
class AppConfig:
    """应用的非敏感配置。"""

    schema_version: str
    language: str
    chat: ProviderConfig
    embedding: ProviderConfig
    knowledge_cache_dir: str
    learning_persistence: str


_FORBIDDEN_SECRET_KEYS = {"api_key", "password", "secret", "token"}
_ROOT_KEYS = {"schema_version", "app", "chat", "embedding", "knowledge", "learning"}
_PROVIDER_KEYS = {
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "timeout_seconds",
    "retry",
}
_RETRY_KEYS = {
    "max_attempts",
    "base_delay_seconds",
    "max_delay_seconds",
    "max_total_delay_seconds",
    "jitter_ratio",
}


def _reject_inline_secrets(value: object, path: str = "") -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{path}[{index}]")
        return
    if not isinstance(value, Mapping):
        return
    for raw_key, child in value.items():
        key = str(raw_key)
        current_path = f"{path}.{key}" if path else key
        if key.lower() in _FORBIDDEN_SECRET_KEYS:
            raise ConfigError(f"配置不得直接包含凭据字段：{current_path}")
        _reject_inline_secrets(child, current_path)


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"缺少配置表：[{name}]")
    return value


def _check_keys(
    table: Mapping[str, Any], *, allowed: set[str], required: set[str], path: str
) -> None:
    keys = {str(key) for key in table}
    missing = sorted(required - keys)
    if missing:
        raise ConfigError(f"{path} 缺少字段：{', '.join(missing)}")
    unknown = sorted(keys - allowed)
    if unknown:
        raise ConfigError(f"{path} 包含未知字段：{', '.join(unknown)}")


def _string(table: Mapping[str, Any], key: str, table_name: str) -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{table_name}.{key} 必须是字符串")
    return value


def _number(table: Mapping[str, Any], key: str, table_name: str) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{table_name}.{key} 必须是数字")
    return float(value)


def _retry(table: Mapping[str, Any], provider_name: str) -> RetryConfig:
    value = table.get("retry")
    if not isinstance(value, Mapping):
        raise ConfigError(f"缺少配置表：[{provider_name}.retry]")
    path = f"{provider_name}.retry"
    _check_keys(value, allowed=_RETRY_KEYS, required=_RETRY_KEYS, path=path)

    max_attempts = value.get("max_attempts")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 5
    ):
        raise ConfigError(f"{path}.max_attempts 必须是 1 到 5 的整数")

    base_delay = _number(value, "base_delay_seconds", path)
    max_delay = _number(value, "max_delay_seconds", path)
    total_delay = _number(value, "max_total_delay_seconds", path)
    jitter = _number(value, "jitter_ratio", path)
    if not 0 <= base_delay <= 30:
        raise ConfigError(f"{path}.base_delay_seconds 必须在 0 到 30 之间")
    if not base_delay <= max_delay <= 30:
        raise ConfigError(f"{path}.max_delay_seconds 必须在基础延迟到 30 秒之间")
    if not 0 <= total_delay <= 60:
        raise ConfigError(f"{path}.max_total_delay_seconds 必须在 0 到 60 之间")
    if not 0 <= jitter <= 1:
        raise ConfigError(f"{path}.jitter_ratio 必须在 0 到 1 之间")
    return RetryConfig(
        max_attempts=max_attempts,
        base_delay_seconds=base_delay,
        max_delay_seconds=max_delay,
        max_total_delay_seconds=total_delay,
        jitter_ratio=jitter,
    )


def _provider(data: Mapping[str, Any], name: str) -> ProviderConfig:
    table = _table(data, name)
    _check_keys(table, allowed=_PROVIDER_KEYS, required=_PROVIDER_KEYS, path=name)
    timeout = table.get("timeout_seconds")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 300
    ):
        raise ConfigError(f"{name}.timeout_seconds 必须是 1 到 300 的整数")
    api_key_env = _string(table, "api_key_env", name)
    if (
        not api_key_env
        or not (api_key_env[0].isalpha() or api_key_env[0] == "_")
        or not all(character.isalnum() or character == "_" for character in api_key_env)
    ):
        raise ConfigError(f"{name}.api_key_env 必须是环境变量名称")
    provider = _string(table, "provider", name)
    base_url = _string(table, "base_url", name)
    model = _string(table, "model", name)
    if not provider:
        raise ConfigError(f"{name}.provider 不得为空")
    if provider == "unconfigured" and (base_url or model):
        raise ConfigError(f"{name} 未配置时 base_url 和 model 必须为空")
    if provider == "deepseek":
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ConfigError(f"{name}.base_url 必须是无凭据、查询和片段的 HTTPS URL")
        if not model:
            raise ConfigError(f"{name}.model 不得为空")
    return ProviderConfig(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout,
        retry=_retry(table, name),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    """读取 TOML；``None`` 使用包内默认配置，显式路径保持兼容。"""

    config_source = default_config_resource() if path is None else Path(path)
    config_label = "包内默认配置" if path is None else str(config_source)
    try:
        with config_source.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {config_label}: {exc}") from exc

    _reject_inline_secrets(data)
    _check_keys(data, allowed=_ROOT_KEYS, required=_ROOT_KEYS, path="根配置")
    schema_version = data.get("schema_version")
    if schema_version != "1.1":
        raise ConfigError("仅支持配置 schema_version = \"1.1\"")

    app = _table(data, "app")
    knowledge = _table(data, "knowledge")
    learning = _table(data, "learning")
    _check_keys(app, allowed={"language"}, required={"language"}, path="app")
    _check_keys(
        knowledge, allowed={"cache_dir"}, required={"cache_dir"}, path="knowledge"
    )
    _check_keys(
        learning, allowed={"persistence"}, required={"persistence"}, path="learning"
    )
    persistence = _string(learning, "persistence", "learning")
    if persistence != "memory":
        raise ConfigError("当前仅允许 learning.persistence = \"memory\"")

    return AppConfig(
        schema_version=schema_version,
        language=_string(app, "language", "app"),
        chat=_provider(data, "chat"),
        embedding=_provider(data, "embedding"),
        knowledge_cache_dir=_string(knowledge, "cache_dir", "knowledge"),
        learning_persistence=persistence,
    )
