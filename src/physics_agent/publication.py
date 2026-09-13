"""从已审核 HEAD 安全导出隔离的公开源码快照。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Iterable

from physics_agent.release import (
    ReleaseBuildError,
    _scan_content,
    render_mit_license,
    render_release_notice,
    render_third_party_notices,
)


PUBLIC_FORMAT = "university-physics-agent-public-source"
PUBLIC_VERSION = "0.5.0"
PUBLIC_HOLDER = "余杰"
REVIEWED_MANIFEST_SHA256 = (
    "704a520ce35d859fe4b0a5305d6c4484b7c5a0a10872e32b411cb05a3c986a1b"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED = {
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES",
    "README.md",
    "pyproject.toml",
    "environment.yml",
    "config/default.toml",
    "config/local.example.toml",
    "schemas/knowledge-package.schema.json",
    "schemas/learning-package-1.1.schema.json",
    "schemas/learning-package.schema.json",
    "schemas/question.schema.json",
    "docs/student/PRIVACY-LIMITS.md",
    "docs/student/QUICKSTART.md",
    "docs/student/REBUILD.md",
    "docs/student/TROUBLESHOOTING.md",
    "examples/questions/net-force-concept-draft.yaml",
    "knowledge/mechanics-zh-reviewed-0.2.0/manifest.json",
    "src/physics_agent/resources/__init__.py",
    "src/physics_agent/resources/config/default.toml",
    "src/physics_agent/resources/schemas/knowledge-package.schema.json",
    "src/physics_agent/resources/schemas/learning-package-1.1.schema.json",
    "src/physics_agent/resources/schemas/learning-package.schema.json",
    "src/physics_agent/resources/schemas/question.schema.json",
}
_OPTIONAL_ROOT = {".gitignore"}
_DENIED_COMPONENTS = {
    ".codex",
    ".env",
    ".git",
    ".pytest_cache",
    ".vscode",
    "__pycache__",
    "cache",
    "scripts",
    "tests",
    "work",
}


class PublicationError(RuntimeError):
    """公开源码快照不满足隔离发布契约。"""


def prepare_public_source(
    *,
    source_root: str | Path,
    source_commit: str,
    target: str | Path,
) -> Path:
    """从与 HEAD 相同的干净 commit 原子导出公开 allowlist。"""

    if not _COMMIT.fullmatch(source_commit):
        raise PublicationError("source_commit 必须是 40 位小写 Git SHA")
    source = _real_directory(source_root, "source_root")
    if Path(_git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve(
        strict=True
    ) != source:
        raise PublicationError("source_root 必须恰为本地 Git 仓库根")
    head = _git(source, "rev-parse", "--verify", "HEAD").decode().strip()
    if head != source_commit:
        raise PublicationError("source_commit 必须明确等于当前 HEAD")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=no"):
        raise PublicationError("tracked 工作树或暂存区不干净")

    tree = _git_tree(source, source_commit)
    selected = _public_allowlist(source, source_commit, tree)
    payload = _validate_payload(source, source_commit, tree, selected)
    _validate_public_contract(payload)

    destination = _new_target(source, target)
    parent = destination.parent
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.public-", dir=parent)
    )
    try:
        entries = []
        for relative, content in sorted(payload.items()):
            output = temporary.joinpath(*PurePosixPath(relative).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            os.chmod(output, 0o644)
            entries.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
        manifest = {
            "format": PUBLIC_FORMAT,
            "format_version": 1,
            "project_version": PUBLIC_VERSION,
            "source_commit": source_commit,
            "payload": entries,
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (temporary / "PUBLIC-SOURCE.json").write_bytes(manifest_bytes)
        for path in (temporary, *temporary.rglob("*")):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        os.rename(temporary, destination)
    except BaseException:
        _safe_remove(parent, temporary, prefix=f".{destination.name}.public-")
        _safe_remove(parent, destination, exact_name=destination.name)
        raise
    return destination


def _real_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PublicationError(f"{label} 不存在或不可访问") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PublicationError(f"{label} 必须是真实目录")
    return path.resolve(strict=True)


def _new_target(source: Path, value: str | Path) -> Path:
    target = Path(value)
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.abspath(target))
    if target == source or target.is_relative_to(source):
        raise PublicationError("公开源码目标必须位于开发仓库之外")
    if target.exists() or target.is_symlink():
        raise PublicationError("公开源码目标必须尚不存在")
    parent = target.parent
    try:
        info = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise PublicationError("公开源码目标父目录不存在或不可访问") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or resolved_parent != parent
    ):
        raise PublicationError("公开源码目标父路径必须是真实目录且不含链接")
    if not target.name or target.name in {".", "..", ".git"}:
        raise PublicationError("公开源码目标名称无效")
    return target


def _git_tree(source: Path, commit: str) -> dict[str, tuple[str, str, str]]:
    output = _git(source, "ls-tree", "-rz", "--full-tree", commit)
    tree: dict[str, tuple[str, str, str]] = {}
    for record in output.split(b"\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        parts = header.decode("ascii").split()
        if not separator or len(parts) != 3:
            raise PublicationError("Git tree 记录结构无效")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicationError("Git tree 含非 UTF-8 路径") from exc
        _safe_path(path)
        if path in tree:
            raise PublicationError("Git tree 含重复路径")
        tree[path] = (parts[0], parts[1], parts[2])
    return tree


def _public_allowlist(
    source: Path,
    commit: str,
    tree: dict[str, tuple[str, str, str]],
) -> set[str]:
    if "PUBLIC-SOURCE.json" in tree:
        raise PublicationError("PUBLIC-SOURCE.json 只能由导出器生成")
    missing = sorted(_REQUIRED - tree.keys())
    if missing:
        raise PublicationError("公开 allowlist 缺少必需文件：" + ", ".join(missing))
    selected = set(_REQUIRED) | (_OPTIONAL_ROOT & tree.keys())
    selected.update(
        path
        for path in tree
        if path.startswith("src/physics_agent/")
        and (path.endswith(".py") or path.startswith("src/physics_agent/resources/"))
    )
    manifest_path = "knowledge/mechanics-zh-reviewed-0.2.0/manifest.json"
    manifest = _git_blob(source, commit, manifest_path)
    if hashlib.sha256(manifest).hexdigest() != REVIEWED_MANIFEST_SHA256:
        raise PublicationError("reviewed manifest 摘要不是固定审核版本")
    try:
        document = json.loads(manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError("reviewed manifest 不是有效 UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("package_id") != "mechanics.zh.reviewed"
        or document.get("version") != "0.2.0"
        or document.get("status") != "published"
        or not isinstance(document.get("items"), list)
    ):
        raise PublicationError("reviewed manifest 身份或状态无效")
    knowledge = {manifest_path}
    prefix = "knowledge/mechanics-zh-reviewed-0.2.0/"
    for item in document["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise PublicationError("reviewed manifest item 路径无效")
        item_path = _safe_path(item["path"])
        knowledge.add(prefix + item_path)
    tracked_knowledge = {path for path in tree if path.startswith(prefix)}
    if knowledge != tracked_knowledge:
        raise PublicationError("reviewed 知识包不是完整且唯一的 manifest 闭包")
    selected.update(knowledge)
    return selected


def _validate_payload(
    source: Path,
    commit: str,
    tree: dict[str, tuple[str, str, str]],
    selected: Iterable[str],
) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    for relative in selected:
        _reject_public_path(relative)
        mode, object_type, _ = tree[relative]
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PublicationError(f"公开输入不是普通 Git blob：{relative}")
        path = source.joinpath(*PurePosixPath(relative).parts)
        _reject_linked_worktree_path(source, path)
        content = path.read_bytes()
        if content != _git_blob(source, commit, relative):
            raise PublicationError(f"公开输入当前字节不属于 HEAD：{relative}")
        try:
            _scan_content(PurePosixPath(relative), content)
        except ReleaseBuildError as exc:
            raise PublicationError(f"公开输入隐私扫描失败：{relative}") from exc
        payload[relative] = content
    return payload


def _validate_public_contract(payload: dict[str, bytes]) -> None:
    if payload["LICENSE"] != render_mit_license(PUBLIC_HOLDER):
        raise PublicationError("LICENSE 未绑定余杰/2026 的完整 MIT 正文")
    if payload["NOTICE"] != render_release_notice(PUBLIC_HOLDER):
        raise PublicationError("NOTICE 与固定公开许可范围不一致")
    if payload["THIRD_PARTY_NOTICES"] != render_third_party_notices():
        raise PublicationError("THIRD_PARTY_NOTICES 与固定环境完整许可正文不一致")


def _reject_linked_worktree_path(source: Path, path: Path) -> None:
    current = source
    relative = path.relative_to(source)
    try:
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            final = current == path
            if stat.S_ISLNK(info.st_mode):
                raise PublicationError("公开输入路径包含符号链接")
            if final and not stat.S_ISREG(info.st_mode):
                raise PublicationError("公开输入不是普通文件")
            if not final and not stat.S_ISDIR(info.st_mode):
                raise PublicationError("公开输入父路径不是普通目录")
    except OSError as exc:
        raise PublicationError("公开输入不存在或不可访问") from exc


def _safe_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise PublicationError("公开路径不是安全 POSIX 相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PublicationError("公开路径包含路径穿越或非规范形式")
    return path.as_posix()


def _reject_public_path(value: str) -> None:
    path = PurePosixPath(_safe_path(value))
    lowered = {part.casefold() for part in path.parts}
    if lowered & _DENIED_COMPONENTS:
        raise PublicationError(f"公开 allowlist 命中禁止路径：{value}")
    if "draft-mechanics-zh-0.1.0" in lowered:
        raise PublicationError("公开 allowlist 不得包含内部 draft 知识包")
    if path.suffix.casefold() in {".pdf", ".ppt", ".pptx", ".sqlite", ".db"}:
        raise PublicationError("公开 allowlist 包含禁止文件类型")


def _git_blob(source: Path, commit: str, relative: str) -> bytes:
    return _git(source, "cat-file", "blob", f"{commit}:{relative}")


def _git(source: Path, *arguments: str) -> bytes:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        completed = subprocess.run(
            ("git", "-C", os.fspath(source), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublicationError("本地 Git 只读校验无法执行") from exc
    if completed.returncode != 0:
        raise PublicationError("本地 Git HEAD/tree/blob 校验失败")
    return completed.stdout


def _safe_remove(
    parent: Path,
    path: Path,
    *,
    prefix: str | None = None,
    exact_name: str | None = None,
) -> None:
    if path.parent != parent:
        raise PublicationError("拒绝清理目标父目录之外的路径")
    if prefix is not None and not path.name.startswith(prefix):
        raise PublicationError("拒绝清理未经验证的临时路径")
    if exact_name is not None and path.name != exact_name:
        raise PublicationError("拒绝清理未经验证的公开目标")
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
