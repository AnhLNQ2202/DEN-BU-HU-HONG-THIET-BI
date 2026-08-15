"""Safe, atomic storage for uploaded TranNNB reference workbooks."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO, Generic, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4
from xml.parsers import expat

from asset_compensation.adapters import (
    CcdcWorkbookIndex,
    FaGlWorkbookIndex,
    TranReferenceError,
)
from asset_compensation.domain import ValidationError

MAX_TRAN_REFERENCE_FILE_BYTES = 50 * 1024 * 1024
_MAX_EXPANDED_BYTES = 250 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 5_000
_MAX_COMPRESSION_RATIO = 200
_MAX_EXTERNAL_LINK_XML_BYTES = 1024 * 1024
_MAX_EXTERNAL_LINK_RELS_BYTES = 256 * 1024
_MAX_EXTERNAL_LINK_ELEMENTS = 4_096
_MAX_EXTERNAL_LINK_DEPTH = 4
_MAX_EXTERNAL_LINK_ATTRIBUTES = 8
_MAX_EXTERNAL_LINK_ATTRIBUTE_CHARS = 4_096
_MAX_EXTERNAL_LINK_TEXT_CHARS = 4_096
_MAX_EXTERNAL_LINK_RELATIONSHIPS = 128
_MAX_RELATIONSHIP_XML_BYTES = 4 * 1024 * 1024
_MAX_RELATIONSHIP_XML_ELEMENTS = 10_000
_MAX_RELATIONSHIP_XML_DEPTH = 16
_MAX_RELATIONSHIP_XML_ATTRIBUTES = 32
_MAX_RELATIONSHIP_XML_ATTRIBUTE_CHARS = 64 * 1024
_MAX_RELATIONSHIP_XML_TEXT_CHARS = 256 * 1024
_MAX_FORMULA_XML_PART_BYTES = 128 * 1024 * 1024
_MAX_FORMULA_XML_ELEMENTS = 5_000_000
_MAX_FORMULA_XML_DEPTH = 64
_MAX_FORMULA_XML_ATTRIBUTES = 128
_MAX_FORMULA_XML_ATTRIBUTE_CHARS = 64 * 1024
_MAX_FORMULA_TEXT_CHARS = 32 * 1024
_XML_SCAN_CHUNK_BYTES = 64 * 1024
_VERSION_RE = re.compile(r"[0-9a-f]{32}")
_EXTERNAL_LINK_PART_RE = re.compile(
    r"xl/externalLinks/externalLink([1-9][0-9]*)\.xml"
)
_EXTERNAL_LINK_RELS_PART_RE = re.compile(
    r"xl/externalLinks/_rels/externalLink([1-9][0-9]*)\.xml\.rels"
)
_XML_DTD_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_FORMULA_TEXT_LOCAL_NAMES = frozenset(
    {
        "calculatedcolumnformula",
        "definedname",
        "f",
        "formula",
        "formula1",
        "formula2",
        "totalsrowformula",
    }
)
_FORMULA_ATTRIBUTE_LOCAL_NAMES = frozenset(
    {
        "calculatedcolumnformula",
        "formula",
        "formula1",
        "formula2",
        "refersto",
        "totalsrowformula",
    }
)
_FORMULA_REFERENCE_DELIMITERS = frozenset("+-*/^&=<>(),;{}:%[]")
_EXTERNAL_LINK_GUIDANCE = (
    "XLSX external workbook metadata must be unused local-file metadata; "
    "remove active or remote workbook links and save the file again"
)
_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_EXTERNAL_LINK_2021_NS = (
    "http://schemas.microsoft.com/office/spreadsheetml/2021/extlinks2021"
)
_EXTERNAL_LINK_PATH_RELATIONSHIP = (
    f"{_OFFICE_RELATIONSHIP_NS}/externalLinkPath"
)
_EXTERNAL_LINK_TAG = f"{{{_SPREADSHEET_NS}}}externalLink"
_EXTERNAL_BOOK_TAG = f"{{{_SPREADSHEET_NS}}}externalBook"
_SHEET_NAMES_TAG = f"{{{_SPREADSHEET_NS}}}sheetNames"
_SHEET_NAME_TAG = f"{{{_SPREADSHEET_NS}}}sheetName"
_SHEET_DATA_SET_TAG = f"{{{_SPREADSHEET_NS}}}sheetDataSet"
_SHEET_DATA_TAG = f"{{{_SPREADSHEET_NS}}}sheetData"
_ALTERNATE_URLS_TAG = f"{{{_EXTERNAL_LINK_2021_NS}}}alternateUrls"
_ABSOLUTE_URL_TAG = f"{{{_EXTERNAL_LINK_2021_NS}}}absoluteUrl"
_ALLOWED_EXTERNAL_LINK_TAGS = frozenset(
    {
        _EXTERNAL_LINK_TAG,
        _EXTERNAL_BOOK_TAG,
        _SHEET_NAMES_TAG,
        _SHEET_NAME_TAG,
        _SHEET_DATA_SET_TAG,
        _SHEET_DATA_TAG,
        _ALTERNATE_URLS_TAG,
        _ABSOLUTE_URL_TAG,
    }
)
_ALLOWED_EXTERNAL_LINK_CHILDREN = {
    _EXTERNAL_LINK_TAG: frozenset({_EXTERNAL_BOOK_TAG}),
    _EXTERNAL_BOOK_TAG: frozenset(
        {_ALTERNATE_URLS_TAG, _SHEET_NAMES_TAG, _SHEET_DATA_SET_TAG}
    ),
    _ALTERNATE_URLS_TAG: frozenset({_ABSOLUTE_URL_TAG}),
    _SHEET_NAMES_TAG: frozenset({_SHEET_NAME_TAG}),
    _SHEET_DATA_SET_TAG: frozenset({_SHEET_DATA_TAG}),
    _ABSOLUTE_URL_TAG: frozenset(),
    _SHEET_NAME_TAG: frozenset(),
    _SHEET_DATA_TAG: frozenset(),
}
_MIME_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def _private_mode(path: Path, mode: int) -> None:
    with suppress(OSError):
        path.chmod(mode)


def _skip_formula_string_literal(formula: str, opening_quote: int) -> int:
    """Return the first offset after an Excel double-quoted string literal."""

    cursor = opening_quote + 1
    while cursor < len(formula):
        if formula[cursor] != '"':
            cursor += 1
            continue
        if cursor + 1 < len(formula) and formula[cursor + 1] == '"':
            cursor += 2
            continue
        return cursor + 1
    return len(formula)


def _formula_bracket_end(formula: str, opening_bracket: int) -> tuple[int | None, bool]:
    """Find a balanced bracket token while respecting formula string literals."""

    depth = 1
    nested = False
    cursor = opening_bracket + 1
    while cursor < len(formula):
        character = formula[cursor]
        if character == '"':
            cursor = _skip_formula_string_literal(formula, cursor)
            continue
        if character == "[":
            depth += 1
            nested = True
        elif character == "]":
            depth -= 1
            if depth == 0:
                return cursor, nested
        cursor += 1
    return None, nested


def _has_table_reference_prefix(
    formula: str,
    opening_bracket: int,
) -> bool:
    """Return whether a bracket is directly qualified by an Excel table name."""

    cursor = opening_bracket - 1
    previous = formula[cursor] if cursor >= 0 else ""
    return bool(
        previous
        and (previous.isalnum() or previous in "_.")
    )


def _has_active_reference_suffix(formula: str, closing_bracket: int) -> bool:
    """Return whether a bracket token is followed by a sheet/name context."""

    suffix = closing_bracket + 1
    if suffix >= len(formula):
        return False
    following = formula[suffix]
    return bool(
        not following.isspace()
        and (
            following == "!"
            or following not in _FORMULA_REFERENCE_DELIMITERS
        )
    )


def _has_external_workbook_reference(formula: str) -> bool:
    """Detect active Excel workbook references without matching strings/tables.

    Excel serializes external sources as a bracketed link index or filename,
    followed by a sheet/name context (for example ``[1]Sheet!A1`` or
    ``[Book.csv]DefinedName``). Brackets inside double-quoted strings and table
    structured references are inert here.
    """

    cursor = 0
    while cursor < len(formula):
        character = formula[cursor]
        if character == '"':
            cursor = _skip_formula_string_literal(formula, cursor)
            continue
        if character != "[":
            cursor += 1
            continue

        closing_bracket, nested = _formula_bracket_end(formula, cursor)
        if closing_bracket is None:
            return False
        token = formula[cursor + 1 : closing_bracket].strip()
        if _has_table_reference_prefix(formula, cursor):
            cursor = closing_bracket + 1
            continue

        if token and _has_active_reference_suffix(formula, closing_bracket):
            return True

        # Marker and nested brackets are structured only after ruling out an
        # active post-bracket sheet/name suffix. This prevents crafted external
        # filenames such as ``[#Book.csv]Sheet!A1`` from bypassing validation.
        if nested or token.startswith(("[", "@", "#")):
            cursor = closing_bracket + 1
            continue
        cursor = closing_bracket + 1
    return False


class TranReferenceUploadError(ValidationError):
    """Raised when a TranNNB reference upload cannot be activated safely."""


@dataclass(frozen=True, slots=True)
class TranReferenceUpload:
    """Transport-neutral workbook upload."""

    filename: str
    content_type: str | None
    stream: BinaryIO


@dataclass(frozen=True, slots=True)
class TranReferenceSnapshot:
    """One version-consistent pair of app-managed reference paths."""

    fa_gl_path: Path | None
    ccdc_path: Path | None
    status: dict[str, Any]


_IndexT = TypeVar("_IndexT", FaGlWorkbookIndex, CcdcWorkbookIndex)
_StatSignature = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _IndexCacheKey:
    """Stable identity for one managed or externally configured workbook."""

    managed: bool
    path: Path
    stat_signature: _StatSignature | None


@dataclass(frozen=True, slots=True)
class _IndexCacheEntry(Generic[_IndexT]):
    """One bounded, fully validated in-memory workbook index."""

    key: _IndexCacheKey
    index: _IndexT


class TranReferenceUploadService:
    """Validate uploads fully before switching one atomic version pointer.

    Only data below ``reference_dir/tran-versions`` is ever pruned or cleared.
    Client filenames are used solely to validate the extension and are never
    written to metadata or used as destination paths.
    """

    def __init__(self, reference_dir: str | Path) -> None:
        self.reference_dir = Path(reference_dir).expanduser().resolve()
        self.versions_dir = self.reference_dir / "tran-versions"
        self.pointer_path = self.reference_dir / "tran-current.json"
        self._lock = RLock()
        self._fa_gl_index_cache: _IndexCacheEntry[FaGlWorkbookIndex] | None = None
        self._ccdc_index_cache: _IndexCacheEntry[CcdcWorkbookIndex] | None = None
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        _private_mode(self.reference_dir, 0o700)
        _private_mode(self.versions_dir, 0o700)
        self._purge_stale_staging()

    def upload(
        self,
        fa_gl: TranReferenceUpload,
        ccdc: TranReferenceUpload | None = None,
        *,
        clear_ccdc: bool = False,
    ) -> dict[str, Any]:
        """Activate a required FA&GL and optional CCDC workbook atomically."""

        if ccdc is not None and clear_ccdc:
            raise TranReferenceUploadError(
                "ccdc_file and clear_ccdc cannot be supplied together"
            )
        with self._lock:
            previous = self.snapshot()
            version = uuid4().hex
            staging = Path(
                tempfile.mkdtemp(prefix="tran-staging-", dir=self.reference_dir)
            )
            _private_mode(staging, 0o700)
            final = self.versions_dir / version
            try:
                fa_path = self._stage_upload(staging, "fa-gl", fa_gl)
                try:
                    fa_gl_index = FaGlWorkbookIndex.from_path(fa_path)
                except (OSError, TranReferenceError, ValueError) as exc:
                    raise TranReferenceUploadError(
                        "The FA&GL workbook does not match the required four-sheet contract"
                    ) from exc

                ccdc_path: Path | None = None
                ccdc_index: CcdcWorkbookIndex | None = None
                if ccdc is not None:
                    ccdc_path = self._stage_upload(staging, "ccdc", ccdc)
                elif not clear_ccdc and previous.ccdc_path is not None:
                    ccdc_path = staging / "ccdc.xlsx"
                    shutil.copyfile(previous.ccdc_path, ccdc_path)
                    _private_mode(ccdc_path, 0o600)
                if ccdc_path is not None:
                    try:
                        ccdc_index = CcdcWorkbookIndex.from_path(ccdc_path)
                    except (OSError, TranReferenceError, ValueError) as exc:
                        raise TranReferenceUploadError(
                            "The CCDC workbook does not match the optional lookup contract"
                        ) from exc

                updated_at = datetime.now(UTC).isoformat()
                metadata = {
                    "configured": True,
                    "updated_at": updated_at,
                    "fa_gl_configured": True,
                    "ccdc_configured": ccdc_path is not None,
                }
                self._write_json(staging / "metadata.json", metadata)
                os.replace(staging, final)
                self._write_json(self.pointer_path, {"version": version})
                self._prune_versions(version)
                self._prime_managed_index_cache(final, fa_gl_index, ccdc_index)
                return metadata
            except Exception:
                if staging.exists():
                    self._remove_tree(staging)
                if final.exists() and not self._is_current_version(version):
                    self._remove_tree(final)
                raise

    def snapshot(self) -> TranReferenceSnapshot:
        """Return active app-managed paths without exposing them to HTTP callers."""

        with self._lock:
            version_dir = self._current_version_dir()
            if version_dir is None:
                return TranReferenceSnapshot(None, None, self._empty_status())
            metadata = self._metadata(version_dir)
            fa_path = version_dir / "fa-gl.xlsx"
            ccdc_path = version_dir / "ccdc.xlsx"
            if not fa_path.is_file() or fa_path.is_symlink():
                raise TranReferenceUploadError("The active FA&GL reference is unavailable")
            if metadata.get("ccdc_configured"):
                if not ccdc_path.is_file() or ccdc_path.is_symlink():
                    raise TranReferenceUploadError("The active CCDC reference is unavailable")
            else:
                ccdc_path = None
            return TranReferenceSnapshot(fa_path, ccdc_path, metadata)

    def status(self) -> dict[str, Any]:
        """Return data-minimized public status for the active managed version."""

        return dict(self.snapshot().status)

    def load_indices(
        self,
        fallback_fa_gl: str | Path | None = None,
        fallback_ccdc: str | Path | None = None,
    ) -> tuple[FaGlWorkbookIndex, CcdcWorkbookIndex | None]:
        """Return one version-consistent, cached pair of read-only indexes.

        App-managed workbooks are immutable after their atomic version switch,
        so their unique final path is their cache identity. Externally configured
        fallbacks can be replaced outside this service; those are keyed by both
        their resolved path and a file-stat signature checked before and after
        every cache fill or hit.
        """

        with self._lock:
            managed = self.snapshot()
            fa_gl_path = managed.fa_gl_path or self._optional_path(fallback_fa_gl)
            ccdc_path = managed.ccdc_path or self._optional_path(fallback_ccdc)
            if fa_gl_path is None:
                raise TranReferenceUploadError("FA&GL reference is not configured")

            fa_gl_managed = managed.fa_gl_path is not None
            ccdc_managed = managed.ccdc_path is not None
            try:
                fa_gl_key = self._index_cache_key(
                    fa_gl_path,
                    managed=fa_gl_managed,
                )
            except OSError as exc:
                raise TranReferenceUploadError(
                    "FA&GL reference is unavailable or invalid"
                ) from exc
            if ccdc_path is None:
                ccdc_key = None
            else:
                try:
                    ccdc_key = self._index_cache_key(
                        ccdc_path,
                        managed=ccdc_managed,
                    )
                except OSError as exc:
                    raise TranReferenceUploadError(
                        "CCDC reference is unavailable or invalid"
                    ) from exc

            try:
                fa_gl_index, fa_gl_cache = self._load_cached_index(
                    key=fa_gl_key,
                    cached=self._fa_gl_index_cache,
                    loader=FaGlWorkbookIndex.from_path,
                )
            except TranReferenceUploadError:
                raise
            except (OSError, TranReferenceError, ValueError) as exc:
                raise TranReferenceUploadError(
                    "FA&GL reference is unavailable or invalid"
                ) from exc
            if ccdc_path is None:
                ccdc_index = None
                ccdc_cache = None
            else:
                try:
                    assert ccdc_key is not None
                    ccdc_index, ccdc_cache = self._load_cached_index(
                        key=ccdc_key,
                        cached=self._ccdc_index_cache,
                        loader=CcdcWorkbookIndex.from_path,
                    )
                except TranReferenceUploadError:
                    raise
                except (OSError, TranReferenceError, ValueError) as exc:
                    raise TranReferenceUploadError(
                        "CCDC reference is unavailable or invalid"
                    ) from exc

            # Externally configured files can be replaced outside this service.
            # Revalidate the *pair* after both loads so one request can never
            # combine an old FA&GL index with a newer CCDC index (or vice versa).
            try:
                fa_gl_changed = not fa_gl_managed and (
                    self._index_cache_key(fa_gl_path, managed=False) != fa_gl_key
                )
                ccdc_changed = (
                    ccdc_path is not None
                    and not ccdc_managed
                    and self._index_cache_key(ccdc_path, managed=False) != ccdc_key
                )
            except OSError as exc:
                raise TranReferenceUploadError(
                    "External TranNNB reference changed while its index was being loaded"
                ) from exc
            if fa_gl_changed or ccdc_changed:
                raise TranReferenceUploadError(
                    "External TranNNB reference changed while its index was being loaded"
                )

            # Publish only after the full pair loaded successfully. A failed
            # external refresh therefore leaves the previous bounded entries
            # intact instead of exposing a mixed reference generation.
            self._fa_gl_index_cache = fa_gl_cache
            self._ccdc_index_cache = ccdc_cache
            return fa_gl_index, ccdc_index

    def clear(self) -> dict[str, int]:
        """Deactivate and delete only app-managed Tran reference versions."""

        with self._lock:
            if self.versions_dir.is_symlink():
                raise TranReferenceUploadError("Tran reference storage is unsafe")
            versions_root = self.versions_dir.resolve()
            if versions_root != self.reference_dir / "tran-versions":
                raise TranReferenceUploadError("Tran reference storage is unsafe")
            if self.pointer_path.is_symlink() or (
                self.pointer_path.exists() and not self.pointer_path.is_file()
            ):
                raise TranReferenceUploadError("Tran reference pointer is unsafe")

            status = self.status()
            removable: list[tuple[Path, bool]] = []
            for candidate in self.versions_dir.iterdir():
                if not _VERSION_RE.fullmatch(candidate.name):
                    continue
                if candidate.is_symlink():
                    removable.append((candidate, True))
                elif candidate.is_dir() and candidate.resolve().parent == versions_root:
                    removable.append((candidate, False))
                else:
                    raise TranReferenceUploadError("Tran reference storage is unsafe")

            self.pointer_path.unlink(missing_ok=True)
            self._fa_gl_index_cache = None
            self._ccdc_index_cache = None
            for candidate, is_link in removable:
                if is_link:
                    candidate.unlink(missing_ok=True)
                else:
                    self._remove_tree(candidate)
                    if candidate.exists():
                        raise TranReferenceUploadError(
                            "Could not remove app-managed Tran references"
                        )
            return {
                "tran_reference_version_count": len(removable),
                "tran_fa_gl_reference_count": int(
                    bool(status.get("fa_gl_configured"))
                ),
                "tran_ccdc_reference_count": int(
                    bool(status.get("ccdc_configured"))
                ),
            }

    @staticmethod
    def _optional_path(value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()

    @staticmethod
    def _index_cache_key(path: str | Path, *, managed: bool) -> _IndexCacheKey:
        resolved = Path(path).expanduser().resolve()
        if managed:
            return _IndexCacheKey(managed=True, path=resolved, stat_signature=None)
        stat_result = resolved.stat()
        signature: _StatSignature = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )
        return _IndexCacheKey(
            managed=False,
            path=resolved,
            stat_signature=signature,
        )

    def _load_cached_index(
        self,
        *,
        key: _IndexCacheKey,
        cached: _IndexCacheEntry[_IndexT] | None,
        loader: Callable[[str | Path], _IndexT],
    ) -> tuple[_IndexT, _IndexCacheEntry[_IndexT]]:
        if cached is not None and cached.key == key:
            return cached.index, cached

        index = loader(key.path)
        entry = _IndexCacheEntry(key=key, index=index)
        return index, entry

    def _prime_managed_index_cache(
        self,
        version_dir: Path,
        fa_gl_index: FaGlWorkbookIndex,
        ccdc_index: CcdcWorkbookIndex | None,
    ) -> None:
        """Publish the exact indexes used to validate a successful upload."""

        fa_gl_path = (version_dir / "fa-gl.xlsx").resolve()
        fa_gl_index.source_path = fa_gl_path
        self._fa_gl_index_cache = _IndexCacheEntry(
            key=self._index_cache_key(fa_gl_path, managed=True),
            index=fa_gl_index,
        )
        if ccdc_index is None:
            self._ccdc_index_cache = None
            return
        ccdc_path = (version_dir / "ccdc.xlsx").resolve()
        ccdc_index.source_path = ccdc_path
        self._ccdc_index_cache = _IndexCacheEntry(
            key=self._index_cache_key(ccdc_path, managed=True),
            index=ccdc_index,
        )

    def _stage_upload(
        self,
        staging: Path,
        role: str,
        upload: TranReferenceUpload,
    ) -> Path:
        basename = re.split(r"[/\\]", str(upload.filename or ""))[-1].strip()
        if not basename or Path(basename).suffix.casefold() != ".xlsx":
            raise TranReferenceUploadError(f"{role} file must use the .xlsx extension")
        content_type = (upload.content_type or "").partition(";")[0].strip().casefold()
        if content_type not in _MIME_TYPES:
            raise TranReferenceUploadError(
                f"{role} file content type does not match .xlsx"
            )

        destination = staging / f"{role}.xlsx"
        size = 0
        try:
            with destination.open("xb") as target:
                while chunk := upload.stream.read(64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise TranReferenceUploadError(
                            f"{role} file stream must be binary"
                        )
                    size += len(chunk)
                    if size > MAX_TRAN_REFERENCE_FILE_BYTES:
                        raise TranReferenceUploadError(
                            f"{role} file exceeds the 50 MiB per-file limit"
                        )
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        _private_mode(destination, 0o600)
        if size == 0:
            raise TranReferenceUploadError(f"{role} file is empty")
        self._preflight_xlsx(destination, role)
        return destination

    @staticmethod
    def _preflight_xlsx(path: Path, role: str) -> None:
        if not zipfile.is_zipfile(path):
            raise TranReferenceUploadError(
                f"{role} content does not match its .xlsx extension"
            )
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise TranReferenceUploadError(
                        f"{role} file is not a valid XLSX workbook"
                    )
                if len(entries) > _MAX_ARCHIVE_ENTRIES:
                    raise TranReferenceUploadError(
                        f"{role} XLSX contains too many archive entries"
                    )
                if len(names) != len(entries):
                    raise TranReferenceUploadError(
                        f"{role} XLSX contains duplicate archive entries"
                    )
                expanded = 0
                external_link_parts: dict[str, zipfile.ZipInfo] = {}
                external_link_rels_parts: dict[str, zipfile.ZipInfo] = {}
                for entry in entries:
                    normalized = entry.filename.replace("\\", "/")
                    parts = normalized.split("/")
                    if normalized.startswith("/") or ".." in parts:
                        raise TranReferenceUploadError(
                            f"{role} XLSX contains an unsafe archive path"
                        )
                    if entry.flag_bits & 0x1:
                        raise TranReferenceUploadError(
                            f"{role} XLSX must not be encrypted"
                        )
                    expanded += entry.file_size
                    if expanded > _MAX_EXPANDED_BYTES:
                        raise TranReferenceUploadError(
                            f"{role} XLSX exceeds the safe expanded-size limit"
                        )
                    if entry.file_size / max(entry.compress_size, 1) > _MAX_COMPRESSION_RATIO:
                        raise TranReferenceUploadError(
                            f"{role} XLSX contains an unsafe compression ratio"
                        )
                    folded = normalized.casefold()
                    if (
                        folded == "xl/vbaproject.bin"
                        or folded.startswith("xl/embeddings/")
                        or folded.startswith("xl/activex/")
                        or folded.startswith("xl/oleobjects/")
                    ):
                        raise TranReferenceUploadError(
                            f"{role} XLSX contains unsupported active, linked, or embedded content"
                        )
                    if folded.startswith("xl/externallinks/"):
                        link_match = _EXTERNAL_LINK_PART_RE.fullmatch(normalized)
                        rels_match = _EXTERNAL_LINK_RELS_PART_RE.fullmatch(normalized)
                        if link_match:
                            external_link_parts[link_match.group(1)] = entry
                        elif rels_match:
                            external_link_rels_parts[rels_match.group(1)] = entry
                        else:
                            raise TranReferenceUploadError(
                                f"{role} {_EXTERNAL_LINK_GUIDANCE}"
                            )
                    elif folded.endswith(".rels"):
                        TranReferenceUploadService._reject_external_relationships(
                            archive,
                            entry,
                            role,
                        )
                if external_link_parts or external_link_rels_parts:
                    TranReferenceUploadService._validate_orphan_external_books(
                        archive,
                        external_link_parts,
                        external_link_rels_parts,
                        role,
                    )
                TranReferenceUploadService._reject_external_workbook_expressions(
                    archive,
                    entries,
                    role,
                )
        except zipfile.BadZipFile as exc:
            raise TranReferenceUploadError(
                f"{role} file is not a valid XLSX workbook"
            ) from exc

    @staticmethod
    def _validate_orphan_external_books(
        archive: zipfile.ZipFile,
        link_parts: dict[str, zipfile.ZipInfo],
        rels_parts: dict[str, zipfile.ZipInfo],
        role: str,
    ) -> None:
        """Allow inert local-file link metadata while rejecting executable link use."""

        if link_parts.keys() != rels_parts.keys():
            raise TranReferenceUploadError(f"{role} {_EXTERNAL_LINK_GUIDANCE}")
        for link_number in sorted(link_parts, key=int):
            TranReferenceUploadService._validate_external_book_part(
                archive,
                link_parts[link_number],
                rels_parts[link_number],
                role,
            )

    @staticmethod
    def _validate_external_book_part(
        archive: zipfile.ZipFile,
        link_entry: zipfile.ZipInfo,
        rels_entry: zipfile.ZipInfo,
        role: str,
    ) -> None:
        unsupported = f"{role} {_EXTERNAL_LINK_GUIDANCE}"
        link_payload = TranReferenceUploadService._bounded_external_link_xml(
            archive,
            link_entry,
            _MAX_EXTERNAL_LINK_XML_BYTES,
            role,
            "definition",
        )
        rels_payload = TranReferenceUploadService._bounded_external_link_xml(
            archive,
            rels_entry,
            _MAX_EXTERNAL_LINK_RELS_BYTES,
            role,
            "relationship",
        )

        relationship_id_attribute = f"{{{_OFFICE_RELATIONSHIP_NS}}}id"
        referenced_relationships: set[str] = set()
        stack: list[str] = []
        element_count = 0
        root_child_count = 0
        try:
            for event, element in ET.iterparse(
                io.BytesIO(link_payload),
                events=("start", "end"),
            ):
                if event == "start":
                    element_count += 1
                    if element_count > _MAX_EXTERNAL_LINK_ELEMENTS:
                        raise TranReferenceUploadError(unsupported)
                    depth = len(stack) + 1
                    if depth > _MAX_EXTERNAL_LINK_DEPTH:
                        raise TranReferenceUploadError(unsupported)
                    if element.tag not in _ALLOWED_EXTERNAL_LINK_TAGS:
                        raise TranReferenceUploadError(unsupported)
                    if stack:
                        if element.tag not in _ALLOWED_EXTERNAL_LINK_CHILDREN[stack[-1]]:
                            raise TranReferenceUploadError(unsupported)
                        if len(stack) == 1:
                            root_child_count += 1
                    elif element.tag != _EXTERNAL_LINK_TAG:
                        raise TranReferenceUploadError(unsupported)
                    if len(element.attrib) > _MAX_EXTERNAL_LINK_ATTRIBUTES:
                        raise TranReferenceUploadError(unsupported)
                    if any(
                        len(str(value)) > _MAX_EXTERNAL_LINK_ATTRIBUTE_CHARS
                        for value in element.attrib.values()
                    ):
                        raise TranReferenceUploadError(unsupported)
                    relationship_id = element.attrib.get(relationship_id_attribute)
                    if relationship_id:
                        referenced_relationships.add(relationship_id)
                    stack.append(element.tag)
                    continue

                if (
                    len(element.text or "") > _MAX_EXTERNAL_LINK_TEXT_CHARS
                    or len(element.tail or "") > _MAX_EXTERNAL_LINK_TEXT_CHARS
                    or (element.text and element.text.strip())
                    or (element.tail and element.tail.strip())
                    or not stack
                    or stack[-1] != element.tag
                ):
                    raise TranReferenceUploadError(unsupported)
                stack.pop()
                element.clear()
        except ET.ParseError as exc:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an invalid external workbook definition"
            ) from exc
        if stack or root_child_count != 1 or not referenced_relationships:
            raise TranReferenceUploadError(unsupported)

        relationships_tag = f"{{{_PACKAGE_RELATIONSHIP_NS}}}Relationships"
        relationship_tag = f"{{{_PACKAGE_RELATIONSHIP_NS}}}Relationship"
        relationship_ids: set[str] = set()
        stack = []
        relationship_count = 0
        try:
            for event, relationship in ET.iterparse(
                io.BytesIO(rels_payload),
                events=("start", "end"),
            ):
                if event == "start":
                    depth = len(stack) + 1
                    if (
                        depth > 2
                        or (depth == 1 and relationship.tag != relationships_tag)
                        or (depth == 2 and relationship.tag != relationship_tag)
                        or len(relationship.attrib) > _MAX_EXTERNAL_LINK_ATTRIBUTES
                        or any(
                            len(str(value)) > _MAX_EXTERNAL_LINK_ATTRIBUTE_CHARS
                            for value in relationship.attrib.values()
                        )
                    ):
                        raise TranReferenceUploadError(unsupported)
                    stack.append(relationship.tag)
                    if depth == 1:
                        continue

                    relationship_count += 1
                    if relationship_count > _MAX_EXTERNAL_LINK_RELATIONSHIPS:
                        raise TranReferenceUploadError(unsupported)
                    relationship_id = str(relationship.attrib.get("Id") or "").strip()
                    target = str(relationship.attrib.get("Target") or "").strip()
                    try:
                        target_scheme = urlsplit(target).scheme.casefold()
                    except ValueError as exc:
                        raise TranReferenceUploadError(unsupported) from exc
                    if (
                        not relationship_id
                        or relationship_id in relationship_ids
                        or relationship.attrib.get("Type")
                        != _EXTERNAL_LINK_PATH_RELATIONSHIP
                        or relationship.attrib.get("TargetMode") != "External"
                        or target_scheme != "file"
                    ):
                        raise TranReferenceUploadError(unsupported)
                    relationship_ids.add(relationship_id)
                    continue

                if (
                    len(relationship.text or "") > _MAX_EXTERNAL_LINK_TEXT_CHARS
                    or len(relationship.tail or "") > _MAX_EXTERNAL_LINK_TEXT_CHARS
                    or (relationship.text and relationship.text.strip())
                    or (relationship.tail and relationship.tail.strip())
                    or not stack
                    or stack[-1] != relationship.tag
                ):
                    raise TranReferenceUploadError(unsupported)
                stack.pop()
                relationship.clear()
        except ET.ParseError as exc:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an invalid external workbook relationship"
            ) from exc
        if stack or relationship_ids != referenced_relationships:
            raise TranReferenceUploadError(unsupported)

    @staticmethod
    def _bounded_external_link_xml(
        archive: zipfile.ZipFile,
        entry: zipfile.ZipInfo,
        size_limit: int,
        role: str,
        label: str,
    ) -> bytes:
        if entry.file_size > size_limit:
            raise TranReferenceUploadError(
                f"{role} XLSX external workbook {label} exceeds the safe per-link size limit; "
                "remove workbook link caches and save the file again"
            )
        payload = archive.read(entry)
        if len(payload) > size_limit or _XML_DTD_RE.search(payload):
            raise TranReferenceUploadError(f"{role} {_EXTERNAL_LINK_GUIDANCE}")
        return payload

    @staticmethod
    def _reject_external_workbook_expressions(
        archive: zipfile.ZipFile,
        entries: list[zipfile.ZipInfo],
        role: str,
    ) -> None:
        for entry in entries:
            normalized = entry.filename.replace("\\", "/")
            folded = normalized.casefold()
            if (
                not folded.startswith("xl/")
                or not folded.endswith(".xml")
                or folded.startswith("xl/externallinks/")
                or folded == "xl/sharedstrings.xml"
            ):
                continue
            TranReferenceUploadService._scan_workbook_expression_part(
                archive,
                entry,
                role,
                normalized,
            )

    @staticmethod
    def _scan_workbook_expression_part(
        archive: zipfile.ZipFile,
        entry: zipfile.ZipInfo,
        role: str,
        normalized: str,
    ) -> None:
        """Stream one XML part with hard limits and inspect formula contexts only."""

        def reject_complexity() -> None:
            raise TranReferenceUploadError(
                f"{role} XLSX XML part {normalized} exceeds the safe XML complexity limit"
            )

        def reject_external_formula() -> None:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an external workbook formula or defined name in "
                f"{normalized}; break the link or replace it with its current value"
            )

        if entry.file_size > _MAX_FORMULA_XML_PART_BYTES:
            raise TranReferenceUploadError(
                f"{role} XLSX XML part {normalized} exceeds the safe per-part size limit"
            )

        depth = 0
        element_count = 0
        formula_depth: int | None = None
        formula_name: str | None = None
        formula_chunks: list[str] = []
        formula_characters = 0

        def start_element(name: str, attributes: dict[str, str]) -> None:
            nonlocal depth, element_count
            nonlocal formula_depth, formula_name, formula_chunks, formula_characters

            depth += 1
            element_count += 1
            if (
                depth > _MAX_FORMULA_XML_DEPTH
                or element_count > _MAX_FORMULA_XML_ELEMENTS
                or len(attributes) > _MAX_FORMULA_XML_ATTRIBUTES
                or sum(len(key) + len(value) for key, value in attributes.items())
                > _MAX_FORMULA_XML_ATTRIBUTE_CHARS
            ):
                reject_complexity()

            for attribute_name, value in attributes.items():
                local_attribute_name = attribute_name.rsplit("}", 1)[-1].casefold()
                if local_attribute_name not in _FORMULA_ATTRIBUTE_LOCAL_NAMES:
                    continue
                if len(value) > _MAX_FORMULA_TEXT_CHARS:
                    reject_complexity()
                if _has_external_workbook_reference(value):
                    reject_external_formula()

            local_name = name.rsplit("}", 1)[-1].casefold()
            if local_name in _FORMULA_TEXT_LOCAL_NAMES:
                if formula_depth is not None:
                    reject_complexity()
                formula_depth = depth
                formula_name = name
                formula_chunks = []
                formula_characters = 0

        def character_data(value: str) -> None:
            nonlocal formula_characters

            if formula_depth is None:
                return
            formula_characters += len(value)
            if formula_characters > _MAX_FORMULA_TEXT_CHARS:
                reject_complexity()
            formula_chunks.append(value)

        def end_element(name: str) -> None:
            nonlocal depth, formula_depth, formula_name
            nonlocal formula_chunks, formula_characters

            if formula_depth == depth:
                if name != formula_name:
                    reject_complexity()
                formula = "".join(formula_chunks)
                if _has_external_workbook_reference(formula):
                    reject_external_formula()
                formula_depth = None
                formula_name = None
                formula_chunks = []
                formula_characters = 0
            depth -= 1

        def reject_xml_declaration(*_arguments: object) -> int:
            reject_complexity()
            return 0

        parser = expat.ParserCreate(namespace_separator="}")
        parser.StartElementHandler = start_element
        parser.CharacterDataHandler = character_data
        parser.EndElementHandler = end_element
        parser.StartDoctypeDeclHandler = reject_xml_declaration
        parser.EntityDeclHandler = reject_xml_declaration
        parser.ExternalEntityRefHandler = reject_xml_declaration
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

        bytes_read = 0
        try:
            with archive.open(entry) as stream:
                while chunk := stream.read(_XML_SCAN_CHUNK_BYTES):
                    bytes_read += len(chunk)
                    if bytes_read > _MAX_FORMULA_XML_PART_BYTES:
                        raise TranReferenceUploadError(
                            f"{role} XLSX XML part {normalized} exceeds the safe per-part "
                            "size limit"
                        )
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        except expat.ExpatError as exc:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an invalid workbook expression definition in "
                f"{normalized}"
            ) from exc
        if depth != 0 or formula_depth is not None:
            reject_complexity()

    @staticmethod
    def _reject_external_relationships(
        archive: zipfile.ZipFile,
        entry: zipfile.ZipInfo,
        role: str,
    ) -> None:
        """Reject every OOXML relationship that can resolve outside the upload."""

        normalized = entry.filename.replace("\\", "/")

        def reject_complexity() -> None:
            raise TranReferenceUploadError(
                f"{role} XLSX relationship part {normalized} exceeds the safe relationship "
                "XML complexity limit"
            )

        if entry.file_size > _MAX_RELATIONSHIP_XML_BYTES:
            raise TranReferenceUploadError(
                f"{role} XLSX relationship part {normalized} exceeds the safe per-part "
                "size limit"
            )

        depth = 0
        element_count = 0
        text_characters = 0

        def start_element(_name: str, attributes: dict[str, str]) -> None:
            nonlocal depth, element_count

            depth += 1
            element_count += 1
            if (
                depth > _MAX_RELATIONSHIP_XML_DEPTH
                or element_count > _MAX_RELATIONSHIP_XML_ELEMENTS
                or len(attributes) > _MAX_RELATIONSHIP_XML_ATTRIBUTES
                or sum(len(key) + len(value) for key, value in attributes.items())
                > _MAX_RELATIONSHIP_XML_ATTRIBUTE_CHARS
            ):
                reject_complexity()

            for attribute_name, value in attributes.items():
                local_name = attribute_name.rsplit("}", 1)[-1].casefold()
                if local_name == "targetmode" and value.strip().casefold() == "external":
                    raise TranReferenceUploadError(
                        f"{role} XLSX contains an external relationship"
                    )

        def character_data(value: str) -> None:
            nonlocal text_characters

            text_characters += len(value)
            if text_characters > _MAX_RELATIONSHIP_XML_TEXT_CHARS:
                reject_complexity()

        def end_element(_name: str) -> None:
            nonlocal depth

            depth -= 1

        def reject_xml_declaration(*_arguments: object) -> int:
            reject_complexity()
            return 0

        parser = expat.ParserCreate(namespace_separator="}")
        parser.StartElementHandler = start_element
        parser.CharacterDataHandler = character_data
        parser.EndElementHandler = end_element
        parser.StartDoctypeDeclHandler = reject_xml_declaration
        parser.EntityDeclHandler = reject_xml_declaration
        parser.ExternalEntityRefHandler = reject_xml_declaration
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

        bytes_read = 0
        try:
            with archive.open(entry) as stream:
                while chunk := stream.read(_XML_SCAN_CHUNK_BYTES):
                    bytes_read += len(chunk)
                    if bytes_read > _MAX_RELATIONSHIP_XML_BYTES:
                        raise TranReferenceUploadError(
                            f"{role} XLSX relationship part {normalized} exceeds the safe "
                            "per-part size limit"
                        )
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        except expat.ExpatError as exc:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an invalid relationship definition in {normalized}"
            ) from exc
        if depth != 0:
            reject_complexity()

    @staticmethod
    def _empty_status() -> dict[str, Any]:
        return {
            "configured": False,
            "updated_at": None,
            "fa_gl_configured": False,
            "ccdc_configured": False,
        }

    def _current_version_dir(self) -> Path | None:
        if not self.pointer_path.exists():
            return None
        if self.pointer_path.is_symlink() or not self.pointer_path.is_file():
            raise TranReferenceUploadError("Tran reference pointer is invalid")
        try:
            pointer = json.loads(self.pointer_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranReferenceUploadError("Tran reference pointer is invalid") from exc
        version = pointer.get("version") if isinstance(pointer, dict) else None
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise TranReferenceUploadError("Tran reference pointer is invalid")
        candidate = self.versions_dir / version
        if candidate.is_symlink():
            raise TranReferenceUploadError("Tran reference pointer is unavailable")
        resolved = candidate.resolve()
        if resolved.parent != self.versions_dir.resolve() or not resolved.is_dir():
            raise TranReferenceUploadError("Tran reference pointer is unavailable")
        return resolved

    def _metadata(self, version_dir: Path) -> dict[str, Any]:
        try:
            metadata = json.loads((version_dir / "metadata.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranReferenceUploadError("Tran reference status is unavailable") from exc
        if not isinstance(metadata, dict) or metadata.get("configured") is not True:
            raise TranReferenceUploadError("Tran reference status is invalid")
        return metadata

    def _is_current_version(self, version: str) -> bool:
        current = self._current_version_dir()
        return current is not None and current.name == version

    def _prune_versions(self, current_version: str) -> None:
        for candidate in self.versions_dir.iterdir():
            if (
                candidate.name != current_version
                and _VERSION_RE.fullmatch(candidate.name)
                and candidate.is_dir()
                and not candidate.is_symlink()
            ):
                self._remove_tree(candidate)

    def _purge_stale_staging(self) -> None:
        for candidate in self.reference_dir.iterdir():
            if (
                candidate.is_dir()
                and not candidate.is_symlink()
                and re.fullmatch(r"tran-staging-[A-Za-z0-9_-]+", candidate.name)
            ):
                self._remove_tree(candidate)
                if candidate.exists():
                    raise TranReferenceUploadError(
                        "Could not remove stale Tran reference upload data"
                    )

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    value,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _private_mode(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_tree(self, path: Path) -> None:
        resolved = path.resolve()
        allowed_parents = {self.reference_dir, self.versions_dir.resolve()}
        if resolved.parent not in allowed_parents:
            raise RuntimeError("Refusing to remove a path outside the Tran reference area")
        shutil.rmtree(resolved, ignore_errors=True)
