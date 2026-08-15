"""HTTP contract and privacy tests for optional Microsoft 365 routes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask

import asset_compensation.web.routes as routes_module
from asset_compensation.adapters.m365_graph import GraphFolder
from asset_compensation.config import Settings
from asset_compensation.services import (
    M365_SESSION_COOKIE,
    M365OutlookDraftResult,
    M365ReconnectRequired,
    M365SyncResult,
)
from asset_compensation.services.m365_auth_service import (
    M365AuthorizationResult,
    M365AuthorizationStart,
    M365SelectedFolder,
)
from asset_compensation.web import create_app


class _Connections:
    configured = True
    secure_cookie = True

    def __init__(self) -> None:
        self.disconnected: list[tuple[str | None, str]] = []
        self.cleared = 0
        self.callback_values: dict[str, str] | None = None

    def status(self, session_id: str | None, role: object) -> dict[str, object]:
        return {
            "configured": True,
            "role": str(role),
            "required_scope": "Mail.ReadWrite" if role == "tran" else "Mail.Read",
            "connected": session_id == "synthetic-session",
            "account": (
                {"display_name": "Synthetic User", "email": "user@example.test"}
                if session_id == "synthetic-session"
                else None
            ),
            "selected_folder": None,
            "cursor_ready": False,
            "storage": "memory",
            "background_sync": False,
        }

    def start_authorization(
        self,
        session_id: str | None,
        role: object,
        *,
        return_to: object,
    ) -> M365AuthorizationStart:
        assert session_id is None
        assert return_to == "/?tab=tran"
        return M365AuthorizationStart(
            "synthetic-session",
            "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?state=opaque",
            str(role),
        )

    def complete_authorization(
        self,
        session_id: str | None,
        callback_values: dict[str, str],
    ) -> M365AuthorizationResult:
        assert session_id == "synthetic-session"
        self.callback_values = callback_values
        return M365AuthorizationResult("tran", "/?tab=tran")

    def disconnect(self, session_id: str | None, role: object) -> None:
        self.disconnected.append((session_id, str(role)))

    def list_folders(self, session_id: str | None, role: object) -> tuple[GraphFolder, ...]:
        assert session_id == "synthetic-session"
        assert role == "tran"
        return (GraphFolder("folder", "Compensation", "parent", 0, "Inbox / Compensation"),)

    def select_folder(
        self,
        session_id: str | None,
        role: object,
        folder_id: object,
    ) -> M365SelectedFolder:
        assert (session_id, role, folder_id) == (
            "synthetic-session",
            "tran",
            "folder",
        )
        return M365SelectedFolder("folder", "Compensation", "Inbox / Compensation")

    def clear_all_sessions(self) -> int:
        self.cleared += 1
        return 2


class _Sync:
    def sync(self, session_id: str | None, role: object, **kwargs: Any) -> M365SyncResult:
        assert (session_id, role) == ("synthetic-session", "tran")
        assert kwargs["ambiguous_supplier_domains"] == frozenset()
        return M365SyncResult(
            role="tran",
            folder={
                "id": "folder",
                "display_name": "Compensation",
                "path": "Inbox / Compensation",
            },
            fetched_count=1,
            ingested=1,
            case_ids=("LOST-202601-SYNTHETIC",),
            warnings=(),
            unknown_files=(),
            skipped_files=(),
            has_more=False,
            cursor_ready=True,
        )


def _app(tmp_path: Path, **overrides: object) -> Flask:
    values: dict[str, object] = {
        "data_dir": tmp_path / "runtime",
        "demo_mode": False,
        "secret_key": "synthetic-route-secret",
        "retain_raw_eml": True,
    }
    values.update(overrides)
    app = create_app(Settings(**values))
    app.config.update(TESTING=True)
    return app


def _install_fakes(app: Flask) -> _Connections:
    connections = _Connections()
    app.extensions["asset_hub"]["m365_connection_service"] = connections
    app.extensions["asset_hub"]["m365_mail_sync_service"] = _Sync()
    return connections


def _cookie(client: Any) -> None:
    client.set_cookie(M365_SESSION_COOKIE, "synthetic-session")


def test_capabilities_fail_closed_when_any_m365_setting_is_missing(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        m365_tenant_id="22222222-2222-2222-2222-222222222222",
    )
    try:
        payload = app.test_client().get("/api/capabilities").get_json()
        assert payload["capabilities"]["m365_configured"] is False
        assert payload["capabilities"]["m365_ngan"] is False
        assert payload["capabilities"]["m365_tran"] is False
        assert payload["capabilities"]["tran_outlook_draft"] is False
        unavailable = app.test_client().post("/api/m365/ngan/connect", json={})
        assert unavailable.status_code == 503
        assert unavailable.get_json()["capability_available"] is False
        assert unavailable.headers["Cache-Control"] == "private, no-store"
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_connect_cookie_and_role_routes_follow_private_json_contract(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    connections = _install_fakes(app)
    client = app.test_client()
    try:
        connected = client.post("/api/m365/tran/connect", json={"return_to": "/?tab=tran"})
        assert connected.status_code == 200
        assert connected.get_json()["role"] == "tran"
        cookie = connected.headers["Set-Cookie"]
        assert f"{M365_SESSION_COOKIE}=synthetic-session" in cookie
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Path=/" in cookie
        assert connected.headers["Cache-Control"] == "private, no-store"

        status = client.get("/api/m365/tran/status")
        assert status.get_json()["account"] == {
            "display_name": "Synthetic User",
            "email": "user@example.test",
        }
        assert status.headers["Cache-Control"] == "private, no-store"

        folders = client.get("/api/m365/tran/folders")
        assert folders.get_json()["folders"][0]["path"] == "Inbox / Compensation"
        assert folders.get_json()["maximum_depth"] == 4

        selected = client.post("/api/m365/tran/folder", json={"folder_id": "folder"})
        assert selected.get_json()["cursor_ready"] is False

        missing_header = client.post("/api/m365/tran/sync", json={})
        assert missing_header.status_code == 400
        synced = client.post(
            "/api/m365/tran/sync",
            json={},
            headers={"X-Asset-Hub-Action": "m365-sync-v1"},
        )
        assert synced.get_json()["case_ids"] == ["LOST-202601-SYNTHETIC"]
        assert synced.get_json()["cursor_ready"] is True
        assert synced.headers["Cache-Control"] == "private, no-store"

        missing_disconnect_header = client.post("/api/m365/tran/disconnect", json={})
        assert missing_disconnect_header.status_code == 400
        disconnected = client.post(
            "/api/m365/tran/disconnect",
            json={},
            headers={"X-Asset-Hub-Action": "m365-disconnect-v1"},
        )
        assert disconnected.get_json()["connected"] is False
        assert connections.disconnected == [("synthetic-session", "tran")]
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_callback_is_basic_auth_exception_but_one_time_state_service_still_runs(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        access_user="demo-team",
        access_password="synthetic-secret",
    )
    connections = _install_fakes(app)
    client = app.test_client()
    _cookie(client)
    try:
        assert client.get("/api/m365/tran/status").status_code == 401
        callback = client.get(
            "/api/m365/callback?code=provider-code&state=opaque&session_state=session"
        )
        assert callback.status_code == 303
        assert callback.headers["Location"] == "/?tab=tran"
        assert callback.headers["Cache-Control"] == "private, no-store"
        assert callback.headers["Referrer-Policy"] == "no-referrer"
        assert b"provider-code" not in callback.data
        assert b"opaque" not in callback.data
        assert connections.callback_values == {
            "code": "provider-code",
            "state": "opaque",
            "session_state": "session",
        }
        duplicate_state = client.get("/api/m365/callback?code=x&state=one&state=two")
        assert duplicate_state.status_code == 400
        assert duplicate_state.headers["Cache-Control"] == "private, no-store"
        assert duplicate_state.headers["Referrer-Policy"] == "no-referrer"
        connections.callback_values = None
        oversized = client.get("/api/m365/callback?state=" + ("x" * 16_385))
        assert oversized.status_code == 400
        assert oversized.headers["Cache-Control"] == "private, no-store"
        assert oversized.headers["Referrer-Policy"] == "no-referrer"
        assert connections.callback_values is None
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_reconnect_error_and_staging_reset_clear_sessions(tmp_path: Path) -> None:
    app = _app(tmp_path, allow_test_reset=True)
    connections = _install_fakes(app)
    client = app.test_client()
    _cookie(client)
    try:

        def reconnect(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise M365ReconnectRequired("Reconnect this Microsoft 365 role")

        connections.list_folders = reconnect  # type: ignore[method-assign]
        response = client.get("/api/m365/tran/folders")
        assert response.status_code == 401
        assert response.get_json()["reconnect_required"] is True
        assert response.headers["Cache-Control"] == "private, no-store"

        cleared = client.post(
            "/api/test-data/clear",
            json={"confirm": "CLEAR_TEST_DATA"},
            headers={"X-Asset-Hub-Action": "clear-test-data-v1"},
        )
        assert cleared.status_code == 200
        assert cleared.get_json()["m365_session_count"] == 2
        assert connections.cleared == 1
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_outlook_draft_route_returns_unsent_private_contract(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    app = _app(tmp_path)
    _install_fakes(app)
    client = app.test_client()
    _cookie(client)
    handle = "eml-sha256-" + ("a" * 64)
    workbook = tmp_path / "runtime" / "outputs" / "tran" / ("workbook-" + ("b" * 32) + ".xlsx")
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"synthetic workbook")
    extension = app.extensions["asset_hub"]
    asset_rows = [
        {
            "asset_code": "LAP10001",
            "asset_name": "Synthetic Laptop",
            "domain": "synthetic.user",
        },
        {
            "asset_code": "MON10002",
            "asset_name": "Synthetic Monitor",
            "domain": "synthetic.user",
        },
    ]
    source_case = extension["case_service"].ingest_one(
        SimpleNamespace(
            case_type="LOST",
            domain="synthetic.user",
            asset_code="LAP10001, MON10002",
            received_at=datetime(2026, 1, 15, tzinfo=UTC),
            source_id="same-domain-source",
            metadata={
                "mail_artifact_handle": handle,
                "asset_rows": asset_rows,
            },
        )
    )
    non_lost_case = extension["case_service"].ingest_one(
        SimpleNamespace(
            case_type="DAMAGED",
            domain="synthetic.user",
            asset_code="LAP20001",
            received_at=datetime(2026, 1, 15, tzinfo=UTC),
            source_id="damaged-source",
            metadata={
                "mail_artifact_handle": handle,
                "asset_rows": [
                    {
                        "asset_code": "LAP20001",
                        "asset_name": "Synthetic Damaged Laptop",
                        "domain": "synthetic.user",
                    }
                ],
            },
        )
    )

    def final_case(source_id: str, tag_number: str, *, closed: bool) -> Any:
        case = extension["case_service"].ingest_one(
            SimpleNamespace(
                case_type="LOST",
                domain="synthetic.final",
                asset_code=tag_number,
                received_at=datetime(2026, 1, 15, tzinfo=UTC),
                source_id=source_id,
                metadata={
                    "mail_artifact_handle": handle,
                    "asset_rows": [
                        {
                            "asset_code": tag_number,
                            "asset_name": "Synthetic Final Asset",
                            "domain": "synthetic.final",
                        }
                    ],
                },
            )
        )
        case_service = extension["case_service"]
        case_service.transition_status(
            case.id,
            "READY_FOR_ACCOUNTING",
            actor="synthetic-reviewer",
        )
        case_service.transition_status(
            case.id,
            "ACCOUNTED",
            actor="synthetic-accountant",
        )
        if closed:
            case_service.transition_status(
                case.id,
                "CLOSED",
                actor="synthetic-reviewer",
            )
        return case_service.get_case(case.id)

    accounted_case = final_case("accounted-source", "LAP30001", closed=False)
    closed_case = final_case("closed-source", "MON30002", closed=True)
    original_get_case = extension["case_service"].get_case
    requested_case_ids: list[str] = []

    def bounded_get_case(case_id: str) -> Any:
        requested_case_ids.append(case_id)
        return original_get_case(case_id)

    def reject_full_scan(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("draft binding must not scan the full case table")

    monkeypatch.setattr(extension["case_service"], "get_case", bounded_get_case)
    monkeypatch.setattr(extension["case_service"], "list_cases", reject_full_scan)
    extension["mail_artifact_store"] = SimpleNamespace(read_bytes=lambda value: b"eml")
    extension["m365_outlook_draft_service"] = SimpleNamespace(
        create=lambda *args, **kwargs: M365OutlookDraftResult(
            "Re: Synthetic", "https://outlook.office.com/mail/deeplink/draft/one"
        )
    )
    monkeypatch.setattr(
        routes_module,
        "_export_tran_workbook",
        lambda data: (
            tuple(object() for _ in data["assets"]),
            workbook,
            "b" * 32,
        ),
    )
    assets = [
        {
            "tag_number": "LAP10001",
            "asset_name": "Synthetic Laptop",
            "domain": "synthetic.user",
            "lost_date": "2026-01-01",
        },
        {
            "tag_number": "MON10002",
            "asset_name": "Synthetic Monitor",
            "domain": "synthetic.user",
            "lost_date": "2026-01-01",
        },
    ]
    source_bindings = [
        {"case_id": source_case.id, "source_row_index": 0},
        {"case_id": source_case.id, "source_row_index": 1},
    ]

    def post_draft(
        request_assets: list[dict[str, object]],
        bindings: list[dict[str, object]],
        *,
        artifact_handle: str = handle,
    ) -> Any:
        return client.post(
            "/api/tran/outlook-drafts",
            json={
                "assets": request_assets,
                "source_bindings": bindings,
                "mail_artifact_handle": artifact_handle,
                "body_intro": "Approved",
            },
        )

    try:
        mismatched_handle = post_draft(
            assets,
            source_bindings,
            artifact_handle="eml-sha256-" + ("c" * 64),
        )
        assert mismatched_handle.status_code == 400

        unknown = post_draft(
            assets[:1],
            [
                {
                    "case_id": "LOST-202601-UNKNOWN",
                    "source_row_index": 0,
                }
            ],
        )
        assert unknown.status_code == 400

        duplicate = post_draft(
            assets,
            [source_bindings[0], source_bindings[0]],
        )
        assert duplicate.status_code == 400

        swapped = post_draft(list(reversed(assets)), source_bindings)
        assert swapped.status_code == 400

        wrong_domain_assets = [dict(assets[0])]
        wrong_domain_assets[0]["domain"] = "different.user"
        wrong_domain = post_draft(wrong_domain_assets, source_bindings[:1])
        assert wrong_domain.status_code == 400

        non_lost = post_draft(
            [
                {
                    "tag_number": "LAP20001",
                    "asset_name": "Synthetic Damaged Laptop",
                    "domain": "synthetic.user",
                }
            ],
            [{"case_id": non_lost_case.id, "source_row_index": 0}],
        )
        assert non_lost.status_code == 400

        for final in (accounted_case, closed_case):
            final_status = post_draft(
                [
                    {
                        "tag_number": final.asset_code,
                        "asset_name": "Synthetic Final Asset",
                        "domain": final.domain,
                    }
                ],
                [{"case_id": final.id, "source_row_index": 0}],
            )
            assert final_status.status_code == 400

        before_success = len(requested_case_ids)
        response = post_draft(assets, source_bindings)
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["sent"] is False
        assert payload["asset_count"] == 2
        assert payload["outlook_draft"] == {
            "subject": "Re: Synthetic",
            "web_url": "https://outlook.office.com/mail/deeplink/draft/one",
        }
        assert payload["workbook_download_url"].startswith("/api/tran/workbooks/")
        assert response.headers["Cache-Control"] == "private, no-store"
        assert requested_case_ids[before_success:] == [source_case.id]
    finally:
        app.extensions["asset_hub"]["repository"].close()
