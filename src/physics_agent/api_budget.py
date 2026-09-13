"""真实 API 冒烟测试的本地、故障关闭 HTTP 尝试预算。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from typing import IO, TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from physics_agent.providers.deepseek import HttpAttempt


LEDGER_SCHEMA_VERSION = "1.0"
MAX_LEDGER_BYTES = 64 * 1024
ALLOWED_RESULTS = {"success", "failed", "incomplete", "unknown"}


class ApiBudgetError(RuntimeError):
    """预算台账不可安全使用；调用方必须在联网前停止。"""


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ApiBudgetError(f"API 预算台账包含重复字段：{key}")
        result[key] = value
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AttemptBudgetLedger:
    """跨进程记录并限制一次获批测试计划的 HTTP 尝试数。"""

    def __init__(
        self,
        path: Path,
        *,
        plan_id: str,
        model: str,
        max_http_attempts: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not plan_id or not model:
            raise ValueError("plan_id 和 model 不得为空")
        if isinstance(max_http_attempts, bool) or max_http_attempts < 1:
            raise ValueError("max_http_attempts 必须是正整数")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.plan_id = plan_id
        self.model = model
        self.max_http_attempts = max_http_attempts
        self._clock = clock

    def initialize(self) -> None:
        """显式创建全新台账；存在任何同名状态时拒绝重置。"""

        parent = self.path.parent
        if not parent.is_dir():
            raise ApiBudgetError("API 预算台账目录不存在")
        if self.path.exists() or self.path.is_symlink():
            raise ApiBudgetError("API 预算台账已存在，禁止自动重建或清零")
        if self.lock_path.exists() or self.lock_path.is_symlink():
            raise ApiBudgetError("API 预算锁已存在，禁止自动重建或清零")

        lock_fd: int | None = None
        try:
            lock_fd = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            self._atomic_write(self._new_document())
            self._fsync_directory()
        except Exception:
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
            # 只回收本次尚未成功初始化的锁，不触碰调用前已有文件。
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    def observer(self, logical_call_id: str) -> Callable[[HttpAttempt], None]:
        """返回供 DeepSeekProvider 注入的发送前 observer。"""

        if logical_call_id not in {"API-1", "API-2", "API-3", "API-4"}:
            raise ValueError("logical_call_id 必须是已批准的 API-1 至 API-4")

        def reserve(attempt: HttpAttempt) -> None:
            if attempt.provider != "deepseek" or attempt.model != self.model:
                raise ApiBudgetError("API 尝试与获批 provider/model 不一致")
            self.reserve(
                logical_call_id=logical_call_id,
                attempt_number=attempt.attempt,
                streaming=attempt.streaming,
            )

        return reserve

    def reserve(
        self,
        *,
        logical_call_id: str,
        attempt_number: int,
        streaming: bool,
    ) -> int:
        """在发送 HTTP 前先永久占用一个额度，返回累计已用额度。"""

        if logical_call_id not in {"API-1", "API-2", "API-3", "API-4"}:
            raise ApiBudgetError("逻辑调用 ID 不在已批准计划中")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
            raise ApiBudgetError("HTTP 尝试序号必须是整数")
        if attempt_number < 1 or attempt_number > 2:
            raise ApiBudgetError("单次逻辑调用最多允许两次 HTTP 尝试")
        if not isinstance(streaming, bool):
            raise ApiBudgetError("streaming 标志必须是布尔值")

        with self._locked():
            document = self._read_and_validate()
            attempts = document["attempts"]
            results = document["results"]
            assert isinstance(attempts, list)
            assert isinstance(results, dict)
            prerequisites = {
                "API-1": (),
                "API-2": ("API-1",),
                "API-3": ("API-1", "API-2"),
                "API-4": ("API-1", "API-2", "API-3"),
            }[logical_call_id]
            if any(results.get(call_id) != "success" for call_id in prerequisites):
                raise ApiBudgetError("前序逻辑调用尚未成功，拒绝联网")
            if len(attempts) >= self.max_http_attempts:
                raise ApiBudgetError("已达到获批的 8 次 HTTP 请求总上限")
            if any(
                item["logical_call_id"] == logical_call_id
                and item["attempt"] == attempt_number
                for item in attempts
            ):
                raise ApiBudgetError("同一逻辑调用的尝试序号重复，拒绝联网")
            prior = [
                item["attempt"]
                for item in attempts
                if item["logical_call_id"] == logical_call_id
            ]
            if prior and attempt_number != max(prior) + 1:
                raise ApiBudgetError("HTTP 尝试序号不连续，拒绝联网")
            if not prior and attempt_number != 1:
                raise ApiBudgetError("逻辑调用必须从第 1 次尝试开始")

            attempts.append(
                {
                    "logical_call_id": logical_call_id,
                    "attempt": attempt_number,
                    "streaming": streaming,
                    "reserved_at": self._timestamp(),
                }
            )
            document["used"] = len(attempts)
            self._atomic_write(document)
            return len(attempts)

    def record_result(self, logical_call_id: str, result: str) -> None:
        """记录不含响应正文的逻辑调用分类；缺失结果按 unknown 解释。"""

        if logical_call_id not in {"API-1", "API-2", "API-3", "API-4"}:
            raise ApiBudgetError("逻辑调用 ID 不在已批准计划中")
        if result not in ALLOWED_RESULTS:
            raise ApiBudgetError("API 结果分类无效")
        with self._locked():
            document = self._read_and_validate()
            attempts = document["attempts"]
            results = document["results"]
            assert isinstance(attempts, list)
            assert isinstance(results, dict)
            if not any(item["logical_call_id"] == logical_call_id for item in attempts):
                raise ApiBudgetError("逻辑调用尚未占用 HTTP 额度")
            if logical_call_id in results:
                raise ApiBudgetError("逻辑调用结果已经记录")
            results[logical_call_id] = result
            self._atomic_write(document)

    def snapshot(self) -> dict[str, object]:
        """返回不含提示、响应、密钥或请求头的校验后快照。"""

        with self._locked():
            document = self._read_and_validate()
            return json.loads(json.dumps(document))

    def _new_document(self) -> dict[str, object]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "model": self.model,
            "max_http_attempts": self.max_http_attempts,
            "used": 0,
            "attempts": [],
            "results": {},
        }

    @contextmanager
    def _locked(self) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags)
        except OSError as exc:
            raise ApiBudgetError("API 预算锁缺失或无法安全打开") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ApiBudgetError("API 预算锁必须是权限 600 的普通文件")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise ApiBudgetError("无法锁定 API 预算台账") from exc
            yield
        finally:
            os.close(fd)

    def _read_and_validate(self) -> dict[str, object]:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise ApiBudgetError("API 预算台账缺失或无法安全打开") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
                raise ApiBudgetError("API 预算台账必须是权限 600 的普通文件")
            if before.st_size > MAX_LEDGER_BYTES:
                raise ApiBudgetError("API 预算台账超过大小上限")
            payload = self._bounded_read(fd)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ApiBudgetError("读取 API 预算台账时文件发生变化")
        finally:
            os.close(fd)

        try:
            text = payload.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiBudgetError("API 预算台账不是有效的 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiBudgetError("API 预算台账顶层必须是对象")
        self._validate_document(value)
        return value

    @staticmethod
    def _bounded_read(fd: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, MAX_LEDGER_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LEDGER_BYTES:
                raise ApiBudgetError("API 预算台账超过大小上限")
        return b"".join(chunks)

    def _validate_document(self, value: dict[str, object]) -> None:
        expected = {
            "schema_version",
            "plan_id",
            "model",
            "max_http_attempts",
            "used",
            "attempts",
            "results",
        }
        if set(value) != expected:
            raise ApiBudgetError("API 预算台账字段集合不匹配")
        if value["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise ApiBudgetError("API 预算台账版本不兼容")
        if value["plan_id"] != self.plan_id or value["model"] != self.model:
            raise ApiBudgetError("API 预算台账的计划 ID 或模型不匹配")
        if value["max_http_attempts"] != self.max_http_attempts:
            raise ApiBudgetError("API 预算台账的获批上限不匹配")
        attempts = value["attempts"]
        used = value["used"]
        results = value["results"]
        if not isinstance(attempts, list) or isinstance(used, bool) or not isinstance(used, int):
            raise ApiBudgetError("API 预算台账计数字段无效")
        if used != len(attempts) or used < 0 or used > self.max_http_attempts:
            raise ApiBudgetError("API 预算台账计数不一致")
        seen: set[tuple[str, int]] = set()
        per_call: dict[str, list[int]] = {}
        for item in attempts:
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id",
                "attempt",
                "streaming",
                "reserved_at",
            }:
                raise ApiBudgetError("API 预算尝试记录结构无效")
            call_id = item["logical_call_id"]
            attempt = item["attempt"]
            if call_id not in {"API-1", "API-2", "API-3", "API-4"}:
                raise ApiBudgetError("API 预算尝试记录含未知逻辑调用")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 2:
                raise ApiBudgetError("API 预算尝试序号无效")
            if not isinstance(item["streaming"], bool):
                raise ApiBudgetError("API 预算 streaming 字段无效")
            self._validate_timestamp(item["reserved_at"])
            identity = (call_id, attempt)
            if identity in seen:
                raise ApiBudgetError("API 预算尝试记录重复")
            seen.add(identity)
            per_call.setdefault(call_id, []).append(attempt)
        if any(sorted(numbers) != list(range(1, len(numbers) + 1)) for numbers in per_call.values()):
            raise ApiBudgetError("API 预算尝试序号不连续")
        if not isinstance(results, dict):
            raise ApiBudgetError("API 预算结果字段无效")
        if any(key not in per_call or result not in ALLOWED_RESULTS for key, result in results.items()):
            raise ApiBudgetError("API 预算结果记录无效")

    @staticmethod
    def _validate_timestamp(value: object) -> None:
        if not isinstance(value, str):
            raise ApiBudgetError("API 预算时间戳无效")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ApiBudgetError("API 预算时间戳无效") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ApiBudgetError("API 预算时间戳必须包含时区")

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiBudgetError("预算时钟必须返回带时区时间")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _atomic_write(self, value: dict[str, object]) -> None:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ApiBudgetError("API 预算台账超过大小上限")
        parent = self.path.parent
        temporary = parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(temporary, flags, 0o600)
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ApiBudgetError("写入 API 预算台账失败")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.path)
            self._fsync_directory()
        except OSError as exc:
            raise ApiBudgetError("无法原子更新 API 预算台账") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _fsync_directory(self) -> None:
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            raise ApiBudgetError("无法打开 API 预算目录") from exc
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
