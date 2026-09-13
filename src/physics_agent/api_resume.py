"""M4 固定 API 计划的只增续跑台账。

历史台账是不可变的验收证据。本模块只读并校验该证据，把后续 HTTP 尝试写入独立台账；
任何父台账变化、额度异常或调用顺序异常都会在 provider 发送请求前失败关闭。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from physics_agent.providers.deepseek import HttpAttempt


RESUME_SCHEMA_VERSION = "1.0"
M4_PARENT_PLAN_ID = "m4-deepseek-smoke-v1"
M4_RESUME_PLAN_ID = "m4-deepseek-smoke-resume-20260913-v1"
M4_MODEL = "deepseek-v4-pro"
M4_ORIGINAL_MAX_ATTEMPTS = 8
M4_ORIGINAL_USED = 1
M4_RESUME_MAX_ATTEMPTS = 7
M4_ORIGINAL_LEDGER_SHA256 = (
    "578b2e77ab0c109b58f971fc568f01f6a91a45485f62fe90b64666ad00b39d62"
)
MAX_LEDGER_BYTES = 64 * 1024
CALL_IDS = ("API-1", "API-2", "API-3", "API-4")
CALL_LIMITS = {"API-1": 1, "API-2": 2, "API-3": 2, "API-4": 2}
CALL_STREAMING = {"API-1": False, "API-2": True, "API-3": False, "API-4": False}
ALLOWED_RESULTS = {"success", "failed", "incomplete", "unknown"}


class ApiResumeError(RuntimeError):
    """M4 续跑台账不可安全使用；调用方必须在联网前停止。"""


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ApiResumeError(f"API 续跑台账包含重复字段：{key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> object:
    raise ApiResumeError(f"API 续跑台账包含非有限数：{value}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class M4ApiResumeLedger:
    """在固定历史台账之外记录 M4 API-1 至 API-4 的剩余尝试。"""

    def __init__(
        self,
        path: Path,
        *,
        original_path: Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.original_path = Path(original_path)
        self.original_lock_path = self.original_path.with_suffix(
            self.original_path.suffix + ".lock"
        )
        if self.path.absolute() == self.original_path.absolute():
            raise ValueError("续跑台账必须与历史台账使用不同路径")
        if self.lock_path.absolute() == self.original_lock_path.absolute():
            raise ValueError("续跑锁必须与历史预算锁使用不同路径")
        self._clock = clock

    def initialize(self) -> None:
        """创建独立续跑台账；已有状态一律拒绝覆盖或重置。"""

        if not self.path.parent.is_dir():
            raise ApiResumeError("API 续跑台账目录不存在")
        self._require_missing(self.path, "API 续跑台账已存在，禁止自动重建或清零")
        self._require_missing(self.lock_path, "API 续跑锁已存在，禁止自动重建或清零")

        lock_created = False
        state_created = False
        lock_fd: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_fd = os.open(self.lock_path, flags, 0o600)
            lock_created = True
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with self._original_digest_locked() as digest:
                document = self._new_document(digest)
                self._atomic_create(document)
                state_created = True
            self._fsync_directory()
        except Exception:
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
            if state_created:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            if lock_created:
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    def observer(self, logical_call_id: str) -> Callable[[HttpAttempt], None]:
        """返回供 DeepSeek provider 在每次 HTTP 发送前调用的额度 observer。"""

        if logical_call_id not in CALL_IDS:
            raise ValueError("logical_call_id 必须是固定计划的 API-1 至 API-4")

        def reserve(attempt: HttpAttempt) -> None:
            if attempt.provider != "deepseek" or attempt.model != M4_MODEL:
                raise ApiResumeError("API 尝试与固定 provider/model 不一致")
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
        """在发送前永久占用一个新增额度，返回历史与续跑累计用量。"""

        self._validate_reservation_input(logical_call_id, attempt_number, streaming)
        with self._locked():
            with self._original_digest_locked() as original_digest:
                document = self._read_resume()
                self._verify_original_binding(document, original_digest)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)

                if any(result != "success" for result in results.values()):
                    raise ApiResumeError("续跑计划已有失败或未完成结果，拒绝继续联网")
                prerequisites = CALL_IDS[: CALL_IDS.index(logical_call_id)]
                if any(results.get(call_id) != "success" for call_id in prerequisites):
                    raise ApiResumeError("前序逻辑调用尚未在续跑中成功，拒绝联网")
                if logical_call_id in results:
                    raise ApiResumeError("逻辑调用结果已经记录，拒绝追加尝试")
                if len(attempts) >= M4_RESUME_MAX_ATTEMPTS:
                    raise ApiResumeError("已达到续跑最多 7 次 HTTP 请求上限")

                prior = [
                    item["provider_attempt"]
                    for item in attempts
                    if item["logical_call_id"] == logical_call_id
                ]
                if len(prior) >= CALL_LIMITS[logical_call_id]:
                    raise ApiResumeError("该逻辑调用已达到续跑尝试上限")
                expected_attempt = len(prior) + 1
                if attempt_number != expected_attempt:
                    raise ApiResumeError("HTTP 尝试序号重复或不连续，拒绝联网")

                attempts.append(
                    {
                        "logical_call_id": logical_call_id,
                        "provider_attempt": attempt_number,
                        "global_attempt": attempt_number
                        + (1 if logical_call_id == "API-1" else 0),
                        "streaming": streaming,
                        "reserved_at": self._timestamp(),
                    }
                )
                document["used"] = len(attempts)
                document["combined_used"] = M4_ORIGINAL_USED + len(attempts)
                self._atomic_write(document)
                return M4_ORIGINAL_USED + len(attempts)

    def record_result(self, logical_call_id: str, result: str) -> None:
        """写入一次不可改写的逻辑调用结果，不保存响应正文。"""

        if logical_call_id not in CALL_IDS:
            raise ApiResumeError("逻辑调用 ID 不在固定计划中")
        if result not in ALLOWED_RESULTS:
            raise ApiResumeError("API 结果分类无效")
        with self._locked():
            with self._original_digest_locked() as original_digest:
                document = self._read_resume()
                self._verify_original_binding(document, original_digest)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if not any(
                    item["logical_call_id"] == logical_call_id for item in attempts
                ):
                    raise ApiResumeError("逻辑调用尚未占用续跑 HTTP 额度")
                if logical_call_id in results:
                    raise ApiResumeError("逻辑调用结果已经记录，禁止改写")
                prerequisites = CALL_IDS[: CALL_IDS.index(logical_call_id)]
                if any(results.get(call_id) != "success" for call_id in prerequisites):
                    raise ApiResumeError("前序逻辑调用尚未在续跑中成功")
                results[logical_call_id] = result
                self._atomic_write(document)

    def snapshot(self) -> dict[str, object]:
        """返回已校验且不含密钥、提示、请求头或响应正文的快照。"""

        with self._locked():
            with self._original_digest_locked() as original_digest:
                document = self._read_resume()
                self._verify_original_binding(document, original_digest)
                return json.loads(json.dumps(document))

    def _new_document(self, original_digest: str) -> dict[str, object]:
        return {
            "schema_version": RESUME_SCHEMA_VERSION,
            "plan_id": M4_RESUME_PLAN_ID,
            "parent_plan_id": M4_PARENT_PLAN_ID,
            "model": M4_MODEL,
            "parent_sha256": original_digest,
            "historical_used": M4_ORIGINAL_USED,
            "max_total_http_attempts": M4_ORIGINAL_MAX_ATTEMPTS,
            "max_new_http_attempts": M4_RESUME_MAX_ATTEMPTS,
            "used": 0,
            "combined_used": M4_ORIGINAL_USED,
            "attempts": [],
            "results": {},
        }

    @staticmethod
    def _require_missing(path: Path, message: str) -> None:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return
        raise ApiResumeError(message)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = self._open_lock(self.lock_path, exclusive=True, label="API 续跑锁")
        try:
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _original_digest_locked(self) -> Iterator[str]:
        fd = self._open_lock(
            self.original_lock_path,
            exclusive=False,
            label="历史 API 预算锁",
        )
        try:
            payload = self._read_regular_file(self.original_path, "历史 API 预算台账")
            value = self._parse_json(payload, "历史 API 预算台账")
            self._validate_original(value)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != M4_ORIGINAL_LEDGER_SHA256:
                raise ApiResumeError("历史 API 预算台账 SHA-256 与固定证据不匹配")
            yield digest
        finally:
            os.close(fd)

    @staticmethod
    def _open_lock(path: Path, *, exclusive: bool, label: str) -> int:
        flags = (os.O_RDWR if exclusive else os.O_RDONLY) | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ApiResumeError(f"{label}缺失或无法安全打开") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ApiResumeError(
                    f"{label}必须是当前用户所有、权限 600 的普通文件"
                )
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            current = os.stat(path, follow_symlinks=False)
            if (
                (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
                != (current.st_dev, current.st_ino, current.st_uid, current.st_mode)
            ):
                raise ApiResumeError(f"{label}在锁定期间发生替换")
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _read_regular_file(path: Path, label: str) -> bytes:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ApiResumeError(f"{label}缺失或无法安全打开") from exc
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid()
            ):
                raise ApiResumeError(
                    f"{label}必须是当前用户所有、权限 600 的普通文件"
                )
            if before.st_size > MAX_LEDGER_BYTES:
                raise ApiResumeError(f"{label}超过大小上限")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(8192, MAX_LEDGER_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise ApiResumeError(f"{label}超过大小上限")
            after = os.fstat(fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_uid,
                before.st_mode,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_uid,
                after.st_mode,
            ):
                raise ApiResumeError(f"读取{label}时文件发生变化")
            current = os.stat(path, follow_symlinks=False)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_uid,
                before.st_mode,
            ) != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
                current.st_uid,
                current.st_mode,
            ):
                raise ApiResumeError(f"读取{label}时路径发生替换")
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _parse_json(payload: bytes, label: str) -> dict[str, object]:
        try:
            text = payload.decode("utf-8")
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiResumeError(f"{label}不是有效的 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiResumeError(f"{label}顶层必须是对象")
        return value

    def _read_resume(self) -> dict[str, object]:
        payload = self._read_regular_file(self.path, "API 续跑台账")
        value = self._parse_json(payload, "API 续跑台账")
        self._validate_resume(value)
        return value

    @staticmethod
    def _validate_original(value: dict[str, object]) -> None:
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
            raise ApiResumeError("历史 API 预算台账字段集合不匹配")
        if (
            value["schema_version"] != "1.0"
            or value["plan_id"] != M4_PARENT_PLAN_ID
            or value["model"] != M4_MODEL
            or value["max_http_attempts"] != M4_ORIGINAL_MAX_ATTEMPTS
            or value["used"] != M4_ORIGINAL_USED
        ):
            raise ApiResumeError("历史 API 预算台账固定计划、模型或计数不匹配")
        attempts = value["attempts"]
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise ApiResumeError("历史 API 预算台账必须只有一次尝试")
        attempt = attempts[0]
        if not isinstance(attempt, dict) or set(attempt) != {
            "logical_call_id",
            "attempt",
            "streaming",
            "reserved_at",
        }:
            raise ApiResumeError("历史 API-1 尝试结构无效")
        if (
            attempt["logical_call_id"] != "API-1"
            or attempt["attempt"] != 1
            or isinstance(attempt["attempt"], bool)
            or attempt["streaming"] is not False
        ):
            raise ApiResumeError("历史台账必须是 API-1 第 1 次非流式尝试")
        M4ApiResumeLedger._validate_timestamp(attempt["reserved_at"])
        if value["results"] != {"API-1": "incomplete"}:
            raise ApiResumeError("历史 API-1 结果必须是 incomplete")

    @staticmethod
    def _validate_resume(value: dict[str, object]) -> None:
        expected = {
            "schema_version",
            "plan_id",
            "parent_plan_id",
            "model",
            "parent_sha256",
            "historical_used",
            "max_total_http_attempts",
            "max_new_http_attempts",
            "used",
            "combined_used",
            "attempts",
            "results",
        }
        if set(value) != expected:
            raise ApiResumeError("API 续跑台账字段集合不匹配")
        if (
            value["schema_version"] != RESUME_SCHEMA_VERSION
            or value["plan_id"] != M4_RESUME_PLAN_ID
            or value["parent_plan_id"] != M4_PARENT_PLAN_ID
            or value["model"] != M4_MODEL
            or value["parent_sha256"] != M4_ORIGINAL_LEDGER_SHA256
            or value["historical_used"] != M4_ORIGINAL_USED
            or value["max_total_http_attempts"] != M4_ORIGINAL_MAX_ATTEMPTS
            or value["max_new_http_attempts"] != M4_RESUME_MAX_ATTEMPTS
        ):
            raise ApiResumeError("API 续跑台账固定计划或额度绑定不匹配")
        attempts = value["attempts"]
        results = value["results"]
        used = value["used"]
        combined = value["combined_used"]
        if (
            not isinstance(attempts, list)
            or not isinstance(results, dict)
            or isinstance(used, bool)
            or not isinstance(used, int)
            or isinstance(combined, bool)
            or not isinstance(combined, int)
        ):
            raise ApiResumeError("API 续跑台账计数或集合字段无效")
        if (
            used != len(attempts)
            or used < 0
            or used > M4_RESUME_MAX_ATTEMPTS
            or combined != M4_ORIGINAL_USED + used
            or combined > M4_ORIGINAL_MAX_ATTEMPTS
        ):
            raise ApiResumeError("API 续跑台账累计计数不一致")

        per_call: dict[str, list[int]] = {}
        previous_call_index = -1
        for item in attempts:
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id",
                "provider_attempt",
                "global_attempt",
                "streaming",
                "reserved_at",
            }:
                raise ApiResumeError("API 续跑尝试记录结构无效")
            call_id = item["logical_call_id"]
            attempt_number = item["provider_attempt"]
            global_attempt = item["global_attempt"]
            if call_id not in CALL_IDS:
                raise ApiResumeError("API 续跑尝试包含未知逻辑调用")
            if (
                isinstance(attempt_number, bool)
                or not isinstance(attempt_number, int)
                or attempt_number < 1
            ):
                raise ApiResumeError("API 续跑尝试序号无效")
            if (
                isinstance(global_attempt, bool)
                or not isinstance(global_attempt, int)
                or global_attempt
                != attempt_number + (1 if call_id == "API-1" else 0)
            ):
                raise ApiResumeError("API 续跑全局尝试序号映射无效")
            if item["streaming"] is not CALL_STREAMING[call_id]:
                raise ApiResumeError("API 续跑尝试的流式模式与固定计划不一致")
            M4ApiResumeLedger._validate_timestamp(item["reserved_at"])
            call_index = CALL_IDS.index(call_id)
            if call_index < previous_call_index:
                raise ApiResumeError("API 续跑尝试顺序与固定计划不一致")
            previous_call_index = call_index
            numbers = per_call.setdefault(call_id, [])
            numbers.append(attempt_number)
            if numbers != list(range(1, len(numbers) + 1)):
                raise ApiResumeError("API 续跑尝试序号重复或不连续")
            if len(numbers) > CALL_LIMITS[call_id]:
                raise ApiResumeError("逻辑调用超过续跑尝试上限")

        if any(key not in CALL_IDS or result not in ALLOWED_RESULTS for key, result in results.items()):
            raise ApiResumeError("API 续跑结果记录无效")
        if any(key not in per_call for key in results):
            raise ApiResumeError("API 续跑结果缺少对应尝试")
        for call_id in per_call:
            prerequisites = CALL_IDS[: CALL_IDS.index(call_id)]
            if any(results.get(prior) != "success" for prior in prerequisites):
                raise ApiResumeError("API 续跑记录绕过了前序成功门禁")
        for call_id, result in results.items():
            if result != "success":
                failed_index = CALL_IDS.index(call_id)
                if any(CALL_IDS.index(item) > failed_index for item in per_call):
                    raise ApiResumeError("API 续跑记录在失败结果后仍有后续尝试")

    @staticmethod
    def _verify_original_binding(
        document: dict[str, object], original_digest: str
    ) -> None:
        if document["parent_sha256"] != original_digest:
            raise ApiResumeError("历史 API 预算台账在续跑期间发生变化")

    @staticmethod
    def _validate_reservation_input(
        logical_call_id: str, attempt_number: int, streaming: bool
    ) -> None:
        if logical_call_id not in CALL_IDS:
            raise ApiResumeError("逻辑调用 ID 不在固定计划中")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise ApiResumeError("HTTP 尝试序号必须是正整数")
        if not isinstance(streaming, bool):
            raise ApiResumeError("streaming 标志必须是布尔值")
        if streaming is not CALL_STREAMING[logical_call_id]:
            raise ApiResumeError("streaming 模式与固定 API 计划不一致")

    @staticmethod
    def _validate_timestamp(value: object) -> None:
        if not isinstance(value, str):
            raise ApiResumeError("API 续跑时间戳无效")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ApiResumeError("API 续跑时间戳无效") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ApiResumeError("API 续跑时间戳必须包含时区")

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiResumeError("续跑预算时钟必须返回带时区时间")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ApiResumeError("API 续跑台账超过大小上限")
        return payload

    def _atomic_create(self, value: dict[str, object]) -> None:
        payload = self._encode(value)
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        fd: int | None = None
        linked = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600)
            os.fchmod(fd, 0o600)
            self._write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.link(temporary, self.path, follow_symlinks=False)
            linked = True
            temporary.unlink()
            self._fsync_directory()
        except Exception as exc:
            if linked:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(exc, ApiResumeError):
                raise
            raise ApiResumeError("无法原子创建 API 续跑台账") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _atomic_write(self, value: dict[str, object]) -> None:
        payload = self._encode(value)
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600)
            os.fchmod(fd, 0o600)
            self._write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.path)
            self._fsync_directory()
        except OSError as exc:
            raise ApiResumeError("无法原子更新 API 续跑台账") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ApiResumeError("写入 API 续跑台账失败")
            view = view[written:]

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            raise ApiResumeError("无法打开 API 续跑台账目录") from exc
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
