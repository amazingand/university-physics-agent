"""M5 学生发布包的确定性、本地且故障关闭的构建器。"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import configparser
import csv
from email.parser import BytesParser
import gzip
import hashlib
import io
from importlib import metadata as importlib_metadata
import json
import math
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
import tomllib
from typing import Iterable
from urllib.parse import urlsplit
import zipfile

import yaml


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_WHEEL_MEMBER_BYTES = 64 * 1024 * 1024
MAX_WHEEL_TOTAL_BYTES = 256 * 1024 * 1024

_BUNDLE_NAME = re.compile(
    r"^university-physics-agent-[0-9]+\.[0-9]+\.[0-9]+-wsl-linux$"
)
_HEX_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CATEGORY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_FORBIDDEN_COMPONENTS = {
    ".codex",
    ".env",
    ".git",
    ".pytest_cache",
    ".vscode",
    "__pycache__",
    "cache",
    "work",
}
_FORBIDDEN_SUFFIXES = {
    ".db",
    ".pdf",
    ".ppt",
    ".pptx",
    ".sh",
    ".sqlite",
    ".sqlite3",
}
_PLACEHOLDER_HOLDERS = {
    "accurate name or organization",
    "copyright holder",
    "real name or full organization name",
    "真实姓名或组织全称",
    "准确姓名或组织名称",
}
_REQUIRED_SCHEMA_NAMES = {
    "knowledge-package.schema.json",
    "learning-package-1.1.schema.json",
    "learning-package.schema.json",
    "question.schema.json",
}
_RESERVED_MEMBERS = {"release-manifest.json", "SHA256SUMS"}
_REQUIRED_EXACT_MEMBERS = {
    "config/default.toml",
    "config/local.example.toml",
    "docs/PRIVACY-LIMITS.md",
    "docs/QUICKSTART.md",
    "docs/REBUILD.md",
    "docs/TROUBLESHOOTING.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES",
    "source/LICENSE",
    "source/README.md",
    "source/config/local.example.toml",
    "source/environment.yml",
    "source/pyproject.toml",
    "source/src/physics_agent/resources/config/default.toml",
    "examples/questions/net-force-concept-draft.yaml",
    "knowledge/mechanics-zh-reviewed-0.2.0/manifest.json",
}
_SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    re.compile(rb"(?:/home/|/Users/)[A-Za-z0-9._-]+/"),
    re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"),
)
_AUTHORIZATION_VALUE = re.compile(
    rb"(?im)^\s*authorization\s*:\s*(?:bearer|basic)\s+([^\r\n]+)"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)^\s*(?:export\s+)?[A-Z0-9_]*"
    rb"(?:API_KEY|TOKEN|SECRET(?:_KEY)?|PASSWORD)\s*[:=]\s*([^\r\n]*)"
)
_REVIEWED_MANIFEST_SHA256 = (
    "704a520ce35d859fe4b0a5305d6c4484b7c5a0a10872e32b411cb05a3c986a1b"
)
_DIRECT_DEPENDENCIES = {
    "httpx": "0.28.1",
    "jsonschema": "4.26.0",
    "numpy": "2.4.6",
    "pint": "0.25.3",
    "pyyaml": "6.0.3",
    "sympy": "1.14.0",
}
_ENVIRONMENT_DEPENDENCIES = {
    **_DIRECT_DEPENDENCIES,
    "anyio": "4.15.1",
    "attrs": "26.1.0",
    "certifi": "2026.7.22",
    "flexcache": "0.3",
    "flexparser": "0.4",
    "h11": "0.16.0",
    "httpcore": "1.0.9",
    "idna": "3.19",
    "jsonschema-specifications": "2025.9.1",
    "mpmath": "1.3.0",
    "platformdirs": "4.11.7",
    "referencing": "0.37.0",
    "rpds-py": "2026.6.3",
    "typing-extensions": "4.16.0",
}
_THIRD_PARTY_DISTRIBUTIONS = (
    ("anyio", "4.15.1", "MIT", "runtime"),
    ("attrs", "26.1.0", "MIT", "runtime"),
    ("certifi", "2026.7.22", "MPL-2.0", "runtime"),
    ("flexcache", "0.3", "BSD", "runtime"),
    ("flexparser", "0.4", "BSD-3-Clause", "runtime"),
    ("h11", "0.16.0", "MIT", "runtime"),
    ("httpcore", "1.0.9", "BSD-3-Clause", "runtime"),
    ("httpx", "0.28.1", "BSD-3-Clause", "runtime"),
    ("idna", "3.19", "BSD-3-Clause", "runtime"),
    ("jsonschema", "4.26.0", "MIT", "runtime"),
    ("jsonschema-specifications", "2025.9.1", "MIT", "runtime"),
    ("mpmath", "1.3.0", "BSD", "runtime"),
    ("numpy", "2.4.6", "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "runtime"),
    ("packaging", "26.3", "Apache-2.0 OR BSD-2-Clause", "build"),
    ("Pint", "0.25.3", "BSD", "runtime"),
    ("pip", "26.2.1", "MIT", "build"),
    ("platformdirs", "4.11.7", "MIT", "runtime"),
    ("PyYAML", "6.0.3", "MIT", "runtime"),
    ("referencing", "0.37.0", "MIT", "runtime"),
    ("rpds-py", "2026.6.3", "MIT", "runtime"),
    ("sympy", "1.14.0", "BSD", "runtime"),
    ("typing_extensions", "4.16.0", "PSF-2.0", "runtime"),
    ("wheel", "0.48.0", "MIT", "build"),
)
_DISTRIBUTION_ATTACHMENT_CONTRACT_SHA256 = {
    "anyio": "4bc741516e4ec6a4f890c3a5cc459b18fdf80f9a24a5481ffd7b81c1bd97e962",
    "attrs": "74426eb67a834af1630136f9f5888b3b7172933f856bc5fa7032169a6aeab17d",
    "certifi": "f7813e0337d787c42a1a3af6a9af9520dd69375e999145d1dbcbb7a3483d44d8",
    "flexcache": "0cb6c89ffd100fc750bfae6dbb1ef57a7ab1dff4be2e0ae8eadde1ada316d58d",
    "flexparser": "bf6d96a3d89d7d0f1d13cd79bf9b19514de53a318bf69c90b0812fd26d73ae05",
    "h11": "77c551c48082ffaf23af4273a844443732629d1f61783695c9533e464d97b839",
    "httpcore": "0d1c012f9e8e810bdf4c1786b510ccd06d8a7847d23bd0fc5c70955c86d694ef",
    "httpx": "89f85d7c43d89fb8a7035e7bcf5e0e9ad76d181aa485cabd26638d85cea01796",
    "idna": "f8f5454e8a75bd9161a18d71dc315683f34b4420e48b72fc56e85956b76e353b",
    "jsonschema": "83e70031e8237220069ace150df0c7434de903a20857b0ec58d8643055df8ad0",
    "jsonschema-specifications": "fcd438fa5aa61bd580c25cf74a2dc46a183d2f6f22f2556663fa9f4a614c684b",
    "mpmath": "092710b75b38826b56afef0203993210086b2d7d4e992b5fec2fd64ba65ff48d",
    "numpy": "5ae7ac4db661d6a8194d72cb10febac0a17da432a907b1235980945805928adf",
    "packaging": "eb6aa51e1596ff568825896e546770d3ba73f0e26ad51f10d0c35256e75d627c",
    "Pint": "eb90e3cb8c73b0230918d63eb93e2e7a85f5b93449d520dc1b99b03e0077a51d",
    "pip": "0f6c7e7c1539f08d952ed675809f06ebd1c8ec1d0ee14ed543e421412d943958",
    "platformdirs": "402e52ddf527ccc67aa258565760e33651dd0132d72d7bb5dc60b9642c3e43eb",
    "PyYAML": "390c4fe5c1eb9871a8d7a65b7458c934e2729c1aeaaf2435ee508b5f1e163f5c",
    "referencing": "d055606fe4902ce0eae7ce2d8f016dd84f86fd8d69c6ca3bb5ebf309b7b7ecb1",
    "rpds-py": "c813d586bcaef4e408cd38806550ac44ad9e689bfa1c4ec0e7b9da9b5a567a27",
    "sympy": "0b99f91486b9eb222198e847407242700239b22309103b5b6ae7934ec8eafe4a",
    "typing_extensions": "b512123ba264965b0419063727b8b32d4ae05d5b68e36087c99be6525c73ba43",
    "wheel": "a0edd829fafc1aaf80de3a5bd8d4654412668040a4d44dad02622e8ad159173e",
}
_CONDA_LICENSE_PACKAGES = {
    "Python": {
        "version": "3.12.14",
        "build": "h5f976f7_3_cpython",
        "package_sha256": "14c579b1016da04e4c9f1c5c857272d83ec447317d8c4074a07d59de3cef70ef",
        "license_expression": "Python-2.0",
        "license_sha256": "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf",
        "role": "build",
    },
    "setuptools": {
        "version": "84.0.0",
        "build": "pyh332efcf_0",
        "package_sha256": "9e200ee5f9ff19a4d94e4b51c4856d53dec849f91032f345cf0c6bc3d51a7183",
        "license_expression": "MIT",
        "license_sha256": "86da0f01aeae46348a3c3d465195dc1ceccde79f79e87769a64b8da04b2a4741",
        "role": "build",
    },
}
_KNOWN_CREDENTIAL_PATTERNS = (
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bxox[bpaors]-[A-Za-z0-9-]{10,255}\b"),
    re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(rb"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,255}\b"),
    re.compile(
        rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        rb"[A-Za-z0-9_-]{8,}\b"
    ),
)
_HIGH_ENTROPY_CANDIDATE = re.compile(rb"[A-Za-z0-9_+=-]{32,255}")
_PYTHON_KEYWORD_ASSIGNMENT = re.compile(
    rb"[A-Za-z_][A-Za-z0-9_]*=[A-Za-z_][A-Za-z0-9_]*"
)
_HTTP_URL = re.compile(rb"https?://[^\s'\"<>]+")
_DOCUMENTED_VARIABLES = {
    b"DEEPSEEK_API_KEY",
    b"PHYSICS_AGENT_CHAT_API_KEY",
}


class ReleaseBuildError(RuntimeError):
    """发布输入或构建过程不符合 M5 安全契约。"""


@dataclass(frozen=True)
class ReleaseInput:
    """一个显式允许的源文件及其学生包内位置。"""

    source: str
    member: str
    category: str


@dataclass(frozen=True)
class ReleaseMetadata:
    """写入发布 manifest 的固定构建身份。"""

    version: str
    source_commit: str
    knowledge_manifest_sha256: str
    source_date_epoch: int
    python_version: str
    pip_version: str
    setuptools_version: str
    wheel_version: str
    license_status: str
    license_holder: str


@dataclass(frozen=True)
class ReleaseResult:
    bundle_path: Path
    archive_path: Path
    sidecar_path: Path
    archive_sha256: str


@dataclass(frozen=True)
class _ValidatedInput:
    source: Path
    member: PurePosixPath
    category: str
    content: bytes


def render_mit_license(holder: str) -> bytes:
    """为已确认主体返回固定、完整的 MIT 许可证正文，不写入文件。"""

    normalized = holder.strip()
    if (
        not normalized
        or normalized.casefold() in _PLACEHOLDER_HOLDERS
        or any(character in normalized for character in "\r\n\x00")
    ):
        raise ReleaseBuildError("MIT 版权主体为空或仍是占位符")
    return (
        "MIT License\n\n"
        f"Copyright (c) 2026 {normalized}\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        'of this software and associated documentation files (the "Software"), to deal\n'
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in all\n"
        "copies or substantial portions of the Software.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
        "SOFTWARE.\n"
    ).encode("utf-8")


def render_release_notice(holder: str) -> bytes:
    """返回固定许可范围、OSTP 署名和人工审核说明。"""

    normalized = holder.strip()
    render_mit_license(normalized)
    return (
        "University Physics Agent NOTICE\n\n"
        "Original program code\n"
        f"Copyright (c) 2026 {normalized}\n"
        "Source repository scope: src/physics_agent/**/*.py, "
        "src/physics_agent/resources/**, .gitignore, README.md, pyproject.toml, "
        "environment.yml, config/**, schemas/**, and docs/student/**.\n"
        "Student bundle scope: the corresponding source/**, config/**, "
        "schemas/**, and docs/** paths, plus the project-authored "
        "program/resource portion of artifacts/*.whl.\n"
        "License: MIT; see LICENSE.\n\n"
        "Reviewed knowledge package and derived student example\n"
        "Scope: knowledge/mechanics-zh-reviewed-0.2.0/** and "
        "examples/questions/net-force-concept-draft.yaml\n"
        "License: CC BY-SA 4.0\n"
        "Work: Introductory Physics: Building Models to Describe Our World\n"
        "Authors: Ryan D. Martin, Emma Neary, Joshua Rinaldo, Olivia Woodman\n"
        "Source commit: dc3ef263f5e00aea467447e08a94f23f573dae31\n"
        "Source: https://github.com/OSTP/PhysicsArtofModelling/blob/"
        "dc3ef263f5e00aea467447e08a94f23f573dae31/tex/NewtonsLaws.tex\n"
        "License URL: https://creativecommons.org/licenses/by-sa/4.0/\n"
        "Change note: 中文重新组织与改编；未复制图片、原例题或答案；"
        "M4 固定审核包已经用户人工审核。\n\n"
        "Third-party software\n"
        "Scope: environment.yml 中的运行依赖及 Python、pip、setuptools、wheel、"
        "packaging 构建工具。\n"
        "These components retain their own licenses; see THIRD_PARTY_NOTICES.\n\n"
        "Generated integrity metadata\n"
        "Scope: PUBLIC-SOURCE.json, release-manifest.json, SHA256SUMS, the "
        "external archive sidecar, and archive container metadata.\n"
        "These factual names, sizes, modes, and cryptographic digests do not "
        "extend any license scope above.\n"
    ).encode("utf-8")


def render_third_party_notices() -> bytes:
    """从固定环境收集受版本和摘要约束的完整第三方许可附件。"""

    packages = [
        _distribution_notice(name, version, expression, role)
        for name, version, expression, role in _THIRD_PARTY_DISTRIBUTIONS
    ]
    packages.extend(
        _conda_license_notice(name, contract)
        for name, contract in _CONDA_LICENSE_PACKAGES.items()
    )
    packages.sort(key=lambda item: str(item["name"]).casefold())
    document = {
        "format": "university-physics-agent-third-party-notices",
        "format_version": 1,
        "policy": (
            "Packages are not relicensed by this project. Each attachment below is "
            "the complete UTF-8 text collected from the fixed local environment."
        ),
        "packages": packages,
    }
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _distribution_notice(
    name: str, version: str, expected_expression: str, role: str
) -> dict[str, object]:
    try:
        distribution = importlib_metadata.distribution(name)
    except importlib_metadata.PackageNotFoundError as exc:
        raise ReleaseBuildError(f"固定第三方 distribution 缺失：{name}") from exc
    actual_name = distribution.metadata.get("Name")
    if _normalized_name(actual_name or "") != _normalized_name(name):
        raise ReleaseBuildError(f"第三方 distribution 名称漂移：{name}")
    if distribution.version != version:
        raise ReleaseBuildError(f"第三方 distribution 版本漂移：{name}")
    expression = (
        distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or ""
    ).strip()
    if expression != expected_expression:
        raise ReleaseBuildError(f"第三方 distribution 许可标识漂移：{name}")
    files = tuple(distribution.files or ())
    metadata_roots = {
        PurePosixPath(os.fspath(value)).parts[0]
        for value in files
        if PurePosixPath(os.fspath(value)).parts
        and PurePosixPath(os.fspath(value)).parts[0].endswith(
            (".dist-info", ".egg-info")
        )
    }
    if len(metadata_roots) != 1:
        raise ReleaseBuildError(f"第三方 distribution 元数据根不唯一：{name}")
    metadata_root = next(iter(metadata_roots))
    declared = distribution.metadata.get_all("License-File", [])
    selected: list[object] = []
    if declared:
        for declared_name in declared:
            normalized = PurePosixPath(declared_name).as_posix()
            matches = [
                value
                for value in files
                if _declared_license_matches(
                    PurePosixPath(os.fspath(value)), metadata_root, normalized
                )
            ]
            if len(matches) != 1:
                raise ReleaseBuildError(
                    f"第三方 License-File 无唯一实际附件：{name}/{declared_name}"
                )
            selected.append(matches[0])
    else:
        for value in files:
            relative = PurePosixPath(os.fspath(value))
            if not relative.parts or relative.parts[0] != metadata_root:
                continue
            basename = relative.name
            if re.match(r"(?i)^(LICENSE|COPYING|AUTHORS|NOTICE)", basename) and not (
                basename.casefold().endswith((".py", ".pyc"))
            ):
                selected.append(value)
    unique = sorted({os.fspath(value): value for value in selected}.values(), key=os.fspath)
    if not unique:
        raise ReleaseBuildError(f"第三方 distribution 没有可审计许可附件：{name}")
    attachments = [
        _license_attachment(
            distribution.locate_file(value),
            PurePosixPath(os.fspath(value)).as_posix(),
            required_root=Path(sys.prefix).resolve(strict=True),
        )
        for value in unique
    ]
    attachment_contract = hashlib.sha256(
        json.dumps(
            attachments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if attachment_contract != _DISTRIBUTION_ATTACHMENT_CONTRACT_SHA256.get(name):
        raise ReleaseBuildError(
            f"第三方 distribution 许可附件路径、摘要、大小或正文漂移：{name}"
        )
    return {
        "attachments": attachments,
        "license_expression": expression,
        "name": actual_name,
        "role": role,
        "version": version,
    }


def _declared_license_matches(
    path: PurePosixPath, metadata_root: str, declared: str
) -> bool:
    if not path.parts or path.parts[0] != metadata_root:
        return False
    relative = PurePosixPath(*path.parts[1:]).as_posix()
    return relative in {declared, f"licenses/{declared}"}


def _license_attachment(
    path_value: str | Path,
    reported_path: str,
    *,
    required_root: Path,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    path = Path(path_value)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("第三方许可附件不存在或不可访问") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseBuildError("第三方许可附件不是普通文件")
    if not resolved.is_relative_to(required_root):
        raise ReleaseBuildError("第三方许可附件越出固定环境或 Conda 包目录")
    content = resolved.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("第三方许可附件不是完整 UTF-8 文本") from exc
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ReleaseBuildError("第三方许可附件摘要漂移")
    return {
        "path": reported_path,
        "sha256": digest,
        "size": len(content),
        "text": text,
    }


def _conda_license_notice(
    name: str, contract: dict[str, str]
) -> dict[str, object]:
    prefix = Path(sys.prefix).resolve(strict=True)
    records = sorted(
        (prefix / "conda-meta").glob(f"{name.casefold()}-{contract['version']}-*.json")
    )
    if len(records) != 1:
        raise ReleaseBuildError(f"Conda 固定包记录不唯一：{name}")
    try:
        record = json.loads(records[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"Conda 固定包记录无法读取：{name}") from exc
    expected = {
        "name": name.casefold(),
        "version": contract["version"],
        "build": contract["build"],
        "sha256": contract["package_sha256"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ReleaseBuildError(f"Conda 固定包身份漂移：{name}")
    extracted = Path(record.get("extracted_package_dir", ""))
    try:
        extracted_root = extracted.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError(f"Conda 固定包解压目录缺失：{name}") from exc
    license_path = extracted_root / "info/licenses/LICENSE"
    attachment = _license_attachment(
        license_path,
        "conda-package/info/licenses/LICENSE",
        required_root=extracted_root,
        expected_sha256=contract["license_sha256"],
    )
    return {
        "attachments": [attachment],
        "conda_build": contract["build"],
        "conda_package_sha256": contract["package_sha256"],
        "license_expression": contract["license_expression"],
        "name": name,
        "role": contract["role"],
        "version": contract["version"],
    }


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def build_release_bundle(
    *,
    source_root: str | Path,
    dist_root: str | Path,
    bundle_name: str,
    inputs: Iterable[ReleaseInput],
    metadata: ReleaseMetadata,
) -> ReleaseResult:
    """从显式 allowlist 构建目录、规范化 tar.gz 和外部摘要。

    该入口代表正式 bundle 构建，因此许可证状态或版权主体未确认时，
    在创建 ``dist_root`` 或任何临时文件之前直接拒绝。
    """

    _validate_metadata(bundle_name, metadata)
    source = _existing_real_directory(source_root, "source_root")
    validated = _validate_inputs(source, tuple(inputs))
    _validate_required_members(validated, metadata)
    _validate_git_binding(source, validated, metadata)

    dist = _prepare_dist_root(dist_root, source)
    bundle_path = dist / bundle_name
    archive_path = dist / f"{bundle_name}.tar.gz"
    sidecar_path = dist / f"{bundle_name}.tar.gz.sha256"
    for output in (bundle_path, archive_path, sidecar_path):
        if output.exists() or output.is_symlink():
            raise ReleaseBuildError(f"拒绝复用已有输出：{output.name}")

    temporary_root = Path(tempfile.mkdtemp(prefix=".m5-release-", dir=dist))
    staging_bundle = temporary_root / bundle_name
    temporary_archive = temporary_root / archive_path.name
    temporary_sidecar = temporary_root / sidecar_path.name
    cleanup_candidates = [archive_path, sidecar_path, bundle_path]
    try:
        staging_bundle.mkdir(mode=0o755)
        manifest_entries = _write_payload(staging_bundle, validated)
        _write_metadata_files(staging_bundle, manifest_entries, metadata)
        _normalize_staging(staging_bundle)
        _write_archive(
            staging_bundle,
            temporary_archive,
            metadata.source_date_epoch,
        )
        archive_sha256 = _file_sha256(temporary_archive)
        temporary_sidecar.write_text(
            f"{archive_sha256}  {archive_path.name}\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(temporary_sidecar, 0o644)

        os.replace(temporary_archive, archive_path)
        os.replace(temporary_sidecar, sidecar_path)
        os.replace(staging_bundle, bundle_path)
    except BaseException as exc:
        _remove_published_outputs(dist, cleanup_candidates)
        if isinstance(exc, ReleaseBuildError):
            raise
        if isinstance(exc, Exception):
            raise ReleaseBuildError("学生发布包构建失败") from exc
        raise
    finally:
        _remove_temporary_root(dist, temporary_root)

    return ReleaseResult(bundle_path, archive_path, sidecar_path, archive_sha256)


def _validate_metadata(bundle_name: str, metadata: ReleaseMetadata) -> None:
    if metadata.license_status != "confirmed":
        raise ReleaseBuildError("license_status 不是 confirmed，禁止生成正式 bundle")
    holder = metadata.license_holder.strip()
    if (
        not holder
        or holder.casefold() in _PLACEHOLDER_HOLDERS
        or any(character in holder for character in "\r\n\x00")
    ):
        raise ReleaseBuildError("MIT 版权主体仍为空或占位符，禁止生成正式 bundle")
    expected_name = f"university-physics-agent-{metadata.version}-wsl-linux"
    if (
        metadata.version != "0.5.0"
        or not _BUNDLE_NAME.fullmatch(bundle_name)
        or bundle_name != expected_name
    ):
        raise ReleaseBuildError("bundle 名称与固定项目名或版本不一致")
    if not _HEX_COMMIT.fullmatch(metadata.source_commit):
        raise ReleaseBuildError("source_commit 必须是 40 位小写 Git SHA")
    if not _SHA256.fullmatch(metadata.knowledge_manifest_sha256):
        raise ReleaseBuildError("knowledge manifest 摘要格式无效")
    if metadata.knowledge_manifest_sha256 != _REVIEWED_MANIFEST_SHA256:
        raise ReleaseBuildError("knowledge manifest 摘要不是固定 reviewed 版本")
    if (
        not isinstance(metadata.source_date_epoch, int)
        or isinstance(metadata.source_date_epoch, bool)
        or metadata.source_date_epoch < 0
    ):
        raise ReleaseBuildError("SOURCE_DATE_EPOCH 必须是非负整数")
    versions = (
        metadata.python_version,
        metadata.pip_version,
        metadata.setuptools_version,
        metadata.wheel_version,
    )
    if any(not value.strip() or "\n" in value or "\r" in value for value in versions):
        raise ReleaseBuildError("构建工具版本必须是非空单行文本")
    actual_versions = (
        platform.python_version(),
        importlib_metadata.version("pip"),
        importlib_metadata.version("setuptools"),
        importlib_metadata.version("wheel"),
    )
    if versions != actual_versions:
        raise ReleaseBuildError("manifest 构建工具版本与当前执行环境不一致")
    if os.environ.get("TZ") != "UTC" or os.environ.get("LC_ALL") != "C.UTF-8":
        raise ReleaseBuildError("正式构建要求 TZ=UTC 且 LC_ALL=C.UTF-8")


def _existing_real_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseBuildError(f"{label} 不存在或不可访问") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseBuildError(f"{label} 必须是真实目录")
    return path.resolve(strict=True)


def _prepare_dist_root(value: str | Path, source_root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    allowed_root = source_root / "dist"
    if path != allowed_root and not path.is_relative_to(allowed_root):
        raise ReleaseBuildError("dist_root 只允许位于 source_root/dist 内")
    current = source_root
    for part in path.relative_to(source_root).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                info = current.lstat()
            except OSError as exc:
                raise ReleaseBuildError("dist_root 路径不可访问") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseBuildError("dist_root 路径必须只包含真实目录")
    if path.exists() or path.is_symlink():
        return _existing_real_directory(path, "dist_root")
    try:
        path.mkdir(mode=0o755, parents=True)
    except OSError as exc:
        raise ReleaseBuildError("无法创建 dist_root") from exc
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(allowed_root):
        raise ReleaseBuildError("dist_root 创建位置异常")
    return resolved


def _validate_inputs(
    source_root: Path, inputs: tuple[ReleaseInput, ...]
) -> tuple[_ValidatedInput, ...]:
    if not inputs:
        raise ReleaseBuildError("发布 allowlist 不得为空")
    members: set[str] = set()
    validated: list[_ValidatedInput] = []
    for item in inputs:
        source_name = _safe_relative_path(item.source, "源路径")
        member = _safe_relative_path(item.member, "归档成员")
        source_key = source_name.as_posix()
        member_key = member.as_posix()
        if member_key in members:
            raise ReleaseBuildError(f"allowlist 包含重复归档成员：{member_key}")
        if not _SAFE_CATEGORY.fullmatch(item.category):
            raise ReleaseBuildError(f"发布分类无效：{item.category!r}")
        _reject_sensitive_path(source_name)
        _reject_sensitive_path(member)
        if member_key in _RESERVED_MEMBERS:
            raise ReleaseBuildError(f"allowlist 不得占用构建器元数据成员：{member_key}")
        expected_category = _category_for_member(member)
        if item.category != expected_category:
            raise ReleaseBuildError(
                f"发布分类与成员位置不一致：{member_key} 应为 {expected_category}"
            )
        if member.parts[0] == "source" and source_key != PurePosixPath(
            *member.parts[1:]
        ).as_posix():
            raise ReleaseBuildError(f"source/ 成员未保持仓库相对路径：{member_key}")
        candidate = source_root.joinpath(*source_name.parts)
        content = _read_regular_file(source_root, candidate)
        if member.suffix == ".whl":
            _scan_wheel(member, content)
        else:
            _scan_content(member, content)
        members.add(member_key)
        validated.append(_ValidatedInput(candidate, member, item.category, content))
    for member_key in members:
        parts = PurePosixPath(member_key).parts
        if any(
            PurePosixPath(*parts[:index]).as_posix() in members
            for index in range(1, len(parts))
        ):
            raise ReleaseBuildError(f"归档成员存在文件/目录前缀冲突：{member_key}")
    return tuple(sorted(validated, key=lambda value: value.member.as_posix()))


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReleaseBuildError(f"{label}必须是非空 POSIX 相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseBuildError(f"{label}包含绝对路径、空段或路径穿越：{value!r}")
    if path.as_posix() != value:
        raise ReleaseBuildError(f"{label}不是规范 POSIX 路径：{value!r}")
    return path


def _reject_sensitive_path(path: PurePosixPath) -> None:
    lowered = tuple(part.casefold() for part in path.parts)
    if any(part in _FORBIDDEN_COMPONENTS for part in lowered):
        raise ReleaseBuildError(f"发布路径命中隐私 denylist：{path.as_posix()}")
    if path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        raise ReleaseBuildError(f"发布路径包含禁止文件类型：{path.as_posix()}")


def _category_for_member(member: PurePosixPath) -> str:
    name = member.as_posix()
    exact = {
        "LICENSE": "license",
        "NOTICE": "notice",
        "THIRD_PARTY_NOTICES": "notice",
    }
    if name in exact:
        return exact[name]
    roots = {
        "artifacts": "artifact",
        "config": "config",
        "docs": "docs",
        "examples": "example",
        "knowledge": "knowledge",
        "schemas": "schema",
        "source": "source",
    }
    category = roots.get(member.parts[0])
    if category is None:
        raise ReleaseBuildError(f"归档成员不在候选布局内：{name}")
    return category


def _read_regular_file(source_root: Path, candidate: Path) -> bytes:
    current = source_root
    relative = candidate.relative_to(source_root)
    try:
        for part in relative.parts[:-1]:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseBuildError("源路径父目录不是普通目录或包含符号链接")
        info = candidate.lstat()
    except OSError as exc:
        raise ReleaseBuildError(f"allowlist 源文件不存在：{relative.as_posix()}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseBuildError(f"allowlist 源不是普通文件：{relative.as_posix()}")
    if info.st_size > MAX_INPUT_BYTES:
        raise ReleaseBuildError(f"allowlist 源文件过大：{relative.as_posix()}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ReleaseBuildError("打开后的 allowlist 源不是普通文件")
            if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise ReleaseBuildError("allowlist 源在校验期间发生变化")
            content = stream.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise ReleaseBuildError(f"无法安全读取 allowlist 源：{relative.as_posix()}") from exc
    if len(content) > MAX_INPUT_BYTES:
        raise ReleaseBuildError(f"allowlist 源文件过大：{relative.as_posix()}")
    try:
        current_info = os.stat(candidate, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseBuildError("allowlist 源在读取后不可访问") from exc
    if (
        _metadata_identity(before) != _metadata_identity(after)
        or _metadata_identity(before) != _metadata_identity(current_info)
        or len(content) != before.st_size
    ):
        raise ReleaseBuildError("allowlist 源在读取期间发生变化")
    return content


def _metadata_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_uid,
        value.st_mode,
    )


def _scan_content(member: PurePosixPath, content: bytes) -> None:
    if any(pattern.search(content) for pattern in _KNOWN_CREDENTIAL_PATTERNS):
        raise ReleaseBuildError(
            f"发布内容命中已知凭据格式：{member.as_posix()}"
        )
    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            raise ReleaseBuildError(
                f"发布内容命中凭据或个人绝对路径扫描：{member.as_posix()}"
            )
    for pattern in (_AUTHORIZATION_VALUE, _CREDENTIAL_ASSIGNMENT):
        for match in pattern.finditer(content):
            if (
                pattern is _CREDENTIAL_ASSIGNMENT
                and member.suffix == ".py"
                and re.match(
                    rb"[A-Za-z_][A-Za-z0-9_.]*(?:\(|$)",
                    match.group(1).strip(),
                )
            ):
                continue
            if not _is_placeholder_value(match.group(1)):
                raise ReleaseBuildError(
                    f"发布内容命中真实凭据赋值：{member.as_posix()}"
                )
    for match in _HIGH_ENTROPY_CANDIDATE.finditer(content):
        candidate = match.group(0)
        if _is_safe_high_entropy_context(
            member, content, match.start(), match.end(), candidate
        ):
            continue
        if _shannon_entropy(candidate) >= 4.3 and _character_class_count(candidate) >= 2:
            raise ReleaseBuildError(
                f"发布内容命中通用高熵 token：{member.as_posix()}"
            )


def _is_placeholder_value(value: bytes) -> bool:
    normalized = value.strip().strip(b"'\"").strip()
    if not normalized:
        return True
    if normalized.startswith(b"<") and normalized.endswith(b">"):
        return True
    if re.fullmatch(rb"\$[A-Za-z_][A-Za-z0-9_]*", normalized):
        return True
    if re.fullmatch(rb"\$\{[A-Za-z_][A-Za-z0-9_]*\}", normalized):
        return True
    return False


def _is_safe_high_entropy_context(
    member: PurePosixPath,
    content: bytes,
    start: int,
    end: int,
    candidate: bytes,
) -> bool:
    if re.fullmatch(rb"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", candidate):
        return True
    if (
        candidate.startswith(b"sha256=")
        and member.name == "RECORD"
        and any(part.endswith(".dist-info") for part in member.parts[:-1])
    ):
        return True
    if candidate in _DOCUMENTED_VARIABLES:
        return True
    if member.suffix == ".py" and _PYTHON_KEYWORD_ASSIGNMENT.fullmatch(candidate):
        return True
    before = content[start - 1 : start] if start else b""
    after = content[end : end + 1]
    if before == b"<" and after == b">":
        return True
    for url_match in _HTTP_URL.finditer(content):
        if not (url_match.start() <= start and end <= url_match.end()):
            continue
        url = url_match.group(0)
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {b"http", b"https"} or not parsed.hostname:
            continue
        relative_start = start - url_match.start()
        relative_end = end - url_match.start()
        query = url.find(b"?")
        fragment = url.find(b"#")
        cutoffs = [value for value in (query, fragment) if value >= 0]
        allowed_end = min(cutoffs) if cutoffs else len(url)
        if 0 <= relative_start and relative_end <= allowed_end:
            return True
    return False


def _shannon_entropy(value: bytes) -> float:
    counts = {byte: value.count(byte) for byte in set(value)}
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _character_class_count(value: bytes) -> int:
    return sum(
        (
            any(97 <= byte <= 122 for byte in value),
            any(65 <= byte <= 90 for byte in value),
            any(48 <= byte <= 57 for byte in value),
            any(byte in b"_+/=-" for byte in value),
        )
    )


def _scan_wheel(member: PurePosixPath, content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names: set[str] = set()
            total = 0
            has_record = False
            for info in archive.infolist():
                raw_name = info.filename[:-1] if info.is_dir() else info.filename
                wheel_member = _safe_relative_path(raw_name, "wheel 成员")
                name = wheel_member.as_posix()
                if name in names:
                    raise ReleaseBuildError(f"wheel 包含重复成员：{name}")
                names.add(name)
                _reject_sensitive_path(wheel_member)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or info.is_dir():
                    if stat.S_ISLNK(mode):
                        raise ReleaseBuildError(f"wheel 包含符号链接：{name}")
                    continue
                if file_type not in {0, stat.S_IFREG}:
                    raise ReleaseBuildError(f"wheel 包含非普通成员：{name}")
                if info.file_size > MAX_WHEEL_MEMBER_BYTES:
                    raise ReleaseBuildError(f"wheel 成员过大：{name}")
                total += info.file_size
                if total > MAX_WHEEL_TOTAL_BYTES:
                    raise ReleaseBuildError("wheel 解压总大小超过限制")
                data = archive.read(info)
                _scan_content(wheel_member, data)
                if name.endswith(".dist-info/RECORD"):
                    has_record = True
            if not has_record:
                raise ReleaseBuildError(f"wheel 缺少 RECORD：{member.as_posix()}")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseBuildError(f"wheel 不是可安全检查的 ZIP：{member.as_posix()}") from exc


def _validate_required_members(
    inputs: tuple[_ValidatedInput, ...], metadata: ReleaseMetadata
) -> None:
    names = {item.member.as_posix() for item in inputs}
    missing = sorted(_REQUIRED_EXACT_MEMBERS - names)
    if missing:
        raise ReleaseBuildError(f"allowlist 缺少正式发布成员：{', '.join(missing)}")
    requirements = {
        "wheel": sum(
            name.startswith("artifacts/") and name.endswith(".whl")
            for name in names
        )
        == 1,
        "包内 Schema": any(
            name.startswith("source/src/physics_agent/resources/schemas/")
            and name.endswith(".json")
            for name in names
        ),
        "外部交换 Schema": any(
            name.startswith("schemas/") and name.endswith(".json") for name in names
        ),
        "reviewed 知识 manifest": any(
            name.startswith("knowledge/") and name.endswith("/manifest.json")
            for name in names
        ),
        "学生文档": any(
            name.startswith("docs/") and name.endswith(".md") for name in names
        ),
        "安全配置样例": any(
            name.startswith("config/") and name.endswith(".toml") for name in names
        ),
    }
    absent = [label for label, present in requirements.items() if not present]
    if absent:
        raise ReleaseBuildError(f"allowlist 缺少发布类别：{', '.join(absent)}")

    by_name = {item.member.as_posix(): item.content for item in inputs}
    expected_wheel = (
        f"artifacts/university_physics_agent-{metadata.version}-py3-none-any.whl"
    )
    wheels = {name for name in names if name.endswith(".whl")}
    if wheels != {expected_wheel}:
        raise ReleaseBuildError("项目 wheel 路径、版本或数量不符合固定发布契约")
    if by_name["LICENSE"] != by_name["source/LICENSE"]:
        raise ReleaseBuildError("bundle 与 source 的 MIT LICENSE 不一致")
    if by_name["config/local.example.toml"] != by_name[
        "source/config/local.example.toml"
    ]:
        raise ReleaseBuildError("bundle 与 source 的本地配置样例不一致")
    _validate_project_contract(by_name["source/pyproject.toml"], metadata)
    _validate_environment_contract(by_name["source/environment.yml"])
    if by_name["config/default.toml"] != by_name[
        "source/src/physics_agent/resources/config/default.toml"
    ]:
        raise ReleaseBuildError("顶层默认配置与包内权威资源不一致")
    external_schemas = {
        name.removeprefix("schemas/"): content
        for name, content in by_name.items()
        if name.startswith("schemas/")
    }
    packaged_schemas = {
        name.removeprefix("source/src/physics_agent/resources/schemas/"): content
        for name, content in by_name.items()
        if name.startswith("source/src/physics_agent/resources/schemas/")
    }
    if external_schemas != packaged_schemas:
        raise ReleaseBuildError("顶层交换 Schema 与包内权威资源集合或内容不一致")
    if not _REQUIRED_SCHEMA_NAMES.issubset(external_schemas):
        missing_schemas = sorted(_REQUIRED_SCHEMA_NAMES - external_schemas.keys())
        raise ReleaseBuildError(
            f"allowlist 缺少固定交换 Schema：{', '.join(missing_schemas)}"
        )
    knowledge_manifests = [
        (name, content)
        for name, content in by_name.items()
        if name.startswith("knowledge/") and name.endswith("/manifest.json")
    ]
    if len(knowledge_manifests) != 1:
        raise ReleaseBuildError("正式发布必须且只能包含一个 reviewed 知识 manifest")
    manifest_name, manifest_content = knowledge_manifests[0]
    if hashlib.sha256(manifest_content).hexdigest() != (
        metadata.knowledge_manifest_sha256
    ):
        raise ReleaseBuildError("知识 manifest 内容与固定摘要不一致")
    _validate_knowledge_payload(manifest_name, manifest_content, by_name)
    if by_name["LICENSE"] != render_mit_license(metadata.license_holder):
        raise ReleaseBuildError("LICENSE 不是绑定已确认主体的完整固定 MIT 正文")
    if by_name["NOTICE"] != render_release_notice(metadata.license_holder):
        raise ReleaseBuildError("NOTICE 未完整绑定代码、知识包、OSTP 与第三方范围")
    if by_name["THIRD_PARTY_NOTICES"] != render_third_party_notices():
        raise ReleaseBuildError("THIRD_PARTY_NOTICES 未绑定固定环境完整许可附件")
    _validate_wheel_payload(by_name, expected_wheel, metadata)


def _validate_project_contract(content: bytes, metadata: ReleaseMetadata) -> None:
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseBuildError("source/pyproject.toml 不是有效 UTF-8 TOML") from exc
    if document.get("build-system") != {
        "requires": ["setuptools>=68"],
        "build-backend": "setuptools.build_meta",
    }:
        raise ReleaseBuildError("pyproject build-system 不符合固定构建契约")
    project = document.get("project")
    if not isinstance(project, dict):
        raise ReleaseBuildError("pyproject 缺少 project 表")
    expected_fields = {
        "name": "university-physics-agent",
        "version": metadata.version,
        "readme": "README.md",
        "license": "MIT",
        "license-files": ["LICENSE"],
        "requires-python": ">=3.11",
    }
    if any(project.get(key) != value for key, value in expected_fields.items()):
        raise ReleaseBuildError(
            "pyproject 项目名、版本、README、MIT 许可或 Python 约束漂移"
        )
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or _parse_pinned_dependencies(
        dependencies, "pyproject dependencies"
    ) != _DIRECT_DEPENDENCIES:
        raise ReleaseBuildError("pyproject 六个直接依赖集合漂移")
    if project.get("scripts") != {"physics-agent": "physics_agent.cli:main"}:
        raise ReleaseBuildError("pyproject console script 漂移")

    tool = document.get("tool")
    setuptools_table = tool.get("setuptools") if isinstance(tool, dict) else None
    if not isinstance(setuptools_table, dict):
        raise ReleaseBuildError("pyproject 缺少 setuptools 配置")
    if setuptools_table.get("include-package-data") is not False:
        raise ReleaseBuildError("setuptools include-package-data 必须为 false")
    if setuptools_table.get("packages") != {"find": {"where": ["src"]}}:
        raise ReleaseBuildError("setuptools package discovery 漂移")
    if setuptools_table.get("package-data") != {
        "physics_agent.resources": ["config/default.toml", "schemas/*.json"]
    }:
        raise ReleaseBuildError("setuptools package-data 漂移")


def _validate_environment_contract(content: bytes) -> None:
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ReleaseBuildError("source/environment.yml 不是有效 YAML") from exc
    if not isinstance(document, dict) or set(document) != {
        "name",
        "channels",
        "dependencies",
    }:
        raise ReleaseBuildError("environment.yml 顶层结构漂移")
    if document.get("name") != "physics-agent":
        raise ReleaseBuildError("environment.yml 环境名漂移")
    if document.get("channels") != ["conda-forge"]:
        raise ReleaseBuildError("environment.yml channels 必须仅为 conda-forge")
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) != 3:
        raise ReleaseBuildError("environment.yml Conda 依赖结构漂移")
    if dependencies.count("python=3.12") != 1 or dependencies.count("pip") != 1:
        raise ReleaseBuildError("environment.yml 必须唯一固定 Python 3.12 和 pip")
    pip_tables = [item for item in dependencies if isinstance(item, dict)]
    if len(pip_tables) != 1 or set(pip_tables[0]) != {"pip"}:
        raise ReleaseBuildError("environment.yml 必须且只能包含一个 pip 依赖表")
    pip_dependencies = pip_tables[0]["pip"]
    if not isinstance(pip_dependencies, list) or _parse_pinned_dependencies(
        pip_dependencies, "environment.yml pip dependencies"
    ) != _ENVIRONMENT_DEPENDENCIES:
        raise ReleaseBuildError("environment.yml 精确依赖闭包漂移")


def _parse_pinned_dependencies(values: list[object], label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    pattern = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
    )
    for value in values:
        if not isinstance(value, str):
            raise ReleaseBuildError(f"{label} 必须全部是字符串")
        match = pattern.fullmatch(value)
        if match is None:
            raise ReleaseBuildError(f"{label} 包含非精确版本：{value!r}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).casefold()
        if name in parsed:
            raise ReleaseBuildError(f"{label} 包含重复依赖：{name}")
        parsed[name] = match.group(2)
    return parsed


def _validate_wheel_payload(
    by_name: dict[str, bytes],
    wheel_member: str,
    metadata: ReleaseMetadata,
) -> None:
    dist_info = f"university_physics_agent-{metadata.version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    wheel_name = f"{dist_info}/WHEEL"
    record_name = f"{dist_info}/RECORD"
    license_name = f"{dist_info}/licenses/LICENSE"
    entry_points_name = f"{dist_info}/entry_points.txt"
    top_level_name = f"{dist_info}/top_level.txt"
    try:
        with zipfile.ZipFile(io.BytesIO(by_name[wheel_member])) as archive:
            files = {
                info.filename: archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseBuildError("无法复核项目 wheel 内容") from exc

    dist_info_roots = {
        PurePosixPath(name).parts[0]
        for name in files
        if PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if dist_info_roots != {dist_info}:
        raise ReleaseBuildError("wheel 的 dist-info 名称或数量与项目版本不一致")
    required = {
        metadata_name,
        wheel_name,
        record_name,
        license_name,
        entry_points_name,
        top_level_name,
    }
    if not required.issubset(files):
        raise ReleaseBuildError("wheel 缺少固定 dist-info 元数据、入口或 MIT LICENSE")
    for basename in ("METADATA", "WHEEL", "RECORD"):
        matches = [name for name in files if name.endswith(f".dist-info/{basename}")]
        if matches != [f"{dist_info}/{basename}"]:
            raise ReleaseBuildError(f"wheel 的 {basename} 数量或路径无效")

    project_metadata = BytesParser().parsebytes(files[metadata_name])
    if project_metadata.get_all("Name", []) != ["university-physics-agent"]:
        raise ReleaseBuildError("wheel METADATA 项目名无效")
    if project_metadata.get_all("Version", []) != [metadata.version]:
        raise ReleaseBuildError("wheel METADATA 项目版本无效")
    if project_metadata.get_all("License-File", []) != ["LICENSE"]:
        raise ReleaseBuildError("wheel METADATA 未唯一声明 MIT LICENSE 文件")
    if project_metadata.get_all("License-Expression", []) != ["MIT"]:
        raise ReleaseBuildError("wheel METADATA 未唯一声明 MIT License-Expression")
    if project_metadata.get_all("Requires-Python", []) != [">=3.11"]:
        raise ReleaseBuildError("wheel METADATA Requires-Python 漂移")
    wheel_dependencies = project_metadata.get_all("Requires-Dist", [])
    if _parse_pinned_dependencies(
        wheel_dependencies, "wheel METADATA Requires-Dist"
    ) != _DIRECT_DEPENDENCIES:
        raise ReleaseBuildError("wheel METADATA 直接依赖集合漂移")
    wheel_metadata = BytesParser().parsebytes(files[wheel_name])
    if wheel_metadata.get_all("Wheel-Version", []) != ["1.0"]:
        raise ReleaseBuildError("wheel WHEEL 元数据缺少固定 Wheel-Version")
    if wheel_metadata.get_all("Root-Is-Purelib", []) != ["true"]:
        raise ReleaseBuildError("wheel 必须是 purelib")
    if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
        raise ReleaseBuildError("wheel Tag 不是固定 py3-none-any")
    if wheel_metadata.get_all("Generator", []) != [
        f"setuptools ({metadata.setuptools_version})"
    ]:
        raise ReleaseBuildError("wheel Generator 未绑定当前 setuptools 版本")

    try:
        entry_points = configparser.ConfigParser(interpolation=None, strict=True)
        entry_points.read_string(files[entry_points_name].decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise ReleaseBuildError("wheel console entry point 元数据无效") from exc
    if entry_points.sections() != ["console_scripts"] or dict(
        entry_points.items("console_scripts")
    ) != {"physics-agent": "physics_agent.cli:main"}:
        raise ReleaseBuildError("wheel console entry point 不是固定 physics-agent CLI")
    if files[top_level_name] != b"physics_agent\n":
        raise ReleaseBuildError("wheel top_level.txt 与包名不一致")

    if files[license_name] != by_name["LICENSE"]:
        raise ReleaseBuildError("wheel 内 MIT LICENSE 与 bundle/source 不一致")
    _validate_wheel_record(files, record_name)

    source_prefix = "source/src/physics_agent/"
    source_package = {
        name.removeprefix("source/src/"): content
        for name, content in by_name.items()
        if name.startswith(source_prefix)
        and (name.endswith(".py") or name.startswith(source_prefix + "resources/"))
    }
    wheel_package = {
        name: content
        for name, content in files.items()
        if name.startswith("physics_agent/")
        and (name.endswith(".py") or name.startswith("physics_agent/resources/"))
    }
    if not any(name.endswith(".py") for name in source_package):
        raise ReleaseBuildError("source/ 缺少 physics_agent Python 源码")
    if not any(name.startswith("physics_agent/resources/") for name in source_package):
        raise ReleaseBuildError("source/ 缺少 physics_agent 包资源")
    if source_package != wheel_package:
        raise ReleaseBuildError("wheel 与 source/src 的 Python 源码或包资源闭包不一致")
    allowed_files = set(source_package) | required
    if set(files) != allowed_files:
        raise ReleaseBuildError("wheel 包含 source/闭包和固定 dist-info 之外的普通成员")


def _validate_wheel_record(files: dict[str, bytes], record_name: str) -> None:
    try:
        record_text = files[record_name].decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ReleaseBuildError("wheel RECORD 不是有效 UTF-8 CSV") from exc
    recorded: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ReleaseBuildError("wheel RECORD 行必须恰有三列")
        name = _safe_relative_path(row[0], "wheel RECORD 路径").as_posix()
        if name in recorded:
            raise ReleaseBuildError(f"wheel RECORD 包含重复路径：{name}")
        recorded[name] = (row[1], row[2])
    if set(recorded) != set(files):
        raise ReleaseBuildError("wheel RECORD 与实际普通文件集合不一致")
    for name, content in files.items():
        digest, size = recorded[name]
        if name == record_name:
            if digest or size:
                raise ReleaseBuildError("wheel RECORD 自身摘要和大小必须为空")
            continue
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()
        ).rstrip(b"=").decode("ascii")
        if digest != f"sha256={expected_digest}" or size != str(len(content)):
            raise ReleaseBuildError(f"wheel RECORD 摘要或大小不一致：{name}")


def _validate_knowledge_payload(
    manifest_name: str,
    manifest_content: bytes,
    by_name: dict[str, bytes],
) -> None:
    try:
        document = json.loads(manifest_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("reviewed 知识 manifest 不是有效 UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("status") != "published":
        raise ReleaseBuildError("知识 manifest 必须明确为 published")
    if document.get("package_id") != "mechanics.zh.reviewed":
        raise ReleaseBuildError("知识 manifest package_id 不是固定 reviewed 包")
    if document.get("version") != "0.2.0":
        raise ReleaseBuildError("知识 manifest version 不是固定 0.2.0")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise ReleaseBuildError("知识 manifest 必须包含非空 items")
    package_prefix = PurePosixPath(manifest_name).parent
    expected = {manifest_name}
    for entry in items:
        if not isinstance(entry, dict):
            raise ReleaseBuildError("知识 manifest item 结构无效")
        path_value = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(path_value, str) or not isinstance(digest, str):
            raise ReleaseBuildError("知识 manifest item 缺少 path 或 sha256")
        item_path = _safe_relative_path(path_value, "知识 item 路径")
        if not _SHA256.fullmatch(digest):
            raise ReleaseBuildError("知识 manifest item 摘要格式无效")
        member_name = (package_prefix / item_path).as_posix()
        if member_name in expected:
            raise ReleaseBuildError("知识 manifest 包含重复 item 路径")
        expected.add(member_name)
        content = by_name.get(member_name)
        if content is None:
            raise ReleaseBuildError(f"allowlist 缺少知识 item：{member_name}")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ReleaseBuildError(f"知识 item 摘要不一致：{member_name}")
    actual = {
        name for name in by_name if name.startswith(package_prefix.as_posix() + "/")
    }
    if actual != expected:
        raise ReleaseBuildError("reviewed 知识包包含 manifest 未绑定的额外文件")


def _validate_git_binding(
    source_root: Path,
    inputs: tuple[_ValidatedInput, ...],
    metadata: ReleaseMetadata,
) -> None:
    source_commit = metadata.source_commit
    top_level = _git(source_root, "rev-parse", "--show-toplevel").decode(
        "utf-8", errors="strict"
    ).strip()
    try:
        git_root = Path(top_level).resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("Git 返回的仓库根不可访问") from exc
    if git_root != source_root:
        raise ReleaseBuildError("source_root 必须恰为本地 Git 仓库根")
    object_type = _git(source_root, "cat-file", "-t", source_commit).strip()
    if object_type != b"commit":
        raise ReleaseBuildError("source_commit 不是仓库中的实际 commit")
    commit_time = _git(
        source_root, "show", "-s", "--format=%ct", source_commit
    ).strip()
    if commit_time != str(metadata.source_date_epoch).encode("ascii"):
        raise ReleaseBuildError("SOURCE_DATE_EPOCH 不等于固定 commit 的 Unix UTC 时间")

    for item in inputs:
        if item.member.parts[0] == "artifacts" and item.member.suffix == ".whl":
            continue
        relative = item.source.relative_to(source_root).as_posix()
        object_name = f"{source_commit}:{relative}"
        size_bytes = _git(source_root, "cat-file", "-s", object_name).strip()
        try:
            blob_size = int(size_bytes)
        except ValueError as exc:
            raise ReleaseBuildError("Git blob 大小无效") from exc
        if blob_size > MAX_INPUT_BYTES:
            raise ReleaseBuildError(f"固定 commit 中的发布文件过大：{relative}")
        committed = _git(source_root, "cat-file", "blob", object_name)
        if committed != item.content:
            raise ReleaseBuildError(f"allowlist 当前字节不属于 source_commit：{relative}")

    tree_output = _git(
        source_root,
        "ls-tree",
        "-rz",
        "--name-only",
        source_commit,
        "--",
        "src/physics_agent",
    )
    committed_package = {
        value.decode("utf-8", errors="strict")
        for value in tree_output.split(b"\x00")
        if value
    }
    committed_relevant = {
        name
        for name in committed_package
        if name.endswith(".py") or name.startswith("src/physics_agent/resources/")
    }
    allowed_relevant = {
        item.member.as_posix().removeprefix("source/")
        for item in inputs
        if item.member.as_posix().startswith("source/src/physics_agent/")
        and (
            item.member.suffix == ".py"
            or item.member.as_posix().startswith(
                "source/src/physics_agent/resources/"
            )
        )
    }
    if committed_relevant != allowed_relevant:
        raise ReleaseBuildError(
            "source/ allowlist 未完整覆盖固定 commit 的 Python 源码与包资源"
        )


def _git(source_root: Path, *arguments: str) -> bytes:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        completed = subprocess.run(
            ("git", "-C", os.fspath(source_root), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseBuildError("本地 Git 校验无法执行") from exc
    if completed.returncode != 0:
        raise ReleaseBuildError("本地 Git 仓库、commit 或 blob 校验失败")
    return completed.stdout


def _write_payload(
    bundle: Path, inputs: tuple[_ValidatedInput, ...]
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for item in inputs:
        target = bundle.joinpath(*item.member.parts)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(item.content)
        os.chmod(target, 0o644)
        entries.append(
            {
                "category": item.category,
                "path": item.member.as_posix(),
                "sha256": hashlib.sha256(item.content).hexdigest(),
                "size": len(item.content),
            }
        )
    return entries


def _write_metadata_files(
    bundle: Path,
    entries: list[dict[str, object]],
    metadata: ReleaseMetadata,
) -> None:
    manifest = {
        "format_version": 1,
        "knowledge_manifest_sha256": metadata.knowledge_manifest_sha256,
        "license_holder": metadata.license_holder.strip(),
        "license_status": metadata.license_status,
        "payload": entries,
        "project_version": metadata.version,
        "source_commit": metadata.source_commit,
        "source_date_epoch": metadata.source_date_epoch,
        "tools": {
            "pip": metadata.pip_version,
            "python": metadata.python_version,
            "setuptools": metadata.setuptools_version,
            "wheel": metadata.wheel_version,
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _scan_content(PurePosixPath("release-manifest.json"), manifest_bytes)
    manifest_path = bundle / "release-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    os.chmod(manifest_path, 0o644)

    sums = [
        f"{entry['sha256']}  {entry['path']}"
        for entry in entries
    ]
    sums.append(
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  release-manifest.json"
    )
    sums_path = bundle / "SHA256SUMS"
    sums_path.write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
    os.chmod(sums_path, 0o644)


def _normalize_staging(bundle: Path) -> None:
    for path in (bundle, *bundle.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o755)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, 0o644)
        else:
            raise ReleaseBuildError("staging 出现符号链接或非普通成员")


def _write_archive(bundle: Path, destination: Path, epoch: int) -> None:
    members = [
        bundle,
        *sorted(
            bundle.rglob("*"),
            key=lambda item: item.relative_to(bundle).as_posix(),
        ),
    ]
    try:
        with destination.open("xb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=epoch
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.GNU_FORMAT,
                ) as archive:
                    for path in members:
                        relative = path.relative_to(bundle.parent).as_posix()
                        info = tarfile.TarInfo(
                            relative + ("/" if path.is_dir() else "")
                        )
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = epoch
                        info.pax_headers = {}
                        if path.is_dir():
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            info.size = 0
                            archive.addfile(info)
                        else:
                            file_info = path.lstat()
                            if not stat.S_ISREG(file_info.st_mode):
                                raise ReleaseBuildError("staging 出现非普通文件")
                            info.type = tarfile.REGTYPE
                            info.mode = 0o644
                            info.size = file_info.st_size
                            with path.open("rb") as stream:
                                archive.addfile(info, stream)
        os.chmod(destination, 0o644)
    except OSError as exc:
        raise ReleaseBuildError("无法写入规范化归档") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_published_outputs(dist: Path, paths: list[Path]) -> None:
    for path in reversed(paths):
        if path.parent != dist:
            raise ReleaseBuildError("拒绝清理 dist 之外的发布路径")
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


def _remove_temporary_root(dist: Path, temporary_root: Path) -> None:
    if temporary_root.parent != dist or not temporary_root.name.startswith(
        ".m5-release-"
    ):
        raise ReleaseBuildError("拒绝清理未经验证的临时目录")
    if temporary_root.exists():
        shutil.rmtree(temporary_root)
