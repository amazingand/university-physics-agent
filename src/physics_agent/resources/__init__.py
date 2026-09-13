"""可移植的运行时配置与 Schema 资源定位。"""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable


_SCHEMA_NAMES = frozenset(
    {
        "knowledge-package.schema.json",
        "learning-package-1.1.schema.json",
        "learning-package.schema.json",
        "question.schema.json",
    }
)


def default_config_resource() -> Traversable:
    """返回包内无密钥默认配置。"""

    return files(__package__).joinpath("config", "default.toml")


def schema_resource(name: str) -> Traversable:
    """按白名单返回包内权威 Schema。"""

    if name not in _SCHEMA_NAMES:
        raise ValueError(f"未知包内 Schema：{name}")
    return files(__package__).joinpath("schemas", name)
