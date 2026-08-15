"""Opt-in private retention for validated uploaded EML artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .email_upload_service import MAX_EMAIL_FILE_BYTES
from .ingestion_service import EmailPayload

_HANDLE_RE = re.compile(r"^eml-sha256-(?P<digest>[0-9a-f]{64})$")
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_ARTIFACT_FILE_RE = re.compile(r"^(?P<digest>[0-9a-f]{64})\.eml$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class MailArtifactError(RuntimeError):
    """Base class for private mail-artifact storage failures."""


class MailArtifactDisabledError(MailArtifactError):
    """Raised when raw EML retention was not explicitly enabled."""


class MailArtifactNotFoundError(MailArtifactError, FileNotFoundError):
    """Raised when a valid artifact handle has no stored content."""


class MailArtifactIntegrityError(MailArtifactError):
    """Raised if content-addressed storage no longer matches its handle."""


@dataclass(frozen=True, slots=True)
class MailArtifactHandle:
    """Opaque retrieval handle plus data-minimized display metadata."""

    handle: str
    safe_filename: str
    content_sha256: str
    size_bytes: int
    created: bool


def safe_eml_basename(filename: str) -> str:
    """Return a cross-platform basename without retaining a client path."""

    basename = re.split(
        r"[/\\]", unicodedata.normalize("NFKC", str(filename or ""))
    )[-1].strip()
    stem, extension = os.path.splitext(basename)
    if extension.casefold() != ".eml":
        raise MailArtifactError("A retained mail artifact must use the .eml extension")
    clean_stem = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in stem
    ).strip(" ._")
    clean_stem = re.sub(r"_+", "_", clean_stem)[:80].rstrip(" ._")
    if not clean_stem:
        clean_stem = "message"
    if clean_stem.casefold() in _WINDOWS_RESERVED_NAMES:
        clean_stem = f"message-{clean_stem}"
    return f"{clean_stem}.eml"


class MailArtifactStore:
    """Content-addressed EML storage, disabled unless explicitly configured.

    Callers must pass ``EmailPayload`` objects produced by
    ``EmailUploadService.validate``. The store applies a second size/name
    boundary before any bytes are persisted.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        enabled: bool = False,
        max_artifact_bytes: int = MAX_EMAIL_FILE_BYTES,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._root = Path(root).expanduser().resolve()
        self.enabled = enabled
        self.max_artifact_bytes = max_artifact_bytes
        if enabled:
            self._ensure_private_directory(self._root)

    @property
    def root(self) -> Path:
        return self._root

    def retain_validated(self, payload: EmailPayload) -> MailArtifactHandle:
        """Persist one validated payload atomically and return an opaque handle."""

        self._require_enabled()
        safe_filename = self._validate_payload_boundary(payload)
        digest = hashlib.sha256(payload.data).hexdigest()
        handle = f"eml-sha256-{digest}"
        shard = self._root / digest[:2]
        self._ensure_private_directory(shard)
        destination = shard / f"{digest}.eml"

        if destination.exists():
            self._verify_file(destination, digest, len(payload.data))
            return MailArtifactHandle(
                handle=handle,
                safe_filename=safe_filename,
                content_sha256=digest,
                size_bytes=len(payload.data),
                created=False,
            )

        with tempfile.NamedTemporaryFile(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=shard,
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(payload.data)
            temporary.flush()
            os.fsync(temporary.fileno())
        self._set_private_file_mode(temp_path)
        try:
            created = self._commit_content_addressed(temp_path, destination, digest)
        finally:
            temp_path.unlink(missing_ok=True)
        self._verify_file(destination, digest, len(payload.data))
        return MailArtifactHandle(
            handle=handle,
            safe_filename=safe_filename,
            content_sha256=digest,
            size_bytes=len(payload.data),
            created=created,
        )

    def retain_many(
        self, payloads: tuple[EmailPayload, ...] | list[EmailPayload]
    ) -> tuple[MailArtifactHandle, ...]:
        """Retain multiple payloads as one all-or-nothing operation.

        Every boundary is validated before persistence starts. If a later
        retain fails, only artifacts newly created by this call are removed;
        content-addressed artifacts that already existed are preserved.
        """

        self._require_enabled()
        for payload in payloads:
            self._validate_payload_boundary(payload)
        retained: list[MailArtifactHandle] = []
        try:
            for payload in payloads:
                retained.append(self.retain_validated(payload))
        except Exception as retention_error:
            rollback_error: Exception | None = None
            for artifact in reversed(retained):
                if not artifact.created:
                    continue
                try:
                    self.delete(artifact.handle)
                except Exception as error:  # pragma: no cover - defensive I/O path
                    rollback_error = rollback_error or error
            if rollback_error is not None:
                raise MailArtifactError(
                    "Mail artifact retention failed and rollback was incomplete"
                ) from retention_error
            raise
        return tuple(retained)

    def resolve(self, handle: str) -> Path:
        """Resolve and integrity-check an opaque handle inside the private root."""

        self._require_enabled()
        match = _HANDLE_RE.fullmatch(str(handle))
        if match is None:
            raise MailArtifactNotFoundError("Invalid mail artifact handle")
        digest = match.group("digest")
        path = self._root / digest[:2] / f"{digest}.eml"
        if not path.is_file():
            raise MailArtifactNotFoundError("Mail artifact is unavailable")
        if path.is_symlink() or not path.resolve().is_relative_to(self._root):
            raise MailArtifactIntegrityError("Mail artifact escaped its private storage root")
        self._verify_file(path, digest, None)
        return path

    def read_bytes(self, handle: str) -> bytes:
        """Retrieve validated bytes for an internal converter or download service."""

        return self.resolve(handle).read_bytes()

    def delete(self, handle: str) -> bool:
        """Delete exactly one app-managed artifact selected by an opaque handle."""

        self._require_enabled()
        match = _HANDLE_RE.fullmatch(str(handle))
        if match is None:
            raise MailArtifactNotFoundError("Invalid mail artifact handle")
        digest = match.group("digest")
        shard = self._root / digest[:2]
        path = shard / f"{digest}.eml"
        if not path.exists():
            return False
        if path.is_symlink() or not path.resolve().is_relative_to(self._root):
            raise MailArtifactIntegrityError("Mail artifact escaped its private storage root")
        path.unlink()
        with suppress(OSError):
            shard.rmdir()
        return True

    def clear_managed(self) -> int:
        """Delete only hash-named artifacts created by this store.

        Unknown files, symlinks and directories are deliberately preserved.
        This narrow cleanup contract is suitable for an explicitly authorized
        disposable-test reset; it is never invoked automatically. Cleanup is
        allowed while new retention is disabled so a deployment can remove
        artifacts left from an earlier opt-in configuration.
        """

        if not self._root.exists():
            return 0
        if self._root.is_symlink() or not self._root.is_dir():
            raise MailArtifactIntegrityError("Private mail storage root is invalid")
        removed = 0
        for shard in self._root.iterdir():
            if (
                shard.is_symlink()
                or not shard.is_dir()
                or _SHARD_RE.fullmatch(shard.name) is None
            ):
                continue
            for artifact in shard.iterdir():
                match = _ARTIFACT_FILE_RE.fullmatch(artifact.name)
                if (
                    match is None
                    or match.group("digest")[:2] != shard.name
                    or artifact.is_symlink()
                    or not artifact.is_file()
                    or not artifact.resolve().is_relative_to(self._root)
                ):
                    continue
                artifact.unlink()
                removed += 1
            with suppress(OSError):
                shard.rmdir()
        return removed

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise MailArtifactDisabledError(
                "Raw EML retention is disabled; enable a private artifact store explicitly"
            )

    def _validate_payload_boundary(self, payload: EmailPayload) -> str:
        safe_filename = safe_eml_basename(payload.filename)
        if not payload.data:
            raise MailArtifactError("A retained mail artifact cannot be empty")
        if len(payload.data) > self.max_artifact_bytes:
            raise MailArtifactError("Mail artifact exceeds the configured retention limit")
        return safe_filename

    def _ensure_private_directory(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve()
        if not resolved.is_relative_to(self._root) and resolved != self._root:
            raise MailArtifactIntegrityError("Artifact storage escaped its configured root")
        with suppress(OSError):
            directory.chmod(0o700)

    @staticmethod
    def _set_private_file_mode(path: Path) -> None:
        with suppress(OSError):
            path.chmod(0o600)

    def _verify_file(self, path: Path, digest: str, expected_size: int | None) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self._root):
            raise MailArtifactIntegrityError("Stored mail artifact escaped its private root")
        actual_size = path.stat().st_size
        if actual_size > self.max_artifact_bytes:
            raise MailArtifactIntegrityError("Stored mail artifact exceeds its size boundary")
        if expected_size is not None and actual_size != expected_size:
            raise MailArtifactIntegrityError("Stored mail artifact has an unexpected size")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                hasher.update(chunk)
        actual_digest = hasher.hexdigest()
        if actual_digest != digest:
            raise MailArtifactIntegrityError("Stored mail artifact failed integrity validation")

    def _commit_content_addressed(
        self, temp_path: Path, destination: Path, digest: str
    ) -> bool:
        try:
            os.link(temp_path, destination)
            self._set_private_file_mode(destination)
            return True
        except FileExistsError:
            self._verify_file(destination, digest, temp_path.stat().st_size)
            return False
        except OSError:
            created = False
            try:
                with temp_path.open("rb") as source, destination.open("xb") as target:
                    created = True
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                self._set_private_file_mode(destination)
                return True
            except FileExistsError:
                self._verify_file(destination, digest, temp_path.stat().st_size)
                return False
            except Exception:
                if created:
                    destination.unlink(missing_ok=True)
                raise
