"""UTF-8 YAML 题目的受限导入、校验与会话级版本注册。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
from importlib.resources.abc import Traversable
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Collection, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
import yaml
from yaml.constructor import ConstructorError
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    MappingStartEvent,
    NodeEvent,
    ScalarEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from physics_agent.resources import schema_resource


MAX_QUESTION_BYTES = 256 * 1024
MAX_YAML_DEPTH = 20
MAX_YAML_NODES = 10_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_UNIT_TEXT = re.compile(r"^[A-Za-z0-9_*/^(). +\-]+$")
_JSON_TAGS = {
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
}
_SHORT_JSON_TAGS = {"str", "int", "float", "bool", "null", "map", "seq"}
_FORBIDDEN_YAML_TAGS = {
    "tag:yaml.org,2002:timestamp",
    "tag:yaml.org,2002:binary",
    "tag:yaml.org,2002:set",
}


class QuestionImportError(RuntimeError):
    """题目无法安全导入。"""


class QuestionFileError(QuestionImportError):
    """输入文件不满足普通文件、有界读取或编码要求。"""


class QuestionSyntaxError(QuestionImportError):
    """YAML 使用了不受支持或不安全的结构。"""


class QuestionValidationError(QuestionImportError):
    """题目不符合 Schema 或应用语义。"""


class QuestionTrustError(QuestionValidationError):
    """题目声明了未经应用侧固定摘要批准的审核状态。"""


class QuestionConflictError(QuestionImportError):
    """同一题目 ID 和 revision 已存在不同内容。"""


@dataclass(frozen=True)
class QuestionImportResult:
    """一次题目导入的稳定结果；``created=False`` 表示幂等命中。"""

    question_id: str
    revision: int
    content_sha256: str
    canonical_json: bytes
    created: bool


@dataclass(frozen=True)
class _StoredQuestion:
    data: dict[str, Any]
    canonical_json: bytes
    content_sha256: str


class _QuestionLoader(yaml.SafeLoader):
    """只构造 JSON 数据类型且拒绝重复键和 merge key。"""

    def construct_mapping(  # type: ignore[override]
        self, node: MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "只允许映射节点", node.start_mark)

        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise ConstructorError(
                    None, None, "YAML 映射键必须是字符串", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ConstructorError(
                    None, None, "YAML 映射键必须是字符串", key_node.start_mark
                )
            if key == "<<" or key_node.tag == "tag:yaml.org,2002:merge":
                raise ConstructorError(
                    None, None, "不允许 YAML merge key", key_node.start_mark
                )
            if key in result:
                raise ConstructorError(
                    None, None, "YAML 映射包含重复键", key_node.start_mark
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _reject_yaml_native_type(
    loader: _QuestionLoader, node: yaml.Node
) -> Any:  # pragma: no cover - 返回类型仅满足 PyYAML 构造器签名
    del loader
    raise ConstructorError(
        None, None, "不允许 YAML timestamp、binary 或 set 类型", node.start_mark
    )


for _tag in _FORBIDDEN_YAML_TAGS:
    _QuestionLoader.add_constructor(_tag, _reject_yaml_native_type)


class QuestionImporter:
    """安全导入并在当前进程保留题目各 revision。

    ``approved_review_sha256`` 是应用侧、针对规范 JSON 的精确人工审核摘要。
    未列入该集合的外部 YAML 不得声明 ``published`` 或
    ``human_reviewed=true``。
    """

    def __init__(
        self,
        *,
        approved_review_sha256: Collection[str] = (),
    ) -> None:
        self._validator = _load_local_validator(
            schema_resource("question.schema.json")
        )
        approved = frozenset(approved_review_sha256)
        if any(type(value) is not str or not _SHA256.fullmatch(value) for value in approved):
            raise ValueError("人工审核摘要必须是小写 SHA-256")
        self._approved_review_sha256 = approved
        self._questions: dict[tuple[str, int], _StoredQuestion] = {}

    def import_file(self, path: str | os.PathLike[str]) -> QuestionImportResult:
        """导入一个显式 YAML 普通文件；失败时不改变已注册版本。"""

        payload = _read_regular_file(path)
        document = _load_restricted_yaml(payload)
        _validate_json_value(document)
        if type(document) is not dict:
            raise QuestionValidationError("题目根节点必须是对象")

        self._validate_schema(document)
        _validate_semantics(document)
        canonical = _canonical_json(document)
        digest = hashlib.sha256(canonical).hexdigest()
        _validate_review_trust(document, digest, self._approved_review_sha256)

        question_id = document["id"]
        revision = document["revision"]
        key = (question_id, revision)
        existing = self._questions.get(key)
        if existing is not None:
            if existing.content_sha256 != digest:
                raise QuestionConflictError(
                    f"题目 {question_id} revision {revision} 已存在不同内容"
                )
            return QuestionImportResult(
                question_id=question_id,
                revision=revision,
                content_sha256=digest,
                canonical_json=existing.canonical_json,
                created=False,
            )

        self._questions[key] = _StoredQuestion(
            data=deepcopy(document),
            canonical_json=canonical,
            content_sha256=digest,
        )
        return QuestionImportResult(
            question_id=question_id,
            revision=revision,
            content_sha256=digest,
            canonical_json=canonical,
            created=True,
        )

    def get(self, question_id: str, revision: int) -> dict[str, Any]:
        """返回指定版本的防御性副本。"""

        try:
            stored = self._questions[(question_id, revision)]
        except KeyError as exc:
            raise KeyError(f"未知题目版本：{question_id}@{revision}") from exc
        return deepcopy(stored.data)

    def revisions(self, question_id: str) -> tuple[int, ...]:
        """按升序返回某题已导入的所有 revision。"""

        return tuple(
            sorted(revision for item_id, revision in self._questions if item_id == question_id)
        )

    def canonical_json(self, question_id: str, revision: int) -> bytes:
        """返回指定版本的稳定、UTF-8 规范 JSON。"""

        try:
            return self._questions[(question_id, revision)].canonical_json
        except KeyError as exc:
            raise KeyError(f"未知题目版本：{question_id}@{revision}") from exc

    def _validate_schema(self, document: Mapping[str, Any]) -> None:
        errors = sorted(
            self._validator.iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise QuestionValidationError(f"题目 Schema 校验失败：{location}: {first.message}")


def _read_regular_file(path: str | os.PathLike[str]) -> bytes:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise QuestionFileError("题目路径必须是显式文件路径") from exc
    if not raw_path:
        raise QuestionFileError("题目路径不得为空")
    if "O_NOFOLLOW" not in dir(os) or "O_CLOEXEC" not in dir(os):
        raise QuestionFileError("当前平台缺少安全打开普通文件所需能力")

    try:
        before_path = os.lstat(raw_path)
    except (OSError, ValueError) as exc:
        raise QuestionFileError("题目文件不存在或不可访问") from exc
    if not stat.S_ISREG(before_path.st_mode):
        raise QuestionFileError("题目输入必须是普通文件且不能是符号链接")

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(raw_path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENOTDIR}:
            raise QuestionFileError("题目输入必须是普通文件且不能是符号链接") from exc
        raise QuestionFileError("题目文件无法安全打开") from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise QuestionFileError("题目输入必须是普通文件")
        if (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino):
            raise QuestionFileError("题目文件在打开过程中发生替换")
        if before.st_size > MAX_QUESTION_BYTES:
            raise QuestionFileError(f"题目文件超过 {MAX_QUESTION_BYTES} 字节上限")

        chunks: list[bytes] = []
        remaining = MAX_QUESTION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise QuestionFileError("读取题目文件失败") from exc
    finally:
        os.close(descriptor)

    if len(payload) > MAX_QUESTION_BYTES:
        raise QuestionFileError(f"题目文件超过 {MAX_QUESTION_BYTES} 字节上限")
    if _file_identity(before) != _file_identity(after) or len(payload) != after.st_size:
        raise QuestionFileError("读取期间题目文件发生变化")
    if b"\x00" in payload:
        raise QuestionFileError("题目文件不得包含 NUL 字节")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QuestionFileError("题目文件必须是有效 UTF-8") from exc
    return payload


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _load_restricted_yaml(payload: bytes) -> Any:
    text = payload.decode("utf-8")
    _scan_yaml(text)
    try:
        document = yaml.load(text, Loader=_QuestionLoader)
    except (yaml.YAMLError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise QuestionSyntaxError("YAML 语法或数据类型不受支持") from exc
    return document


def _scan_yaml(text: str) -> None:
    try:
        for token in yaml.scan(text, Loader=_QuestionLoader):
            if isinstance(token, (AnchorToken, AliasToken)):
                raise QuestionSyntaxError("不允许 YAML anchor 或 alias")
            if isinstance(token, TagToken) and not _is_json_tag(token.value):
                raise QuestionSyntaxError("不允许 YAML 自定义或非 JSON tag")

        documents = 0
        depth = 0
        nodes = 0
        for event in yaml.parse(text, Loader=_QuestionLoader):
            if isinstance(event, AliasEvent) or (
                isinstance(event, NodeEvent) and event.anchor is not None
            ):
                raise QuestionSyntaxError("不允许 YAML anchor 或 alias")
            if event.__class__.__name__ == "DocumentStartEvent":
                documents += 1
                if documents > 1:
                    raise QuestionSyntaxError("一份题目文件只允许一个 YAML 文档")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                nodes += 1
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise QuestionSyntaxError(f"YAML 深度超过 {MAX_YAML_DEPTH}")
            elif isinstance(event, ScalarEvent):
                nodes += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
                if depth < 0:
                    raise QuestionSyntaxError("YAML 集合结构不平衡")
            if nodes > MAX_YAML_NODES:
                raise QuestionSyntaxError(f"YAML 节点数超过 {MAX_YAML_NODES}")
        if documents != 1 or depth != 0:
            raise QuestionSyntaxError("题目文件必须恰好包含一个完整 YAML 文档")
    except QuestionSyntaxError:
        raise
    except yaml.YAMLError as exc:
        raise QuestionSyntaxError("YAML 语法不受支持") from exc


def _is_json_tag(value: tuple[str | None, str]) -> bool:
    handle, suffix = value
    if handle == "!!":
        return suffix in _SHORT_JSON_TAGS
    if handle is None:
        return suffix in _JSON_TAGS
    return False


def _validate_json_value(value: Any, *, path: str = "<root>") -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise QuestionValidationError(f"{path} 不得包含非有限数")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise QuestionValidationError(f"{path} 的键必须是字符串")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise QuestionValidationError(f"{path} 包含非 JSON 数据类型")


def _load_local_validator(schema_path: Path | Traversable) -> Draft202012Validator:
    try:
        payload = schema_path.read_bytes()
        schema = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QuestionValidationError("固定本地题目 Schema 无法读取") from exc
    _reject_remote_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise QuestionValidationError("固定本地题目 Schema 无效") from exc
    return Draft202012Validator(schema)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON Schema 含重复键：{key}")
        result[key] = value
    return result


def _reject_remote_refs(value: Any) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if key == "$ref" and (type(item) is not str or not item.startswith("#")):
                raise QuestionValidationError("固定题目 Schema 不允许远程 $ref")
            _reject_remote_refs(item)
    elif type(value) is list:
        for item in value:
            _reject_remote_refs(item)


def _validate_semantics(document: Mapping[str, Any]) -> None:
    answer = document.get("answer")
    expected_kinds = {
        "conceptual": "text",
        "numeric": "numeric",
        "derivation": "derivation",
        "multiple_choice": "choice",
    }
    if answer is not None:
        expected = expected_kinds[document["type"]]
        if answer.get("kind") != expected:
            raise QuestionValidationError("题型与 answer.kind 不一致")

    hints = document.get("hints")
    if hints is not None:
        levels = [hint["level"] for hint in hints]
        if levels != list(range(1, len(levels) + 1)):
            raise QuestionValidationError("hint level 必须从 1 开始连续且按升序排列")
        for hint in hints:
            _nonblank(hint["text"], "hint.text")

    if document["type"] != "multiple_choice" and "choices" in document:
        raise QuestionValidationError("非选择题不得包含 choices")
    if document["type"] == "multiple_choice" and answer is not None:
        choices = document["choices"]
        if any(index >= len(choices) for index in answer["choice_indexes"]):
            raise QuestionValidationError("选择题答案下标超出 choices 范围")
        for choice in choices:
            _nonblank(choice, "choices")

    if document["type"] == "numeric" and answer is not None:
        _finite_number(answer["value"], "answer.value")
        _validate_unit(answer["unit"])
        tolerance = answer["tolerance"]
        for name in ("absolute", "relative"):
            if name in tolerance:
                value = _finite_number(tolerance[name], f"answer.tolerance.{name}")
                if value < 0:
                    raise QuestionValidationError("数值答案容差不得为负")

    if document["type"] == "derivation" and answer is not None:
        for name in ("assumptions", "equivalent_expressions", "key_steps"):
            values = answer[name]
            if not values:
                raise QuestionValidationError(f"derivation.{name} 不得为空")
            for value in values:
                _nonblank(value, f"derivation.{name}")

    if document["type"] == "conceptual" and answer is not None:
        _nonblank(answer["text"], "answer.text")

    for item in document.get("rubric", []):
        _nonblank(item["criterion"], "rubric.criterion")
        points = _finite_number(item["points"], "rubric.points")
        if points <= 0:
            raise QuestionValidationError("rubric.points 必须大于 0")

    _nonblank(document["statement"], "statement")
    if "explanation" in document:
        _nonblank(document["explanation"], "explanation")
    provenance = document["provenance"]
    alignment = provenance["course_alignment"]
    for name in ("book_title", "edition"):
        _nonblank(alignment[name], f"provenance.course_alignment.{name}")
    sources = provenance["factual_sources"]
    if not sources:
        raise QuestionValidationError("provenance.factual_sources 至少需要一个稳定来源")
    for source in sources:
        for name in ("source_id", "source_version", "license_id"):
            _nonblank(source[name], f"provenance.factual_sources.{name}")
        locator = source["locator"]
        for value in locator.values():
            if type(value) is str:
                _nonblank(value, "provenance.factual_sources.locator")
    transformation = provenance.get("transformation")
    if transformation is not None:
        _nonblank(transformation, "provenance.transformation")

    review = document["review"]
    for field in ("reviewer_id",):
        if field in review:
            _nonblank(review[field], f"review.{field}")
    for evidence in review.get("evidence_refs", []):
        _nonblank(evidence, "review.evidence_refs")
    if "reviewed_at" in review:
        _validate_rfc3339(review["reviewed_at"])


def _validate_review_trust(
    document: Mapping[str, Any], digest: str, approved: Collection[str]
) -> None:
    elevated = (
        document["review"]["status"] == "published"
        or document["provenance"]["human_reviewed"] is True
    )
    if elevated and digest not in approved:
        raise QuestionTrustError(
            "外部 YAML 的 published/human_reviewed 声明缺少应用侧精确摘要批准"
        )


def _validate_rfc3339(value: str) -> None:
    if not _RFC3339.fullmatch(value):
        raise QuestionValidationError("review.reviewed_at 必须是带时区的 RFC 3339 时间")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QuestionValidationError("review.reviewed_at 不是有效日期时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QuestionValidationError("review.reviewed_at 必须包含时区")


def _finite_number(value: Any, field: str) -> float:
    if type(value) not in {int, float}:
        raise QuestionValidationError(f"{field} 必须是有限数")
    try:
        number = float(value)
    except OverflowError as exc:
        raise QuestionValidationError(f"{field} 必须是有限数") from exc
    if not math.isfinite(number):
        raise QuestionValidationError(f"{field} 必须是有限数")
    return number


def _validate_unit(value: str) -> None:
    _nonblank(value, "answer.unit")
    if len(value) > 64 or not _UNIT_TEXT.fullmatch(value) or "__" in value:
        raise QuestionValidationError("answer.unit 不符合受限单位语法")
    import pint

    try:
        pint.UnitRegistry(autoconvert_offset_to_baseunit=False).parse_units(
            value.replace("^", "**")
        )
    except (pint.errors.PintError, ValueError, TypeError) as exc:
        raise QuestionValidationError("answer.unit 不是可识别单位") from exc


def _nonblank(value: str, field: str) -> None:
    if not value.strip():
        raise QuestionValidationError(f"{field} 不得为空白")


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # 防御：正常由 _validate_json_value 提前拦截
        raise QuestionValidationError("题目不能规范化为 JSON") from exc
    return encoded + b"\n"
