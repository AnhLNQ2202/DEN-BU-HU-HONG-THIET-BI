"""Role-scoped Microsoft 365 OAuth sessions and bounded folder synchronization."""

from __future__ import annotations

import importlib
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from asset_compensation.adapters.m365_graph import (
    GraphFolder,
    GraphHttpClient,
    M365GraphCursorExpired,
    M365GraphError,
    M365GraphMessageTooLarge,
    M365GraphMessageUnavailable,
    M365GraphReconnectRequired,
)

M365_SESSION_COOKIE = "asset_hub_m365_sid"
M365_SESSION_TTL_SECONDS = 8 * 60 * 60
M365_AUTH_FLOW_TTL_SECONDS = 10 * 60
M365_MAX_SESSIONS = 512
M365_MAX_SYNC_PAGES = 1
M365_MAX_SYNC_MESSAGES = 10
M365_MAX_SYNC_TOTAL_BYTES = 25 * 1024 * 1024
M365_INITIAL_SYNC_LOOKBACK_DAYS = 30

_SID_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_TENANT_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)
_ROLES = frozenset({"ngan", "tran"})
_ROLE_SCOPES = {"ngan": "Mail.Read", "tran": "Mail.ReadWrite"}
_RESERVED_TENANTS = frozenset({"common", "organizations", "consumers"})


class M365ServiceError(RuntimeError):
    """Sanitized Microsoft 365 service failure."""


class M365UnavailableError(M365ServiceError):
    """Microsoft 365 is not fully configured in this deployment."""


class M365ReconnectRequired(M365ServiceError):
    """The browser session needs a fresh role-specific OAuth connection."""


class M365ProviderError(M365ServiceError):
    """A sanitized upstream Microsoft 365 operation failure."""


class M365OAuthStateError(M365ServiceError):
    """OAuth state/callback validation failed."""


@dataclass(frozen=True, slots=True, repr=False)
class M365OAuthConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_settings(cls, settings: Any) -> M365OAuthConfig | None:
        values = (
            settings.m365_tenant_id,
            settings.m365_client_id,
            settings.m365_client_secret,
            settings.m365_redirect_uri,
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            return None
        tenant = str(settings.m365_tenant_id).strip()
        client = str(settings.m365_client_id).strip()
        secret = str(settings.m365_client_secret)
        redirect = str(settings.m365_redirect_uri).strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in redirect):
            return None
        if tenant.casefold() in _RESERVED_TENANTS:
            return None
        try:
            UUID(client)
        except ValueError:
            return None
        tenant_is_guid = True
        try:
            UUID(tenant)
        except ValueError:
            tenant_is_guid = False
        if not tenant_is_guid and not _TENANT_DOMAIN_RE.fullmatch(tenant):
            return None
        if not 8 <= len(secret) <= 4096 or any(
            ord(character) < 32 or ord(character) == 127 for character in secret
        ):
            return None
        try:
            parsed = urlsplit(redirect)
            port = parsed.port
        except ValueError:
            return None
        is_local_http = parsed.scheme.casefold() == "http" and (
            parsed.hostname or ""
        ).casefold() in {"127.0.0.1", "localhost"}
        if (
            not (parsed.scheme.casefold() == "https" or is_local_http)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            and not 1 <= port <= 65535
            or parsed.path != "/api/m365/callback"
            or parsed.query
            or parsed.fragment
        ):
            return None
        return cls(tenant, client, secret, redirect)

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.redirect_uri).scheme.casefold() == "https"


@dataclass(frozen=True, slots=True)
class M365PublicAccount:
    display_name: str | None
    email: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"display_name": self.display_name, "email": self.email}


@dataclass(frozen=True, slots=True)
class M365SelectedFolder:
    id: str
    display_name: str
    path: str

    @classmethod
    def from_graph(cls, folder: GraphFolder) -> M365SelectedFolder:
        return cls(folder.id, folder.display_name, folder.path)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "display_name": self.display_name, "path": self.path}


@dataclass(slots=True)
class _PendingFlow:
    flow: dict[str, Any]
    state: str
    return_to: str
    expires_at: float


@dataclass(slots=True)
class _RoleState:
    cache: Any
    account_username: str | None = None
    public_account: M365PublicAccount | None = None
    pending_flow: _PendingFlow | None = None
    selected_folder: M365SelectedFolder | None = None
    delta_cursor: str | None = None
    cursor_ready: bool = False


@dataclass(slots=True)
class _BrowserSession:
    roles: dict[str, _RoleState]
    created_at: float
    last_seen: float


@dataclass(frozen=True, slots=True)
class M365AuthorizationStart:
    session_id: str
    authorization_url: str
    role: str


@dataclass(frozen=True, slots=True)
class M365AuthorizationResult:
    role: str
    return_to: str


@dataclass(frozen=True, slots=True)
class M365RawMessage:
    filename: str
    mime_bytes: bytes


@dataclass(frozen=True, slots=True)
class M365RawSyncBatch:
    role: str
    folder: M365SelectedFolder
    messages: tuple[M365RawMessage, ...]
    base_cursor: str | None
    next_cursor: str
    has_more: bool
    cursor_ready: bool
    unavailable_count: int = 0
    oversized_count: int = 0


def m365_role(value: object) -> str:
    role = str(value or "").casefold()
    if role not in _ROLES:
        raise M365ServiceError("Microsoft 365 role must be ngan or tran")
    return role


def _safe_claim(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    return text


def _safe_return_to(value: object) -> str:
    text = str(value or "/")
    if len(text) > 512 or not text.startswith("/") or text.startswith("//") or "\\" in text:
        raise M365OAuthStateError("OAuth return path is invalid")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise M365OAuthStateError("OAuth return path is invalid") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment or any(ord(c) < 32 for c in text):
        raise M365OAuthStateError("OAuth return path is invalid")
    return text


class M365ConnectionService:
    """Own per-browser, per-role MSAL caches without serializing tokens anywhere."""

    def __init__(
        self,
        config: M365OAuthConfig | None,
        *,
        msal_module: Any | None = None,
        graph_factory: Callable[[str], GraphHttpClient] = GraphHttpClient,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if msal_module is None:
            try:
                msal_module = importlib.import_module("msal")
            except ImportError:
                msal_module = None
        self._config = config
        self._msal = msal_module
        self._graph_factory = graph_factory
        self._clock = clock
        self._utcnow = utcnow
        from threading import RLock

        self._lock = RLock()
        self._sessions: dict[str, _BrowserSession] = {}

    @property
    def configured(self) -> bool:
        return bool(
            self._config is not None
            and self._msal is not None
            and hasattr(self._msal, "SerializableTokenCache")
            and hasattr(self._msal, "ConfidentialClientApplication")
        )

    @property
    def secure_cookie(self) -> bool:
        return bool(self._config and self._config.secure_cookie)

    @staticmethod
    def required_scope(role: object) -> str:
        return _ROLE_SCOPES[m365_role(role)]

    def _require_configured(self) -> None:
        if not self.configured:
            raise M365UnavailableError("Microsoft 365 integration is not configured")

    def _new_role_state(self) -> _RoleState:
        assert self._msal is not None
        return _RoleState(cache=self._msal.SerializableTokenCache())

    def _purge_locked(self, now: float) -> None:
        expired = [
            sid
            for sid, session in self._sessions.items()
            if now - session.created_at >= M365_SESSION_TTL_SECONDS
            or now - session.last_seen >= M365_SESSION_TTL_SECONDS
        ]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _session_locked(
        self, session_id: str | None, *, create: bool
    ) -> tuple[str, _BrowserSession]:
        now = self._clock()
        self._purge_locked(now)
        sid = str(session_id or "")
        if not _SID_RE.fullmatch(sid) or sid not in self._sessions:
            if not create:
                raise M365ReconnectRequired("Connect this Microsoft 365 role first")
            while len(self._sessions) >= M365_MAX_SESSIONS:
                oldest = min(
                    self._sessions,
                    key=lambda candidate: self._sessions[candidate].last_seen,
                )
                self._sessions.pop(oldest, None)
            sid = secrets.token_urlsafe(32)
            session = _BrowserSession(
                roles={role: self._new_role_state() for role in _ROLES},
                created_at=now,
                last_seen=now,
            )
            self._sessions[sid] = session
        session = self._sessions[sid]
        session.last_seen = now
        return sid, session

    def _application(self, role_state: _RoleState) -> Any:
        assert self._config is not None and self._msal is not None
        return self._msal.ConfidentialClientApplication(
            self._config.client_id,
            authority=self._config.authority,
            client_credential=self._config.client_secret,
            token_cache=role_state.cache,
        )

    def start_authorization(
        self,
        session_id: str | None,
        role: object,
        *,
        return_to: object = "/",
    ) -> M365AuthorizationStart:
        self._require_configured()
        selected_role = m365_role(role)
        safe_return = _safe_return_to(return_to)
        with self._lock:
            sid, session = self._session_locked(session_id, create=True)
            session.roles[selected_role] = self._new_role_state()
            role_state = session.roles[selected_role]
            state = secrets.token_urlsafe(32)
            assert self._config is not None
            try:
                flow = self._application(role_state).initiate_auth_code_flow(
                    scopes=[f"https://graph.microsoft.com/{_ROLE_SCOPES[selected_role]}"],
                    redirect_uri=self._config.redirect_uri,
                    state=state,
                    prompt="select_account",
                )
            except Exception as exc:
                session.roles[selected_role] = self._new_role_state()
                raise M365ProviderError(
                    "Microsoft 365 authorization is temporarily unavailable"
                ) from exc
            if not isinstance(flow, dict) or not isinstance(flow.get("auth_uri"), str):
                raise M365ServiceError("Microsoft 365 authorization could not start")
            authorization_url = self._validate_authorization_url(flow["auth_uri"])
            if flow.get("state") != state:
                raise M365ServiceError("Microsoft 365 authorization state is invalid")
            role_state.pending_flow = _PendingFlow(
                flow=dict(flow),
                state=state,
                return_to=safe_return,
                expires_at=self._clock() + M365_AUTH_FLOW_TTL_SECONDS,
            )
            return M365AuthorizationStart(sid, authorization_url, selected_role)

    @staticmethod
    def _validate_authorization_url(value: str) -> str:
        if len(value) > 8192 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise M365ServiceError("Microsoft 365 authorization URL is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise M365ServiceError("Microsoft 365 authorization URL is invalid") from exc
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != "login.microsoftonline.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            raise M365ServiceError("Microsoft 365 authorization URL is invalid")
        return value

    def complete_authorization(
        self,
        session_id: str | None,
        callback_values: Mapping[str, str],
    ) -> M365AuthorizationResult:
        self._require_configured()
        returned_state = str(callback_values.get("state") or "")
        if not returned_state or len(returned_state) > 128:
            raise M365OAuthStateError("OAuth state is missing or invalid")
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            selected_role: str | None = None
            pending: _PendingFlow | None = None
            for role, role_state in session.roles.items():
                candidate = role_state.pending_flow
                if candidate and secrets.compare_digest(candidate.state, returned_state):
                    selected_role = role
                    pending = candidate
                    role_state.pending_flow = None
                    break
            if selected_role is None or pending is None:
                raise M365OAuthStateError("OAuth state is missing, expired, or already used")
            if self._clock() > pending.expires_at:
                raise M365OAuthStateError("OAuth state has expired")
            role_state = session.roles[selected_role]
            try:
                result = self._application(role_state).acquire_token_by_auth_code_flow(
                    pending.flow,
                    dict(callback_values),
                )
            except ValueError as exc:
                raise M365OAuthStateError("OAuth callback validation failed") from exc
            except Exception as exc:
                session.roles[selected_role] = self._new_role_state()
                raise M365ProviderError("Microsoft 365 authorization did not complete") from exc
            if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
                session.roles[selected_role] = self._new_role_state()
                raise M365OAuthStateError("Microsoft 365 authorization was not completed")
            claims = result.get("id_token_claims")
            claims = claims if isinstance(claims, dict) else {}
            try:
                app = self._application(role_state)
                accounts = app.get_accounts()
            except Exception as exc:
                session.roles[selected_role] = self._new_role_state()
                raise M365ProviderError("Microsoft 365 authorization did not complete") from exc
            if not isinstance(accounts, list) or len(accounts) != 1:
                session.roles[selected_role] = self._new_role_state()
                raise M365OAuthStateError("Microsoft 365 account selection is invalid")
            username = _safe_claim(accounts[0].get("username"))
            email = _safe_claim(claims.get("preferred_username") or claims.get("email") or username)
            display_name = _safe_claim(claims.get("name"))
            role_state.account_username = username
            role_state.public_account = M365PublicAccount(display_name, email)
            return M365AuthorizationResult(selected_role, pending.return_to)

    def _access_token_locked(self, session: _BrowserSession, role: str) -> str:
        role_state = session.roles[role]
        if role_state.public_account is None:
            raise M365ReconnectRequired("Connect this Microsoft 365 role first")
        try:
            app = self._application(role_state)
            accounts = app.get_accounts(username=role_state.account_username)
        except Exception as exc:
            session.roles[role] = self._new_role_state()
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        if not isinstance(accounts, list) or len(accounts) != 1:
            session.roles[role] = self._new_role_state()
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role")
        try:
            result = app.acquire_token_silent(
                [f"https://graph.microsoft.com/{_ROLE_SCOPES[role]}"],
                account=accounts[0],
            )
        except Exception as exc:
            session.roles[role] = self._new_role_state()
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
            session.roles[role] = self._new_role_state()
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role")
        return result["access_token"]

    def _client(self, session_id: str | None, role: object) -> GraphHttpClient:
        self._require_configured()
        selected_role = m365_role(role)
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            token = self._access_token_locked(session, selected_role)
        try:
            return self._graph_factory(token)
        except M365GraphReconnectRequired as exc:
            self.invalidate(session_id, selected_role)
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        except M365GraphError as exc:
            raise M365ProviderError("Microsoft 365 connection is temporarily unavailable") from exc

    def status(self, session_id: str | None, role: object) -> dict[str, object]:
        selected_role = m365_role(role)
        base: dict[str, object] = {
            "configured": self.configured,
            "role": selected_role,
            "required_scope": _ROLE_SCOPES[selected_role],
            "connected": False,
            "account": None,
            "selected_folder": None,
            "cursor_ready": False,
            "storage": "memory",
            "background_sync": False,
        }
        if not self.configured:
            return base
        try:
            with self._lock:
                _, session = self._session_locked(session_id, create=False)
                self._access_token_locked(session, selected_role)
                role_state = session.roles[selected_role]
                base.update(
                    {
                        "connected": True,
                        "account": (
                            role_state.public_account.to_dict()
                            if role_state.public_account
                            else None
                        ),
                        "selected_folder": (
                            role_state.selected_folder.to_dict()
                            if role_state.selected_folder
                            else None
                        ),
                        "cursor_ready": role_state.cursor_ready,
                    }
                )
        except M365ReconnectRequired:
            pass
        return base

    def disconnect(self, session_id: str | None, role: object) -> None:
        selected_role = m365_role(role)
        self._require_configured()
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            session.roles[selected_role] = self._new_role_state()

    def clear_all_sessions(self) -> int:
        """Drop every in-memory token cache/cursor during authorized staging reset."""

        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def invalidate(self, session_id: str | None, role: object) -> None:
        selected_role = m365_role(role)
        with self._lock:
            sid = str(session_id or "")
            session = self._sessions.get(sid)
            if session is not None:
                session.roles[selected_role] = self._new_role_state()

    def list_folders(self, session_id: str | None, role: object) -> tuple[GraphFolder, ...]:
        selected_role = m365_role(role)
        client = self._client(session_id, selected_role)
        try:
            return client.list_folders()
        except M365GraphReconnectRequired as exc:
            self.invalidate(session_id, selected_role)
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        except M365GraphError as exc:
            raise M365ProviderError("Microsoft 365 folders are temporarily unavailable") from exc

    def select_folder(
        self,
        session_id: str | None,
        role: object,
        folder_id: object,
    ) -> M365SelectedFolder:
        selected_role = m365_role(role)
        requested = str(folder_id or "")
        if (
            not isinstance(folder_id, str)
            or not 1 <= len(requested) <= 512
            or any(ord(character) < 32 or ord(character) == 127 for character in requested)
        ):
            raise M365ServiceError("Microsoft 365 folder selection is invalid")
        folders = self.list_folders(session_id, selected_role)
        matches = [folder for folder in folders if folder.id == requested]
        if len(matches) != 1:
            raise M365ServiceError("Select exactly one folder from the available folder list")
        selected = M365SelectedFolder.from_graph(matches[0])
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            role_state = session.roles[selected_role]
            if role_state.public_account is None:
                raise M365ReconnectRequired("Reconnect this Microsoft 365 role")
            role_state.selected_folder = selected
            role_state.delta_cursor = None
            role_state.cursor_ready = False
        return selected

    def collect_sync_batch(self, session_id: str | None, role: object) -> M365RawSyncBatch:
        selected_role = m365_role(role)
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            token = self._access_token_locked(session, selected_role)
            role_state = session.roles[selected_role]
            folder = role_state.selected_folder
            base_cursor = role_state.delta_cursor
        if folder is None:
            raise M365ServiceError("Select one Microsoft 365 folder before syncing")
        try:
            client = self._graph_factory(token)
        except M365GraphReconnectRequired as exc:
            self.invalidate(session_id, selected_role)
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        except M365GraphError as exc:
            raise M365ProviderError("Microsoft 365 connection is temporarily unavailable") from exc
        cursor = base_cursor
        message_ids: list[str] = []
        seen: set[str] = set()
        has_more = False
        cursor_ready = False
        try:
            for _ in range(M365_MAX_SYNC_PAGES):
                page = client.read_delta_page(
                    folder.id,
                    cursor,
                    initial_since=(
                        self._utcnow() - timedelta(days=M365_INITIAL_SYNC_LOOKBACK_DAYS)
                        if cursor is None
                        else None
                    ),
                )
                for message_id in page.messages:
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    message_ids.append(message_id)
                    if len(message_ids) > M365_MAX_SYNC_MESSAGES:
                        raise M365ServiceError(
                            "Microsoft 365 sync exceeds the 10-message safety limit"
                        )
                if page.delta_link is not None:
                    cursor = page.delta_link
                    cursor_ready = True
                    has_more = False
                    break
                assert page.next_link is not None
                cursor = page.next_link
                has_more = True
            if cursor is None:
                raise M365ServiceError("Microsoft 365 sync did not return a cursor")
            messages: list[M365RawMessage] = []
            total_bytes = 0
            unavailable_count = 0
            oversized_count = 0
            for index, message_id in enumerate(message_ids, start=1):
                try:
                    payload = client.get_message_mime(folder.id, message_id)
                except M365GraphMessageUnavailable:
                    unavailable_count += 1
                    continue
                except M365GraphMessageTooLarge:
                    oversized_count += 1
                    continue
                total_bytes += len(payload)
                if total_bytes > M365_MAX_SYNC_TOTAL_BYTES:
                    raise M365ServiceError("Microsoft 365 sync exceeds the 25 MiB aggregate limit")
                messages.append(
                    M365RawMessage(
                        filename=f"m365-{selected_role}-{index:02d}.eml",
                        mime_bytes=payload,
                    )
                )
        except M365GraphCursorExpired as exc:
            with self._lock:
                current = self._sessions.get(str(session_id or ""))
                if current is not None:
                    current.roles[selected_role].delta_cursor = None
                    current.roles[selected_role].cursor_ready = False
            raise M365ProviderError(
                "Microsoft 365 folder cursor expired; run sync again to restart"
            ) from exc
        except M365GraphReconnectRequired as exc:
            self.invalidate(session_id, selected_role)
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role") from exc
        except M365GraphError as exc:
            raise M365ProviderError("Microsoft 365 mail sync did not complete") from exc
        return M365RawSyncBatch(
            role=selected_role,
            folder=folder,
            messages=tuple(messages),
            base_cursor=base_cursor,
            next_cursor=cursor,
            has_more=has_more,
            cursor_ready=cursor_ready,
            unavailable_count=unavailable_count,
            oversized_count=oversized_count,
        )

    def commit_sync_cursor(self, session_id: str | None, batch: M365RawSyncBatch) -> None:
        with self._lock:
            _, session = self._session_locked(session_id, create=False)
            role_state = session.roles[batch.role]
            if (
                role_state.selected_folder is None
                or role_state.selected_folder.id != batch.folder.id
                or role_state.delta_cursor != batch.base_cursor
            ):
                raise M365ServiceError("Microsoft 365 folder changed during sync; retry")
            role_state.delta_cursor = batch.next_cursor
            role_state.cursor_ready = batch.cursor_ready

    def graph_client(self, session_id: str | None, role: object) -> GraphHttpClient:
        """Internal composition hook for the draft service; tokens never leave backend code."""

        return self._client(session_id, role)


__all__ = [
    "M365_AUTH_FLOW_TTL_SECONDS",
    "M365_MAX_SESSIONS",
    "M365_INITIAL_SYNC_LOOKBACK_DAYS",
    "M365_SESSION_COOKIE",
    "M365_SESSION_TTL_SECONDS",
    "M365AuthorizationResult",
    "M365AuthorizationStart",
    "M365ConnectionService",
    "M365OAuthConfig",
    "M365OAuthStateError",
    "M365ProviderError",
    "M365PublicAccount",
    "M365RawMessage",
    "M365RawSyncBatch",
    "M365ReconnectRequired",
    "M365SelectedFolder",
    "M365ServiceError",
    "M365UnavailableError",
    "m365_role",
]
