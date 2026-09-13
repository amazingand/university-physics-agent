"""本地知识包的安全加载与可重建全文检索。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from physics_agent.core import KnowledgeHit, KnowledgePackageRef, SourceReference


INDEX_SCHEMA_VERSION = "1"
DEFAULT_MAX_QUERY_CHARS = 128
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ITEM_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 10_000
MAX_RESULTS = 100
MAX_FTS_TERMS = 64

_STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPES = {"text/markdown", "application/json", "application/yaml"}
_PROVENANCE_KINDS = {"original", "open_source", "course_mapping"}
_LOCATOR_KEYS = {
    "section",
    "paragraph",
    "stable_entry_id",
    "pdf_file_page",
    "printed_page",
}


class KnowledgeError(RuntimeError):
    """知识包或检索无法安全使用。"""


class ManifestValidationError(KnowledgeError):
    """manifest 结构或语义不符合知识包契约。"""


class PackageIntegrityError(KnowledgeError):
    """权威文件路径或内容校验失败。"""


class IndexCapabilityError(KnowledgeError):
    """当前 SQLite 不支持要求的 FTS5 trigram 能力。"""


class QueryValidationError(KnowledgeError):
    """检索请求超出受控范围。"""


@dataclass(frozen=True)
class _LoadedItem:
    item_id: str
    text: str
    sources: tuple[SourceReference, ...]


@dataclass(frozen=True)
class _LoadedPackage:
    reference: KnowledgePackageRef
    status: str
    manifest_sha256: str
    items: Mapping[str, _LoadedItem]


class LocalKnowledgeRepository:
    """绑定单一知识包版本的本地只读仓库。"""

    def __init__(
        self,
        package_root: str | Path,
        index_path: str | Path,
        expected_package: KnowledgePackageRef,
        *,
        max_query_chars: int = DEFAULT_MAX_QUERY_CHARS,
    ) -> None:
        if not isinstance(max_query_chars, int) or isinstance(max_query_chars, bool):
            raise ValueError("max_query_chars 必须是整数")
        if not 1 <= max_query_chars <= 4096:
            raise ValueError("max_query_chars 必须在 1 到 4096 之间")

        try:
            self._package_root = Path(package_root).resolve(strict=True)
        except OSError as exc:
            raise PackageIntegrityError("知识包根路径不存在或不可访问") from exc
        if not self._package_root.is_dir():
            raise PackageIntegrityError("知识包根路径不是目录")
        self._index_path = Path(index_path).resolve(strict=False)
        if self._index_path == self._package_root or self._index_path.is_relative_to(
            self._package_root
        ):
            raise PackageIntegrityError("派生索引必须位于知识包根目录之外")
        self._max_query_chars = max_query_chars
        self._package = self._load_package(expected_package)
        self._ensure_index()

    @property
    def package(self) -> KnowledgePackageRef:
        return self._package.reference

    @property
    def manifest_sha256(self) -> str:
        return self._package.manifest_sha256

    @property
    def status(self) -> str:
        """返回已校验 manifest 状态，供教学入口阻断 draft。"""

        return self._package.status

    def search(
        self, query: str, package: KnowledgePackageRef, *, limit: int
    ) -> Sequence[KnowledgeHit]:
        if package != self._package.reference:
            raise QueryValidationError(
                "检索请求的 package_id/version 与当前固定知识包不一致"
            )
        if not isinstance(query, str):
            raise QueryValidationError("查询必须是字符串")
        normalized = query.strip()
        if not normalized:
            raise QueryValidationError("查询不得为空")
        if "\x00" in normalized:
            raise QueryValidationError("查询不得包含 NUL 字符")
        if len(normalized) > self._max_query_chars:
            raise QueryValidationError(
                f"查询超过 {self._max_query_chars} 个字符的上限"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_RESULTS
        ):
            raise QueryValidationError(f"limit 必须是 1 到 {MAX_RESULTS} 的整数")

        try:
            item_ids = self._search_index(normalized, limit)
        except sqlite3.DatabaseError:
            self.rebuild_index()
            try:
                item_ids = self._search_index(normalized, limit)
            except sqlite3.DatabaseError as exc:
                raise KnowledgeError("派生索引损坏且重建后仍无法检索") from exc

        hits: list[KnowledgeHit] = []
        for item_id in item_ids:
            item = self._package.items.get(item_id)
            if item is None:
                self.rebuild_index()
                raise KnowledgeError("派生索引引用了知识包中不存在的条目")
            hits.append(
                KnowledgeHit(
                    item_id=item.item_id,
                    text=item.text,
                    sources=item.sources,
                )
            )
        return tuple(hits)

    def rebuild_index(self) -> None:
        """从已校验权威内容全量创建临时数据库，再原子替换派生索引。"""

        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise KnowledgeError("无法创建派生索引目录") from exc

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._index_path.name}.",
            suffix=".tmp",
            dir=self._index_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with sqlite3.connect(temporary_path) as connection:
                self._create_index_schema(connection)
                metadata = {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "package_id": self._package.reference.package_id,
                    "package_version": self._package.reference.version,
                    "manifest_sha256": self._package.manifest_sha256,
                }
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    sorted(metadata.items()),
                )
                connection.executemany(
                    "INSERT INTO search_index(item_id, body) VALUES (?, ?)",
                    (
                        (item.item_id, item.text)
                        for item in sorted(
                            self._package.items.values(), key=lambda value: value.item_id
                        )
                    ),
                )
            os.replace(temporary_path, self._index_path)
        except sqlite3.OperationalError as exc:
            raise IndexCapabilityError(
                "SQLite 无法创建 FTS5 trigram 索引；不会安装静默替代方案"
            ) from exc
        except OSError as exc:
            raise KnowledgeError("无法原子替换派生索引") from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_package(self, expected: KnowledgePackageRef) -> _LoadedPackage:
        _validate_stable_id(expected.package_id, "期望 package_id")
        if not isinstance(expected.version, str) or not expected.version:
            raise ManifestValidationError("期望知识包 version 不得为空")

        try:
            manifest_path = (self._package_root / "manifest.json").resolve(strict=True)
        except OSError as exc:
            raise PackageIntegrityError("知识包缺少可读取的 manifest.json") from exc
        if (
            not manifest_path.is_relative_to(self._package_root)
            or not manifest_path.is_file()
        ):
            raise PackageIntegrityError("manifest.json 必须是知识包根目录内的普通文件")
        manifest_bytes = _read_limited(manifest_path, MAX_MANIFEST_BYTES, "manifest.json")
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError("manifest.json 必须是有效 UTF-8 JSON") from exc
        manifest = _mapping(manifest, "manifest")
        _require_keys(
            manifest,
            required={
                "schema_version",
                "package_id",
                "version",
                "created_at",
                "language",
                "status",
                "sources",
                "items",
            },
            allowed={
                "schema_version",
                "package_id",
                "version",
                "created_at",
                "language",
                "status",
                "sources",
                "items",
                "indexes",
            },
            context="manifest",
        )
        if manifest["schema_version"] != "1.0":
            raise ManifestValidationError("仅支持知识包 schema_version=1.0")
        package_id = _string(manifest, "package_id", "manifest")
        _validate_stable_id(package_id, "manifest.package_id")
        version = _string(manifest, "version", "manifest")
        if package_id != expected.package_id or version != expected.version:
            raise ManifestValidationError("manifest 的 package_id/version 与固定版本不一致")
        if manifest["language"] != "zh-CN":
            raise ManifestValidationError("知识包 language 必须是 zh-CN")
        status = manifest["status"]
        if status not in {"draft", "published"}:
            raise ManifestValidationError("知识包 status 必须是 draft 或 published")
        _string(manifest, "created_at", "manifest")
        if "indexes" in manifest and not isinstance(manifest["indexes"], list):
            raise ManifestValidationError("manifest.indexes 必须是数组")

        sources = self._load_sources(manifest["sources"], published=status == "published")
        items = self._load_items(
            manifest["items"], sources, published=status == "published"
        )
        return _LoadedPackage(
            reference=KnowledgePackageRef(package_id=package_id, version=version),
            status=status,
            manifest_sha256=manifest_hash,
            items=MappingProxyType(items),
        )

    def _load_sources(
        self, value: object, *, published: bool
    ) -> dict[str, tuple[str, str]]:
        if not isinstance(value, list):
            raise ManifestValidationError("manifest.sources 必须是数组")
        sources: dict[str, tuple[str, str]] = {}
        for index, raw_source in enumerate(value):
            context = f"manifest.sources[{index}]"
            source = _mapping(raw_source, context)
            _require_keys(
                source,
                required={
                    "id",
                    "title",
                    "url",
                    "version",
                    "license",
                    "locator_method",
                    "verification",
                },
                allowed={
                    "id",
                    "title",
                    "url",
                    "version",
                    "license",
                    "locator_method",
                    "verification",
                },
                context=context,
            )
            source_id = _string(source, "id", context)
            _validate_stable_id(source_id, f"{context}.id")
            if source_id in sources:
                raise ManifestValidationError(f"重复来源 ID：{source_id}")
            _nonempty_string(source, "title", context)
            _validate_url(_string(source, "url", context), f"{context}.url")
            source_version = _nonempty_string(source, "version", context)
            _nonempty_string(source, "locator_method", context)

            license_context = f"{context}.license"
            license_data = _mapping(source["license"], license_context)
            _require_keys(
                license_data,
                required={
                    "id",
                    "url",
                    "attribution",
                    "allows_adaptation",
                    "allows_redistribution",
                },
                allowed={
                    "id",
                    "url",
                    "attribution",
                    "allows_adaptation",
                    "allows_redistribution",
                },
                context=license_context,
            )
            license_id = _nonempty_string(license_data, "id", license_context)
            _validate_url(
                _string(license_data, "url", license_context),
                f"{license_context}.url",
            )
            _nonempty_string(license_data, "attribution", license_context)
            allows_adaptation = _boolean(
                license_data, "allows_adaptation", license_context
            )
            allows_redistribution = _boolean(
                license_data, "allows_redistribution", license_context
            )

            verification_context = f"{context}.verification"
            verification = _mapping(source["verification"], verification_context)
            _require_keys(
                verification,
                required={"status", "evidence_ref"},
                allowed={"status", "evidence_ref", "verified_at"},
                context=verification_context,
            )
            verification_status = verification.get("status")
            if verification_status not in {"pending", "verified"}:
                raise ManifestValidationError(
                    f"{verification_context}.status 无效"
                )
            _nonempty_string(verification, "evidence_ref", verification_context)
            if "verified_at" in verification:
                _nonempty_string(verification, "verified_at", verification_context)

            if published:
                if not allows_adaptation or not allows_redistribution:
                    raise ManifestValidationError(
                        f"published 知识包来源 {source_id} 缺少改编或再分发许可"
                    )
                if verification_status != "verified" or "verified_at" not in verification:
                    raise ManifestValidationError(
                        f"published 知识包来源 {source_id} 尚未完成许可核实"
                    )
            sources[source_id] = (source_version, license_id)
        return sources

    def _load_items(
        self,
        value: object,
        sources: Mapping[str, tuple[str, str]],
        *,
        published: bool,
    ) -> dict[str, _LoadedItem]:
        if not isinstance(value, list):
            raise ManifestValidationError("manifest.items 必须是数组")
        if len(value) > MAX_ITEMS:
            raise ManifestValidationError(f"知识包条目超过 {MAX_ITEMS} 条上限")
        if published and not value:
            raise ManifestValidationError("published 知识包不得为空")

        items: dict[str, _LoadedItem] = {}
        seen_paths: set[str] = set()
        for index, raw_item in enumerate(value):
            context = f"manifest.items[{index}]"
            item = _mapping(raw_item, context)
            _require_keys(
                item,
                required={"id", "path", "media_type", "sha256", "provenance"},
                allowed={"id", "path", "media_type", "sha256", "provenance"},
                context=context,
            )
            item_id = _string(item, "id", context)
            _validate_stable_id(item_id, f"{context}.id")
            if item_id in items:
                raise ManifestValidationError(f"重复条目 ID：{item_id}")
            relative_path = _safe_relative_path(_string(item, "path", context), context)
            path_text = relative_path.as_posix()
            if path_text in seen_paths:
                raise ManifestValidationError(f"重复条目路径：{path_text}")
            seen_paths.add(path_text)
            media_type = item.get("media_type")
            if media_type not in _MEDIA_TYPES:
                raise ManifestValidationError(f"{context}.media_type 不受支持")
            expected_hash = _string(item, "sha256", context)
            if not _SHA256.fullmatch(expected_hash):
                raise ManifestValidationError(f"{context}.sha256 格式无效")

            try:
                content_path = (self._package_root / relative_path).resolve(strict=True)
            except OSError as exc:
                raise PackageIntegrityError(f"条目 {item_id} 文件不存在或不可读取") from exc
            if (
                not content_path.is_relative_to(self._package_root)
                or not content_path.is_file()
            ):
                raise PackageIntegrityError(f"条目 {item_id} 路径逃逸知识包根目录")
            content = _read_limited(content_path, MAX_ITEM_BYTES, f"条目 {item_id}")
            actual_hash = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise PackageIntegrityError(f"条目 {item_id} 的 SHA-256 校验失败")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PackageIntegrityError(f"条目 {item_id} 不是有效 UTF-8") from exc

            references = _load_provenance(
                item["provenance"],
                sources,
                context=f"{context}.provenance",
                published=published,
            )
            items[item_id] = _LoadedItem(
                item_id=item_id,
                text=text,
                sources=references,
            )
        return items

    def _ensure_index(self) -> None:
        if not self._index_is_current():
            self.rebuild_index()

    def _index_is_current(self) -> bool:
        if not self._index_path.is_file():
            return False
        expected_metadata = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "package_id": self._package.reference.package_id,
            "package_version": self._package.reference.version,
            "manifest_sha256": self._package.manifest_sha256,
        }
        try:
            with sqlite3.connect(
                _read_only_sqlite_uri(self._index_path), uri=True
            ) as connection:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    return False
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if metadata != expected_metadata:
                    return False
                indexed_rows = connection.execute(
                    "SELECT item_id, body FROM search_index ORDER BY item_id"
                ).fetchall()
        except (sqlite3.DatabaseError, OSError):
            return False
        expected_rows = [
            (item.item_id, item.text)
            for item in sorted(self._package.items.values(), key=lambda value: value.item_id)
        ]
        return indexed_rows == expected_rows

    @staticmethod
    def _create_index_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE search_index USING "
            "fts5(item_id UNINDEXED, body, tokenize='trigram')"
        )

    def _search_index(self, query: str, limit: int) -> list[str]:
        with sqlite3.connect(
            _read_only_sqlite_uri(self._index_path), uri=True
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            terms = _fts_terms(query)
            if len(query) <= 2 or not terms:
                escaped = _escape_like(query)
                rows = connection.execute(
                    "SELECT item_id FROM search_index "
                    "WHERE body LIKE ? ESCAPE '\\' "
                    "ORDER BY instr(body, ?) ASC, item_id ASC LIMIT ?",
                    (f"%{escaped}%", query, limit),
                ).fetchall()
            else:
                literal_query = " OR ".join(f'"{term}"' for term in terms)
                rows = connection.execute(
                    "SELECT item_id FROM search_index "
                    "WHERE search_index MATCH ? "
                    "ORDER BY bm25(search_index) ASC, item_id ASC LIMIT ?",
                    (literal_query, limit),
                ).fetchall()
        return [row[0] for row in rows]


def _load_provenance(
    value: object,
    sources: Mapping[str, tuple[str, str]],
    *,
    context: str,
    published: bool,
) -> tuple[SourceReference, ...]:
    provenance = _mapping(value, context)
    _require_keys(
        provenance,
        required={
            "kind",
            "course_alignment",
            "factual_sources",
            "copied_text",
            "human_reviewed",
        },
        allowed={
            "kind",
            "course_alignment",
            "factual_sources",
            "copied_text",
            "human_reviewed",
            "transformation",
        },
        context=context,
    )
    kind = provenance.get("kind")
    if kind not in _PROVENANCE_KINDS:
        raise ManifestValidationError(f"{context}.kind 无效")
    _boolean(provenance, "copied_text", context)
    human_reviewed = _boolean(provenance, "human_reviewed", context)
    if published and not human_reviewed:
        raise ManifestValidationError("published 条目必须有真实 human_reviewed 门禁")
    if "transformation" in provenance:
        transformation = provenance["transformation"]
        if transformation is not None and (
            not isinstance(transformation, str) or not transformation.strip()
        ):
            raise ManifestValidationError(f"{context}.transformation 必须是非空字符串或 null")

    alignment_context = f"{context}.course_alignment"
    alignment = _mapping(provenance["course_alignment"], alignment_context)
    _require_keys(
        alignment,
        required={"book_title", "edition", "chapter", "verification_status"},
        allowed={"book_title", "edition", "chapter", "verification_status"},
        context=alignment_context,
    )
    _nonempty_string(alignment, "book_title", alignment_context)
    _nonempty_string(alignment, "edition", alignment_context)
    chapter = alignment.get("chapter")
    if chapter is not None and not isinstance(chapter, str):
        raise ManifestValidationError(f"{alignment_context}.chapter 必须是字符串或 null")
    if alignment.get("verification_status") not in {"pending", "verified"}:
        raise ManifestValidationError(f"{alignment_context}.verification_status 无效")

    raw_references = provenance["factual_sources"]
    if not isinstance(raw_references, list):
        raise ManifestValidationError(f"{context}.factual_sources 必须是数组")
    if kind in {"original", "open_source"} and not raw_references:
        raise ManifestValidationError(f"{kind} 条目必须包含 factual_sources")

    references: list[SourceReference] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_reference in enumerate(raw_references):
        reference_context = f"{context}.factual_sources[{index}]"
        reference = _mapping(raw_reference, reference_context)
        _require_keys(
            reference,
            required={"source_id", "locator", "source_version", "license_id"},
            allowed={"source_id", "locator", "source_version", "license_id"},
            context=reference_context,
        )
        source_id = _string(reference, "source_id", reference_context)
        source_version = _nonempty_string(
            reference, "source_version", reference_context
        )
        license_id = _nonempty_string(reference, "license_id", reference_context)
        declared_source = sources.get(source_id)
        if declared_source is None:
            raise ManifestValidationError(f"条目引用了缺失来源：{source_id}")
        if declared_source != (source_version, license_id):
            raise ManifestValidationError(
                f"条目来源 {source_id} 的版本或许可证与来源清单不一致"
            )
        locator = _load_locator(reference["locator"], reference_context)
        marker = (source_id, json.dumps(locator, sort_keys=True, ensure_ascii=False))
        if marker in seen:
            raise ManifestValidationError(f"重复事实来源定位：{source_id}")
        seen.add(marker)
        references.append(
            SourceReference(
                source_id=source_id,
                source_version=source_version,
                license_id=license_id,
                locator=MappingProxyType(locator),
            )
        )
    return tuple(references)


def _load_locator(value: object, context: str) -> dict[str, str | int]:
    locator = _mapping(value, f"{context}.locator")
    keys = set(locator)
    if not keys or not keys.issubset(_LOCATOR_KEYS):
        raise ManifestValidationError(f"{context}.locator 缺失或包含未知定位字段")
    normalized: dict[str, str | int] = {}
    for key, raw_value in locator.items():
        if key == "pdf_file_page":
            if (
                not isinstance(raw_value, int)
                or isinstance(raw_value, bool)
                or raw_value < 1
            ):
                raise ManifestValidationError("pdf_file_page 必须是正整数")
        elif not isinstance(raw_value, str) or not raw_value.strip():
            raise ManifestValidationError(f"locator.{key} 必须是非空字符串")
        normalized[key] = raw_value
    return normalized


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{context} 必须是对象")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"manifest JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _require_keys(
    value: Mapping[str, Any], *, required: set[str], allowed: set[str], context: str
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        raise ManifestValidationError(f"{context} 缺少字段：{', '.join(missing)}")
    unknown = sorted(keys - allowed)
    if unknown:
        raise ManifestValidationError(f"{context} 包含未知字段：{', '.join(unknown)}")


def _string(value: Mapping[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ManifestValidationError(f"{context}.{key} 必须是字符串")
    return result


def _nonempty_string(value: Mapping[str, Any], key: str, context: str) -> str:
    result = _string(value, key, context)
    if not result.strip():
        raise ManifestValidationError(f"{context}.{key} 不得为空")
    return result


def _boolean(value: Mapping[str, Any], key: str, context: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ManifestValidationError(f"{context}.{key} 必须是布尔值")
    return result


def _validate_stable_id(value: str, context: str) -> None:
    if not _STABLE_ID.fullmatch(value):
        raise ManifestValidationError(f"{context} 不是有效稳定 ID")


def _validate_url(value: str, context: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ManifestValidationError(f"{context} 必须是无凭据的 HTTP(S) URL")


def _safe_relative_path(value: str, context: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise PackageIntegrityError(f"{context}.path 必须是 POSIX 相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageIntegrityError(f"{context}.path 包含绝对路径或路径穿越")
    return path


def _read_limited(path: Path, maximum: int, context: str) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise PackageIntegrityError(f"{context} 超过 {maximum} 字节上限")
        return content
    except PackageIntegrityError:
        raise
    except OSError as exc:
        raise PackageIntegrityError(f"无法读取 {context}") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_terms(value: str) -> tuple[str, ...]:
    """把自然查询编译为数量受限的三字符字面 term。"""

    terms: list[str] = []
    seen: set[str] = set()
    for segment in re.findall(r"\w+", value, flags=re.UNICODE):
        if len(segment) < 3:
            continue
        for index in range(len(segment) - 2):
            term = segment[index : index + 3]
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= MAX_FTS_TERMS:
                return tuple(terms)
    return tuple(terms)


def _read_only_sqlite_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"
