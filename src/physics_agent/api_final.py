"""CR-0004 最后一次 API-4 的四父只增预算台账。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from physics_agent.api_completion import M4ApiCompletionLedger
from physics_agent.api_finalize import ApiFinalizeError, M4ApiFinalizeLedger

if TYPE_CHECKING:
    from physics_agent.core import HttpAttempt


FINAL_SCHEMA_VERSION = "1.0"
FINAL_PLAN_ID = "m4-api4-final-20260913-v1"
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
COMPLETION_PARENT_SHA256 = (
    "118f402fffa70ddc977ba35b6c3d805321aca84aff8bd3ad076c34300c8b2c39"
)
HISTORICAL_USED = 7
MAX_TOTAL_HTTP_ATTEMPTS = 8
MAX_NEW_HTTP_ATTEMPTS = 1
CALL_ID = "FINAL-LAST-API-4"
ORIGINAL_CALL_ID = "API-4"
GLOBAL_ATTEMPT = 2
ALLOWED_RESULTS = {"success", "failed", "incomplete", "unknown"}
MAX_LEDGER_BYTES = 64 * 1024


ApiFinalError = ApiFinalizeError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class M4ApiFinalLedger(M4ApiFinalizeLedger):
    """绑定四份不可变历史证据，只允许 API-4 的最后一次 HTTP。"""

    def __init__(
        self,
        path: Path,
        *,
        original_path: Path,
        resume_path: Path,
        finalize_path: Path,
        completion_path: Path,
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
        self.completion_path = Path(completion_path)
        self.completion_lock_path = self.completion_path.with_suffix(
            self.completion_path.suffix + ".lock"
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
                self.completion_path,
                self.completion_lock_path,
            )
        }
        if len(paths) != 10:
            raise ValueError("最终台账、四份父台账及其锁必须全部使用不同路径")
        self._clock = clock

    def initialize(self) -> None:
        """显式创建独立只增台账；任一同名状态存在都拒绝重建。"""

        if not self.path.parent.is_dir():
            raise ApiFinalError("最终 API-4 台账目录不存在")
        self._require_missing(self.path, "最终 API-4 台账已存在，禁止重建或清零")
        self._require_missing(self.lock_path, "最终 API-4 锁已存在，禁止重建或清零")
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
            with self._final_parents_locked() as digests:
                self._atomic_create(self._new_final_document(digests))
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

    def validate_parents(self) -> tuple[str, str, str, str]:
        """只读锁定并校验四份父台账的权限、内容和固定摘要。"""

        with self._final_parents_locked() as digests:
            return digests

    def observer(self, logical_call_id: str) -> Callable[[HttpAttempt], None]:
        """创建 provider 在发送 HTTP 前必须调用的单次额度 observer。"""

        if logical_call_id != CALL_ID:
            raise ValueError(f"logical_call_id 必须是 {CALL_ID}")

        def reserve(attempt: HttpAttempt) -> None:
            if attempt.provider != "deepseek" or attempt.model != MODEL:
                raise ApiFinalError("最终 API-4 尝试与固定 provider/model 不一致")
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
        """在 HTTP 发送前原子且永久占用最后一个额度，返回累计用量。"""

        self._validate_final_reservation(
            logical_call_id, attempt_number, streaming
        )
        with self._locked():
            with self._final_parents_locked() as digests:
                document = self._read_final_ledger()
                self._verify_final_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if attempts:
                    raise ApiFinalError("最终 API-4 的唯一 HTTP 额度已永久占用")
                if results:
                    raise ApiFinalError("最终 API-4 结果状态异常，拒绝联网")
                attempts.append(
                    {
                        "logical_call_id": CALL_ID,
                        "original_call_id": ORIGINAL_CALL_ID,
                        "provider_attempt": 1,
                        "global_attempt": GLOBAL_ATTEMPT,
                        "streaming": False,
                        "reserved_at": self._timestamp(),
                    }
                )
                document["used"] = 1
                document["combined_used"] = HISTORICAL_USED + 1
                self._atomic_write(document)
                return HISTORICAL_USED + 1

    def record_result(self, logical_call_id: str, result: str) -> None:
        """为已占额尝试记录不可改写的安全结果分类。"""

        if logical_call_id != CALL_ID:
            raise ApiFinalError("逻辑调用不在最终 API-4 计划中")
        if result not in ALLOWED_RESULTS:
            raise ApiFinalError("最终 API-4 结果分类无效")
        with self._locked():
            with self._final_parents_locked() as digests:
                document = self._read_final_ledger()
                self._verify_final_binding(document, digests)
                attempts = document["attempts"]
                results = document["results"]
                assert isinstance(attempts, list)
                assert isinstance(results, dict)
                if not attempts:
                    raise ApiFinalError("最终 API-4 尚未占用 HTTP 额度")
                if logical_call_id in results:
                    raise ApiFinalError("最终 API-4 结果已记录，禁止改写")
                results[logical_call_id] = result
                self._atomic_write(document)

    def snapshot(self) -> dict[str, object]:
        """返回校验后的非敏感快照。"""

        with self._locked():
            with self._final_parents_locked() as digests:
                document = self._read_final_ledger()
                self._verify_final_binding(document, digests)
                return json.loads(json.dumps(document))

    @contextmanager
    def _final_parents_locked(self) -> Iterator[tuple[str, str, str, str]]:
        fds: list[int] = []
        try:
            for path, label in (
                (self.original_lock_path, "根历史预算锁"),
                (self.resume_lock_path, "第一续跑预算锁"),
                (self.finalize_lock_path, "第二续验预算锁"),
                (self.completion_lock_path, "第三完成预算锁"),
            ):
                fds.append(self._open_lock(path, exclusive=False, label=label))
            payloads = (
                self._read_regular(self.original_path, "根历史预算台账"),
                self._read_regular(self.resume_path, "第一续跑预算台账"),
                self._read_regular(self.finalize_path, "第二续验预算台账"),
                self._read_regular(self.completion_path, "第三完成预算台账"),
            )
            parents = tuple(
                self._parse_json(payload, label)
                for payload, label in zip(
                    payloads,
                    (
                        "根历史预算台账",
                        "第一续跑预算台账",
                        "第二续验预算台账",
                        "第三完成预算台账",
                    ),
                    strict=True,
                )
            )
            self._validate_root_parent(parents[0])
            self._validate_resume_parent(parents[1])
            self._validate_final_parent(parents[2])
            self._validate_completion_parent(parents[3])
            digests = tuple(hashlib.sha256(payload).hexdigest() for payload in payloads)
            expected = (
                ROOT_PARENT_SHA256,
                RESUME_PARENT_SHA256,
                FINALIZE_PARENT_SHA256,
                COMPLETION_PARENT_SHA256,
            )
            if digests != expected:
                raise ApiFinalError("四份父预算台账 SHA-256 不匹配")
            yield digests
        finally:
            for fd in reversed(fds):
                os.close(fd)

    @staticmethod
    def _validate_final_parent(value: dict[str, object]) -> None:
        M4ApiFinalizeLedger._validate_final(value)
        if (
            value["used"] != 1
            or value["combined_used"] != 5
            or value["results"] != {"FINAL-API-3": "incomplete"}
        ):
            raise ApiFinalError("第二续验父台账固定结果不匹配")
        attempts = value["attempts"]
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise ApiFinalError("第二续验父台账尝试数不匹配")
        item = attempts[0]
        if not isinstance(item, dict) or (
            item["logical_call_id"],
            item["original_call_id"],
            item["provider_attempt"],
            item["global_attempt"],
            item["streaming"],
        ) != ("FINAL-API-3", "API-3", 1, 2, False):
            raise ApiFinalError("第二续验父台账尝试映射不匹配")

    @staticmethod
    def _validate_completion_parent(value: dict[str, object]) -> None:
        M4ApiCompletionLedger._validate_completion(value)
        if (
            value["used"] != 2
            or value["combined_used"] != HISTORICAL_USED
            or value["results"]
            != {"COMPLETE-API-3": "success", "COMPLETE-API-4": "incomplete"}
        ):
            raise ApiFinalError("第三完成父台账固定结果不匹配")
        attempts = value["attempts"]
        expected = (
            ("COMPLETE-API-3", "API-3", 1, 3, False),
            ("COMPLETE-API-4", "API-4", 1, 1, False),
        )
        if not isinstance(attempts, list) or len(attempts) != len(expected):
            raise ApiFinalError("第三完成父台账尝试数不匹配")
        for item, fixed in zip(attempts, expected, strict=True):
            if not isinstance(item, dict) or (
                item["logical_call_id"],
                item["original_call_id"],
                item["provider_attempt"],
                item["global_attempt"],
                item["streaming"],
            ) != fixed:
                raise ApiFinalError("第三完成父台账尝试映射不匹配")

    @staticmethod
    def _new_final_document(
        digests: tuple[str, str, str, str]
    ) -> dict[str, object]:
        return {
            "schema_version": FINAL_SCHEMA_VERSION,
            "plan_id": FINAL_PLAN_ID,
            "model": MODEL,
            "root_parent_sha256": digests[0],
            "resume_parent_sha256": digests[1],
            "finalize_parent_sha256": digests[2],
            "completion_parent_sha256": digests[3],
            "historical_used": HISTORICAL_USED,
            "max_total_http_attempts": MAX_TOTAL_HTTP_ATTEMPTS,
            "max_new_http_attempts": MAX_NEW_HTTP_ATTEMPTS,
            "used": 0,
            "combined_used": HISTORICAL_USED,
            "attempts": [],
            "results": {},
        }

    def _read_final_ledger(self) -> dict[str, object]:
        value = self._parse_json(
            self._read_regular(self.path, "最终 API-4 台账"), "最终 API-4 台账"
        )
        self._validate_final_ledger(value)
        return value

    @staticmethod
    def _validate_final_ledger(value: dict[str, object]) -> None:
        expected_fields = {
            "schema_version",
            "plan_id",
            "model",
            "root_parent_sha256",
            "resume_parent_sha256",
            "finalize_parent_sha256",
            "completion_parent_sha256",
            "historical_used",
            "max_total_http_attempts",
            "max_new_http_attempts",
            "used",
            "combined_used",
            "attempts",
            "results",
        }
        if set(value) != expected_fields:
            raise ApiFinalError("最终 API-4 台账字段集合不匹配")
        if (
            value["schema_version"] != FINAL_SCHEMA_VERSION
            or value["plan_id"] != FINAL_PLAN_ID
            or value["model"] != MODEL
            or value["root_parent_sha256"] != ROOT_PARENT_SHA256
            or value["resume_parent_sha256"] != RESUME_PARENT_SHA256
            or value["finalize_parent_sha256"] != FINALIZE_PARENT_SHA256
            or value["completion_parent_sha256"] != COMPLETION_PARENT_SHA256
            or value["historical_used"] != HISTORICAL_USED
            or value["max_total_http_attempts"] != MAX_TOTAL_HTTP_ATTEMPTS
            or value["max_new_http_attempts"] != MAX_NEW_HTTP_ATTEMPTS
            or isinstance(value["max_new_http_attempts"], bool)
        ):
            raise ApiFinalError("最终 API-4 台账固定计划或父绑定不匹配")
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
            or used not in (0, 1)
            or combined != HISTORICAL_USED + used
            or combined > MAX_TOTAL_HTTP_ATTEMPTS
        ):
            raise ApiFinalError("最终 API-4 累计计数不一致")
        if len(attempts) == 1:
            item = attempts[0]
            if not isinstance(item, dict) or set(item) != {
                "logical_call_id",
                "original_call_id",
                "provider_attempt",
                "global_attempt",
                "streaming",
                "reserved_at",
            }:
                raise ApiFinalError("最终 API-4 尝试结构无效")
            if (
                item["logical_call_id"] != CALL_ID
                or item["original_call_id"] != ORIGINAL_CALL_ID
                or item["provider_attempt"] != 1
                or isinstance(item["provider_attempt"], bool)
                or item["global_attempt"] != GLOBAL_ATTEMPT
                or isinstance(item["global_attempt"], bool)
                or item["streaming"] is not False
            ):
                raise ApiFinalError("最终 API-4 尝试映射或模式无效")
            M4ApiFinalizeLedger._validate_timestamp(item["reserved_at"])
        if any(
            call_id != CALL_ID or result not in ALLOWED_RESULTS
            for call_id, result in results.items()
        ) or (results and not attempts):
            raise ApiFinalError("最终 API-4 结果记录无效")

    @staticmethod
    def _verify_final_binding(
        document: dict[str, object], digests: tuple[str, str, str, str]
    ) -> None:
        if (
            document["root_parent_sha256"],
            document["resume_parent_sha256"],
            document["finalize_parent_sha256"],
            document["completion_parent_sha256"],
        ) != digests:
            raise ApiFinalError("最终 API-4 执行期间父台账发生变化")

    @staticmethod
    def _validate_final_reservation(
        logical_call_id: str, attempt_number: int, streaming: bool
    ) -> None:
        if logical_call_id != CALL_ID:
            raise ApiFinalError("逻辑调用不在最终 API-4 计划中")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number != 1
        ):
            raise ApiFinalError("最终 API-4 只允许 provider_attempt=1")
        if streaming is not False:
            raise ApiFinalError("最终 API-4 必须是非流式调用")

    @staticmethod
    def _encode(value: dict[str, object]) -> bytes:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ApiFinalError("最终 API-4 台账超过大小上限")
        return payload
