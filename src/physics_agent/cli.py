"""命令行入口：原始模型通路、知识检索、教学服务与确定性工具。"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Sequence, TextIO

from physics_agent import __version__
from physics_agent.api_budget import ApiBudgetError, AttemptBudgetLedger
from physics_agent.config import ConfigError, load_config
from physics_agent.core import CompletionRequest, KnowledgePackageRef, LearningEvent, Message
from physics_agent.knowledge import (
    KnowledgeError,
    LocalKnowledgeRepository,
    QueryValidationError,
)
from physics_agent.physics_tools import DeterministicPhysicsTool
from physics_agent.publication import PublicationError, prepare_public_source
from physics_agent.learning import (
    InMemoryLearningRepository,
    LearningDataError,
    LearningRecord,
)
from physics_agent.providers.deepseek import (
    DeepSeekProvider,
    IncompleteCompletionError,
    IncompleteStreamError,
    ProviderError,
)
from physics_agent.questions import QuestionImportError, QuestionImporter
from physics_agent.release import ReleaseBuildError
from physics_agent.release_workflow import ReleaseWorkflowError, build_local_release
from physics_agent.teaching import (
    ErrorSignal,
    TeachingRequest,
    TeachingResponse,
    TeachingService,
    ToolCall,
    preflight_teaching_request,
)


M4_API_PLAN_ID = "m4-deepseek-smoke-v1"
M4_API_MAX_HTTP_ATTEMPTS = 8
M4_API_CHAT_PROMPTS = {
    "API-1": "只用一句中文说明：合外力为零是否意味着物体静止？",
    "API-2": "请用不超过 80 个汉字解释牛顿第二定律，不给例题。",
}
M4_API_TEACH_PROBLEM = (
    "在惯性系中，光滑水平面上的 3.2 kg 物体所受水平方向合外力为 -8.0 N，"
    "规定 +x 向右。求物体的加速度。"
)
M4_API_TEACH_KNOWN = (
    "m=3.2 kg",
    "F_net,x=-8.0 N",
    "+x 向右",
    "水平面光滑",
)
M4_REVIEWED_PACKAGE_ROOT = Path("knowledge/mechanics-zh-reviewed-0.2.0")
M4_REVIEWED_PACKAGE = KnowledgePackageRef("mechanics.zh.reviewed", "0.2.0")
M4_REVIEWED_MANIFEST_SHA256 = (
    "704a520ce35d859fe4b0a5305d6c4484b7c5a0a10872e32b411cb05a3c986a1b"
)


def _validate_m4_api_config(config: object) -> None:
    """把获批真实调用锁定到唯一主机、模型、Key 名和退避参数。"""

    expected = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "api_key_env": "PHYSICS_AGENT_CHAT_API_KEY",
        "timeout_seconds": 30,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(config, name, None) != value
    ]
    retry = getattr(config, "retry", None)
    retry_expected = {
        "max_attempts": 2,
        "base_delay_seconds": 0.5,
        "max_delay_seconds": 4.0,
        "max_total_delay_seconds": 8.0,
        "jitter_ratio": 0.2,
    }
    mismatches.extend(
        f"retry.{name}"
        for name, value in retry_expected.items()
        if getattr(retry, name, None) != value
    )
    if mismatches:
        raise ConfigError(
            "M4 真实 API 配置偏离获批固定计划：" + ", ".join(mismatches)
        )


def _add_package_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--package-root",
        type=Path,
        required=True,
        help="包含 manifest.json 的固定知识包目录",
    )
    parser.add_argument(
        "--index",
        type=Path,
        required=True,
        help="位于知识包外的可重建 SQLite 索引路径",
    )
    parser.add_argument("--package-id", required=True, help="预期固定 package_id")
    parser.add_argument("--package-version", required=True, help="预期固定版本")


def _add_attempt_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attempt-ledger",
        type=Path,
        help="真实 API 验证的已初始化预算台账；省略时不启用 M4 调用额度",
    )
    parser.add_argument(
        "--logical-call-id",
        choices=("API-1", "API-2", "API-3", "API-4"),
        help="与预算台账绑定的已批准逻辑调用 ID",
    )


def _add_learning_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--learner-id", required=True, help="匿名学习者 ID（anon_...）")
    parser.add_argument("--package-id", required=True, help="固定知识包 ID")
    parser.add_argument("--package-version", required=True, help="固定知识包版本")
    parser.add_argument("--data-version", default="1", help="学习数据版本（默认 1）")


def _add_learning_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--knowledge-point", required=True, help="知识点稳定 ID")
    parser.add_argument("--question-id", required=True, help="题目稳定 ID")
    parser.add_argument("--question-revision", type=int, required=True, help="题目 revision")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="physics-agent",
        description=(
            "大学物理教学 Agent（M2 原始模型通路与 M3 本地检索/教学/工具）"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    config_parser = subparsers.add_parser(
        "config-check", help="只读检查非敏感 TOML 配置"
    )
    config_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="配置路径（默认：包内无密钥配置）",
    )

    chat_parser = subparsers.add_parser(
        "chat",
        aliases=["ask"],
        help="发送一次文本请求；默认流式输出",
    )
    chat_parser.add_argument(
        "prompt",
        nargs="?",
        help="问题文本；省略时从标准输入读取，以免留在 shell 参数中",
    )
    chat_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="非敏感配置路径（默认：包内无密钥配置）",
    )
    chat_parser.add_argument(
        "--no-stream",
        action="store_true",
        help="关闭流式输出，使用普通 Chat Completions",
    )
    chat_parser.add_argument(
        "--max-tokens",
        type=int,
        help="最大输出 token 数（1—8192）；省略时保持 M2 行为",
    )
    _add_attempt_budget_arguments(chat_parser)

    check_parser = subparsers.add_parser(
        "knowledge-check", help="校验固定知识包并确保派生索引可用"
    )
    _add_package_arguments(check_parser)
    check_parser.add_argument(
        "--rebuild", action="store_true", help="显式全量重建派生索引"
    )

    search_parser = subparsers.add_parser(
        "knowledge-search", aliases=["search"], help="检索固定本地知识包"
    )
    search_parser.add_argument(
        "query", nargs="?", help="查询文本；省略时从标准输入读取"
    )
    search_parser.add_argument("--limit", type=int, default=6, help="返回条数（默认 6）")
    _add_package_arguments(search_parser)

    tool_parser = subparsers.add_parser(
        "tool", help="执行受限的 SymPy/NumPy/Pint 确定性检查"
    )
    tool_parser.add_argument("operation", help="白名单工具操作名")
    tool_parser.add_argument(
        "arguments",
        nargs="?",
        help="JSON 参数对象；省略时从标准输入读取",
    )

    teach_parser = subparsers.add_parser(
        "teach", help="使用已发布知识包执行提示优先教学；拒绝内部 draft"
    )
    teach_parser.add_argument(
        "problem", nargs="?", help="题目文本；省略时从标准输入读取"
    )
    teach_parser.add_argument("--goal", required=True, help="本轮学习目标")
    teach_parser.add_argument(
        "--known", action="append", default=[], help="已知条件；可重复"
    )
    teach_parser.add_argument(
        "--missing", action="append", default=[], help="已知缺失条件；可重复"
    )
    teach_parser.add_argument("--student-answer", help="学生当前答案或思路")
    teach_parser.add_argument(
        "--error-signal",
        action="append",
        default=[],
        metavar="TYPE:EVIDENCE",
        help="结构化错误信号；可重复",
    )
    teach_parser.add_argument(
        "--hint-level", type=int, choices=(1, 2, 3), default=1
    )
    teach_parser.add_argument(
        "--full-solution",
        action="store_true",
        help="明确请求完整解答；默认只给当前级别提示",
    )
    teach_parser.add_argument(
        "--tool-call",
        action="append",
        default=[],
        metavar="JSON",
        help="确定性工具 JSON 参数对象（含 operation）；可重复",
    )
    teach_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="非敏感模型配置路径（默认：包内无密钥配置）",
    )
    teach_parser.add_argument(
        "--max-tokens",
        type=int,
        help="最大输出 token 数（1—8192）；真实 M4 验证固定为 256",
    )
    _add_attempt_budget_arguments(teach_parser)
    _add_package_arguments(teach_parser)

    budget_init_parser = subparsers.add_parser(
        "api-budget-init", help="初始化一次获批的 M4 真实 API 请求预算"
    )
    budget_init_parser.add_argument("--ledger", type=Path, required=True)
    budget_init_parser.add_argument(
        "--config", type=Path, default=None
    )

    budget_status_parser = subparsers.add_parser(
        "api-budget-status", help="校验并显示非敏感 M4 API 预算摘要"
    )
    budget_status_parser.add_argument("--ledger", type=Path, required=True)
    budget_status_parser.add_argument(
        "--config", type=Path, default=None
    )

    learning_parser = subparsers.add_parser(
        "learning", help="会话级结构化学习记录及主动 JSON 导入导出"
    )
    learning_actions = learning_parser.add_subparsers(
        dest="learning_action", required=True
    )
    record_parser = learning_actions.add_parser("record", help="记录一次结构化表现")
    _add_learning_identity(record_parser)
    _add_learning_key(record_parser)
    record_parser.add_argument("--hint-level", type=int, default=0)
    record_parser.add_argument(
        "--performance",
        required=True,
        choices=("correct", "partial", "incorrect", "unattempted"),
    )
    record_parser.add_argument(
        "--error-type",
        choices=(
            "concept",
            "model",
            "force_omission",
            "symbol",
            "unit",
            "algebra",
            "order_of_magnitude",
            "other",
        ),
    )
    record_parser.add_argument("--import", dest="import_path", type=Path)
    record_parser.add_argument("--export", dest="export_path", type=Path)
    record_parser.add_argument("--overwrite", action="store_true")

    show_parser = learning_actions.add_parser("show", help="导入并显示结构化摘要")
    _add_learning_identity(show_parser)
    show_parser.add_argument("--import", dest="import_path", type=Path, required=True)

    for action, help_text in (
        ("correct", "纠正一条误记并降低置信度"),
        ("reclassify", "显式修改错误类型"),
        ("delete", "删除一条指定记录"),
    ):
        action_parser = learning_actions.add_parser(action, help=help_text)
        _add_learning_identity(action_parser)
        _add_learning_key(action_parser)
        action_parser.add_argument(
            "--import", dest="import_path", type=Path, required=True
        )
        action_parser.add_argument(
            "--export", dest="export_path", type=Path, required=True
        )
        action_parser.add_argument("--overwrite", action="store_true")
        if action == "reclassify":
            action_parser.add_argument(
                "--error-type",
                required=True,
                choices=(
                    "concept",
                    "model",
                    "force_omission",
                    "symbol",
                    "unit",
                    "algebra",
                    "order_of_magnitude",
                    "other",
                ),
            )

    export_parser = learning_actions.add_parser(
        "export", help="校验已有学习包并另存为 1.1"
    )
    _add_learning_identity(export_parser)
    export_parser.add_argument("--import", dest="import_path", type=Path, required=True)
    export_parser.add_argument("--export", dest="export_path", type=Path, required=True)
    export_parser.add_argument("--overwrite", action="store_true")

    question_parser = subparsers.add_parser(
        "question-import", help="安全导入一至多个 UTF-8 YAML 题目并规范化为 JSON"
    )
    question_parser.add_argument("inputs", type=Path, nargs="+")
    question_parser.add_argument("--output-dir", type=Path, required=True)

    release_parser = subparsers.add_parser(
        "release-build", help="从明确的干净 Git commit 双重构建 M5 本地产物"
    )
    release_parser.add_argument(
        "--source-root", type=Path, default=Path("."), help="本地 Git 仓库根"
    )
    release_parser.add_argument(
        "--source-commit", required=True, help="与本地 HEAD 一致的 40 位 commit SHA"
    )
    release_parser.add_argument(
        "--license-holder", required=True, help="已确认的 MIT 版权主体"
    )

    public_source_parser = subparsers.add_parser(
        "public-source-prepare", help="从干净 HEAD 导出隔离公开源码快照"
    )
    public_source_parser.add_argument(
        "--source-root", type=Path, default=Path("."), help="本地 Git 仓库根"
    )
    public_source_parser.add_argument(
        "--source-commit", required=True, help="必须与 HEAD 相同的 40 位 commit SHA"
    )
    public_source_parser.add_argument(
        "--target", type=Path, required=True, help="必须尚不存在的仓库外目标目录"
    )

    demo_parser = subparsers.add_parser(
        "demo", help="运行 M4 案例 A/B/C 的薄封装演示"
    )
    demo_parser.add_argument("case", choices=("case-a", "case-b", "case-c"))
    demo_parser.add_argument("--hint-level", type=int, choices=(1, 2, 3), default=1)
    demo_parser.add_argument("--full-solution", action="store_true")
    demo_parser.add_argument(
        "--max-tokens", type=int, default=256,
        help="案例 B 非计划人工测试输出上限（1—8192，默认 256）；不计入 API-4",
    )
    demo_parser.add_argument("--config", type=Path, default=None)
    demo_parser.add_argument(
        "--package-root", type=Path, default=M4_REVIEWED_PACKAGE_ROOT
    )
    demo_parser.add_argument("--index", type=Path, default=Path("cache/m4-demo.sqlite3"))
    demo_parser.add_argument("--export", dest="export_path", type=Path)
    demo_parser.add_argument("--overwrite", action="store_true")
    demo_parser.add_argument(
        "--learner-id", default="anon_demo_case_c_001", help="案例 C 匿名学习者 ID"
    )
    return parser


def _input_text(
    value: str | None,
    *,
    stdin: TextIO,
    stderr: TextIO,
    label: str,
) -> str | None:
    if value is None:
        if stdin.isatty():
            print(f"错误：请通过位置参数或标准输入提供{label}", file=stderr)
            return None
        value = stdin.read()
    value = value.strip()
    if not value:
        print(f"错误：{label}不得为空", file=stderr)
        return None
    return value


def _expected_package(args: argparse.Namespace) -> KnowledgePackageRef:
    return KnowledgePackageRef(
        package_id=args.package_id,
        version=args.package_version,
    )


def _repository(
    args: argparse.Namespace,
    factory: Callable[..., LocalKnowledgeRepository],
) -> LocalKnowledgeRepository:
    return factory(
        args.package_root,
        args.index,
        _expected_package(args),
    )


def _json_mapping(text: str, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}必须是有效 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON 对象")
    return value


def _error_signals(values: Sequence[str]) -> tuple[ErrorSignal, ...]:
    signals: list[ErrorSignal] = []
    for value in values:
        error_type, separator, evidence = value.partition(":")
        if not separator or not error_type.strip() or not evidence.strip():
            raise ValueError("--error-signal 必须使用 TYPE:EVIDENCE 格式")
        signals.append(
            ErrorSignal(error_type=error_type.strip(), evidence=evidence.strip())
        )
    return tuple(signals)


def _m4_budget(
    args: argparse.Namespace,
    *,
    model: str,
) -> tuple[AttemptBudgetLedger | None, Callable[[object], None] | None]:
    ledger_path = getattr(args, "attempt_ledger", None)
    logical_call_id = getattr(args, "logical_call_id", None)
    if (ledger_path is None) != (logical_call_id is None):
        raise ApiBudgetError("--attempt-ledger 与 --logical-call-id 必须同时提供")
    if ledger_path is None:
        return None, None
    ledger = AttemptBudgetLedger(
        ledger_path,
        plan_id=M4_API_PLAN_ID,
        model=model,
        max_http_attempts=M4_API_MAX_HTTP_ATTEMPTS,
    )
    return ledger, ledger.observer(logical_call_id)


def _provider(
    factory: Callable[..., DeepSeekProvider],
    config: object,
    *,
    environ: Mapping[str, str],
    observer: Callable[[object], None] | None,
) -> DeepSeekProvider:
    kwargs: dict[str, object] = {"environ": environ}
    if observer is not None:
        kwargs["attempt_observer"] = observer
    return factory(config, **kwargs)


def _record_api_result(
    ledger: AttemptBudgetLedger | None,
    logical_call_id: str | None,
    result: str,
    *,
    stderr: TextIO,
) -> bool:
    if ledger is None or logical_call_id is None:
        return True
    try:
        ledger.record_result(logical_call_id, result)
    except ApiBudgetError as exc:
        print(f"错误：无法记录 API 预算结果：{exc}", file=stderr)
        return False
    return True


def _print_teaching_response(response: TeachingResponse, *, stdout: TextIO) -> None:
    print(f"教学状态：{response.status}", file=stdout)
    if response.provider_error is not None:
        print(
            "provider_error："
            + json.dumps(response.provider_error, ensure_ascii=False, sort_keys=True),
            file=stdout,
        )
    print(f"应用层提示级别：{response.hint_level}", file=stdout)
    if response.diagnostics:
        print("错误诊断：", file=stdout)
        for diagnostic in response.diagnostics:
            print(
                f"- {diagnostic.error_type}: {diagnostic.evidence} "
                f"{diagnostic.suggestion}",
                file=stdout,
            )
    print("教学文本：", file=stdout)
    print(response.readable_text, file=stdout)
    if response.citations:
        print("已核验引用：" + ", ".join(response.citations), file=stdout)
    if response.tool_results:
        print("确定性工具结果：", file=stdout)
        for tool_result in response.tool_results:
            print(
                json.dumps(
                    {
                        "name": tool_result.name,
                        "status": tool_result.status,
                        "details": tool_result.details,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=stdout,
            )


def _learning_repository(
    args: argparse.Namespace,
    factory: Callable[..., InMemoryLearningRepository],
) -> InMemoryLearningRepository:
    return factory(
        args.learner_id,
        KnowledgePackageRef(args.package_id, args.package_version),
        data_version=args.data_version,
    )


def _record_summary(record: LearningRecord) -> dict[str, object]:
    error: dict[str, object] | None = None
    if record.error is not None:
        error = {
            "type": record.error.error_type,
            "count": record.error.count,
            "correct_count": record.error.correct_count,
            "status": record.error.status,
        }
    return {
        "knowledge_point_id": record.knowledge_point_id,
        "question_id": record.question_id,
        "question_revision": record.question_revision,
        "hint_level": record.hint_level,
        "performance": record.performance,
        "error": error,
        "weakness_confidence": record.weakness_confidence,
        "updated_at": record.updated_at.isoformat(),
    }


def _read_existing_canonical_output(path: Path, *, limit: int) -> bytes:
    """不跟随链接地读取既有 revision，用于跨进程幂等/冲突判定。"""

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("既有题目输出不是普通文件")
        if metadata.st_size > limit:
            raise QuestionImportError(
                "同一题目 ID/revision 已存在不同内容；请增加 revision"
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_canonical_output(path: Path, payload: bytes) -> bool:
    """写入稳定 revision；相同内容幂等，不同内容永远拒绝。"""

    parent = path.parent
    if not parent.is_dir():
        raise OSError("输出目录不存在或不是目录")
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("写入规范 JSON 失败")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            created = True
        except FileExistsError:
            existing = _read_existing_canonical_output(path, limit=len(payload))
            if existing != payload:
                raise QuestionImportError(
                    "同一题目 ID/revision 已存在不同内容；请增加 revision"
                )
            created = False
        directory_fd = os.open(parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return created


def _validate_m4_scheduled_call(args: argparse.Namespace, user_text: str) -> None:
    call_id = getattr(args, "logical_call_id", None)
    if call_id is None:
        return
    if len(user_text) > 200:
        raise ApiBudgetError("真实 API 测试的用户输入不得超过 200 个字符")
    if args.command in {"chat", "ask"}:
        if call_id not in M4_API_CHAT_PROMPTS:
            raise ApiBudgetError("原始 chat 只能使用 API-1 或 API-2")
        if user_text != M4_API_CHAT_PROMPTS[call_id]:
            raise ApiBudgetError("真实 chat 输入与已批准固定文本不一致")
        if (call_id == "API-1") != bool(args.no_stream):
            raise ApiBudgetError("API-1 必须非流式，API-2 必须流式")
        if args.max_tokens != 256:
            raise ApiBudgetError("M4 真实 API 测试必须使用 max_tokens=256")
    elif args.command == "teach":
        if call_id not in {"API-3", "API-4"}:
            raise ApiBudgetError("正式 teach 只能使用 API-3 或 API-4")
        if user_text != M4_API_TEACH_PROBLEM:
            raise ApiBudgetError("真实 teach 题面与已批准案例 B 不一致")
        if args.goal.strip() != "求加速度":
            raise ApiBudgetError("真实 teach 学习目标与已批准案例 B 不一致")
        if tuple(args.known) != M4_API_TEACH_KNOWN:
            raise ApiBudgetError("真实 teach 已知条件与已批准案例 B 不一致")
        if args.missing or args.student_answer is not None:
            raise ApiBudgetError("真实 teach 不允许增加缺失条件或学生回答")
        if args.error_signal or args.tool_call:
            raise ApiBudgetError("真实 teach 不允许增加错误信号或外部工具调用")
        if args.max_tokens != 256:
            raise ApiBudgetError("M4 真实 API 测试必须使用 max_tokens=256")
        if call_id == "API-3" and (args.full_solution or args.hint_level != 1):
            raise ApiBudgetError("API-3 必须是默认第 1 级提示")
        if call_id == "API-4" and not args.full_solution:
            raise ApiBudgetError("API-4 必须明确请求完整解答")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    provider_factory: Callable[..., DeepSeekProvider] | None = None,
    repository_factory: Callable[..., LocalKnowledgeRepository] | None = None,
    tool_factory: Callable[..., DeterministicPhysicsTool] | None = None,
    learning_factory: Callable[..., InMemoryLearningRepository] | None = None,
    question_factory: Callable[..., QuestionImporter] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    if args.command == "public-source-prepare":
        try:
            result = prepare_public_source(
                source_root=args.source_root,
                source_commit=args.source_commit,
                target=args.target,
            )
        except KeyboardInterrupt:
            print("错误：公开源码导出已中断，未保留不完整目标", file=error_stream)
            return 130
        except PublicationError as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            "公开源码快照已准备："
            f"target={result.name}, source_commit={args.source_commit}",
            file=output_stream,
        )
        return 0

    if args.command == "release-build":
        try:
            result = build_local_release(
                source_root=args.source_root,
                source_commit=args.source_commit,
                license_holder=args.license_holder,
            )
        except KeyboardInterrupt:
            print("错误：M5 本地构建已中断，未保留不完整产物", file=error_stream)
            return 130
        except (ReleaseBuildError, ReleaseWorkflowError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            "M5 本地产物已通过双构建一致性验证："
            f"bundle={result.bundle_path.name}, "
            f"archive={result.archive_path.name}, "
            f"sha256={result.archive_sha256}, "
            f"source_commit={result.source_commit}",
            file=output_stream,
        )
        return 0

    if args.command in {"api-budget-init", "api-budget-status"}:
        try:
            config = load_config(args.config)
            _validate_m4_api_config(config.chat)
            ledger = AttemptBudgetLedger(
                args.ledger,
                plan_id=M4_API_PLAN_ID,
                model=config.chat.model,
                max_http_attempts=M4_API_MAX_HTTP_ATTEMPTS,
            )
            if args.command == "api-budget-init":
                ledger.initialize()
            snapshot = ledger.snapshot()
        except (ApiBudgetError, ConfigError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            "API 预算有效："
            f"plan={snapshot['plan_id']}, model={snapshot['model']}, "
            f"used={snapshot['used']}/{snapshot['max_http_attempts']}, "
            f"results={json.dumps(snapshot['results'], ensure_ascii=False, sort_keys=True)}",
            file=output_stream,
        )
        return 0

    if args.command == "config-check":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            parser.error(str(exc))
        print(
            "配置有效："
            f"schema={config.schema_version}, "
            f"chat={config.chat.provider}, "
            f"embedding={config.embedding.provider}, "
            f"learning={config.learning_persistence}",
            file=output_stream,
        )
        return 0

    if args.command == "knowledge-check":
        repository_builder = (
            LocalKnowledgeRepository
            if repository_factory is None
            else repository_factory
        )
        try:
            repository = _repository(args, repository_builder)
            if args.rebuild:
                repository.rebuild_index()
        except (KnowledgeError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            "知识包有效："
            f"package={repository.package.package_id}@{repository.package.version}, "
            f"status={repository.status}, "
            f"manifest_sha256={repository.manifest_sha256}",
            file=output_stream,
        )
        return 0

    if args.command in {"knowledge-search", "search"}:
        query = _input_text(
            args.query,
            stdin=input_stream,
            stderr=error_stream,
            label="查询内容",
        )
        if query is None:
            return 2
        repository_builder = (
            LocalKnowledgeRepository
            if repository_factory is None
            else repository_factory
        )
        try:
            repository = _repository(args, repository_builder)
            hits = repository.search(query, repository.package, limit=args.limit)
        except (KnowledgeError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        if repository.status == "draft":
            print(
                "警告：当前知识包是内部 draft，只可检查和检索，不得用于学生教学或发布。",
                file=error_stream,
            )
        for hit in hits:
            print(f"[{hit.item_id}]", file=output_stream)
            print(hit.text.rstrip(), file=output_stream)
            for source in hit.sources:
                locator = json.dumps(
                    dict(source.locator), ensure_ascii=False, sort_keys=True
                )
                print(
                    "来源："
                    f"{source.source_id}@{source.source_version}; "
                    f"license={source.license_id}; locator={locator}",
                    file=output_stream,
                )
        return 0

    if args.command == "tool":
        raw_arguments = _input_text(
            args.arguments,
            stdin=input_stream,
            stderr=error_stream,
            label="工具 JSON 参数",
        )
        if raw_arguments is None:
            return 2
        try:
            arguments = _json_mapping(raw_arguments, label="工具参数")
            declared_operation = arguments.get("operation")
            if declared_operation is not None and declared_operation != args.operation:
                raise ValueError("位置参数 operation 与 JSON 中的 operation 不一致")
            arguments["operation"] = args.operation
            factory = DeterministicPhysicsTool if tool_factory is None else tool_factory
            result = dict(factory().execute(arguments))
        except (TypeError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            file=output_stream,
        )
        return 0 if result.get("status") == "verified" else 1

    if args.command == "learning":
        factory = (
            InMemoryLearningRepository
            if learning_factory is None
            else learning_factory
        )
        try:
            repository = _learning_repository(args, factory)
            if args.import_path is not None:
                repository.import_file(args.import_path)
            action = args.learning_action
            if action == "record":
                record = repository.record(
                    LearningEvent(
                        learner_id=args.learner_id,
                        knowledge_point_id=args.knowledge_point,
                        question_id=args.question_id,
                        question_revision=args.question_revision,
                        occurred_at=datetime.now(timezone.utc),
                        hint_level=args.hint_level,
                        performance=args.performance,
                        error_type=args.error_type,
                    )
                )
                if args.export_path is not None:
                    repository.export_file(args.export_path, overwrite=args.overwrite)
                else:
                    print(
                        "警告：记录仅存在于本进程，退出后丢失；使用 --export 主动保存。",
                        file=error_stream,
                    )
                records = (record,)
            elif action == "show":
                records = repository.list_records()
            elif action == "correct":
                record = repository.correct(
                    args.knowledge_point,
                    args.question_id,
                    args.question_revision,
                )
                repository.export_file(args.export_path, overwrite=args.overwrite)
                records = (record,)
            elif action == "reclassify":
                record = repository.reclassify(
                    args.knowledge_point,
                    args.question_id,
                    args.question_revision,
                    args.error_type,
                )
                repository.export_file(args.export_path, overwrite=args.overwrite)
                records = (record,)
            elif action == "delete":
                repository.delete(
                    args.knowledge_point,
                    args.question_id,
                    args.question_revision,
                )
                repository.export_file(args.export_path, overwrite=args.overwrite)
                records = repository.list_records()
            elif action == "export":
                repository.export_file(args.export_path, overwrite=args.overwrite)
                records = repository.list_records()
            else:  # argparse 已限制；保留故障关闭分支
                raise LearningDataError("未知 learning 操作")
        except (LearningDataError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            json.dumps(
                {
                    "learner_id": repository.learner_id,
                    "knowledge_package": {
                        "id": repository.knowledge_package.package_id,
                        "version": repository.knowledge_package.version,
                    },
                    "records": [_record_summary(record) for record in records],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=output_stream,
        )
        return 0

    if args.command == "question-import":
        factory = QuestionImporter if question_factory is None else question_factory
        try:
            # 外部 YAML 自报审核状态不能由命令行参数自行放行；首轮 CLI 只导入 draft。
            importer = factory()
            results = [importer.import_file(path) for path in args.inputs]
            args.output_dir.mkdir(parents=True, exist_ok=True)
            if not args.output_dir.is_dir():
                raise OSError("输出路径不是目录")
            outputs: list[dict[str, object]] = []
            for result in results:
                target = args.output_dir / (
                    f"{result.question_id}.r{result.revision}.json"
                )
                created = _write_canonical_output(target, result.canonical_json)
                outputs.append(
                    {
                        "id": result.question_id,
                        "revision": result.revision,
                        "sha256": result.content_sha256,
                        "created": created,
                        "output": str(target),
                    }
                )
        except (QuestionImportError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            json.dumps(outputs, ensure_ascii=False, sort_keys=True),
            file=output_stream,
        )
        return 0

    if args.command == "demo":
        if args.case == "case-a":
            request = TeachingRequest(
                problem="粗糙水平面上的物体受到水平拉力，求摩擦力和加速度。",
                goal="判断摩擦力和运动状态",
                known_conditions=("接触面粗糙",),
                missing_conditions=(),
                student_answer=None,
                error_signals=(),
                hint_level=args.hint_level,
                full_solution=args.full_solution,
                max_tokens=256,
            )
            try:
                response = preflight_teaching_request(request)
            except ValueError as exc:
                print(f"错误：{exc}", file=error_stream)
                return 2
            if response is None:
                print("错误：案例 A 未触发条件不足门禁", file=error_stream)
                return 1
            _print_teaching_response(response, stdout=output_stream)
            print("模型调用：0（本地 preflight）", file=output_stream)
            return 0
        if args.case == "case-b":
            print(
                "人工测试（非固定 M4 计划）：不计入 API-4 验收，不写固定预算台账；"
                f"max_tokens={args.max_tokens}",
                file=output_stream,
            )
            nested = [
                "teach",
                M4_API_TEACH_PROBLEM,
                "--goal",
                "求加速度",
                "--known",
                M4_API_TEACH_KNOWN[0],
                "--known",
                M4_API_TEACH_KNOWN[1],
                "--known",
                M4_API_TEACH_KNOWN[2],
                "--known",
                M4_API_TEACH_KNOWN[3],
                "--hint-level",
                str(args.hint_level),
                "--max-tokens",
                str(args.max_tokens),
                "--package-root",
                str(args.package_root),
                "--index",
                str(args.index),
                "--package-id",
                M4_REVIEWED_PACKAGE.package_id,
                "--package-version",
                M4_REVIEWED_PACKAGE.version,
            ]
            if args.config is not None:
                nested.extend(("--config", str(args.config)))
            if args.full_solution:
                nested.append("--full-solution")
            return main(
                nested,
                environ=environ,
                stdin=input_stream,
                stdout=output_stream,
                stderr=error_stream,
                provider_factory=provider_factory,
                repository_factory=repository_factory,
                tool_factory=tool_factory,
                learning_factory=learning_factory,
                question_factory=question_factory,
            )
        if args.export_path is None:
            print("错误：案例 C 必须用 --export 主动保存学习包", file=error_stream)
            return 2
        tool_builder = DeterministicPhysicsTool if tool_factory is None else tool_factory
        learning_builder = (
            InMemoryLearningRepository
            if learning_factory is None
            else learning_factory
        )
        try:
            tool_result = dict(
                tool_builder().execute(
                    {
                        "operation": "unit_convert",
                        "value": 72,
                        "from_unit": "km/h",
                        "to_unit": "m/s",
                    }
                )
            )
            if tool_result.get("status") != "verified":
                raise ValueError("72 km/h 的确定性换算未通过")
            repository = learning_builder(
                args.learner_id,
                M4_REVIEWED_PACKAGE,
                data_version="1",
            )
            record = repository.record(
                LearningEvent(
                    learner_id=args.learner_id,
                    knowledge_point_id="mechanics.unit-conversion",
                    question_id="case-c-unit-001",
                    question_revision=1,
                    occurred_at=datetime.now(timezone.utc),
                    hint_level=0,
                    performance="incorrect",
                    error_type="unit",
                )
            )
            repository.export_file(args.export_path, overwrite=args.overwrite)
            restored = learning_builder(
                args.learner_id,
                M4_REVIEWED_PACKAGE,
                data_version="1",
            )
            restored.import_file(args.export_path)
        except (LearningDataError, OSError, TypeError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        print(
            "确定性换算："
            + json.dumps(tool_result, ensure_ascii=False, sort_keys=True),
            file=output_stream,
        )
        print(
            "首次单位错误："
            + json.dumps(_record_summary(record), ensure_ascii=False, sort_keys=True),
            file=output_stream,
        )
        print(
            f"新进程等价导入：records={len(restored.list_records())}, "
            f"package={restored.knowledge_package.package_id}@"
            f"{restored.knowledge_package.version}",
            file=output_stream,
        )
        return 0

    if args.command == "teach":
        problem = _input_text(
            args.problem,
            stdin=input_stream,
            stderr=error_stream,
            label="题目内容",
        )
        if problem is None:
            return 2
        try:
            _validate_m4_scheduled_call(args, problem)
            error_signals = _error_signals(args.error_signal)
            tool_calls = tuple(
                ToolCall(
                    name="physics_deterministic",
                    arguments=_json_mapping(raw, label="--tool-call"),
                )
                for raw in args.tool_call
            )
            request = TeachingRequest(
                problem=problem,
                goal=args.goal.strip(),
                known_conditions=tuple(args.known),
                missing_conditions=tuple(args.missing),
                student_answer=args.student_answer,
                error_signals=error_signals,
                hint_level=args.hint_level,
                full_solution=args.full_solution,
                tool_calls=tool_calls,
                max_tokens=args.max_tokens,
            )
            preflight = preflight_teaching_request(request)
        except (ApiBudgetError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        if preflight is not None:
            _print_teaching_response(preflight, stdout=output_stream)
            return 0

        repository_builder = (
            LocalKnowledgeRepository
            if repository_factory is None
            else repository_factory
        )
        provider: DeepSeekProvider | None = None
        try:
            repository = _repository(args, repository_builder)
            if repository.status != "published":
                raise KnowledgeError(
                    "教学入口只接受通过人工审核门禁的 published 知识包；内部 draft 已拒绝"
                )
            if (
                repository.package == M4_REVIEWED_PACKAGE
                and repository.manifest_sha256 != M4_REVIEWED_MANIFEST_SHA256
            ):
                raise KnowledgeError("用户审核的 M4 知识包 manifest 不匹配")
            if getattr(args, "attempt_ledger", None) is not None:
                if (
                    repository.package != M4_REVIEWED_PACKAGE
                    or repository.manifest_sha256 != M4_REVIEWED_MANIFEST_SHA256
                ):
                    raise KnowledgeError(
                        "M4 真实 teach 只允许用户审核的固定 published manifest"
                    )
            config = load_config(args.config)
            if config.chat.provider != "deepseek":
                raise ConfigError(
                    f"M3 teach 不支持 provider={config.chat.provider!r}"
                )
            if getattr(args, "attempt_ledger", None) is not None:
                _validate_m4_api_config(config.chat)
            ledger, observer = _m4_budget(args, model=config.chat.model)
            provider_builder = (
                DeepSeekProvider if provider_factory is None else provider_factory
            )
            provider = _provider(
                provider_builder,
                config.chat,
                environ=os.environ if environ is None else environ,
                observer=observer,
            )
            physics_tool_builder = (
                DeterministicPhysicsTool if tool_factory is None else tool_factory
            )
            physics_tool = physics_tool_builder()
        except (
            ApiBudgetError,
            ConfigError,
            KnowledgeError,
            OSError,
            ProviderError,
            ValueError,
        ) as exc:
            if provider is not None:
                provider.close()
            print(f"错误：{exc}", file=error_stream)
            return 2
        try:
            response = TeachingService(
                provider=provider,
                repository=repository,
                package=repository.package,
                tools=(physics_tool,),
                timeout_seconds=config.chat.timeout_seconds,
            ).respond(request)
        except (KnowledgeError, QueryValidationError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2
        finally:
            provider.close()

        result_class = (
            "success"
            if response.status in {"needs_information", "hint", "full_solution"}
            else "incomplete"
        )
        if not _record_api_result(
            ledger,
            args.logical_call_id,
            result_class,
            stderr=error_stream,
        ):
            return 1

        _print_teaching_response(response, stdout=output_stream)
        return 0 if response.status in {"needs_information", "hint", "full_solution"} else 1

    if args.command in {"chat", "ask"}:
        prompt = args.prompt
        if prompt is None:
            if input_stream.isatty():
                print("错误：请通过位置参数或标准输入提供问题", file=error_stream)
                return 2
            prompt = input_stream.read()
        prompt = prompt.strip()
        if not prompt:
            print("错误：问题内容不得为空", file=error_stream)
            return 2

        try:
            _validate_m4_scheduled_call(args, prompt)
            config = load_config(args.config)
            if config.chat.provider != "deepseek":
                raise ConfigError(
                    f"M2 chat 不支持 provider={config.chat.provider!r}"
                )
            if getattr(args, "attempt_ledger", None) is not None:
                _validate_m4_api_config(config.chat)
            request = CompletionRequest(
                messages=(Message(role="user", content=prompt),),
                timeout_seconds=config.chat.timeout_seconds,
                max_tokens=args.max_tokens,
            )
            ledger, observer = _m4_budget(args, model=config.chat.model)
            factory = DeepSeekProvider if provider_factory is None else provider_factory
            provider = _provider(
                factory,
                config.chat,
                environ=os.environ if environ is None else environ,
                observer=observer,
            )
        except (ApiBudgetError, ConfigError, ProviderError, ValueError) as exc:
            print(f"错误：{exc}", file=error_stream)
            return 2

        try:
            if args.no_stream:
                result = provider.complete(request)
                if not result.completed:
                    raise IncompleteCompletionError("provider-marked-incomplete", result.text)
                print(result.text, file=output_stream)
            else:
                for content in provider.stream(request):
                    print(content, end="", file=output_stream, flush=True)
                print(file=output_stream)
        except ProviderError as exc:
            _record_api_result(
                ledger,
                args.logical_call_id,
                "incomplete",
                stderr=error_stream,
            )
            print(f"错误：{exc}", file=error_stream)
            return 1
        finally:
            provider.close()
        if not _record_api_result(
            ledger,
            args.logical_call_id,
            "success",
            stderr=error_stream,
        ):
            return 1
        return 0

    parser.print_help(file=output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
