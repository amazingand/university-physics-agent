"""厂商无关的领域值对象与外部能力端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Message:
    """发送给模型适配器的单条消息。"""

    role: str
    content: str


@dataclass(frozen=True)
class ProviderCapabilities:
    """提供商能力声明，用于显式降级。"""

    streaming: bool = False
    tool_calls: bool = False
    structured_output: bool = False


@dataclass(frozen=True)
class CompletionRequest:
    """与具体厂商无关的最小生成请求。"""

    messages: tuple[Message, ...]
    timeout_seconds: int
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or not 1 <= self.max_tokens <= 8192
        ):
            raise ValueError("max_tokens 必须是 1 到 8192 的整数或 None")


@dataclass(frozen=True)
class HttpAttempt:
    """HTTP 发送前的非敏感尝试事件，供外部故障关闭地占用额度。"""

    provider: str
    model: str
    attempt: int
    streaming: bool


@dataclass(frozen=True)
class CompletionResult:
    """生成结果；未完成的流不得被当作最终答案。"""

    text: str
    completed: bool
    provider: str
    model: str


@dataclass(frozen=True)
class EmbeddingResult:
    """与聊天提供商独立的嵌入结果。"""

    vectors: tuple[tuple[float, ...], ...]
    provider: str
    model: str


@dataclass(frozen=True)
class KnowledgePackageRef:
    """会话固定使用的知识包及版本。"""

    package_id: str
    version: str


@dataclass(frozen=True)
class SourceReference:
    """可验证的事实来源引用。"""

    source_id: str
    source_version: str
    license_id: str
    locator: Mapping[str, str | int]


@dataclass(frozen=True)
class KnowledgeHit:
    """检索命中的稳定条目及其可验证来源。"""

    item_id: str
    text: str
    sources: tuple[SourceReference, ...]


@dataclass(frozen=True)
class LearningEvent:
    """允许进入学习记录层的结构化事件。"""

    learner_id: str
    knowledge_point_id: str
    question_id: str
    question_revision: int
    occurred_at: datetime
    hint_level: int
    performance: str
    error_type: str | None = None


class ChatProvider(Protocol):
    """聊天模型适配器端口。"""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, request: CompletionRequest) -> CompletionResult: ...

    def stream(self, request: CompletionRequest) -> Iterable[str]: ...


class EmbeddingProvider(Protocol):
    """可独立替换和配置的嵌入提供商端口。"""

    def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...


class KnowledgeRepository(Protocol):
    """对固定版本知识包进行只读检索的端口。"""

    @property
    def status(self) -> str: ...

    def search(
        self, query: str, package: KnowledgePackageRef, *, limit: int
    ) -> Sequence[KnowledgeHit]: ...


class LearningRepository(Protocol):
    """结构化学习记录的会话级端口。"""

    def record(self, event: LearningEvent) -> None: ...

    def export_json(self) -> bytes: ...

    def import_json(self, payload: bytes) -> None: ...


class DeterministicTool(Protocol):
    """受控确定性工具端口；参数由实现自行校验。"""

    @property
    def name(self) -> str: ...

    def execute(self, arguments: Mapping[str, object]) -> Mapping[str, object]: ...
