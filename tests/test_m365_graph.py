"""Synthetic security and contract tests for the Microsoft Graph boundary."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from asset_compensation.adapters.m365_graph import (
    GRAPH_CONNECT_TIMEOUT_SECONDS,
    GRAPH_READ_TIMEOUT_SECONDS,
    MAX_GRAPH_MIME_BYTES,
    GraphHttpClient,
    M365GraphDraftUncertain,
    M365GraphError,
    M365GraphMessageTooLarge,
    M365GraphMessageUnavailable,
    M365GraphReconnectRequired,
)


class _Response:
    def __init__(
        self,
        status: int,
        body: bytes | dict[str, Any] = b"",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [
            self.body[offset : offset + chunk_size]
            for offset in range(0, len(self.body), chunk_size)
        ]

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *responses: _Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(*responses: _Response | Exception) -> tuple[GraphHttpClient, _Session]:
    session = _Session(*responses)
    return GraphHttpClient("synthetic-access-token", session=session), session


def _draft(identifier: str = "draft-1") -> dict[str, Any]:
    return {
        "id": identifier,
        "isDraft": True,
        "subject": "Re: synthetic",
        "body": {"contentType": "HTML", "content": "<div>quoted</div>"},
        "webLink": "https://outlook.office.com/mail/deeplink/draft/synthetic",
    }


def test_http_boundary_uses_tls_timeout_no_redirect_and_sanitized_errors() -> None:
    client, session = _client(_Response(200, {"value": []}))

    assert client.list_folders() == ()

    _, url, options = session.calls[0]
    assert url == "https://graph.microsoft.com/v1.0/me/mailFolders"
    assert options["timeout"] == (
        GRAPH_CONNECT_TIMEOUT_SECONDS,
        GRAPH_READ_TIMEOUT_SECONDS,
    )
    assert options["allow_redirects"] is False
    assert options["verify"] is True
    assert options["stream"] is True
    assert options["headers"]["Authorization"] == "Bearer synthetic-access-token"

    failing, _ = _client(RuntimeError("synthetic-access-token provider detail"))
    with pytest.raises(M365GraphError) as captured:
        failing.list_folders()
    assert "synthetic-access-token" not in str(captured.value)
    assert "provider detail" not in str(captured.value)


@pytest.mark.parametrize(
    "cursor",
    [
        "https://evil.example/v1.0/me/mailFolders/f/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=x",
        "http://graph.microsoft.com/v1.0/me/mailFolders/f/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/mailFolders/other/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/mailFolders/f/messages/delta?redirect=x",
    ],
)
def test_delta_cursor_rejects_non_exact_graph_continuations(cursor: str) -> None:
    client, session = _client()
    with pytest.raises(M365GraphError):
        client.read_delta_page("f", cursor)
    assert session.calls == []


def test_nested_folders_are_bounded_and_paths_are_explicit() -> None:
    client, session = _client(
        _Response(
            200,
            {
                "value": [
                    {
                        "id": "parent",
                        "displayName": "Inbox",
                        "parentFolderId": "root",
                        "childFolderCount": 1,
                        "isHidden": False,
                    }
                ]
            },
        ),
        _Response(
            200,
            {
                "value": [
                    {
                        "id": "child",
                        "displayName": "Compensation",
                        "parentFolderId": "parent",
                        "childFolderCount": 0,
                        "isHidden": False,
                    }
                ]
            },
        ),
    )

    folders = client.list_folders()

    assert [folder.path for folder in folders] == ["Inbox", "Inbox / Compensation"]
    assert session.calls[1][1].endswith("/mailFolders/parent/childFolders")


@pytest.mark.parametrize("bad_count", [True, -1, "1", None, 100_001])
def test_malformed_folder_child_count_is_a_sanitized_graph_error(
    bad_count: object,
) -> None:
    client, _ = _client(
        _Response(
            200,
            {
                "value": [
                    {
                        "id": "folder",
                        "displayName": "Inbox",
                        "childFolderCount": bad_count,
                    }
                ]
            },
        )
    )
    with pytest.raises(M365GraphError, match="child count"):
        client.list_folders()


def test_folder_next_link_cannot_switch_to_another_allowed_operation() -> None:
    client, session = _client(
        _Response(
            200,
            {
                "value": [],
                "@odata.nextLink": ("https://graph.microsoft.com/v1.0/me/messages?$skiptoken=x"),
            },
        )
    )
    with pytest.raises(M365GraphError, match="continuation path"):
        client.list_folders()
    assert len(session.calls) == 1


def test_initial_delta_is_created_only_and_has_lookback_then_uses_cursor() -> None:
    delta_url = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/f/messages/delta?$deltatoken=synthetic"
    )
    client, session = _client(
        _Response(200, {"value": [{"id": "m1"}], "@odata.deltaLink": delta_url}),
        _Response(200, {"value": [], "@odata.deltaLink": delta_url}),
    )
    lower_bound = datetime(2026, 7, 16, 3, 4, 5, tzinfo=UTC)

    first = client.read_delta_page("f", initial_since=lower_bound)
    second = client.read_delta_page("f", first.delta_link)

    first_options = session.calls[0][2]
    assert first.messages == ("m1",)
    assert first_options["params"] == {
        "changeType": "created",
        "$select": "id,internetMessageId,receivedDateTime",
        "$filter": "receivedDateTime ge 2026-07-16T03:04:05Z",
        "$orderby": "receivedDateTime desc",
        "$top": "10",
    }
    assert session.calls[1][2]["params"] == {}
    assert second.delta_link == delta_url


def test_mime_size_and_reconnect_boundaries() -> None:
    oversized, _ = _client(
        _Response(
            200,
            b"ignored",
            headers={"Content-Length": str(MAX_GRAPH_MIME_BYTES + 1)},
        )
    )
    with pytest.raises(M365GraphMessageTooLarge, match="MIME limit"):
        oversized.get_message_mime("folder", "message")

    streamed, _ = _client(_Response(200, b"x" * (MAX_GRAPH_MIME_BYTES + 1)))
    with pytest.raises(M365GraphMessageTooLarge, match="MIME limit"):
        streamed.get_message_mime("folder", "message")

    expired, _ = _client(_Response(401))
    with pytest.raises(M365GraphReconnectRequired):
        expired.list_folders()

    disappeared, session = _client(_Response(404, b"provider detail"))
    with pytest.raises(M365GraphMessageUnavailable):
        disappeared.get_message_mime("selected-folder", "moved-message")
    assert len(session.calls) == 1
    assert "/me/messages/" not in session.calls[0][1]


def test_exact_message_id_lookup_zero_ambiguous_and_success() -> None:
    missing, _ = _client(_Response(200, {"value": []}))
    with pytest.raises(M365GraphError, match="not found"):
        missing.find_message_by_internet_id("<synthetic@example.test>")

    ambiguous, _ = _client(
        _Response(
            200,
            {
                "value": [
                    {"id": "one", "internetMessageId": "<same@example.test>"},
                    {"id": "two", "internetMessageId": "<same@example.test>"},
                ]
            },
        )
    )
    with pytest.raises(M365GraphError, match="ambiguous"):
        ambiguous.find_message_by_internet_id("<same@example.test>")

    found, session = _client(
        _Response(
            200,
            {"value": [{"id": "one", "internetMessageId": "<a'b@example.test>"}]},
        )
    )
    assert found.find_message_by_internet_id("<a'b@example.test>") == "one"
    assert session.calls[0][2]["params"]["$filter"] == (
        "internetMessageId eq '<a''b@example.test>'"
    )


def test_reply_all_patch_attachment_and_rollback_operations_are_exact(
    tmp_path: Path,
) -> None:
    client, session = _client(
        _Response(201, _draft()),
        _Response(200, _draft()),
        _Response(201, {"id": "attachment"}),
        _Response(204),
    )
    workbook = tmp_path / "synthetic.xlsx"
    workbook.write_bytes(b"synthetic workbook")

    created = client.create_reply_all_draft("source")
    updated = client.update_draft_html(created.id, "<p>approved</p>")
    client.attach_workbook(created.id, workbook)
    client.delete_draft(created.id)

    assert updated.web_link == ("https://outlook.office.com/mail/deeplink/draft/synthetic")
    assert [(method, url.rsplit("/", 1)[-1]) for method, url, _ in session.calls] == [
        ("POST", "createReplyAll"),
        ("PATCH", "draft-1"),
        ("POST", "attachments"),
        ("DELETE", "draft-1"),
    ]
    patch_body = session.calls[1][2]["json"]
    assert patch_body == {"body": {"contentType": "HTML", "content": "<p>approved</p>"}}
    attachment = session.calls[2][2]["json"]
    assert attachment["name"] == "synthetic.xlsx"
    assert attachment["isInline"] is False


def test_invalid_create_reply_response_preserves_safe_draft_id_for_rollback() -> None:
    client, _ = _client(
        _Response(
            201,
            {
                "id": "created-draft",
                "isDraft": False,
                "body": {"contentType": "HTML", "content": "invalid"},
            },
        )
    )

    with pytest.raises(M365GraphDraftUncertain) as captured:
        client.create_reply_all_draft("source")

    assert captured.value.draft_id == "created-draft"


def test_graph_adapter_has_no_send_operation() -> None:
    source = inspect.getsource(GraphHttpClient)
    assert "/send" not in source.casefold()
    assert "sendmail" not in source.casefold()
    assert "send_mail" not in source.casefold()
