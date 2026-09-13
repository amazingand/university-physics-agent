"""M4 API-3/API-4 最小续验的双父只增预算台账。"""

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


FINAL_SCHEMA_VERSION = "1.0"
FINAL_PLAN_ID = "m4-api3-api4-resume-20260913-v1"
MODEL = "deepseek-v4-pro"
ROOT_PARENT_SHA256 = (
    "578b2e77ab0c109b58f971fc568f01f6a91a45485f62fe90b64666ad00b39d62"
)
RESUME_PARENT_SHA256 = (
    "3891180f70eac7eebe7a2033c5a6ffc1907b5240b8e5d8490d87e39fcc51a4a8"
)
HISTORICAL_USED = 4
MAX_TOTAL_HTTP_ATTEMPTS = 8
MAX_NEW_HTTP_ATTEMPTS = 3
MAX_COMBINED_USED = 7
MAX_LEDGER_BYTES = 64 * 1024
CALL_IDS = ("FINAL-API-3", "FINAL-API-4")
CALL_LIMITS = {"FINAL-API-3": 1, "FINAL-API-4": 2}
ORIGINAL_CALL_IDS = {"FINAL-API-3": "API-3", "FINAL-API-4": "API-4"}
ALLOWED_RESULTS = {"success", "failed", "incomplete", "unknown"}


class ApiFinalizeError(RuntimeError):
    """最终续验台账不安全；调用方必须在联网前停止。"""


def _duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ApiFinalizeError(f"最终续验台账包含重复字段：{key}")
        value[key] = item
    return value


def _nonfinite(value: str) -> object:
    raise ApiFinalizeError(f"最终续验台账包含非有限数：{value}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class M4ApiFinalizeLedger:
    """绑定两份不可变父证据，限制最后 API-3/API-4 尝试。"""

    def __init__(
        self,
        path: Path,
        *,
        original_path: Path,
        resume_path: Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.original_path = Path(original_path)
        self.original_lock_path = self.original_path.with_suffix(
            self.original_path.suffix + ".lock"
        )
        self.resume_path = Path(resume_path)
        self.resume_lock_path = self.resume_path.with_suffix(
            self.resume_path.suffix + ".lock"
        )
        parent_paths = {
            self.original_path.absolute(),
            self.original_lock_path.absolute(),
            self.resume_path.absolute(),
            self.resume_lock_path.absolute(),
        }
        if len(parent_paths) != 4:
            raise ValueError("两个父台账及其锁必须使用不同路径")
        if self.path.absolute() in parent_paths or self.lock_path.absolute() in parent_paths:
            raise ValueError("最终续验台账必须与两个父台账完全分离")
        self._clock = clock

    def initialize(self) -> None:
        """显式创建独立最终台账；存在同名状态时拒绝覆盖。"""

        if not self.path.parent.is_dir():
            raise ApiFinalizeError("最终续验台账目录不存在")
        self._require_missing(self.path, "最终续验台账已存在，禁止重建或清零")
        self._require_missing(self.lock_path, "最终续验锁已存在，禁止重建或清零")
        lock_fd: int | None = None
        lock_created = False
        state_created = False
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_fd = os.open(self.lock_path, flags, 0o600)
            lock_created = True
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with self._parents_locked() as digests:
                self._atomic_create(self._new_document(digests))
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
        """创建 DeepSeek provider 的发送前预算 observer。"""

        if logical_call_id not in CALL_IDS:
            raise ValueError("logical_call_id 必须是 FINAL-API-3 或 FINAL-API-4")

        def reserve(attempt: HttpAttempt) -> None:
            if attempt.provider != "deepseek" or attempt.model != MODEL:
                raise ApiFinalizeError("最终续验尝试与固定 provider/model 不一致")
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
        """在 HTTP 发送前原子占用额度，返回三个台账的累计用量。"""

        self._validate_reservation_input(logical_call_id, attempt_number, streaming)
        with self._locked():
            with self._parents_locked() as digests:
                document = self._read_final()
                self._verify_parent_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if any(result != "success" for result in results.values()):
                    raise ApiFinalizeError("最终续验已有失败或未完成结果，拒绝联网")
                if (
                    logical_call_id == "FINAL-API-4"
                    and results.get("FINAL-API-3") != "success"
                ):
                    raise ApiFinalizeError("FINAL-API-3 尚未成功，拒绝执行 FINAL-API-4")
                if logical_call_id in results:
                    raise ApiFinalizeError("逻辑调用结果已经记录，拒绝追加尝试")
                if len(attempts) >= MAX_NEW_HTTP_ATTEMPTS:
                    raise ApiFinalizeError("最终续验已达到新增 3 次 HTTP 上限")
                prior = [
                    item["provider_attempt"]
                    for item in attempts
                    if item["logical_call_id"] == logical_call_id
                ]
                if len(prior) >= CALL_LIMITS[logical_call_id]:
                    raise ApiFinalizeError("该逻辑调用已达到最终续验尝试上限")
                if attempt_number != len(prior) + 1:
                    raise ApiFinalizeError("provider 尝试序号重复或不连续")
                global_attempt = (
                    2 if logical_call_id == "FINAL-API-3" else attempt_number
                )
                attempts.append(
                    {
                        "logical_call_id": logical_call_id,
                        "original_call_id": ORIGINAL_CALL_IDS[logical_call_id],
                        "provider_attempt": attempt_number,
                        "global_attempt": global_attempt,
                        "streaming": False,
                        "reserved_at": self._timestamp(),
                    }
                )
                document["used"] = len(attempts)
                document["combined_used"] = HISTORICAL_USED + len(attempts)
                self._atomic_write(document)
                return HISTORICAL_USED + len(attempts)

    def record_result(self, logical_call_id: str, result: str) -> None:
        """记录不可改写的安全结果分类。"""

        if logical_call_id not in CALL_IDS:
            raise ApiFinalizeError("逻辑调用 ID 不在最终续验计划中")
        if result not in ALLOWED_RESULTS:
            raise ApiFinalizeError("最终续验结果分类无效")
        with self._locked():
            with self._parents_locked() as digests:
                document = self._read_final()
                self._verify_parent_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if not any(
                    item["logical_call_id"] == logical_call_id for item in attempts
                ):
                    raise ApiFinalizeError("逻辑调用尚未占用最终续验 HTTP 额度")
                if logical_call_id in results:
                    raise ApiFinalizeError("最终续验结果已经记录，禁止改写")
                if (
                    logical_call_id == "FINAL-API-4"
                    and results.get("FINAL-API-3") != "success"
                ):
                    raise ApiFinalizeError("FINAL-API-3 尚未成功")
                results[logical_call_id] = result
                self._atomic_write(document)

    def snapshot(self) -> dict[str, object]:
        """返回不含提示、响应、密钥或请求头的校验后快照。"""

        with self._locked():
            with self._parents_locked() as digests:
                document = self._read_final()
                self._verify_parent_binding(document, digests)
                return json.loads(json.dumps(document))

    def validate_parents(self) -> tuple[str, str]:
        """只读校验两个父台账，供创建最终台账之前的联网预检使用。"""

        with self._parents_locked() as digests:
            return digests

    @staticmethod
    def _new_document(digests: tuple[str, str]) -> dict[str, object]:
        return {
            "schema_version": FINAL_SCHEMA_VERSION,
            "plan_id": FINAL_PLAN_ID,
            "model": MODEL,
            "root_parent_sha256": digests[0],
            "resume_parent_sha256": digests[1],
            "historical_used": HISTORICAL_USED,
            "max_total_http_attempts": MAX_TOTAL_HTTP_ATTEMPTS,
            "max_new_http_attempts": MAX_NEW_HTTP_ATTEMPTS,
            "max_combined_used": MAX_COMBINED_USED,
            "used": 0,
            "combined_used": HISTORICAL_USED,
            "attempts": [],
            "results": {},
        }

    @staticmethod
    def _require_missing(path: Path, message: str) -> None:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return
        raise ApiFinalizeError(message)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = self._open_lock(self.lock_path, exclusive=True, label="最终续验锁")
        try:
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _parents_locked(self) -> Iterator[tuple[str, str]]:
        fds: list[int] = []
        try:
            for lock_path, label in (
                (self.original_lock_path, "根历史预算锁"),
                (self.resume_lock_path, "第一续跑预算锁"),
            ):
                fds.append(self._open_lock(lock_path, exclusive=False, label=label))
            root_payload = self._read_regular(self.original_path, "根历史预算台账")
            resume_payload = self._read_regular(self.resume_path, "第一续跑预算台账")
            root = self._parse_json(root_payload, "根历史预算台账")
            resume = self._parse_json(resume_payload, "第一续跑预算台账")
            self._validate_root_parent(root)
            self._validate_resume_parent(resume)
            root_digest = hashlib.sha256(root_payload).hexdigest()
            resume_digest = hashlib.sha256(resume_payload).hexdigest()
            if root_digest != ROOT_PARENT_SHA256:
                raise ApiFinalizeError("根历史预算台账 SHA-256 不匹配")
            if resume_digest != RESUME_PARENT_SHA256:
                raise ApiFinalizeError("第一续跑预算台账 SHA-256 不匹配")
            yield root_digest, resume_digest
        finally:
            for fd in reversed(fds):
                os.close(fd)

    @staticmethod
    def _open_lock(path: Path, *, exclusive: bool, label: str) -> int:
        flags = (os.O_RDWR if exclusive else os.O_RDONLY) | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ApiFinalizeError(f"{label}缺失或无法安全打开") from exc
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid()
            ):
                raise ApiFinalizeError(
                    f"{label}必须是当前用户所有、权限 600 的普通文件"
                )
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            current = os.stat(path, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_uid, before.st_mode) != (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_mode,
            ):
                raise ApiFinalizeError(f"{label}在锁定期间发生替换")
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _read_regular(path: Path, label: str) -> bytes:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ApiFinalizeError(f"{label}缺失或无法安全打开") from exc
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid()
            ):
                raise ApiFinalizeError(
                    f"{label}必须是当前用户所有、权限 600 的普通文件"
                )
            if before.st_size > MAX_LEDGER_BYTES:
                raise ApiFinalizeError(f"{label}超过大小上限")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(8192, MAX_LEDGER_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise ApiFinalizeError(f"{label}超过大小上限")
            after = os.fstat(fd)
            fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_uid",
                "st_mode",
            )
            if tuple(getattr(before, item) for item in fields) != tuple(
                getattr(after, item) for item in fields
            ):
                raise ApiFinalizeError(f"读取{label}时文件发生变化")
            current = os.stat(path, follow_symlinks=False)
            if tuple(getattr(before, item) for item in fields) != tuple(
                getattr(current, item) for item in fields
            ):
                raise ApiFinalizeError(f"读取{label}时路径发生替换")
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _parse_json(payload: bytes, label: str) -> dict[str, object]:
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_duplicate_pairs,
                parse_constant=_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiFinalizeError(f"{label}不是有效 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiFinalizeError(f"{label}顶层必须是对象")
        return value

    def _read_final(self) -> dict[str, object]:
        value = self._parse_json(
            self._read_regular(self.path, "最终续验台账"), "最终续验台账"
        )
        self._validate_final(value)
        return value

    @staticmethod
    def _validate_root_parent(value: dict[str, object]) -> None:
        expected = {
            "schema_version", "plan_id", "model", "max_http_attempts",
            "used", "attempts", "results",
        }
        if set(value) != expected:
            raise ApiFinalizeError("根历史预算台账字段不匹配")
        if (
            value["schema_version"] != "1.0"
            or value["plan_id"] != "m4-deepseek-smoke-v1"
            or value["model"] != MODEL
            or value["max_http_attempts"] != 8
            or value["used"] != 1
            or value["results"] != {"API-1": "incomplete"}
        ):
            raise ApiFinalizeError("根历史预算台账固定状态不匹配")
        attempts = value["attempts"]
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise ApiFinalizeError("根历史预算台账尝试数不匹配")
        item = attempts[0]
        if not isinstance(item, dict) or set(item) != {
            "logical_call_id", "attempt", "streaming", "reserved_at"
        }:
            raise ApiFinalizeError("根历史 API-1 尝试结构无效")
        if (
            item["logical_call_id"] != "API-1"
            or item["attempt"] != 1
            or isinstance(item["attempt"], bool)
            or item["streaming"] is not False
        ):
            raise ApiFinalizeError("根历史 API-1 尝试不匹配")
        M4ApiFinalizeLedger._validate_timestamp(item["reserved_at"])

    @staticmethod
    def _validate_resume_parent(value: dict[str, object]) -> None:
        expected = {
            "schema_version", "plan_id", "parent_plan_id", "model",
            "parent_sha256", "historical_used", "max_total_http_attempts",
            "max_new_http_attempts", "used", "combined_used", "attempts", "results",
        }
        if set(value) != expected:
            raise ApiFinalizeError("第一续跑预算台账字段不匹配")
        if (
            value["schema_version"] != "1.0"
            or value["plan_id"] != "m4-deepseek-smoke-resume-20260913-v1"
            or value["parent_plan_id"] != "m4-deepseek-smoke-v1"
            or value["model"] != MODEL
            or value["parent_sha256"] != ROOT_PARENT_SHA256
            or value["historical_used"] != 1
            or value["max_total_http_attempts"] != 8
            or value["max_new_http_attempts"] != 7
            or value["used"] != 3
            or value["combined_used"] != HISTORICAL_USED
            or value["results"]
            != {"API-1": "success", "API-2": "success", "API-3": "incomplete"}
        ):
            raise ApiFinalizeError("第一续跑预算台账固定状态不匹配")
        attempts = value["attempts"]
        expected_attempts = (
            ("API-1", 1, 2, False),
            ("API-2", 1, 1, True),
            ("API-3", 1, 1, False),
        )
        if not isinstance(attempts, list) or len(attempts) != 3:
            raise ApiFinalizeError("第一续跑预算台账尝试数不匹配")
        for item, fixed in zip(attempts, expected_attempts, strict=True):
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id", "provider_attempt", "global_attempt",
                "streaming", "reserved_at",
            }:
                raise ApiFinalizeError("第一续跑尝试结构无效")
            if (
                item["logical_call_id"], item["provider_attempt"],
                item["global_attempt"], item["streaming"]
            ) != fixed or isinstance(item["provider_attempt"], bool) or isinstance(
                item["global_attempt"], bool
            ):
                raise ApiFinalizeError("第一续跑尝试固定映射不匹配")
            M4ApiFinalizeLedger._validate_timestamp(item["reserved_at"])

    @staticmethod
    def _validate_final(value: dict[str, object]) -> None:
        expected = {
            "schema_version", "plan_id", "model", "root_parent_sha256",
            "resume_parent_sha256", "historical_used", "max_total_http_attempts",
            "max_new_http_attempts", "max_combined_used", "used", "combined_used",
            "attempts", "results",
        }
        if set(value) != expected:
            raise ApiFinalizeError("最终续验台账字段集合不匹配")
        if (
            value["schema_version"] != FINAL_SCHEMA_VERSION
            or value["plan_id"] != FINAL_PLAN_ID
            or value["model"] != MODEL
            or value["root_parent_sha256"] != ROOT_PARENT_SHA256
            or value["resume_parent_sha256"] != RESUME_PARENT_SHA256
            or value["historical_used"] != HISTORICAL_USED
            or value["max_total_http_attempts"] != MAX_TOTAL_HTTP_ATTEMPTS
            or value["max_new_http_attempts"] != MAX_NEW_HTTP_ATTEMPTS
            or value["max_combined_used"] != MAX_COMBINED_USED
        ):
            raise ApiFinalizeError("最终续验台账固定计划或父绑定不匹配")
        attempts = value["attempts"]
        results = value["results"]
        used = value["used"]
        combined = value["combined_used"]
        if (
            not isinstance(attempts, list) or not isinstance(results, dict)
            or isinstance(used, bool) or not isinstance(used, int)
            or isinstance(combined, bool) or not isinstance(combined, int)
            or used != len(attempts) or not 0 <= used <= MAX_NEW_HTTP_ATTEMPTS
            or combined != HISTORICAL_USED + used
            or combined > MAX_COMBINED_USED or combined > MAX_TOTAL_HTTP_ATTEMPTS
        ):
            raise ApiFinalizeError("最终续验累计计数不一致")
        per_call: dict[str, list[int]] = {}
        previous_index = -1
        for item in attempts:
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id", "original_call_id", "provider_attempt",
                "global_attempt", "streaming", "reserved_at",
            }:
                raise ApiFinalizeError("最终续验尝试结构无效")
            call_id = item["logical_call_id"]
            provider_attempt = item["provider_attempt"]
            if call_id not in CALL_IDS:
                raise ApiFinalizeError("最终续验包含未知逻辑调用")
            if (
                item["original_call_id"] != ORIGINAL_CALL_IDS[call_id]
                or isinstance(provider_attempt, bool)
                or not isinstance(provider_attempt, int)
                or provider_attempt < 1
                or item["global_attempt"]
                != (2 if call_id == "FINAL-API-3" else provider_attempt)
                or isinstance(item["global_attempt"], bool)
                or item["streaming"] is not False
            ):
                raise ApiFinalizeError("最终续验尝试映射或模式无效")
            M4ApiFinalizeLedger._validate_timestamp(item["reserved_at"])
            call_index = CALL_IDS.index(call_id)
            if call_index < previous_index:
                raise ApiFinalizeError("最终续验调用顺序无效")
            previous_index = call_index
            numbers = per_call.setdefault(call_id, [])
            numbers.append(provider_attempt)
            if numbers != list(range(1, len(numbers) + 1)):
                raise ApiFinalizeError("最终续验 provider 尝试序号不连续")
            if len(numbers) > CALL_LIMITS[call_id]:
                raise ApiFinalizeError("最终续验单逻辑调用超过上限")
        if any(
            call_id not in CALL_IDS or result not in ALLOWED_RESULTS
            for call_id, result in results.items()
        ) or any(call_id not in per_call for call_id in results):
            raise ApiFinalizeError("最终续验结果记录无效")
        if "FINAL-API-4" in per_call and results.get("FINAL-API-3") != "success":
            raise ApiFinalizeError("最终续验绕过了 FINAL-API-3 成功门禁")
        if any(result != "success" for result in results.values()):
            failed = min(
                CALL_IDS.index(call_id)
                for call_id, result in results.items() if result != "success"
            )
            if any(CALL_IDS.index(call_id) > failed for call_id in per_call):
                raise ApiFinalizeError("最终续验在非成功结果后仍有后续尝试")

    @staticmethod
    def _verify_parent_binding(
        document: dict[str, object], digests: tuple[str, str]
    ) -> None:
        if (
            document["root_parent_sha256"] != digests[0]
            or document["resume_parent_sha256"] != digests[1]
        ):
            raise ApiFinalizeError("最终续验期间父台账发生变化")

    @staticmethod
    def _validate_reservation_input(
        logical_call_id: str, attempt_number: int, streaming: bool
    ) -> None:
        if logical_call_id not in CALL_IDS:
            raise ApiFinalizeError("逻辑调用 ID 不在最终续验计划中")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise ApiFinalizeError("provider 尝试序号必须是正整数")
        if streaming is not False:
            raise ApiFinalizeError("FINAL-API-3/4 必须是非流式调用")

    @staticmethod
    def _validate_timestamp(value: object) -> None:
        if not isinstance(value, str):
            raise ApiFinalizeError("预算时间戳无效")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ApiFinalizeError("预算时间戳无效") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ApiFinalizeError("预算时间戳必须包含时区")

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiFinalizeError("预算时钟必须返回带时区时间")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ApiFinalizeError("最终续验台账超过大小上限")
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
            if isinstance(exc, ApiFinalizeError):
                raise
            raise ApiFinalizeError("无法原子创建最终续验台账") from exc
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
            raise ApiFinalizeError("无法原子更新最终续验台账") from exc
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
                raise ApiFinalizeError("写入最终续验台账失败")
            view = view[written:]

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            raise ApiFinalizeError("无法打开最终续验目录") from exc
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
