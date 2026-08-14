"""Validate and atomically activate uploaded Supplier reference directories."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4

from asset_compensation.domain import ValidationError
from asset_compensation.parsers import SupplierLoadError, SupplierRecord, load_supplier_records

MAX_SUPPLIER_FILE_BYTES = 20 * 1024 * 1024
MAX_SUPPLIER_TOTAL_BYTES = 48 * 1024 * 1024
_MAX_XLSX_EXPANDED_BYTES = 100 * 1024 * 1024
_VERSION_RE = re.compile(r"[0-9a-f]{32}")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FORMATS = frozenset({".csv", ".xls", ".xlsx"})
_MIME_TYPES = {
    ".csv": frozenset(
        {
            "",
            "application/csv",
            "application/octet-stream",
            "application/vnd.ms-excel",
            "text/csv",
            "text/plain",
        }
    ),
    ".xls": frozenset(
        {
            "",
            "application/octet-stream",
            "application/vnd.ms-excel",
            "text/html",
        }
    ),
    ".xlsx": frozenset(
        {
            "",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        }
    ),
}


def _best_effort_chmod(path: Path, mode: int) -> None:
    with suppress(OSError):
        path.chmod(mode)


class SupplierUploadError(ValidationError):
    """Raised when an uploaded supplier pair cannot be safely activated."""


@dataclass(frozen=True, slots=True)
class SupplierUpload:
    """Transport-neutral upload supplied by the HTTP adapter."""

    filename: str
    content_type: str | None
    stream: BinaryIO


@dataclass(frozen=True, slots=True)
class SupplierCollision:
    """One normalized employee domain that cannot be mapped unambiguously."""

    domain: str
    supplier_numbers: tuple[str, ...]
    supplier_sites: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "supplier_numbers": list(self.supplier_numbers),
            "supplier_sites": list(self.supplier_sites),
            "message": (
                "Domain maps to conflicting Supplier Number/Employee Number/"
                "Site/name/status identities"
            ),
        }


@dataclass(frozen=True, slots=True)
class SupplierUploadResult:
    active_count: int
    inactive_count: int
    records: tuple[SupplierRecord, ...]
    collisions: tuple[SupplierCollision, ...]
    warnings: tuple[str, ...]
    imported_count: int
    updated_count: int
    unchanged_count: int
    removed_count: int
    updated_at: str

    def status_dict(self) -> dict[str, Any]:
        return {
            "configured": True,
            "updated_at": self.updated_at,
            "active_count": self.active_count,
            "inactive_count": self.inactive_count,
            "total_count": len(self.records),
            "collision_count": len(self.collisions),
            "review_required": bool(self.collisions),
        }

    def to_dict(self) -> dict[str, Any]:
        status = self.status_dict()
        return {
            **status,
            "status": status,
            "imported_count": self.imported_count,
            "updated_count": self.updated_count,
            "unchanged_count": self.unchanged_count,
            "removed_count": self.removed_count,
            "collisions": [collision.to_dict() for collision in self.collisions],
            "warnings": list(self.warnings),
        }


class SupplierUploadService:
    """Own safe supplier upload storage and the active-directory pointer."""

    def __init__(self, reference_dir: str | Path) -> None:
        self.reference_dir = Path(reference_dir).resolve()
        self.versions_dir = self.reference_dir / "supplier-versions"
        self.pointer_path = self.reference_dir / "supplier-current.json"
        self._lock = RLock()
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        _best_effort_chmod(self.reference_dir, 0o700)
        _best_effort_chmod(self.versions_dir, 0o700)
        self._purge_stale_staging()

    def upload(self, active: SupplierUpload, inactive: SupplierUpload) -> SupplierUploadResult:
        """Validate both files completely, then switch one atomic version pointer."""

        with self._lock:
            previous = self.load_directory()
            version = uuid4().hex
            staging = Path(tempfile.mkdtemp(prefix="supplier-staging-", dir=self.reference_dir))
            _best_effort_chmod(staging, 0o700)
            final = self.versions_dir / version
            try:
                active_path, active_size = self._stage_file(staging, "active", active)
                inactive_path, inactive_size = self._stage_file(
                    staging, "inactive", inactive
                )
                if active_size + inactive_size > MAX_SUPPLIER_TOTAL_BYTES:
                    raise SupplierUploadError("Supplier upload exceeds the 48 MiB total limit")
                active_records, active_warnings = self._load_records(active_path, active=True)
                inactive_records, inactive_warnings = self._load_records(
                    inactive_path, active=False
                )
                records, collisions, merge_warnings = self._merge(
                    [*active_records, *inactive_records]
                )
                warnings = [*active_warnings, *inactive_warnings, *merge_warnings]
                # Raw Oracle exports contain unnecessary bank/tax/address fields.
                # Retain only the data-minimized normalized directory.
                active_path.unlink()
                inactive_path.unlink()
                self._write_normalized(staging / "directory.csv", records)
                updated_at = datetime.now(UTC).isoformat()
                imported, updated, unchanged, removed = self._changes(previous, records)
                metadata = {
                    "configured": True,
                    "version": version,
                    "updated_at": updated_at,
                    "active_count": len(active_records),
                    "inactive_count": len(inactive_records),
                    "total_count": len(records),
                    "collision_count": len(collisions),
                    "review_required": bool(collisions),
                    "collisions": [item.to_dict() for item in collisions],
                    "warnings": list(warnings),
                }
                self._write_json(staging / "metadata.json", metadata)
                os.replace(staging, final)
                self._write_json(self.pointer_path, {"version": version})
                self._prune_versions(version)
            except Exception:
                self._remove_staging(staging)
                if final.exists() and not self._is_current_version(version):
                    self._remove_staging(final)
                raise

            return SupplierUploadResult(
                active_count=len(active_records),
                inactive_count=len(inactive_records),
                records=tuple(records),
                collisions=tuple(collisions),
                warnings=tuple(warnings),
                imported_count=imported,
                updated_count=updated,
                unchanged_count=unchanged,
                removed_count=removed,
                updated_at=updated_at,
            )

    def load_directory(self) -> dict[str, SupplierRecord]:
        """Load the currently activated normalized directory without a process restart."""

        with self._lock:
            version_dir = self._current_version_dir()
            if version_dir is None:
                return {}
            source = version_dir / "directory.csv"
            try:
                records = load_supplier_records(source)
            except (OSError, SupplierLoadError) as exc:
                raise SupplierUploadError(
                    "The active supplier directory is unavailable or invalid"
                ) from exc
            return {record.domain: record for record in records}

    def status(self) -> dict[str, Any]:
        with self._lock:
            version_dir = self._current_version_dir()
            if version_dir is None:
                return {
                    "configured": False,
                    "updated_at": None,
                    "active_count": 0,
                    "inactive_count": 0,
                    "total_count": 0,
                    "collision_count": 0,
                    "review_required": False,
                    "collisions": [],
                    "warnings": [],
                }
            try:
                metadata = json.loads((version_dir / "metadata.json").read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SupplierUploadError("Supplier upload status is unavailable") from exc
            if not isinstance(metadata, dict):
                raise SupplierUploadError("Supplier upload status is invalid")
            return metadata

    def snapshot(
        self,
    ) -> tuple[dict[str, SupplierRecord], frozenset[str], dict[str, Any]]:
        """Return one version-consistent lookup, collision set and status snapshot."""

        with self._lock:
            status = self.status()
            directory = self.load_directory() if status["configured"] else {}
            collisions = status.get("collisions", [])
            ambiguous = frozenset(
                str(item.get("domain") or "")
                for item in collisions
                if isinstance(item, dict) and item.get("domain")
            )
            return directory, ambiguous, status

    def clear(self) -> dict[str, int]:
        """Deactivate and remove only app-managed normalized supplier versions."""

        with self._lock:
            if self.versions_dir.is_symlink():
                raise SupplierUploadError("Supplier version storage is unsafe")
            versions_root = self.versions_dir.resolve()
            expected_root = self.reference_dir / "supplier-versions"
            if versions_root != expected_root:
                raise SupplierUploadError("Supplier version storage is unsafe")
            if self.pointer_path.is_symlink() or (
                self.pointer_path.exists() and not self.pointer_path.is_file()
            ):
                raise SupplierUploadError("Supplier directory pointer is unsafe")

            status = self.status()
            removable: list[tuple[Path, bool]] = []
            for candidate in self.versions_dir.iterdir():
                if not _VERSION_RE.fullmatch(candidate.name):
                    continue
                if candidate.is_symlink():
                    removable.append((candidate, True))
                    continue
                if not candidate.is_dir() or candidate.resolve().parent != versions_root:
                    raise SupplierUploadError("Supplier version storage is unsafe")
                removable.append((candidate, False))

            # Removing the pointer first atomically stops new readers from using a
            # version while its private normalized files are being removed.
            self.pointer_path.unlink(missing_ok=True)
            for candidate, is_link in removable:
                if is_link:
                    candidate.unlink(missing_ok=True)
                else:
                    self._remove_staging(candidate)
                    if candidate.exists():
                        raise SupplierUploadError(
                            "Could not remove normalized supplier reference data"
                        )

            return {
                "supplier_record_count": int(status.get("total_count") or 0),
                "supplier_collision_count": int(status.get("collision_count") or 0),
                "supplier_version_count": len(removable),
            }

    def _stage_file(
        self,
        staging: Path,
        role: str,
        upload: SupplierUpload,
    ) -> tuple[Path, int]:
        basename = re.split(r"[/\\]", str(upload.filename or ""))[-1]
        suffix = Path(basename).suffix.casefold()
        if not basename or suffix not in _FORMATS:
            raise SupplierUploadError(
                f"{role} file must use one of: .csv, .xls, .xlsx"
            )
        content_type = (upload.content_type or "").partition(";")[0].strip().casefold()
        if content_type not in _MIME_TYPES[suffix]:
            raise SupplierUploadError(f"{role} file content type does not match {suffix}")

        destination = staging / f"{role}{suffix}"
        size = 0
        with destination.open("xb") as target:
            while chunk := upload.stream.read(64 * 1024):
                if not isinstance(chunk, bytes):
                    raise SupplierUploadError(f"{role} file stream must be binary")
                size += len(chunk)
                if size > MAX_SUPPLIER_FILE_BYTES:
                    raise SupplierUploadError(
                        f"{role} file exceeds the 20 MiB per-file limit"
                    )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        _best_effort_chmod(destination, 0o600)
        if size == 0:
            raise SupplierUploadError(f"{role} file is empty")
        self._validate_content(destination, suffix, role)
        return destination, size

    @staticmethod
    def _validate_content(path: Path, suffix: str, role: str) -> None:
        with path.open("rb") as stream:
            prefix = stream.read(65536)
        if suffix == ".csv":
            if b"\x00" in prefix:
                raise SupplierUploadError(f"{role} CSV contains binary content")
            try:
                decoded_prefix = prefix.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise SupplierUploadError(f"{role} CSV must use UTF-8 encoding") from exc
            if decoded_prefix.lstrip().casefold().startswith(("<html", "<!doctype html")):
                raise SupplierUploadError(f"{role} content does not match its .csv extension")
            return
        if suffix == ".xls":
            try:
                folded = prefix.decode("utf-8-sig", errors="strict").lstrip().casefold()
            except UnicodeDecodeError as exc:
                raise SupplierUploadError(f"{role} .xls must use UTF-8 encoding") from exc
            if not folded.startswith(("<html", "<!doctype html")):
                raise SupplierUploadError(
                    f"{role} .xls must be an Oracle BI Publisher HTML export"
                )
            if "oracle bi publisher" not in folded:
                raise SupplierUploadError(
                    f"{role} .xls is missing the Oracle BI Publisher signature"
                )
            return

        if not zipfile.is_zipfile(path):
            raise SupplierUploadError(f"{role} content does not match its .xlsx extension")
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                names = {item.filename for item in entries}
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise SupplierUploadError(f"{role} file is not a valid XLSX workbook")
                if len(entries) > 1000:
                    raise SupplierUploadError(f"{role} XLSX contains too many archive entries")
                if len(names) != len(entries):
                    raise SupplierUploadError(f"{role} XLSX contains duplicate archive entries")
                for item in entries:
                    normalized = item.filename.replace("\\", "/")
                    parts = normalized.split("/")
                    if normalized.startswith("/") or ".." in parts:
                        raise SupplierUploadError(
                            f"{role} XLSX contains an unsafe archive path"
                        )
                    if item.flag_bits & 0x1:
                        raise SupplierUploadError(f"{role} XLSX must not be encrypted")
                    if item.file_size > 0:
                        ratio = item.file_size / max(item.compress_size, 1)
                        if ratio > 200:
                            raise SupplierUploadError(
                                f"{role} XLSX contains an unsafe compression ratio"
                            )
                    folded_name = normalized.casefold()
                    if (
                        folded_name == "xl/vbaproject.bin"
                        or folded_name.startswith("xl/externallinks/")
                        or folded_name.startswith("xl/embeddings/")
                    ):
                        raise SupplierUploadError(
                            f"{role} XLSX contains unsupported active or embedded content"
                        )
                expanded = sum(item.file_size for item in entries)
                if expanded > _MAX_XLSX_EXPANDED_BYTES:
                    raise SupplierUploadError(
                        f"{role} XLSX exceeds the safe expanded-size limit"
                    )
        except zipfile.BadZipFile as exc:
            raise SupplierUploadError(f"{role} file is not a valid XLSX workbook") from exc

    @staticmethod
    def _load_records(
        path: Path, *, active: bool
    ) -> tuple[list[SupplierRecord], list[str]]:
        warnings: list[str] = []
        try:
            records = load_supplier_records(
                path,
                active_override=active,
                reject_duplicate_domains=False,
                skip_unresolved_domains=True,
                warnings=warnings,
            )
        except Exception as exc:  # third-party workbook parsers raise varied safe failures
            raise SupplierUploadError(
                f"Could not parse the {'active' if active else 'inactive'} supplier file"
            ) from exc
        if not records:
            raise SupplierUploadError(
                f"The {'active' if active else 'inactive'} supplier file has no data rows"
            )
        for record in records:
            SupplierUploadService._validate_record(record)
        role = "Active" if active else "Inactive"
        return records, [f"{role}: {warning}" for warning in warnings]

    @staticmethod
    def _validate_record(record: SupplierRecord) -> None:
        fields = {
            "domain": record.domain,
            "employee number": str(record.metadata.get("employee_number") or ""),
            "supplier number": record.supplier_number,
            "supplier site": record.supplier_site,
            "supplier name": record.supplier_name or "",
        }
        limits = {
            "domain": 64,
            "employee number": 100,
            "supplier number": 100,
            "supplier site": 100,
            "supplier name": 300,
        }
        for name, value in fields.items():
            if len(value) > limits[name]:
                raise SupplierUploadError(f"Supplier {name} exceeds the allowed length")
            if any(ord(char) < 32 and char not in "\t" for char in value):
                raise SupplierUploadError(f"Supplier {name} contains control characters")
        if not _DOMAIN_RE.fullmatch(record.domain):
            raise SupplierUploadError("Supplier domain contains invalid characters")

    @staticmethod
    def _merge(
        records: list[SupplierRecord],
    ) -> tuple[list[SupplierRecord], list[SupplierCollision], list[str]]:
        grouped: dict[str, list[SupplierRecord]] = defaultdict(list)
        for record in records:
            grouped[record.domain].append(record)

        resolved: list[SupplierRecord] = []
        collisions: list[SupplierCollision] = []
        duplicate_count = 0
        for domain in sorted(grouped):
            candidates = grouped[domain]
            identities = {
                (
                    item.supplier_number,
                    str(item.metadata.get("employee_number") or ""),
                    item.supplier_site,
                    item.supplier_name or "",
                    item.active,
                )
                for item in candidates
            }
            if len(identities) > 1:
                collisions.append(
                    SupplierCollision(
                        domain=domain,
                        supplier_numbers=tuple(
                            sorted({item.supplier_number for item in candidates})
                        ),
                        supplier_sites=tuple(sorted({item.supplier_site for item in candidates})),
                    )
                )
                continue
            if len(candidates) > 1:
                duplicate_count += len(candidates) - 1
            selected = next((item for item in candidates if item.active is True), candidates[0])
            resolved.append(selected)

        warnings: list[str] = []
        if duplicate_count:
            warnings.append(f"Collapsed {duplicate_count} exact duplicate supplier rows")
        if collisions:
            warnings.append(
                f"Excluded {len(collisions)} ambiguous domains pending manual review"
            )
        return resolved, collisions, warnings

    @staticmethod
    def _write_normalized(path: Path, records: list[SupplierRecord]) -> None:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                ["Domain", "Supplier Number", "Supplier Site", "Supplier Name", "Active"]
            )
            for record in records:
                writer.writerow(
                    [
                        record.domain,
                        record.supplier_number,
                        record.supplier_site,
                        record.supplier_name or "",
                        "true" if record.active else "false",
                    ]
                )
            stream.flush()
            os.fsync(stream.fileno())
        _best_effort_chmod(path, 0o600)

    @staticmethod
    def _record_signature(record: SupplierRecord) -> tuple[object, ...]:
        return (
            record.supplier_number,
            record.supplier_site,
            record.supplier_name,
            record.active,
        )

    @classmethod
    def _changes(
        cls,
        previous: dict[str, SupplierRecord],
        records: list[SupplierRecord],
    ) -> tuple[int, int, int, int]:
        current = {record.domain: record for record in records}
        imported = len(current.keys() - previous.keys())
        removed = len(previous.keys() - current.keys())
        updated = sum(
            cls._record_signature(current[key]) != cls._record_signature(previous[key])
            for key in current.keys() & previous.keys()
        )
        unchanged = len(current.keys() & previous.keys()) - updated
        return imported, updated, unchanged, removed

    def _current_version_dir(self) -> Path | None:
        if not self.pointer_path.is_file():
            return None
        try:
            pointer = json.loads(self.pointer_path.read_text("utf-8"))
            version = pointer.get("version") if isinstance(pointer, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            raise SupplierUploadError("Supplier directory pointer is invalid") from exc
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise SupplierUploadError("Supplier directory pointer is invalid")
        candidate = (self.versions_dir / version).resolve()
        if candidate.parent != self.versions_dir.resolve() or not candidate.is_dir():
            raise SupplierUploadError("Supplier directory pointer is unavailable")
        return candidate

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
                self._remove_staging(candidate)

    def _purge_stale_staging(self) -> None:
        for candidate in self.reference_dir.iterdir():
            if (
                candidate.is_dir()
                and not candidate.is_symlink()
                and re.fullmatch(r"supplier-staging-[A-Za-z0-9_-]+", candidate.name)
            ):
                self._remove_staging(candidate)
                if candidate.exists():
                    raise SupplierUploadError("Could not remove stale supplier upload data")

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _best_effort_chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_staging(self, path: Path) -> None:
        resolved = path.resolve()
        allowed_parents = {self.reference_dir, self.versions_dir.resolve()}
        if resolved.parent not in allowed_parents:
            raise RuntimeError("Refusing to remove a path outside the supplier reference area")
        shutil.rmtree(resolved, ignore_errors=True)
