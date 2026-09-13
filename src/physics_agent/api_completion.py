"""CR-0003 最终 API-3/API-4 复验的三父只增预算台账。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING, Iterator

from physics_agent.api_finalize import ApiFinalizeError, M4ApiFinalizeLedger

if TYPE_CHECKING:
    from physics_agent.providers.deepseek import HttpAttempt


COMPLETION_SCHEMA_VERSION = "1.0"
COMPLETION_PLAN_ID = "m4-api3-api4-completion-20260913-v1"
MODEL = "deepseek-v4-pro"
ROOT_PARENT_SHA256 = (
    "578b2e77ab0c109b58f971fc568f01f6a91a45485f62fe90b64666ad00b39d62"
)
RESUME_PARENT_SHA256 = (
    "3891180f70eac7eebe7a2033c5a6ffc1907b5240b8e5d8490d87e39fcc51a4a8"
)
FINALIZE_PARENT_SHA256 = (
    "1806d5bd37973fc127c12cb5321a004ebc24ce0922e236ddf889970b651d81e9"
)
HISTORICAL_USED = 5
MAX_TOTAL_HTTP_ATTEMPTS = 8
MAX_NEW_HTTP_ATTEMPTS = 3
CALL_IDS = ("COMPLETE-API-3", "COMPLETE-API-4")
CALL_LIMITS = {"COMPLETE-API-3": 1, "COMPLETE-API-4": 2}
ORIGINAL_CALL_IDS = {"COMPLETE-API-3": "API-3", "COMPLETE-API-4": "API-4"}
ALLOWED_RESULTS = {"success", "failed", "incomplete", "unknown"}
MAX_LEDGER_BYTES = 64 * 1024


ApiCompletionError = ApiFinalizeError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class M4ApiCompletionLedger(M4ApiFinalizeLedger):
    """绑定根、首次续跑和最终续验三份证据，限制最后三次 HTTP。"""

    def __init__(
        self,
        path: Path,
        *,
        original_path: Path,
        resume_path: Path,
        finalize_path: Path,
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
        self.finalize_path = Path(finalize_path)
        self.finalize_lock_path = self.finalize_path.with_suffix(
            self.finalize_path.suffix + ".lock"
        )
        paths = {
            item.absolute()
            for item in (
                self.path,
                self.lock_path,
                self.original_path,
                self.original_lock_path,
                self.resume_path,
                self.resume_lock_path,
                self.finalize_path,
                self.finalize_lock_path,
            )
        }
        if len(paths) != 8:
            raise ValueError("最终完成台账、三份父台账及其锁必须全部使用不同路径")
        self._clock = clock

    def initialize(self) -> None:
        """创建独立只增台账；已有路径一律拒绝覆盖或重置。"""

        if not self.path.parent.is_dir():
            raise ApiCompletionError("最终完成台账目录不存在")
        self._require_missing(self.path, "最终完成台账已存在，禁止重建或清零")
        self._require_missing(self.lock_path, "最终完成锁已存在，禁止重建或清零")
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
            with self._completion_parents_locked() as digests:
                self._atomic_create(self._new_completion_document(digests))
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

    def validate_parents(self) -> tuple[str, str, str]:
        """只读校验三份父台账及固定摘要。"""

        with self._completion_parents_locked() as digests:
            return digests

    def observer(self, logical_call_id: str) -> Callable[[HttpAttempt], None]:
        """创建 DeepSeek provider 每次发送前使用的额度 observer。"""

        if logical_call_id not in CALL_IDS:
            raise ValueError("logical_call_id 必须是 COMPLETE-API-3 或 COMPLETE-API-4")

        def reserve(attempt: HttpAttempt) -> None:
            if attempt.provider != "deepseek" or attempt.model != MODEL:
                raise ApiCompletionError("最终完成尝试与固定 provider/model 不一致")
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
        """在 HTTP 发送前永久占用额度，返回四个台账的累计用量。"""

        self._validate_completion_reservation(
            logical_call_id, attempt_number, streaming
        )
        with self._locked():
            with self._completion_parents_locked() as digests:
                document = self._read_completion()
                self._verify_completion_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if any(result != "success" for result in results.values()):
                    raise ApiCompletionError("最终完成计划已有非成功结果，拒绝联网")
                if (
                    logical_call_id == "COMPLETE-API-4"
                    and results.get("COMPLETE-API-3") != "success"
                ):
                    raise ApiCompletionError("COMPLETE-API-3 尚未成功，拒绝 API-4")
                if logical_call_id in results:
                    raise ApiCompletionError("逻辑调用结果已记录，拒绝追加尝试")
                if len(attempts) >= MAX_NEW_HTTP_ATTEMPTS:
                    raise ApiCompletionError("最终完成计划已达到新增 3 次 HTTP 上限")
                prior = [
                    item["provider_attempt"]
                    for item in attempts
                    if item["logical_call_id"] == logical_call_id
                ]
                if len(prior) >= CALL_LIMITS[logical_call_id]:
                    raise ApiCompletionError("该逻辑调用已达到最终完成尝试上限")
                if attempt_number != len(prior) + 1:
                    raise ApiCompletionError("provider 尝试序号重复或不连续")
                attempts.append(
                    {
                        "logical_call_id": logical_call_id,
                        "original_call_id": ORIGINAL_CALL_IDS[logical_call_id],
                        "provider_attempt": attempt_number,
                        "global_attempt": (
                            3
                            if logical_call_id == "COMPLETE-API-3"
                            else attempt_number
                        ),
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
            raise ApiCompletionError("逻辑调用不在最终完成计划中")
        if result not in ALLOWED_RESULTS:
            raise ApiCompletionError("最终完成结果分类无效")
        with self._locked():
            with self._completion_parents_locked() as digests:
                document = self._read_completion()
                self._verify_completion_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if not any(
                    item["logical_call_id"] == logical_call_id for item in attempts
                ):
                    raise ApiCompletionError("逻辑调用尚未占用最终完成 HTTP 额度")
                if logical_call_id in results:
                    raise ApiCompletionError("最终完成结果已记录，禁止改写")
                if (
                    logical_call_id == "COMPLETE-API-4"
                    and results.get("COMPLETE-API-3") != "success"
                ):
                    raise ApiCompletionError("COMPLETE-API-3 尚未成功")
                results[logical_call_id] = result
                self._atomic_write(document)

    def snapshot(self) -> dict[str, object]:
        """返回不含提示、响应、凭据或请求头的校验后快照。"""

        with self._locked():
            with self._completion_parents_locked() as digests:
                document = self._read_completion()
                self._verify_completion_binding(document, digests)
                return json.loads(json.dumps(document))

    @contextmanager
    def _completion_parents_locked(self) -> Iterator[tuple[str, str, str]]:
        fds: list[int] = []
        try:
            for path, label in (
                (self.original_lock_path, "根历史预算锁"),
                (self.resume_lock_path, "第一续跑预算锁"),
                (self.finalize_lock_path, "第二续验预算锁"),
            ):
                fds.append(self._open_lock(path, exclusive=False, label=label))
            root_payload = self._read_regular(self.original_path, "根历史预算台账")
            resume_payload = self._read_regular(self.resume_path, "第一续跑预算台账")
            finalize_payload = self._read_regular(
                self.finalize_path, "第二续验预算台账"
            )
            root = self._parse_json(root_payload, "根历史预算台账")
            resume = self._parse_json(resume_payload, "第一续跑预算台账")
            finalize = self._parse_json(finalize_payload, "第二续验预算台账")
            self._validate_root_parent(root)
            self._validate_resume_parent(resume)
            self._validate_final(finalize)
            digests = (
                hashlib.sha256(root_payload).hexdigest(),
                hashlib.sha256(resume_payload).hexdigest(),
                hashlib.sha256(finalize_payload).hexdigest(),
            )
            if digests != (
                ROOT_PARENT_SHA256,
                RESUME_PARENT_SHA256,
                FINALIZE_PARENT_SHA256,
            ):
                raise ApiCompletionError("三份父预算台账 SHA-256 不匹配")
            yield digests
        finally:
            for fd in reversed(fds):
                os.close(fd)

    @staticmethod
    def _new_completion_document(
        digests: tuple[str, str, str]
    ) -> dict[str, object]:
        return {
            "schema_version": COMPLETION_SCHEMA_VERSION,
            "plan_id": COMPLETION_PLAN_ID,
            "model": MODEL,
            "root_parent_sha256": digests[0],
            "resume_parent_sha256": digests[1],
            "finalize_parent_sha256": digests[2],
            "historical_used": HISTORICAL_USED,
            "max_total_http_attempts": MAX_TOTAL_HTTP_ATTEMPTS,
            "max_new_http_attempts": MAX_NEW_HTTP_ATTEMPTS,
            "used": 0,
            "combined_used": HISTORICAL_USED,
            "attempts": [],
            "results": {},
        }

    def _read_completion(self) -> dict[str, object]:
        value = self._parse_json(
            self._read_regular(self.path, "最终完成台账"), "最终完成台账"
        )
        self._validate_completion(value)
        return value

    @staticmethod
    def _validate_completion(value: dict[str, object]) -> None:
        expected = {
            "schema_version",
            "plan_id",
            "model",
            "root_parent_sha256",
            "resume_parent_sha256",
            "finalize_parent_sha256",
            "historical_used",
            "max_total_http_attempts",
            "max_new_http_attempts",
            "used",
            "combined_used",
            "attempts",
            "results",
        }
        if set(value) != expected:
            raise ApiCompletionError("最终完成台账字段集合不匹配")
        if (
            value["schema_version"] != COMPLETION_SCHEMA_VERSION
            or value["plan_id"] != COMPLETION_PLAN_ID
            or value["model"] != MODEL
            or value["root_parent_sha256"] != ROOT_PARENT_SHA256
            or value["resume_parent_sha256"] != RESUME_PARENT_SHA256
            or value["finalize_parent_sha256"] != FINALIZE_PARENT_SHA256
            or value["historical_used"] != HISTORICAL_USED
            or value["max_total_http_attempts"] != MAX_TOTAL_HTTP_ATTEMPTS
            or value["max_new_http_attempts"] != MAX_NEW_HTTP_ATTEMPTS
        ):
            raise ApiCompletionError("最终完成台账固定计划或父绑定不匹配")
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
            or used != len(attempts)
            or not 0 <= used <= MAX_NEW_HTTP_ATTEMPTS
            or combined != HISTORICAL_USED + used
            or combined > MAX_TOTAL_HTTP_ATTEMPTS
        ):
            raise ApiCompletionError("最终完成累计计数不一致")
        per_call: dict[str, list[int]] = {}
        previous_index = -1
        for item in attempts:
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id",
                "original_call_id",
                "provider_attempt",
                "global_attempt",
                "streaming",
                "reserved_at",
            }:
                raise ApiCompletionError("最终完成尝试结构无效")
            call_id = item["logical_call_id"]
            provider_attempt = item["provider_attempt"]
            if call_id not in CALL_IDS:
                raise ApiCompletionError("最终完成包含未知逻辑调用")
            if (
                item["original_call_id"] != ORIGINAL_CALL_IDS[call_id]
                or isinstance(provider_attempt, bool)
                or not isinstance(provider_attempt, int)
                or provider_attempt < 1
                or item["global_attempt"]
                != (3 if call_id == "COMPLETE-API-3" else provider_attempt)
                or isinstance(item["global_attempt"], bool)
                or item["streaming"] is not False
            ):
                raise ApiCompletionError("最终完成尝试映射或模式无效")
            self_index = CALL_IDS.index(call_id)
            if self_index < previous_index:
                raise ApiCompletionError("最终完成调用顺序无效")
            previous_index = self_index
            numbers = per_call.setdefault(call_id, [])
            numbers.append(provider_attempt)
            if numbers != list(range(1, len(numbers) + 1)):
                raise ApiCompletionError("最终完成 provider 尝试序号不连续")
            if len(numbers) > CALL_LIMITS[call_id]:
                raise ApiCompletionError("最终完成单逻辑调用超过上限")
            M4ApiFinalizeLedger._validate_timestamp(item["reserved_at"])
        if any(
            call_id not in CALL_IDS or result not in ALLOWED_RESULTS
            for call_id, result in results.items()
        ) or any(call_id not in per_call for call_id in results):
            raise ApiCompletionError("最终完成结果记录无效")
        if (
            "COMPLETE-API-4" in per_call
            and results.get("COMPLETE-API-3") != "success"
        ):
            raise ApiCompletionError("最终完成绕过了 API-3 成功门禁")
        if any(result != "success" for result in results.values()):
            failed_index = min(
                CALL_IDS.index(call_id)
                for call_id, result in results.items()
                if result != "success"
            )
            if any(CALL_IDS.index(call_id) > failed_index for call_id in per_call):
                raise ApiCompletionError("最终完成在非成功结果后仍有后续尝试")

    @staticmethod
    def _verify_completion_binding(
        document: dict[str, object], digests: tuple[str, str, str]
    ) -> None:
        if (
            document["root_parent_sha256"],
            document["resume_parent_sha256"],
            document["finalize_parent_sha256"],
        ) != digests:
            raise ApiCompletionError("最终完成期间父台账发生变化")

    @staticmethod
    def _validate_completion_reservation(
        logical_call_id: str, attempt_number: int, streaming: bool
    ) -> None:
        if logical_call_id not in CALL_IDS:
            raise ApiCompletionError("逻辑调用不在最终完成计划中")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise ApiCompletionError("provider 尝试序号必须是正整数")
        if streaming is not False:
            raise ApiCompletionError("最终 API-3/API-4 必须是非流式调用")

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ApiCompletionError("最终完成台账超过大小上限")
        return payload
