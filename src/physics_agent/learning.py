"""会话级结构化学习记录、版本迁移和安全 JSON 导入导出。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from physics_agent.core import KnowledgePackageRef, LearningEvent
from physics_agent.resources import schema_resource


LEARNING_SCHEMA_VERSION = "1.1"
DEFAULT_DATA_VERSION = "1"
MAX_IMPORT_BYTES = 1024 * 1024
MAX_RECORDS = 10_000
MAX_WEAKNESS_CONFIDENCE = 0.95

_STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_ANONYMOUS_ID = re.compile(r"^anon_[A-Za-z0-9_-]{12,128}$")
_ERROR_TYPES = frozenset(
    {
        "concept",
        "model",
        "force_omission",
        "symbol",
        "unit",
        "algebra",
        "order_of_magnitude",
        "other",
    }
)
_OUTCOMES = frozenset({"correct", "partial", "incorrect", "unattempted"})
_ERROR_OUTCOMES = frozenset({"partial", "incorrect"})
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)

LearningKey = tuple[str, str, int]


class LearningDataError(RuntimeError):
    """学习数据无法被安全处理。"""


class LearningValidationError(LearningDataError):
    """输入结构、版本或字段值不符合学习包契约。"""


class LearningConflictError(LearningDataError):
    """操作与现有稳定记录冲突。"""


class LearningNotFoundError(LearningDataError):
    """指定的稳定记录不存在。"""


class LearningExportError(LearningDataError):
    """学习包无法安全写入目标路径。"""


@dataclass(frozen=True)
class ErrorSummary:
    """不包含原始回答的错误累计摘要。"""

    error_type: str
    count: int
    correct_count: int
    status: str
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class LearningRecord:
    """单个知识点、题目及修订的结构化学习记录。"""

    knowledge_point_id: str
    question_id: str
    question_revision: int
    hint_level: int
    performance: str
    score: float | None
    error: ErrorSummary | None
    weakness_confidence: float
    updated_at: datetime

    @property
    def key(self) -> LearningKey:
        return (
            self.knowledge_point_id,
            self.question_id,
            self.question_revision,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"JSON 对象包含重复键：{key}")
        result[key] = value
    return result


def _reject_non_finite(token: str) -> None:
    raise ValueError(f"JSON 包含非有限数：{token}")


def _as_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise LearningValidationError(f"{field} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LearningValidationError(f"{field} 必须带时区")
    normalized = value.astimezone(timezone.utc)
    if normalized.year < 1 or normalized.year > 9999:
        raise LearningValidationError(f"{field} 超出支持范围")
    return normalized


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise LearningValidationError(f"{field} 必须是 RFC 3339 字符串")
    candidate = value
    if value.endswith("Z"):
        candidate = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LearningValidationError(f"{field} 不是有效的 RFC 3339 时间") from exc
    return _as_utc(parsed, field=field)


def _format_datetime(value: datetime) -> str:
    return _as_utc(value, field="时间").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_stable_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise LearningValidationError(f"{field} 不是有效稳定 ID")
    return value


def _check_anonymous_id(value: object) -> str:
    if not isinstance(value, str) or _ANONYMOUS_ID.fullmatch(value) is None:
        raise LearningValidationError("learner_id 必须是匿名稳定 ID")
    return value


def _check_plain_version(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LearningValidationError(f"{field} 必须是非空字符串")
    return value


def _fsync_directory(path: Path) -> None:
    """把原子目录项更新刷新到目录；失败时不得宣称导出完成。"""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise LearningExportError("无法打开学习包导出目录") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise LearningExportError("无法刷新学习包导出目录") from exc
    finally:
        os.close(descriptor)


class InMemoryLearningRepository:
    """绑定匿名学习者与知识包版本的会话级内存仓库。"""

    def __init__(
        self,
        learner_id: str,
        knowledge_package: KnowledgePackageRef,
        *,
        data_version: str = DEFAULT_DATA_VERSION,
        clock: Callable[[], datetime] = _utc_now,
        schema_directory: str | Path | None = None,
    ) -> None:
        self._learner_id = _check_anonymous_id(learner_id)
        if not isinstance(knowledge_package, KnowledgePackageRef):
            raise LearningValidationError("knowledge_package 类型无效")
        package_id = _check_stable_id(
            knowledge_package.package_id, field="knowledge_package.id"
        )
        package_version = _check_plain_version(
            knowledge_package.version, field="knowledge_package.version"
        )
        self._knowledge_package = KnowledgePackageRef(package_id, package_version)
        self._data_version = _check_plain_version(data_version, field="data_version")
        if not callable(clock):
            raise LearningValidationError("clock 必须可调用")
        self._clock = clock
        self._schema_directory = (
            None if schema_directory is None else Path(schema_directory)
        )
        self._records: dict[LearningKey, LearningRecord] = {}

    @property
    def learner_id(self) -> str:
        return self._learner_id

    @property
    def knowledge_package(self) -> KnowledgePackageRef:
        return self._knowledge_package

    @property
    def data_version(self) -> str:
        return self._data_version

    def records(self) -> tuple[LearningRecord, ...]:
        """返回按稳定键排序的不可变快照。"""

        return tuple(self._records[key] for key in sorted(self._records))

    def list_records(self) -> tuple[LearningRecord, ...]:
        """``records`` 的显式命名别名，便于 CLI 调用。"""

        return self.records()

    def get(
        self,
        knowledge_point_id: str,
        question_id: str,
        question_revision: int,
    ) -> LearningRecord:
        key = self._validated_key(
            knowledge_point_id, question_id, question_revision
        )
        try:
            return self._records[key]
        except KeyError as exc:
            raise LearningNotFoundError("指定学习记录不存在") from exc

    def record(self, event: LearningEvent) -> LearningRecord:
        """按确定性状态机记录一次表现或错误事件。"""

        if not isinstance(event, LearningEvent):
            raise LearningValidationError("event 必须是 LearningEvent")
        if event.learner_id != self._learner_id:
            raise LearningValidationError("事件 learner_id 与仓库绑定值不一致")
        key = self._validated_key(
            event.knowledge_point_id, event.question_id, event.question_revision
        )
        occurred_at = _as_utc(event.occurred_at, field="occurred_at")
        if not _is_int(event.hint_level) or event.hint_level < 0:
            raise LearningValidationError("hint_level 必须是非负整数")
        if event.performance not in _OUTCOMES:
            raise LearningValidationError("performance 不受支持")
        if event.error_type is not None:
            if event.error_type not in _ERROR_TYPES:
                raise LearningValidationError("error_type 不受支持")
            if event.performance not in _ERROR_OUTCOMES:
                raise LearningValidationError("错误事件的 performance 必须为 partial 或 incorrect")

        current = self._records.get(key)
        if current is None and len(self._records) >= MAX_RECORDS:
            raise LearningValidationError("学习记录已达到 10000 条上限")
        if current is not None and occurred_at < current.updated_at:
            raise LearningConflictError("拒绝早于当前记录的乱序事件")

        if current is None:
            error = None
            confidence = 0.0
            if event.error_type is not None:
                error = ErrorSummary(
                    error_type=event.error_type,
                    count=1,
                    correct_count=0,
                    status="pending_confirmation",
                    first_seen_at=occurred_at,
                    last_seen_at=occurred_at,
                )
                confidence = 0.35
            updated = LearningRecord(
                knowledge_point_id=key[0],
                question_id=key[1],
                question_revision=key[2],
                hint_level=event.hint_level,
                performance=event.performance,
                score=None,
                error=error,
                weakness_confidence=confidence,
                updated_at=occurred_at,
            )
        else:
            updated = self._update_record(current, event, occurred_at)

        self._records[key] = updated
        return updated

    def correct(
        self,
        knowledge_point_id: str,
        question_id: str,
        question_revision: int,
        *,
        occurred_at: datetime | None = None,
    ) -> LearningRecord:
        """纠正一条误记；保留历史次数并降低置信度。"""

        current, timestamp = self._record_and_timestamp(
            knowledge_point_id, question_id, question_revision, occurred_at
        )
        if current.error is None:
            raise LearningConflictError("指定学习记录没有可纠正的错误")
        updated = replace(
            current,
            error=replace(current.error, status="corrected"),
            weakness_confidence=max(0.0, current.weakness_confidence - 0.35),
            updated_at=timestamp,
        )
        self._records[current.key] = updated
        return updated

    def reclassify(
        self,
        knowledge_point_id: str,
        question_id: str,
        question_revision: int,
        error_type: str,
        *,
        occurred_at: datetime | None = None,
    ) -> LearningRecord:
        """显式修改错误类型，不改变次数、正确次数或置信度。"""

        if error_type not in _ERROR_TYPES:
            raise LearningValidationError("error_type 不受支持")
        current, timestamp = self._record_and_timestamp(
            knowledge_point_id, question_id, question_revision, occurred_at
        )
        if current.error is None:
            raise LearningConflictError("指定学习记录没有可重分类的错误")
        updated = replace(
            current,
            error=replace(current.error, error_type=error_type),
            updated_at=timestamp,
        )
        self._records[current.key] = updated
        return updated

    def delete(
        self,
        knowledge_point_id: str,
        question_id: str,
        question_revision: int,
    ) -> None:
        key = self._validated_key(
            knowledge_point_id, question_id, question_revision
        )
        if key not in self._records:
            raise LearningNotFoundError("指定学习记录不存在")
        del self._records[key]

    def export_json(self) -> bytes:
        """生成规范化 1.1 JSON；调用本身不写磁盘。"""

        exported_at = _as_utc(self._clock(), field="clock 返回值")
        package = {
            "schema_version": LEARNING_SCHEMA_VERSION,
            "data_version": self._data_version,
            "exported_at": _format_datetime(exported_at),
            "learner_id": self._learner_id,
            "knowledge_package": {
                "id": self._knowledge_package.package_id,
                "version": self._knowledge_package.version,
            },
            "records": [self._record_to_object(record) for record in self.records()],
        }
        return (
            json.dumps(
                package,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def import_json(self, payload: bytes) -> None:
        """完整校验后一次性替换内存状态；失败时原状态不变。"""

        if not isinstance(payload, bytes):
            raise LearningValidationError("学习包输入必须是 bytes")
        if len(payload) > MAX_IMPORT_BYTES:
            raise LearningValidationError("学习包超过 1 MiB 上限")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LearningValidationError("学习包不是有效 UTF-8") from exc
        try:
            document = json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except (_DuplicateKey, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise LearningValidationError(f"学习包 JSON 无效：{exc}") from exc
        if not isinstance(document, dict):
            raise LearningValidationError("学习包根节点必须是对象")

        schema_version = document.get("schema_version")
        if schema_version not in {"1.0", "1.1"}:
            raise LearningValidationError("不支持的学习包 schema_version")
        self._validate_schema(document, schema_version)
        candidate = self._document_to_records(document, schema_version)
        self._records = candidate

    def import_file(self, path: str | Path) -> None:
        """有界读取单个普通文件，然后执行事务式导入。"""

        source = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            try:
                if source.is_symlink():
                    raise LearningValidationError("学习包输入不能是符号链接")
            except OSError as exc:
                raise LearningValidationError("无法检查学习包输入路径") from exc
        try:
            descriptor = os.open(source, flags | nofollow)
        except OSError as exc:
            raise LearningValidationError("无法安全打开学习包输入") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise LearningValidationError("学习包输入必须是普通文件")
            if before.st_size > MAX_IMPORT_BYTES:
                raise LearningValidationError("学习包超过 1 MiB 上限")
            chunks: list[bytes] = []
            remaining = MAX_IMPORT_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_IMPORT_BYTES:
                raise LearningValidationError("学习包超过 1 MiB 上限")
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise LearningValidationError("读取期间学习包输入发生变化")
        finally:
            os.close(descriptor)
        self.import_json(payload)

    def export_file(
        self,
        path: str | Path,
        *,
        overwrite: bool = False,
    ) -> None:
        """以 600 权限在同目录原子创建或替换学习包。"""

        if not isinstance(overwrite, bool):
            raise LearningValidationError("overwrite 必须是布尔值")
        destination = Path(path)
        parent = destination.parent
        if not parent.is_dir():
            raise LearningExportError("导出目标目录不存在或不是目录")
        try:
            existing = os.lstat(destination)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise LearningExportError("无法检查导出目标") from exc
        if existing is not None:
            if not overwrite:
                raise LearningExportError("导出目标已存在；需显式允许覆盖")
            if not stat.S_ISREG(existing.st_mode):
                raise LearningExportError("只允许覆盖普通文件，不能覆盖符号链接")

        payload = self.export_json()
        descriptor = -1
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            if overwrite:
                os.replace(temporary_name, destination)
                temporary_name = None
            else:
                try:
                    os.link(temporary_name, destination, follow_symlinks=False)
                except FileExistsError as exc:
                    raise LearningExportError(
                        "导出目标在写入期间已出现；未覆盖"
                    ) from exc
                committed_temporary_name = temporary_name
                temporary_name = None
                try:
                    os.unlink(committed_temporary_name)
                except OSError:
                    # 目标文件已经原子创建成功；残留临时硬链接不改变导出结果，
                    # 且不能在提交后把清理失败误报成“目标未改变”。
                    pass
            _fsync_directory(parent)
        except LearningDataError:
            raise
        except OSError as exc:
            raise LearningExportError("无法安全导出学习包") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _update_record(
        self,
        current: LearningRecord,
        event: LearningEvent,
        occurred_at: datetime,
    ) -> LearningRecord:
        error = current.error
        confidence = current.weakness_confidence
        if event.error_type is not None:
            if error is not None and error.error_type != event.error_type:
                raise LearningConflictError(
                    "同一稳定键的 error_type 不一致；请先显式重分类或删除"
                )
            if error is None:
                error = ErrorSummary(
                    error_type=event.error_type,
                    count=1,
                    correct_count=0,
                    status="pending_confirmation",
                    first_seen_at=occurred_at,
                    last_seen_at=occurred_at,
                )
                confidence = 0.35
            else:
                increment = (
                    0.20
                    if error.count == 1 and error.status != "corrected"
                    else 0.10
                )
                error = replace(
                    error,
                    count=error.count + 1,
                    status="confirmed",
                    last_seen_at=occurred_at,
                )
                confidence = min(MAX_WEAKNESS_CONFIDENCE, confidence + increment)
        elif event.performance == "correct" and error is not None:
            error = replace(
                error,
                correct_count=error.correct_count + 1,
                status="corrected",
            )
            confidence = max(0.0, confidence - 0.20)

        return replace(
            current,
            hint_level=event.hint_level,
            performance=event.performance,
            score=None,
            error=error,
            weakness_confidence=confidence,
            updated_at=occurred_at,
        )

    def _record_and_timestamp(
        self,
        knowledge_point_id: str,
        question_id: str,
        question_revision: int,
        occurred_at: datetime | None,
    ) -> tuple[LearningRecord, datetime]:
        current = self.get(knowledge_point_id, question_id, question_revision)
        raw_timestamp = self._clock() if occurred_at is None else occurred_at
        timestamp = _as_utc(raw_timestamp, field="occurred_at")
        if timestamp < current.updated_at:
            raise LearningConflictError("拒绝早于当前记录的乱序操作")
        return current, timestamp

    def _validated_key(
        self,
        knowledge_point_id: object,
        question_id: object,
        question_revision: object,
    ) -> LearningKey:
        point = _check_stable_id(knowledge_point_id, field="knowledge_point_id")
        question = _check_stable_id(question_id, field="question.id")
        if not _is_int(question_revision) or question_revision < 1:
            raise LearningValidationError("question.revision 必须是正整数")
        return point, question, question_revision

    def _validate_schema(self, document: dict[str, Any], version: str) -> None:
        filename = (
            "learning-package.schema.json"
            if version == "1.0"
            else "learning-package-1.1.schema.json"
        )
        try:
            schema_source = (
                schema_resource(filename)
                if self._schema_directory is None
                else self._schema_directory / filename
            )
            schema_payload = schema_source.read_bytes()
            schema = json.loads(schema_payload)
            Draft202012Validator.check_schema(schema)
        except (OSError, ValueError, TypeError, SchemaError) as exc:
            raise LearningValidationError("无法加载固定本地学习包 Schema") from exc
        try:
            errors = sorted(
                Draft202012Validator(schema).iter_errors(document),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
        except RecursionError as exc:
            raise LearningValidationError("学习包嵌套层级过深") from exc
        if errors:
            first = errors[0]
            path = ".".join(str(part) for part in first.absolute_path) or "$"
            raise LearningValidationError(
                f"学习包 Schema 校验失败（{path}，规则 {first.validator}）"
            )

    def _document_to_records(
        self, document: dict[str, Any], schema_version: str
    ) -> dict[LearningKey, LearningRecord]:
        if document["data_version"] != self._data_version:
            raise LearningValidationError("data_version 与当前仓库不一致")
        if document["learner_id"] != self._learner_id:
            raise LearningValidationError("learner_id 与当前仓库不一致")
        package = document["knowledge_package"]
        if (
            package["id"] != self._knowledge_package.package_id
            or package["version"] != self._knowledge_package.version
        ):
            raise LearningValidationError("knowledge_package 与当前仓库不一致")
        _parse_datetime(document["exported_at"], field="exported_at")
        raw_records = document["records"]
        if len(raw_records) > MAX_RECORDS:
            raise LearningValidationError("学习记录超过 10000 条上限")

        candidate: dict[LearningKey, LearningRecord] = {}
        for index, raw in enumerate(raw_records):
            record = self._object_to_record(raw, schema_version, index)
            if record.key in candidate:
                raise LearningValidationError("学习包包含重复稳定记录键")
            candidate[record.key] = record
        return candidate

    def _object_to_record(
        self, raw: Mapping[str, Any], schema_version: str, index: int
    ) -> LearningRecord:
        prefix = f"records[{index}]"
        question = raw["question"]
        key = self._validated_key(
            raw["knowledge_point_id"], question["id"], question["revision"]
        )
        updated_at = _parse_datetime(raw["updated_at"], field=f"{prefix}.updated_at")
        confidence = float(raw["weakness_confidence"])
        if not math.isfinite(confidence) or not 0 <= confidence <= MAX_WEAKNESS_CONFIDENCE:
            raise LearningValidationError(
                f"{prefix}.weakness_confidence 必须在 0 到 0.95 之间"
            )
        performance = raw["performance"]
        score = performance.get("score")
        if score is not None:
            score = float(score)
            if not math.isfinite(score):
                raise LearningValidationError(f"{prefix}.performance.score 必须有限")

        error = None
        raw_error = raw.get("error")
        if raw_error is not None:
            first_seen = _parse_datetime(
                raw_error["first_seen_at"], field=f"{prefix}.error.first_seen_at"
            )
            last_seen = _parse_datetime(
                raw_error["last_seen_at"], field=f"{prefix}.error.last_seen_at"
            )
            if first_seen > last_seen or last_seen > updated_at:
                raise LearningValidationError(f"{prefix} 的时间顺序无效")
            correct_count = 0 if schema_version == "1.0" else raw_error["correct_count"]
            if raw_error["status"] == "pending_confirmation" and raw_error["count"] != 1:
                raise LearningValidationError(
                    f"{prefix} 的 pending_confirmation 只能对应首次错误"
                )
            if raw_error["status"] == "pending_confirmation" and (
                correct_count != 0 or confidence != 0.35
            ):
                raise LearningValidationError(
                    f"{prefix} 的首次待确认错误状态不一致"
                )
            if raw_error["status"] == "confirmed" and raw_error["count"] < 2:
                raise LearningValidationError(
                    f"{prefix} 的 confirmed 至少需要两次错误"
                )
            error = ErrorSummary(
                error_type=raw_error["type"],
                count=raw_error["count"],
                correct_count=correct_count,
                status=raw_error["status"],
                first_seen_at=first_seen,
                last_seen_at=last_seen,
            )
        elif confidence != 0:
            raise LearningValidationError(f"{prefix} 无错误时置信度必须为 0")

        return LearningRecord(
            knowledge_point_id=key[0],
            question_id=key[1],
            question_revision=key[2],
            hint_level=raw["hint_level"],
            performance=performance["outcome"],
            score=score,
            error=error,
            weakness_confidence=confidence,
            updated_at=updated_at,
        )

    @staticmethod
    def _record_to_object(record: LearningRecord) -> dict[str, Any]:
        performance: dict[str, Any] = {"outcome": record.performance}
        if record.score is not None:
            performance["score"] = record.score
        result: dict[str, Any] = {
            "knowledge_point_id": record.knowledge_point_id,
            "question": {
                "id": record.question_id,
                "revision": record.question_revision,
            },
            "hint_level": record.hint_level,
            "performance": performance,
            "weakness_confidence": record.weakness_confidence,
            "updated_at": _format_datetime(record.updated_at),
        }
        if record.error is not None:
            result["error"] = {
                "type": record.error.error_type,
                "count": record.error.count,
                "correct_count": record.error.correct_count,
                "status": record.error.status,
                "first_seen_at": _format_datetime(record.error.first_seen_at),
                "last_seen_at": _format_datetime(record.error.last_seen_at),
            }
        return result
