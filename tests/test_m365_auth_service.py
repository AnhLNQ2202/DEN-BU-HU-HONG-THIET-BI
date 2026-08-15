"""Role/session isolation tests for in-memory Microsoft 365 OAuth state."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest

import asset_compensation.services.m365_auth_service as auth_module
from asset_compensation.adapters.m365_graph import (
    GraphDeltaPage,
    GraphFolder,
    M365GraphError,
    M365GraphMessageTooLarge,
)
from asset_compensation.services.m365_auth_service import (
    M365ConnectionService,
    M365OAuthConfig,
    M365OAuthStateError,
    M365ProviderError,
    M365ReconnectRequired,
)


@dataclass
class _Cache:
    account: dict[str, str] | None = None
    token: str | None = None


class _FakeMsal:
    def __init__(self) -> None:
        self.caches: list[_Cache] = []
        self.requested_scopes: list[tuple[str, ...]] = []
        self.prompts: list[str | None] = []
        owner = self

        class SerializableTokenCache(_Cache):
            def __init__(self) -> None:
                super().__init__()
                owner.caches.append(self)

        class ConfidentialClientApplication:
            def __init__(
                self,
                client_id: str,
                *,
                authority: str,
                client_credential: str,
                token_cache: _Cache,
            ) -> None:
                assert client_id == "11111111-1111-1111-1111-111111111111"
                assert authority.endswith("/22222222-2222-2222-2222-222222222222")
                assert client_credential == "synthetic-secret"
                self.cache = token_cache

            def initiate_auth_code_flow(
                self,
                *,
                scopes: list[str],
                redirect_uri: str,
                state: str,
                prompt: str | None,
            ) -> dict[str, str]:
                owner.requested_scopes.append(tuple(scopes))
                owner.prompts.append(prompt)
                return {
                    "state": state,
                    "auth_uri": (
                        "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize"
                        f"?state={state}"
                    ),
                    "redirect_uri": redirect_uri,
                }

            def acquire_token_by_auth_code_flow(
                self,
                flow: dict[str, str],
                callback_values: dict[str, str],
            ) -> dict[str, object]:
                if flow["state"] != callback_values.get("state"):
                    raise ValueError("bad state")
                code = callback_values.get("code", "account")
                username = f"{code}@example.test"
                self.cache.account = {"username": username}
                self.cache.token = f"token-for-{code}"
                return {
                    "access_token": self.cache.token,
                    "id_token_claims": {
                        "name": f"User {code}",
                        "preferred_username": username,
                    },
                }

            def get_accounts(self, username: str | None = None) -> list[dict[str, str]]:
                if self.cache.account is None:
                    return []
                if username is not None and self.cache.account["username"] != username:
                    return []
                return [self.cache.account]

            def acquire_token_silent(
                self,
                scopes: list[str],
                *,
                account: dict[str, str],
            ) -> dict[str, str] | None:
                del scopes, account
                return {"access_token": self.cache.token} if self.cache.token is not None else None

        self.SerializableTokenCache = SerializableTokenCache
        self.ConfidentialClientApplication = ConfidentialClientApplication


def _config(*, secure: bool = True) -> M365OAuthConfig:
    scheme = "https" if secure else "http"
    host = "example.test" if secure else "127.0.0.1:5000"
    return M365OAuthConfig(
        tenant_id="22222222-2222-2222-2222-222222222222",
        client_id="11111111-1111-1111-1111-111111111111",
        client_secret="synthetic-secret",
        redirect_uri=f"{scheme}://{host}/api/m365/callback",
    )


def _state(authorization_url: str) -> str:
    return parse_qs(urlsplit(authorization_url).query)["state"][0]


def _complete(
    service: M365ConnectionService,
    session_id: str,
    authorization_url: str,
    *,
    code: str,
) -> None:
    service.complete_authorization(
        session_id,
        {"state": _state(authorization_url), "code": code},
    )


def test_roles_use_least_privilege_and_separate_token_caches() -> None:
    fake = _FakeMsal()
    service = M365ConnectionService(_config(), msal_module=fake)

    ngan = service.start_authorization(None, "ngan")
    tran = service.start_authorization(ngan.session_id, "tran")
    _complete(service, ngan.session_id, ngan.authorization_url, code="ngan-user")

    assert service.status(ngan.session_id, "ngan")["connected"] is True
    assert service.status(ngan.session_id, "tran")["connected"] is False
    _complete(service, tran.session_id, tran.authorization_url, code="tran-user")
    assert service.status(ngan.session_id, "tran")["connected"] is True
    assert fake.requested_scopes == [
        ("https://graph.microsoft.com/Mail.Read",),
        ("https://graph.microsoft.com/Mail.ReadWrite",),
    ]
    assert fake.prompts == ["select_account", "select_account"]
    connected = [cache for cache in fake.caches if cache.account is not None]
    assert len(connected) == 2
    assert connected[0] is not connected[1]


def test_oauth_state_is_one_time_short_lived_and_return_path_is_same_origin() -> None:
    now = [100.0]
    fake = _FakeMsal()
    service = M365ConnectionService(_config(), msal_module=fake, clock=lambda: now[0])
    started = service.start_authorization(None, "ngan", return_to="/?tab=ngan")
    state = _state(started.authorization_url)

    result = service.complete_authorization(started.session_id, {"state": state, "code": "one"})
    assert result.return_to == "/?tab=ngan"
    with pytest.raises(M365OAuthStateError, match="already used"):
        service.complete_authorization(started.session_id, {"state": state, "code": "two"})

    expiring = service.start_authorization(started.session_id, "tran")
    now[0] += auth_module.M365_AUTH_FLOW_TTL_SECONDS + 1
    with pytest.raises(M365OAuthStateError, match="expired"):
        service.complete_authorization(
            expiring.session_id,
            {"state": _state(expiring.authorization_url), "code": "late"},
        )

    with pytest.raises(M365OAuthStateError, match="return path"):
        service.start_authorization(None, "ngan", return_to="https://evil.test/")
    with pytest.raises(M365OAuthStateError, match="return path"):
        service.start_authorization(None, "ngan", return_to="//evil.test/")


def test_expired_token_invalidates_only_its_role_and_requires_reconnect() -> None:
    fake = _FakeMsal()
    service = M365ConnectionService(_config(), msal_module=fake)
    ngan = service.start_authorization(None, "ngan")
    tran = service.start_authorization(ngan.session_id, "tran")
    _complete(service, ngan.session_id, ngan.authorization_url, code="ngan")
    _complete(service, tran.session_id, tran.authorization_url, code="tran")
    ngan_cache = next(
        cache
        for cache in fake.caches
        if cache.account and cache.account["username"] == "ngan@example.test"
    )
    ngan_cache.token = None

    assert service.status(ngan.session_id, "ngan")["connected"] is False
    assert service.status(ngan.session_id, "tran")["connected"] is True
    with pytest.raises(M365ReconnectRequired):
        service.list_folders(ngan.session_id, "ngan")


def test_session_store_is_bounded_expires_and_reset_drops_all_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module, "M365_MAX_SESSIONS", 2)
    monkeypatch.setattr(auth_module, "M365_SESSION_TTL_SECONDS", 10)
    now = [0.0]
    fake = _FakeMsal()
    service = M365ConnectionService(_config(), msal_module=fake, clock=lambda: now[0])
    first = service.start_authorization(None, "ngan")
    now[0] = 1
    second = service.start_authorization(None, "ngan")
    now[0] = 2
    third = service.start_authorization(None, "ngan")

    assert service.status(first.session_id, "ngan")["connected"] is False
    with pytest.raises(M365ReconnectRequired):
        service.complete_authorization(
            first.session_id,
            {"state": _state(first.authorization_url), "code": "evicted"},
        )
    assert service.clear_all_sessions() == 2
    assert service.clear_all_sessions() == 0
    assert service.status(second.session_id, "ngan")["connected"] is False
    assert service.status(third.session_id, "ngan")["connected"] is False
    fresh = service.start_authorization(None, "ngan")
    now[0] += 11
    assert service.status(fresh.session_id, "ngan")["connected"] is False
    assert service.clear_all_sessions() == 0


def test_secure_cookie_is_derived_from_configured_redirect_not_request_scheme() -> None:
    assert M365ConnectionService(_config(), msal_module=_FakeMsal()).secure_cookie
    assert not M365ConnectionService(_config(secure=False), msal_module=_FakeMsal()).secure_cookie
    assert "synthetic-secret" not in repr(_config())


def test_absolute_session_ttl_expires_both_roles_despite_hourly_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_module, "M365_SESSION_TTL_SECONDS", 8 * 60 * 60)
    now = [0.0]
    fake = _FakeMsal()
    service = M365ConnectionService(_config(), msal_module=fake, clock=lambda: now[0])
    ngan = service.start_authorization(None, "ngan")
    tran = service.start_authorization(ngan.session_id, "tran")
    _complete(service, ngan.session_id, ngan.authorization_url, code="ngan")
    _complete(service, tran.session_id, tran.authorization_url, code="tran")

    for hour in range(1, 8):
        now[0] = hour * 60 * 60
        assert service.status(ngan.session_id, "ngan")["connected"] is True
        assert service.status(ngan.session_id, "tran")["connected"] is True

    now[0] = 8 * 60 * 60
    assert service.status(ngan.session_id, "ngan")["connected"] is False
    assert service.status(ngan.session_id, "tran")["connected"] is False
    assert service.clear_all_sessions() == 0


class _SyncGraph:
    def __init__(self, *, transport_failure: bool = False) -> None:
        self.transport_failure = transport_failure

    def list_folders(self) -> tuple[GraphFolder, ...]:
        return (GraphFolder("folder", "Compensation", "inbox", 0, "Inbox / Compensation"),)

    def read_delta_page(
        self,
        folder_id: str,
        cursor: str | None,
        *,
        initial_since: object = None,
    ) -> GraphDeltaPage:
        assert folder_id == "folder"
        assert cursor is None
        assert initial_since is not None
        return GraphDeltaPage(
            ("valid-one", "oversized", "valid-two"),
            None,
            "https://graph.microsoft.com/v1.0/me/mailFolders/folder/messages/delta"
            "?$deltatoken=synthetic",
        )

    def get_message_mime(self, folder_id: str, message_id: str) -> bytes:
        assert folder_id == "folder"
        if message_id == "oversized":
            if self.transport_failure:
                raise M365GraphError("synthetic transport failure")
            raise M365GraphMessageTooLarge("synthetic over-limit MIME")
        return f"Message-ID: <{message_id}@example.test>\n\nbody".encode()


def _connected_sync_service(graph: object) -> tuple[M365ConnectionService, str]:
    fake = _FakeMsal()
    service = M365ConnectionService(
        _config(),
        msal_module=fake,
        graph_factory=lambda token: graph,
    )
    started = service.start_authorization(None, "ngan")
    _complete(service, started.session_id, started.authorization_url, code="sync-user")
    service.select_folder(started.session_id, "ngan", "folder")
    return service, started.session_id


def test_oversized_mime_is_skipped_and_cursor_can_commit() -> None:
    service, session_id = _connected_sync_service(_SyncGraph())

    batch = service.collect_sync_batch(session_id, "ngan")

    assert [message.filename for message in batch.messages] == [
        "m365-ngan-01.eml",
        "m365-ngan-03.eml",
    ]
    assert batch.oversized_count == 1
    service.commit_sync_cursor(session_id, batch)
    assert service.status(session_id, "ngan")["cursor_ready"] is True


def test_generic_mime_transport_failure_aborts_without_committing_cursor() -> None:
    service, session_id = _connected_sync_service(_SyncGraph(transport_failure=True))

    with pytest.raises(M365ProviderError, match="sync did not complete"):
        service.collect_sync_batch(session_id, "ngan")

    assert service.status(session_id, "ngan")["cursor_ready"] is False


class _TwoPageSyncGraph:
    def __init__(self) -> None:
        self.payload = b"x" * (2 * 1024 * 1024 - 1)
        self.pages_read: list[str | None] = []

    def list_folders(self) -> tuple[GraphFolder, ...]:
        return (GraphFolder("folder", "Compensation", "inbox", 0, "Inbox / Compensation"),)

    def read_delta_page(
        self,
        folder_id: str,
        cursor: str | None,
        *,
        initial_since: object = None,
    ) -> GraphDeltaPage:
        assert folder_id == "folder"
        self.pages_read.append(cursor)
        if cursor is None:
            assert initial_since is not None
            return GraphDeltaPage(
                tuple(f"page-one-{index}" for index in range(10)),
                "https://graph.microsoft.com/v1.0/me/mailFolders/folder/messages/delta"
                "?$skiptoken=page-two",
                None,
            )
        assert initial_since is None
        return GraphDeltaPage(
            tuple(f"page-two-{index}" for index in range(10)),
            None,
            "https://graph.microsoft.com/v1.0/me/mailFolders/folder/messages/delta"
            "?$deltatoken=complete",
        )

    def get_message_mime(self, folder_id: str, message_id: str) -> bytes:
        assert folder_id == "folder"
        assert message_id.startswith(("page-one-", "page-two-"))
        return self.payload


def test_large_two_page_delta_commits_each_page_without_cursor_poison() -> None:
    graph = _TwoPageSyncGraph()
    service, session_id = _connected_sync_service(graph)

    first = service.collect_sync_batch(session_id, "ngan")
    assert len(first.messages) == 10
    assert first.has_more is True
    assert first.cursor_ready is False
    service.commit_sync_cursor(session_id, first)

    second = service.collect_sync_batch(session_id, "ngan")
    assert len(second.messages) == 10
    assert second.has_more is False
    assert second.cursor_ready is True
    service.commit_sync_cursor(session_id, second)

    assert graph.pages_read == [None, first.next_cursor]
    assert service.status(session_id, "ngan")["cursor_ready"] is True
