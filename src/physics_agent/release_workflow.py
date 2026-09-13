"""M5 正式本地发布的固定 commit、双构建可重现编排。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from importlib import metadata as importlib_metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterator

from physics_agent.release import (
    ReleaseBuildError,
    ReleaseInput,
    ReleaseMetadata,
    ReleaseResult,
    build_release_bundle,
)


VERSION = "0.5.0"
BUNDLE_NAME = f"university-physics-agent-{VERSION}-wsl-linux"
WHEEL_NAME = f"university_physics_agent-{VERSION}-py3-none-any.whl"
REVIEWED_ROOT = "knowledge/mechanics-zh-reviewed-0.2.0"
REVIEWED_MANIFEST_SHA256 = (
    "704a520ce35d859fe4b0a5305d6c4484b7c5a0a10872e32b411cb05a3c986a1b"
)
SCHEMA_NAMES = (
    "knowledge-package.schema.json",
    "learning-package-1.1.schema.json",
    "learning-package.schema.json",
    "question.schema.json",
)
STUDENT_DOCUMENTS = (
    "PRIVACY-LIMITS.md",
    "QUICKSTART.md",
    "REBUILD.md",
    "TROUBLESHOOTING.md",
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PLACEHOLDER_HOLDERS = {
    "accurate name or organization",
    "copyright holder",
    "real name or full organization name",
    "准确姓名或组织名称",
    "真实姓名或组织全称",
}


class ReleaseWorkflowError(RuntimeError):
    """正式本地发布的身份、工作树或可重现性门禁失败。"""


@dataclass(frozen=True)
class ReleaseWorkflowResult:
    """两次构建一致后保留的唯一本地产物。"""

    bundle_path: Path
    archive_path: Path
    sidecar_path: Path
    archive_sha256: str
    source_commit: str


WheelBuilder = Callable[[Path, Path, int], Path]
BundleBuilder = Callable[..., ReleaseResult]


def build_local_release(
    *,
    source_root: str | Path,
    source_commit: str,
    license_holder: str,
    wheel_builder: WheelBuilder | None = None,
    bundle_builder: BundleBuilder = build_release_bundle,
) -> ReleaseWorkflowResult:
    """从明确的干净 commit 双重构建，一致后才发布到 ``dist/``。"""

    holder = _validated_holder(license_holder)
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise ReleaseWorkflowError("source_commit 必须是明确的 40 位小写 Git SHA")
    source = _real_directory(source_root, "source_root")
    tree_paths = _validate_commit(source, source_commit)
    inputs_without_wheel = _release_inputs(source, source_commit, tree_paths)
    tracked_payload = sorted({item.source for item in inputs_without_wheel})
    _require_clean_payload(source, source_commit, tracked_payload)
    source_date_epoch = _commit_time(source, source_commit)
    metadata = ReleaseMetadata(
        version=VERSION,
        source_commit=source_commit,
        knowledge_manifest_sha256=REVIEWED_MANIFEST_SHA256,
        source_date_epoch=source_date_epoch,
        python_version=platform.python_version(),
        pip_version=importlib_metadata.version("pip"),
        setuptools_version=importlib_metadata.version("setuptools"),
        wheel_version=importlib_metadata.version("wheel"),
        license_status="confirmed",
        license_holder=holder,
    )

    dist, dist_created = _prepare_dist(source)
    final_paths = (
        dist / BUNDLE_NAME,
        dist / f"{BUNDLE_NAME}.tar.gz",
        dist / f"{BUNDLE_NAME}.tar.gz.sha256",
    )
    _require_missing_outputs(final_paths)
    work = Path(tempfile.mkdtemp(prefix=".m5-workflow-", dir=dist))
    cleanup_candidates: list[Path] = []
    builder = _build_wheel if wheel_builder is None else wheel_builder
    try:
        source_stages = (work / "source-a", work / "source-b")
        wheel_directories = (work / "wheel-a", work / "wheel-b")
        for stage in source_stages:
            stage.mkdir(mode=0o755)
            _archive_commit(source, source_commit, stage)
        wheels = tuple(
            builder(stage, wheel_directory, source_date_epoch)
            for stage, wheel_directory in zip(
                source_stages, wheel_directories, strict=True
            )
        )
        if _regular_bytes(wheels[0], "第一份 wheel") != _regular_bytes(
            wheels[1], "第二份 wheel"
        ):
            raise ReleaseWorkflowError("两次全新源码 staging 构建的 wheel 不一致")

        release_inputs = tuple(inputs_without_wheel) + (
            ReleaseInput(
                source=wheels[0].relative_to(source).as_posix(),
                member=f"artifacts/{WHEEL_NAME}",
                category="artifact",
            ),
        )
        with _fixed_build_environment(source_date_epoch):
            first = bundle_builder(
                source_root=source,
                dist_root=work / "verify-a",
                bundle_name=BUNDLE_NAME,
                inputs=release_inputs,
                metadata=metadata,
            )
            second_inputs = tuple(inputs_without_wheel) + (
                ReleaseInput(
                    source=wheels[1].relative_to(source).as_posix(),
                    member=f"artifacts/{WHEEL_NAME}",
                    category="artifact",
                ),
            )
            second = bundle_builder(
                source_root=source,
                dist_root=work / "verify-b",
                bundle_name=BUNDLE_NAME,
                inputs=second_inputs,
                metadata=metadata,
            )
        _compare_results(first, second)
        _require_missing_outputs(final_paths)
        cleanup_candidates.extend(final_paths)
        for candidate, destination in zip(
            (first.bundle_path, first.archive_path, first.sidecar_path),
            final_paths,
            strict=True,
        ):
            os.replace(candidate, destination)
        return ReleaseWorkflowResult(
            bundle_path=final_paths[0],
            archive_path=final_paths[1],
            sidecar_path=final_paths[2],
            archive_sha256=first.archive_sha256,
            source_commit=source_commit,
        )
    except BaseException as exc:
        _remove_published(cleanup_candidates, dist)
        if isinstance(exc, (ReleaseWorkflowError, ReleaseBuildError)):
            raise
        if isinstance(exc, Exception):
            raise ReleaseWorkflowError("正式本地发布流程失败") from exc
        raise
    finally:
        _remove_work_tree(work, dist)
        if dist_created:
            try:
                dist.rmdir()
            except OSError:
                pass


def _validated_holder(value: object) -> str:
    if not isinstance(value, str):
        raise ReleaseWorkflowError("MIT 版权主体必须是明确文本")
    holder = value.strip()
    if (
        not holder
        or holder.casefold() in _PLACEHOLDER_HOLDERS
        or any(character in holder for character in "\r\n\x00")
    ):
        raise ReleaseWorkflowError("MIT 版权主体仍为空或占位符，禁止正式构建")
    return holder


def _real_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseWorkflowError(f"{label} 不存在或不可访问") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseWorkflowError(f"{label} 必须是非符号链接真实目录")
    return path.resolve(strict=True)


def _validate_commit(source: Path, commit: str) -> set[str]:
    head = _git(source, "rev-parse", "--verify", "HEAD").decode().strip()
    if head != commit:
        raise ReleaseWorkflowError("source_commit 必须与当前本地 HEAD 完全一致")
    if _git(source, "cat-file", "-t", commit).strip() != b"commit":
        raise ReleaseWorkflowError("source_commit 不是本地仓库中的 commit")
    top = Path(_git(source, "rev-parse", "--show-toplevel").decode().strip())
    if top.resolve(strict=True) != source:
        raise ReleaseWorkflowError("source_root 必须恰为本地 Git 仓库根")
    raw = _git(source, "ls-tree", "-rz", "--name-only", commit)
    paths = {
        item.decode("utf-8", errors="strict")
        for item in raw.split(b"\0")
        if item
    }
    if not paths:
        raise ReleaseWorkflowError("固定 commit 没有可用的 tracked 文件")
    return paths


def _release_inputs(
    source: Path, commit: str, tree_paths: set[str]
) -> tuple[ReleaseInput, ...]:
    fixed_sources = {
        "LICENSE",
        "NOTICE",
        "README.md",
        "THIRD_PARTY_NOTICES",
        "config/default.toml",
        "config/local.example.toml",
        "environment.yml",
        "examples/questions/net-force-concept-draft.yaml",
        "pyproject.toml",
    }
    fixed_sources.update(f"schemas/{name}" for name in SCHEMA_NAMES)
    fixed_sources.update(f"docs/student/{name}" for name in STUDENT_DOCUMENTS)
    missing = sorted(fixed_sources - tree_paths)
    if missing:
        raise ReleaseWorkflowError(
            "固定 commit 缺少正式发布输入：" + ", ".join(missing)
        )

    package_sources = sorted(
        path
        for path in tree_paths
        if path.startswith("src/physics_agent/")
        and (path.endswith(".py") or path.startswith("src/physics_agent/resources/"))
    )
    if not package_sources:
        raise ReleaseWorkflowError("固定 commit 缺少 physics_agent 源码")
    manifest_path = f"{REVIEWED_ROOT}/manifest.json"
    if manifest_path not in tree_paths:
        raise ReleaseWorkflowError("固定 commit 缺少 reviewed 知识 manifest")
    manifest_bytes = _git(source, "cat-file", "blob", f"{commit}:{manifest_path}")
    if hashlib.sha256(manifest_bytes).hexdigest() != REVIEWED_MANIFEST_SHA256:
        raise ReleaseWorkflowError("reviewed 知识 manifest 摘要不匹配")
    try:
        manifest = json.loads(manifest_bytes)
        items = manifest["items"]
        knowledge_sources = {manifest_path}
        for item in items:
            item_path = PurePosixPath(str(item["path"]))
            if item_path.is_absolute() or any(
                part in {"", ".", ".."} for part in item_path.parts
            ):
                raise ValueError
            knowledge_sources.add(f"{REVIEWED_ROOT}/{item_path.as_posix()}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseWorkflowError("reviewed 知识 manifest 结构无效") from exc
    missing_knowledge = sorted(knowledge_sources - tree_paths)
    if missing_knowledge:
        raise ReleaseWorkflowError(
            "固定 commit 缺少 reviewed 知识 item：" + ", ".join(missing_knowledge)
        )

    inputs: list[ReleaseInput] = [
        ReleaseInput("LICENSE", "LICENSE", "license"),
        ReleaseInput("NOTICE", "NOTICE", "notice"),
        ReleaseInput("THIRD_PARTY_NOTICES", "THIRD_PARTY_NOTICES", "notice"),
        ReleaseInput("LICENSE", "source/LICENSE", "source"),
        ReleaseInput("README.md", "source/README.md", "source"),
        ReleaseInput("pyproject.toml", "source/pyproject.toml", "source"),
        ReleaseInput("environment.yml", "source/environment.yml", "source"),
        ReleaseInput(
            "config/local.example.toml",
            "source/config/local.example.toml",
            "source",
        ),
        ReleaseInput("config/default.toml", "config/default.toml", "config"),
        ReleaseInput("config/local.example.toml", "config/local.example.toml", "config"),
        ReleaseInput(
            "examples/questions/net-force-concept-draft.yaml",
            "examples/questions/net-force-concept-draft.yaml",
            "example",
        ),
    ]
    inputs.extend(
        ReleaseInput(path, f"source/{path}", "source") for path in package_sources
    )
    inputs.extend(
        ReleaseInput(f"schemas/{name}", f"schemas/{name}", "schema")
        for name in SCHEMA_NAMES
    )
    inputs.extend(
        ReleaseInput(f"docs/student/{name}", f"docs/{name}", "docs")
        for name in STUDENT_DOCUMENTS
    )
    inputs.extend(
        ReleaseInput(path, path, "knowledge") for path in sorted(knowledge_sources)
    )
    members = [item.member for item in inputs]
    if len(members) != len(set(members)):
        raise ReleaseWorkflowError("自动生成的 ReleaseInput 包含重复成员")
    return tuple(sorted(inputs, key=lambda item: item.member))


def _require_clean_payload(source: Path, commit: str, paths: list[str]) -> None:
    status = _git(
        source,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--",
        *paths,
    )
    if status:
        raise ReleaseWorkflowError("正式发布 payload 存在 staged 或 unstaged 差异")
    diff = _run_git(
        source,
        "diff",
        "--quiet",
        commit,
        "--",
        *paths,
        check=False,
    )
    if diff.returncode == 1:
        raise ReleaseWorkflowError("当前 payload 字节不属于固定 source_commit")
    if diff.returncode != 0:
        raise ReleaseWorkflowError("Git 无法校验正式发布 payload")


def _commit_time(source: Path, commit: str) -> int:
    raw = _git(source, "show", "-s", "--format=%ct", commit).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReleaseWorkflowError("Git commit 时间无效") from exc
    if value < 0:
        raise ReleaseWorkflowError("Git commit 时间不得为负")
    return value


def _prepare_dist(source: Path) -> tuple[Path, bool]:
    dist = source / "dist"
    if dist.exists() or dist.is_symlink():
        return _real_directory(dist, "dist"), False
    try:
        dist.mkdir(mode=0o755)
    except OSError as exc:
        raise ReleaseWorkflowError("无法创建本地 dist 目录") from exc
    return dist.resolve(strict=True), True


def _require_missing_outputs(paths: Iterable[Path]) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            raise ReleaseWorkflowError(f"拒绝复用已有正式输出：{path.name}")


def _archive_commit(source: Path, commit: str, destination: Path) -> None:
    archive = _git(source, "archive", "--format=tar", commit)
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            for member in stream.getmembers():
                raw = member.name.rstrip("/") if member.isdir() else member.name
                path = PurePosixPath(raw)
                if (
                    not raw
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or path.as_posix() != raw
                    or raw in seen
                ):
                    raise ReleaseWorkflowError("git archive 包含重复或不安全路径")
                seen.add(raw)
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ReleaseWorkflowError("git archive 包含非普通文件")
                parent = target.parent
                parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = stream.extractfile(member)
                if extracted is None:
                    raise ReleaseWorkflowError("git archive 普通文件无法读取")
                target.write_bytes(extracted.read())
                os.chmod(target, 0o644)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseWorkflowError("无法安全解包固定 commit") from exc


def _build_wheel(source_stage: Path, wheel_directory: Path, epoch: int) -> Path:
    wheel_directory.mkdir(mode=0o755)
    command = (
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-build-isolation",
        "--no-deps",
        "--no-cache-dir",
        "--wheel-dir",
        os.fspath(wheel_directory),
        os.fspath(source_stage),
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "SOURCE_DATE_EPOCH": str(epoch),
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=source_stage.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseWorkflowError("wheel 构建子进程无法启动") from exc
    if completed.returncode != 0:
        raise ReleaseWorkflowError("wheel 构建失败；未保留子进程输出")
    wheels = tuple(wheel_directory.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != WHEEL_NAME:
        raise ReleaseWorkflowError("wheel 构建未生成唯一固定版本产物")
    _regular_bytes(wheels[0], "wheel")
    return wheels[0]


@contextmanager
def _fixed_build_environment(epoch: int) -> Iterator[None]:
    names = ("SOURCE_DATE_EPOCH", "TZ", "LC_ALL")
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(
        {"SOURCE_DATE_EPOCH": str(epoch), "TZ": "UTC", "LC_ALL": "C.UTF-8"}
    )
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _compare_results(first: ReleaseResult, second: ReleaseResult) -> None:
    if _bundle_snapshot(first.bundle_path) != _bundle_snapshot(second.bundle_path):
        raise ReleaseWorkflowError("两次 bundle payload、manifest 或 SHA256SUMS 不一致")
    if _regular_bytes(first.archive_path, "第一份归档") != _regular_bytes(
        second.archive_path, "第二份归档"
    ):
        raise ReleaseWorkflowError("两次 tar.gz 归档不一致")
    if _regular_bytes(first.sidecar_path, "第一份 sidecar") != _regular_bytes(
        second.sidecar_path, "第二份 sidecar"
    ):
        raise ReleaseWorkflowError("两次归档 sidecar 不一致")
    if first.archive_sha256 != second.archive_sha256:
        raise ReleaseWorkflowError("两次归档摘要不一致")


def _bundle_snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    directory = _real_directory(root, "bundle")
    snapshot: list[tuple[str, int, bytes]] = []
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseWorkflowError("bundle 比较遇到符号链接")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseWorkflowError("bundle 比较遇到非普通文件")
        snapshot.append(
            (
                path.relative_to(directory).as_posix(),
                stat.S_IMODE(info.st_mode),
                _regular_bytes(path, "bundle 文件"),
            )
        )
    return tuple(snapshot)


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseWorkflowError(f"{label} 不存在或不可读") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseWorkflowError(f"{label} 必须是普通文件")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseWorkflowError(f"{label} 无法读取") from exc


def _remove_published(paths: list[Path], dist: Path) -> None:
    for path in reversed(paths):
        if path.parent != dist or path.name not in {
            BUNDLE_NAME,
            f"{BUNDLE_NAME}.tar.gz",
            f"{BUNDLE_NAME}.tar.gz.sha256",
        }:
            raise ReleaseWorkflowError("拒绝清理非本次正式输出")
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass


def _remove_work_tree(work: Path, dist: Path) -> None:
    if work.parent != dist or not work.name.startswith(".m5-workflow-"):
        raise ReleaseWorkflowError("拒绝清理非本次 dist 临时目录")
    try:
        info = work.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseWorkflowError("本次 dist 临时路径类型异常，拒绝清理")
    shutil.rmtree(work)


def _run_git(
    source: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    try:
        completed = subprocess.run(
            ("git", "-C", os.fspath(source), *arguments),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseWorkflowError("Git 本地校验无法启动") from exc
    if check and completed.returncode != 0:
        raise ReleaseWorkflowError("固定 Git commit 或 tracked payload 校验失败")
    return completed


def _git(source: Path, *arguments: str) -> bytes:
    return _run_git(source, *arguments).stdout
