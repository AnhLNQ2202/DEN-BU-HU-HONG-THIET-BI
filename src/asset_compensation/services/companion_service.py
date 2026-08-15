"""Short-lived, data-minimized pairing for trusted desktop companions.

The Outlook add-in and Windows bridge cannot reuse the dashboard's HTTP Basic
credentials safely.  This module therefore issues a one-time human pairing
code and exchanges it for an opaque, role-bound bearer token.  Raw codes and
tokens are returned once and are never retained by the service.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

PAIRING_TTL_SECONDS = 5 * 60
SESSION_TTL_SECONDS = 8 * 60 * 60
DRAFT_PACKAGE_TTL_SECONDS = 30 * 60
MAX_WORKBOOK_BYTES = 25 * 1024 * 1024

_ROLES = frozenset({"ngan", "tran"})
_CLIENT_TYPES = frozenset({"outlook_addin", "local_bridge"})
_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PAIRING_CODE_RE = re.compile(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}")
_ARTIFACT_HANDLE_RE = re.compile(r"eml-sha256-[0-9a-f]{64}")
_PACKAGE_ID_RE = re.compile(r"[0-9a-f]{32}")
_WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_MAX_BODY_HTML_CHARS = 1_000_000


class CompanionError(RuntimeError):
    """Base class for companion pairing and queue failures."""


class CompanionAuthenticationError(CompanionError):
    """Raised when a pairing code or bearer token is invalid or expired."""


class CompanionAuthorizationError(CompanionError):
    """Raised when a valid client is not authorized for an operation."""


class CompanionPackageNotFoundError(CompanionError):
    """Raised when a draft package is unavailable to the current client."""


class CompanionPackageError(CompanionError):
    """Raised when a package violates the private managed-output boundary."""


@dataclass(frozen=True, slots=True)
class CompanionPairing:
    code: str
    role: str
    client_type: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class CompanionPrincipal:
    """Non-secret identity derived from an authenticated bearer token."""

    session_id: str
    role: str
    client_type: str


@dataclass(frozen=True, slots=True)
class CompanionExchange:
    token: str
    principal: CompanionPrincipal
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class CompanionDraftPackage:
    package_id: str
    artifact_handle: str
    asset_count: int
    body_html: str
    workbook_path: Path
    workbook_filename: str
    workbook_content_type: str
    expires_in_seconds: int

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.package_id,
            "asset_count": self.asset_count,
            "source_eml_handle": self.artifact_handle,
            "workbook_filename": self.workbook_filename,
            "expires_in_seconds": self.expires_in_seconds,
            "sent": False,
        }


@dataclass(slots=True)
class _PairingRecord:
    digest: bytes
    role: str
    client_type: str
    created_at: float
    expires_at: float


@dataclass(slots=True)
class _SessionRecord:
    digest: bytes
    session_id: str
    role: str
    client_type: str
    created_at: float
    expires_at: float
    uploaded_handles: dict[str, float] = field(default_factory=dict)
    acknowledged_packages: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _PackageRecord:
    package_id: str
    artifact_handle: str
    asset_count: int
    body_html: str
    workbook_path: Path
    workbook_filename: str
    workbook_content_type: str
    created_at: float
    expires_at: float


class CompanionService:
    """In-memory pairing/session store and role-bound draft package queue.

    Restarting the web process intentionally invalidates every code and token.
    This keeps the staging feature fail-closed without introducing a new
    long-lived credential database.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        pairing_ttl_seconds: int = PAIRING_TTL_SECONDS,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
        package_ttl_seconds: int = DRAFT_PACKAGE_TTL_SECONDS,
        max_pairings: int = 64,
        max_sessions: int = 128,
        max_packages: int = 256,
        max_handles_per_session: int = 256,
    ) -> None:
        if min(
            pairing_ttl_seconds,
            session_ttl_seconds,
            package_ttl_seconds,
            max_pairings,
            max_sessions,
            max_packages,
            max_handles_per_session,
        ) < 1:
            raise ValueError("Companion limits and TTLs must be positive")
        self._clock = clock
        self._pairing_ttl = pairing_ttl_seconds
        self._session_ttl = session_ttl_seconds
        self._package_ttl = package_ttl_seconds
        self._max_pairings = max_pairings
        self._max_sessions = max_sessions
        self._max_packages = max_packages
        self._max_handles_per_session = max_handles_per_session
        self._pepper = secrets.token_bytes(32)
        self._pairings: dict[bytes, _PairingRecord] = {}
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._packages: dict[str, _PackageRecord] = {}
        self._lock = RLock()

    def create_pairing(self, role: str, client_type: str) -> CompanionPairing:
        normalized_role = str(role or "").strip().casefold()
        normalized_client = str(client_type or "").strip().casefold()
        if normalized_role not in _ROLES:
            raise CompanionAuthorizationError("Companion role is invalid")
        if normalized_client not in _CLIENT_TYPES:
            raise CompanionAuthorizationError("Companion client type is invalid")
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._make_room(self._pairings, self._max_pairings)
            # Collision handling is deliberately bounded; 60 bits of human-code
            # entropy makes the retry path vanishingly unlikely.
            for _ in range(4):
                raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(12))
                code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
                digest = self._digest(code)
                if digest not in self._pairings:
                    break
            else:  # pragma: no cover - cryptographic collision guard
                raise CompanionError("Could not allocate a pairing code")
            self._pairings[digest] = _PairingRecord(
                digest=digest,
                role=normalized_role,
                client_type=normalized_client,
                created_at=now,
                expires_at=now + self._pairing_ttl,
            )
        return CompanionPairing(
            code=code,
            role=normalized_role,
            client_type=normalized_client,
            expires_in_seconds=self._pairing_ttl,
        )

    def exchange(self, code: str) -> CompanionExchange:
        normalized = str(code or "").strip().upper()
        if _PAIRING_CODE_RE.fullmatch(normalized) is None:
            # Hash even malformed input so invalid formats do not skip the
            # secret-dependent comparison path entirely.
            now = self._clock()
            with self._lock:
                self._prune(now)
                self._constant_time_lookup(self._pairings, self._digest(normalized))
            raise CompanionAuthenticationError("Pairing code is invalid or expired")
        now = self._clock()
        candidate = self._digest(normalized)
        with self._lock:
            self._prune(now)
            record = self._constant_time_lookup(self._pairings, candidate)
            if record is None or record.expires_at <= now:
                raise CompanionAuthenticationError("Pairing code is invalid or expired")
            # Consume before issuing: the human code is strictly one-time.
            del self._pairings[record.digest]
            self._make_room(self._sessions, self._max_sessions)
            token = secrets.token_urlsafe(32)
            token_digest = self._digest(token)
            session_id = secrets.token_hex(16)
            session = _SessionRecord(
                digest=token_digest,
                session_id=session_id,
                role=record.role,
                client_type=record.client_type,
                created_at=now,
                expires_at=now + self._session_ttl,
            )
            self._sessions[token_digest] = session
        return CompanionExchange(
            token=token,
            principal=self._principal(session),
            expires_in_seconds=self._session_ttl,
        )

    def authenticate(self, token: str) -> CompanionPrincipal:
        raw = str(token or "")
        candidate = self._digest(raw)
        now = self._clock()
        with self._lock:
            self._prune(now)
            record = self._constant_time_lookup(self._sessions, candidate)
            if not raw or record is None or record.expires_at <= now:
                raise CompanionAuthenticationError("Bearer token is invalid or expired")
            return self._principal(record)

    def record_uploaded_handle(
        self, principal: CompanionPrincipal, artifact_handle: str
    ) -> None:
        handle = str(artifact_handle or "")
        if _ARTIFACT_HANDLE_RE.fullmatch(handle) is None:
            raise CompanionPackageError("Mail artifact handle is invalid")
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._session_for_principal(principal)
            session.uploaded_handles.pop(handle, None)
            if len(session.uploaded_handles) >= self._max_handles_per_session:
                oldest = min(
                    session.uploaded_handles,
                    key=session.uploaded_handles.__getitem__,
                )
                del session.uploaded_handles[oldest]
            session.uploaded_handles[handle] = now

    def register_draft_package(
        self,
        *,
        package_id: str,
        artifact_handle: str,
        asset_count: int,
        body_html: str,
        workbook_path: Path,
        output_root: Path,
    ) -> CompanionDraftPackage:
        identifier = str(package_id or "")
        handle = str(artifact_handle or "")
        if _PACKAGE_ID_RE.fullmatch(identifier) is None:
            raise CompanionPackageError("Draft package id is invalid")
        if _ARTIFACT_HANDLE_RE.fullmatch(handle) is None:
            raise CompanionPackageError("Mail artifact handle is invalid")
        if (
            isinstance(asset_count, bool)
            or not isinstance(asset_count, int)
            or not 1 <= asset_count <= 100
        ):
            raise CompanionPackageError("Draft package asset count is invalid")
        if not isinstance(body_html, str) or not body_html or len(body_html) > _MAX_BODY_HTML_CHARS:
            raise CompanionPackageError("Draft package body is invalid")

        path = self._managed_workbook(workbook_path, output_root, identifier)
        suffix = path.suffix.casefold()
        filename = f"tran-compensation-{identifier[:12]}{suffix}"
        content_type = (
            "application/vnd.ms-excel.sheet.macroEnabled.12"
            if suffix == ".xlsm"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        now = self._clock()
        with self._lock:
            self._prune(now)
            if identifier in self._packages:
                raise CompanionPackageError("Draft package already exists")
            superseded = {
                package_id
                for package_id, package in self._packages.items()
                if package.artifact_handle == handle
            }
            for package_id in superseded:
                del self._packages[package_id]
            for session in self._sessions.values():
                session.acknowledged_packages.difference_update(superseded)
            self._make_room(self._packages, self._max_packages)
            record = _PackageRecord(
                package_id=identifier,
                artifact_handle=handle,
                asset_count=asset_count,
                body_html=body_html,
                workbook_path=path,
                workbook_filename=filename,
                workbook_content_type=content_type,
                created_at=now,
                expires_at=now + self._package_ttl,
            )
            self._packages[identifier] = record
            return self._public_package(record, now)

    def list_draft_packages(
        self, principal: CompanionPrincipal
    ) -> tuple[CompanionDraftPackage, ...]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._authorized_tran_session(principal)
            visible = [
                item
                for item in self._packages.values()
                if item.artifact_handle in session.uploaded_handles
                and item.package_id not in session.acknowledged_packages
            ]
            visible.sort(key=lambda item: (item.created_at, item.package_id))
            return tuple(self._public_package(item, now) for item in visible)

    def get_draft_package(
        self, principal: CompanionPrincipal, package_id: str, *, output_root: Path
    ) -> CompanionDraftPackage:
        identifier = self._validated_package_id(package_id)
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._authorized_tran_session(principal)
            package = self._packages.get(identifier)
            if package is None or package.artifact_handle not in session.uploaded_handles:
                raise CompanionPackageNotFoundError("Draft package is unavailable")
            # Re-check the filesystem boundary and size on every download.  A
            # package never turns a server path into a generic file-read API.
            package.workbook_path = self._managed_workbook(
                package.workbook_path, output_root, package.package_id
            )
            return self._public_package(package, now)

    def acknowledge_draft_package(
        self, principal: CompanionPrincipal, package_id: str
    ) -> None:
        identifier = self._validated_package_id(package_id)
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._authorized_tran_session(principal)
            package = self._packages.get(identifier)
            if package is None or package.artifact_handle not in session.uploaded_handles:
                raise CompanionPackageNotFoundError("Draft package is unavailable")
            session.acknowledged_packages.add(identifier)

    def clear_all(self) -> dict[str, int]:
        with self._lock:
            counts = {
                "companion_pairing_count": len(self._pairings),
                "companion_session_count": len(self._sessions),
                "companion_package_count": len(self._packages),
            }
            self._pairings.clear()
            self._sessions.clear()
            self._packages.clear()
            return counts

    def _digest(self, value: str) -> bytes:
        return hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _constant_time_lookup(records: dict[bytes, object], candidate: bytes) -> object | None:
        match: object | None = None
        # Stores are deliberately bounded, so scanning avoids a secret-indexed
        # early return while remaining tiny at staging scale.
        for digest, record in records.items():
            if secrets.compare_digest(candidate, digest):
                match = record
        # Preserve one compare even when the store is empty.
        if not records:
            secrets.compare_digest(candidate, bytes(len(candidate)))
        return match

    @staticmethod
    def _make_room(records: dict[object, object], maximum: int) -> None:
        if len(records) < maximum:
            return
        oldest_key = min(
            records,
            key=lambda key: records[key].created_at,
        )
        del records[oldest_key]

    def _prune(self, now: float) -> None:
        self._pairings = {
            digest: item
            for digest, item in self._pairings.items()
            if item.expires_at > now
        }
        self._sessions = {
            digest: item
            for digest, item in self._sessions.items()
            if item.expires_at > now
        }
        self._packages = {
            identifier: item
            for identifier, item in self._packages.items()
            if item.expires_at > now
        }

    def _session_for_principal(self, principal: CompanionPrincipal) -> _SessionRecord:
        for record in self._sessions.values():
            if secrets.compare_digest(record.session_id, principal.session_id):
                if record.role != principal.role or record.client_type != principal.client_type:
                    break
                return record
        raise CompanionAuthenticationError("Companion session is invalid or expired")

    def _authorized_tran_session(self, principal: CompanionPrincipal) -> _SessionRecord:
        session = self._session_for_principal(principal)
        if session.role != "tran":
            raise CompanionAuthorizationError("Draft packages require a TranNNB pairing")
        return session

    @staticmethod
    def _principal(record: _SessionRecord) -> CompanionPrincipal:
        return CompanionPrincipal(
            session_id=record.session_id,
            role=record.role,
            client_type=record.client_type,
        )

    @staticmethod
    def _validated_package_id(package_id: str) -> str:
        identifier = str(package_id or "")
        if _PACKAGE_ID_RE.fullmatch(identifier) is None:
            raise CompanionPackageNotFoundError("Draft package is unavailable")
        return identifier

    @staticmethod
    def _managed_workbook(path: Path, output_root: Path, package_id: str) -> Path:
        raw_root = Path(output_root)
        root = raw_root.resolve()
        candidate = Path(path)
        resolved = candidate.resolve()
        suffix = resolved.suffix.casefold()
        expected_name = f"workbook-{package_id}{suffix}"
        if (
            suffix not in _WORKBOOK_SUFFIXES
            or raw_root.is_symlink()
            or resolved.name != expected_name
            or resolved.parent != root
            or candidate.is_symlink()
            or not resolved.is_file()
        ):
            raise CompanionPackageError("Draft workbook is outside managed storage")
        size = resolved.stat().st_size
        if size < 1 or size > MAX_WORKBOOK_BYTES:
            raise CompanionPackageError("Draft workbook exceeds the safe size limit")
        return resolved

    @staticmethod
    def _public_package(record: _PackageRecord, now: float) -> CompanionDraftPackage:
        return CompanionDraftPackage(
            package_id=record.package_id,
            artifact_handle=record.artifact_handle,
            asset_count=record.asset_count,
            body_html=record.body_html,
            workbook_path=record.workbook_path,
            workbook_filename=record.workbook_filename,
            workbook_content_type=record.workbook_content_type,
            expires_in_seconds=max(0, math.ceil(record.expires_at - now)),
        )
