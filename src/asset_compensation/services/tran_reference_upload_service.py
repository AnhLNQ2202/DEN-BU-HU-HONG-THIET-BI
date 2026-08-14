"""Safe, atomic storage for uploaded TranNNB reference workbooks."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4

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
_VERSION_RE = re.compile(r"[0-9a-f]{32}")
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
                    FaGlWorkbookIndex.from_path(fa_path)
                except (OSError, TranReferenceError, ValueError) as exc:
                    raise TranReferenceUploadError(
                        "The FA&GL workbook does not match the required four-sheet contract"
                    ) from exc

                ccdc_path: Path | None = None
                if ccdc is not None:
                    ccdc_path = self._stage_upload(staging, "ccdc", ccdc)
                elif not clear_ccdc and previous.ccdc_path is not None:
                    ccdc_path = staging / "ccdc.xlsx"
                    shutil.copyfile(previous.ccdc_path, ccdc_path)
                    _private_mode(ccdc_path, 0o600)
                if ccdc_path is not None:
                    try:
                        CcdcWorkbookIndex.from_path(ccdc_path)
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
                        or folded.startswith("xl/externallinks/")
                        or folded.startswith("xl/embeddings/")
                        or folded.startswith("xl/activex/")
                        or folded.startswith("xl/oleobjects/")
                    ):
                        raise TranReferenceUploadError(
                            f"{role} XLSX contains unsupported active, linked, or embedded content"
                        )
                    if folded.endswith(".rels"):
                        TranReferenceUploadService._reject_external_relationships(
                            archive,
                            entry,
                            role,
                        )
        except zipfile.BadZipFile as exc:
            raise TranReferenceUploadError(
                f"{role} file is not a valid XLSX workbook"
            ) from exc

    @staticmethod
    def _reject_external_relationships(
        archive: zipfile.ZipFile,
        entry: zipfile.ZipInfo,
        role: str,
    ) -> None:
        """Reject every OOXML relationship that can resolve outside the upload."""

        try:
            with archive.open(entry) as relationship_stream:
                for _, element in ET.iterparse(relationship_stream, events=("end",)):
                    attributes = {
                        key.rsplit("}", 1)[-1].casefold(): str(value).strip().casefold()
                        for key, value in element.attrib.items()
                    }
                    if attributes.get("targetmode") == "external":
                        raise TranReferenceUploadError(
                            f"{role} XLSX contains an external relationship"
                        )
                    element.clear()
        except ET.ParseError as exc:
            raise TranReferenceUploadError(
                f"{role} XLSX contains an invalid relationship definition"
            ) from exc

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
