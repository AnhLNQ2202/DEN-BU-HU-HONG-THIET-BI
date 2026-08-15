"""Strict Microsoft Graph HTTP adapter for role-scoped mailbox workflows.

The adapter intentionally exposes no generic public request method and no mail
send operation. Every outbound Graph path is checked against a small allowlist,
redirects are disabled, TLS verification is mandatory, and response bodies are
bounded before parsing.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .m365_contract import MAX_GRAPH_BODY_CHARS

GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_API_ROOT = f"{GRAPH_ORIGIN}/v1.0"
GRAPH_CONNECT_TIMEOUT_SECONDS = 3.05
GRAPH_READ_TIMEOUT_SECONDS = 15
MAX_GRAPH_JSON_BYTES = 1024 * 1024
MAX_GRAPH_MIME_BYTES = 2 * 1024 * 1024
MAX_DIRECT_ATTACHMENT_BYTES = 2_800_000
MAX_GRAPH_ATTACHMENT_RESPONSE_BYTES = 5 * 1024 * 1024

_GRAPH_PATH_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"/v1\.0/me/mailFolders/?")),
    ("GET", re.compile(r"/v1\.0/me/mailFolders/[^/]+/childFolders/?")),
    ("GET", re.compile(r"/v1\.0/me/mailFolders/[^/]+/messages/delta")),
    ("GET", re.compile(r"/v1\.0/me/mailFolders/[^/]+/messages/[^/]+/\$value")),
    ("GET", re.compile(r"/v1\.0/me/messages/?")),
    ("GET", re.compile(r"/v1\.0/me/messages/[^/]+/?")),
    ("POST", re.compile(r"/v1\.0/me/messages/?")),
    ("POST", re.compile(r"/v1\.0/me/messages/[^/]+/createReplyAll")),
    ("PATCH", re.compile(r"/v1\.0/me/messages/[^/]+")),
    ("POST", re.compile(r"/v1\.0/me/messages/[^/]+/attachments")),
    ("DELETE", re.compile(r"/v1\.0/me/messages/[^/]+")),
)
_OUTLOOK_WEB_HOSTS = frozenset(
    {"outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"}
)
_GRAPH_ID_RE = re.compile(r"[^\x00-\x1f\x7f]{1,512}")


class M365GraphError(RuntimeError):
    """Sanitized failure from the Microsoft Graph boundary."""


class M365GraphReconnectRequired(M365GraphError):
    """The delegated token is no longer accepted and OAuth must be repeated."""


class M365GraphCursorExpired(M365GraphError):
    """The stored folder delta cursor can no longer be used."""


class M365GraphMessageUnavailable(M365GraphError):
    """A delta item moved or disappeared before its folder-scoped MIME read."""


class M365GraphMessageTooLarge(M365GraphError):
    """A folder message exceeds the bounded raw-MIME download limit."""


class M365GraphDraftUncertain(M365GraphError):
    """Graph may have created a draft but did not return a fully usable response."""

    def __init__(self, message: str, *, draft_id: str | None) -> None:
        super().__init__(message)
        self.draft_id = draft_id


@dataclass(frozen=True, slots=True)
class GraphFolder:
    id: str
    display_name: str
    parent_folder_id: str | None
    child_folder_count: int
    path: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "child_folder_count": self.child_folder_count,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class GraphDeltaPage:
    messages: tuple[str, ...]
    next_link: str | None
    delta_link: str | None


@dataclass(frozen=True, slots=True)
class GraphDraft:
    id: str
    subject: str | None
    body_content_type: str
    body_content: str
    web_link: str | None


def _graph_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise M365GraphError(f"{label} is invalid")
    text = value
    if not _GRAPH_ID_RE.fullmatch(text):
        raise M365GraphError(f"{label} is invalid")
    return text


def validate_outlook_web_link(value: object) -> str | None:
    """Return a validated Outlook HTTPS link or ``None`` for absent/unsafe data."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in _OUTLOOK_WEB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
    ):
        return None
    return urlunsplit(parsed)


class GraphHttpClient:
    """Bounded synchronous Graph v1.0 client with an explicit operation allowlist."""

    def __init__(self, access_token: str, *, session: Any | None = None) -> None:
        token = str(access_token or "")
        if not token or len(token) > 16_384 or any(character.isspace() for character in token):
            raise M365GraphReconnectRequired("Microsoft 365 connection must be renewed")
        if session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - dependency/runtime boundary
                raise M365GraphError("Microsoft 365 HTTP support is unavailable") from exc
            session = requests.Session()
        self._access_token = token
        self._session = session

    @staticmethod
    def _url(path_or_url: str) -> str:
        value = str(path_or_url or "")
        if value.startswith("/"):
            value = GRAPH_ORIGIN + value
        if len(value) > 8192 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise M365GraphError("Microsoft Graph continuation URL is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise M365GraphError("Microsoft Graph URL is invalid") from exc
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != "graph.microsoft.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path.startswith("/v1.0/")
            or parsed.fragment
        ):
            raise M365GraphError("Microsoft Graph URL is not allowed")
        return urlunsplit(parsed)

    @staticmethod
    def _allow(method: str, url: str) -> None:
        path = urlsplit(url).path
        if not any(
            expected_method == method and pattern.fullmatch(path)
            for expected_method, pattern in _GRAPH_PATH_RULES
        ):
            raise M365GraphError("Microsoft Graph operation is not allowed")

    @classmethod
    def _continuation_url(
        cls,
        value: object,
        *,
        expected_path: str,
        allowed_query_keys: frozenset[str],
    ) -> str:
        if not isinstance(value, str):
            raise M365GraphError("Microsoft Graph continuation URL is invalid")
        url = cls._url(value)
        parsed = urlsplit(url)
        if parsed.path != expected_path:
            raise M365GraphError("Microsoft Graph continuation path is invalid")
        try:
            query = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise M365GraphError("Microsoft Graph continuation query is invalid") from exc
        keys = [key for key, _ in query]
        if (
            not query
            or len(keys) != len(set(keys))
            or any(key not in allowed_query_keys for key in keys)
            or any(
                not value or len(value) > 4096 or any(ord(char) < 32 for char in value)
                for _, value in query
            )
        ):
            raise M365GraphError("Microsoft Graph continuation query is invalid")
        return url

    def _request_bytes(
        self,
        method: str,
        path_or_url: str,
        *,
        expected_statuses: Iterable[int],
        max_bytes: int,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        message_unavailable_on_404: bool = False,
        message_too_large_on_limit: bool = False,
    ) -> bytes:
        method = method.upper()
        url = self._url(path_or_url)
        self._allow(method, url)
        request_headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            if any(key.casefold() == "authorization" for key in headers):
                raise M365GraphError("Authorization header override is not allowed")
            request_headers.update(headers)
        response: Any | None = None
        try:
            response = self._session.request(
                method,
                url,
                params=dict(params or {}),
                json=dict(json_body) if json_body is not None else None,
                headers=request_headers,
                timeout=(GRAPH_CONNECT_TIMEOUT_SECONDS, GRAPH_READ_TIMEOUT_SECONDS),
                allow_redirects=False,
                verify=True,
                stream=True,
            )
            status = int(response.status_code)
            if status in {401, 403}:
                raise M365GraphReconnectRequired("Microsoft 365 connection must be renewed")
            if status == 410:
                raise M365GraphCursorExpired("Microsoft 365 folder cursor has expired")
            if status == 404 and message_unavailable_on_404:
                raise M365GraphMessageUnavailable(
                    "Microsoft 365 folder message is no longer available"
                )
            if status not in set(expected_statuses):
                raise M365GraphError("Microsoft Graph operation did not complete")
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                    if length < 0:
                        raise M365GraphError("Microsoft Graph response exceeds the safe limit")
                    if length > max_bytes:
                        if message_too_large_on_limit:
                            raise M365GraphMessageTooLarge(
                                "Microsoft 365 folder message exceeds the safe MIME limit"
                            )
                        raise M365GraphError("Microsoft Graph response exceeds the safe limit")
                except ValueError as exc:
                    raise M365GraphError("Microsoft Graph response length is invalid") from exc
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if not isinstance(chunk, bytes):
                    raise M365GraphError("Microsoft Graph returned an invalid response")
                size += len(chunk)
                if size > max_bytes:
                    if message_too_large_on_limit:
                        raise M365GraphMessageTooLarge(
                            "Microsoft 365 folder message exceeds the safe MIME limit"
                        )
                    raise M365GraphError("Microsoft Graph response exceeds the safe limit")
                chunks.append(chunk)
            return b"".join(chunks)
        except M365GraphError:
            raise
        except Exception as exc:
            raise M365GraphError("Microsoft Graph is temporarily unavailable") from exc
        finally:
            if response is not None:
                with suppress(Exception):
                    response.close()

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        expected_statuses: Iterable[int],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = self._request_bytes(
            method,
            path_or_url,
            expected_statuses=expected_statuses,
            max_bytes=MAX_GRAPH_JSON_BYTES,
            params=params,
            json_body=json_body,
            headers=headers,
        )
        try:
            value = json.loads(payload.decode("utf-8")) if payload else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise M365GraphError("Microsoft Graph returned an invalid JSON response") from exc
        if not isinstance(value, dict):
            raise M365GraphError("Microsoft Graph returned an invalid JSON object")
        return value

    def list_folders(
        self,
        *,
        maximum: int = 200,
        maximum_pages: int = 10,
        maximum_depth: int = 4,
    ) -> tuple[GraphFolder, ...]:
        if not 1 <= maximum <= 200 or not 1 <= maximum_pages <= 10 or not 1 <= maximum_depth <= 4:
            raise ValueError("Folder bounds are invalid")
        folders: list[GraphFolder] = []
        seen: set[str] = set()
        queue: list[tuple[str, int, str]] = [("/v1.0/me/mailFolders", 1, "")]
        pages_used = 0
        while queue:
            collection_url, depth, parent_path = queue.pop(0)
            url: str | None = collection_url
            params: Mapping[str, str] | None = {
                "$select": "id,displayName,parentFolderId,childFolderCount,isHidden",
                "$top": "100",
            }
            while url is not None:
                pages_used += 1
                if pages_used > maximum_pages:
                    raise M365GraphError("Microsoft Graph folder list exceeds the safe page limit")
                payload = self._request_json("GET", url, expected_statuses={200}, params=params)
                params = None
                values = payload.get("value")
                if not isinstance(values, list):
                    raise M365GraphError("Microsoft Graph folder response is invalid")
                for raw in values:
                    if not isinstance(raw, dict) or raw.get("isHidden") is True:
                        continue
                    identifier = _graph_id(raw.get("id"), "Folder ID")
                    display_name = str(raw.get("displayName") or "").strip()
                    if (
                        not display_name
                        or len(display_name) > 255
                        or any(
                            ord(character) < 32 or ord(character) == 127
                            for character in display_name
                        )
                    ):
                        raise M365GraphError("Microsoft Graph folder name is invalid")
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    raw_child_count = raw.get("childFolderCount", 0)
                    if (
                        isinstance(raw_child_count, bool)
                        or not isinstance(raw_child_count, int)
                        or not 0 <= raw_child_count <= 100_000
                    ):
                        raise M365GraphError("Microsoft Graph folder child count is invalid")
                    child_count = raw_child_count
                    path = f"{parent_path} / {display_name}" if parent_path else display_name
                    folders.append(
                        GraphFolder(
                            id=identifier,
                            display_name=display_name,
                            parent_folder_id=(
                                _graph_id(raw["parentFolderId"], "Parent folder ID")
                                if raw.get("parentFolderId")
                                else None
                            ),
                            child_folder_count=child_count,
                            path=path,
                        )
                    )
                    if len(folders) > maximum:
                        raise M365GraphError("Microsoft Graph returned too many folders")
                    if child_count and depth < maximum_depth:
                        encoded = quote(identifier, safe="")
                        queue.append(
                            (
                                f"/v1.0/me/mailFolders/{encoded}/childFolders",
                                depth + 1,
                                path,
                            )
                        )
                next_link = payload.get("@odata.nextLink")
                url = (
                    self._continuation_url(
                        next_link,
                        expected_path=urlsplit(collection_url).path,
                        allowed_query_keys=frozenset({"$skiptoken", "$top", "$select"}),
                    )
                    if next_link is not None
                    else None
                )
        return tuple(folders)

    def read_delta_page(
        self,
        folder_id: str,
        cursor: str | None = None,
        *,
        initial_since: datetime | None = None,
    ) -> GraphDeltaPage:
        encoded_folder = quote(_graph_id(folder_id, "Folder ID"), safe="")
        expected_path = f"/v1.0/me/mailFolders/{encoded_folder}/messages/delta"
        url = (
            self._continuation_url(
                cursor,
                expected_path=expected_path,
                allowed_query_keys=frozenset(
                    {
                        "$deltatoken",
                        "$filter",
                        "$orderby",
                        "$select",
                        "$skiptoken",
                        "$top",
                        "changeType",
                    }
                ),
            )
            if cursor is not None
            else expected_path
        )
        params: Mapping[str, str] | None = None
        if cursor is None:
            if initial_since is None or initial_since.tzinfo is None:
                raise ValueError("Initial delta sync requires a timezone-aware lower bound")
            normalized_since = initial_since.astimezone(UTC).isoformat().replace("+00:00", "Z")
            params = {
                "changeType": "created",
                "$select": "id,internetMessageId,receivedDateTime",
                "$filter": f"receivedDateTime ge {normalized_since}",
                "$orderby": "receivedDateTime desc",
                "$top": "10",
            }
        payload = self._request_json(
            "GET",
            url,
            expected_statuses={200},
            params=params,
            headers={"Prefer": "odata.maxpagesize=10"},
        )
        values = payload.get("value")
        if not isinstance(values, list):
            raise M365GraphError("Microsoft Graph delta response is invalid")
        messages: list[str] = []
        for raw in values:
            if not isinstance(raw, dict) or "@removed" in raw:
                continue
            messages.append(_graph_id(raw.get("id"), "Message ID"))
        next_link = payload.get("@odata.nextLink")
        delta_link = payload.get("@odata.deltaLink")
        if bool(next_link) == bool(delta_link):
            raise M365GraphError("Microsoft Graph delta cursor response is invalid")
        return GraphDeltaPage(
            messages=tuple(messages),
            next_link=(
                self._continuation_url(
                    next_link,
                    expected_path=expected_path,
                    allowed_query_keys=frozenset(
                        {
                            "$filter",
                            "$orderby",
                            "$select",
                            "$skiptoken",
                            "$top",
                            "changeType",
                        }
                    ),
                )
                if next_link is not None
                else None
            ),
            delta_link=(
                self._continuation_url(
                    delta_link,
                    expected_path=expected_path,
                    allowed_query_keys=frozenset(
                        {
                            "$deltatoken",
                            "$filter",
                            "$orderby",
                            "$select",
                            "$top",
                            "changeType",
                        }
                    ),
                )
                if delta_link is not None
                else None
            ),
        )

    def get_message_mime(self, folder_id: str, message_id: str) -> bytes:
        folder = quote(_graph_id(folder_id, "Folder ID"), safe="")
        message = quote(_graph_id(message_id, "Message ID"), safe="")
        return self._request_bytes(
            "GET",
            f"/v1.0/me/mailFolders/{folder}/messages/{message}/$value",
            expected_statuses={200},
            max_bytes=MAX_GRAPH_MIME_BYTES,
            headers={"Accept": "message/rfc822"},
            message_unavailable_on_404=True,
            message_too_large_on_limit=True,
        )

    def find_message_by_internet_id(self, internet_message_id: str) -> str:
        value = str(internet_message_id or "")
        if not value or len(value) > 998 or "\r" in value or "\n" in value:
            raise M365GraphError("Source email Message-ID is invalid")
        escaped = value.replace("'", "''")
        payload = self._request_json(
            "GET",
            "/v1.0/me/messages",
            expected_statuses={200},
            params={
                "$filter": f"internetMessageId eq '{escaped}'",
                "$select": "id,internetMessageId",
                "$top": "2",
            },
        )
        values = payload.get("value")
        if not isinstance(values, list):
            raise M365GraphError("Microsoft Graph message lookup is invalid")
        exact = [
            raw for raw in values if isinstance(raw, dict) and raw.get("internetMessageId") == value
        ]
        if len(exact) == 0:
            raise M365GraphError("The source email was not found in the connected mailbox")
        if len(exact) != 1 or payload.get("@odata.nextLink"):
            raise M365GraphError("The source email is ambiguous in the connected mailbox")
        return _graph_id(exact[0].get("id"), "Message ID")

    @staticmethod
    def _draft(raw: Mapping[str, object]) -> GraphDraft:
        identifier = _graph_id(raw.get("id"), "Draft ID")
        if raw.get("isDraft") is not True:
            raise M365GraphError("Microsoft Graph did not return an Outlook draft")
        body = raw.get("body")
        if not isinstance(body, dict):
            raise M365GraphError("Microsoft Graph draft body is invalid")
        content = body.get("content")
        content_type = str(body.get("contentType") or "").casefold()
        if not isinstance(content, str) or content_type not in {"html", "text"}:
            raise M365GraphError("Microsoft Graph draft body is invalid")
        if len(content) > MAX_GRAPH_BODY_CHARS:
            raise M365GraphError("Microsoft Graph draft body exceeds the safe limit")
        raw_subject = raw.get("subject")
        subject = (
            raw_subject[:998]
            if isinstance(raw_subject, str)
            and not any(ord(character) < 32 or ord(character) == 127 for character in raw_subject)
            else None
        )
        return GraphDraft(
            id=identifier,
            subject=subject,
            body_content_type=content_type,
            body_content=content,
            web_link=validate_outlook_web_link(raw.get("webLink")),
        )

    def create_reply_all_draft(self, message_id: str) -> GraphDraft:
        message = quote(_graph_id(message_id, "Message ID"), safe="")
        try:
            payload = self._request_json(
                "POST",
                f"/v1.0/me/messages/{message}/createReplyAll",
                expected_statuses={200, 201},
            )
        except M365GraphReconnectRequired:
            raise
        except M365GraphError as exc:
            raise M365GraphDraftUncertain(
                "Outlook draft creation status is uncertain", draft_id=None
            ) from exc
        try:
            return self._draft(payload)
        except M365GraphError as exc:
            try:
                draft_id = _graph_id(payload.get("id"), "Draft ID")
            except M365GraphError:
                draft_id = None
            raise M365GraphDraftUncertain(
                "Outlook draft response is invalid", draft_id=draft_id
            ) from exc

    def create_message_draft(self, subject: str, html_body: str) -> GraphDraft:
        """Create one standalone unsent draft with no recipients and no send call."""

        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > 998
            or any(ord(character) < 32 or ord(character) == 127 for character in subject)
        ):
            raise M365GraphError("Outlook draft subject is invalid")
        if not isinstance(html_body, str) or not html_body or len(html_body) > MAX_GRAPH_BODY_CHARS:
            raise M365GraphError("Outlook draft HTML exceeds the safe limit")
        try:
            payload = self._request_json(
                "POST",
                "/v1.0/me/messages",
                expected_statuses={201},
                json_body={
                    "subject": subject.strip(),
                    "body": {"contentType": "HTML", "content": html_body},
                },
            )
        except M365GraphReconnectRequired:
            raise
        except M365GraphError as exc:
            raise M365GraphDraftUncertain(
                "Outlook draft creation status is uncertain", draft_id=None
            ) from exc
        try:
            return self._draft(payload)
        except M365GraphError as exc:
            try:
                draft_id = _graph_id(payload.get("id"), "Draft ID")
            except M365GraphError:
                draft_id = None
            raise M365GraphDraftUncertain(
                "Outlook draft response is invalid", draft_id=draft_id
            ) from exc

    def update_draft_html(self, draft_id: str, html_body: str) -> GraphDraft:
        draft = quote(_graph_id(draft_id, "Draft ID"), safe="")
        if not isinstance(html_body, str) or not html_body or len(html_body) > MAX_GRAPH_BODY_CHARS:
            raise M365GraphError("Outlook draft HTML exceeds the safe limit")
        payload = self._request_json(
            "PATCH",
            f"/v1.0/me/messages/{draft}",
            expected_statuses={200},
            json_body={"body": {"contentType": "HTML", "content": html_body}},
        )
        return self._draft(payload)

    def attach_workbook(self, draft_id: str, workbook_path: str | Path) -> None:
        draft = quote(_graph_id(draft_id, "Draft ID"), safe="")
        try:
            path = Path(workbook_path).resolve()
            if not path.is_file() or path.suffix.casefold() not in {".xlsx", ".xlsm"}:
                raise M365GraphError("Outlook draft attachment is unavailable")
            size = path.stat().st_size
            if size <= 0 or size >= MAX_DIRECT_ATTACHMENT_BYTES:
                raise M365GraphError(
                    "Workbook is too large for the safe Outlook direct-attachment limit"
                )
            name = path.name
            if len(name) > 255 or any(
                ord(character) < 32 or ord(character) == 127 for character in name
            ):
                raise M365GraphError("Outlook attachment name is invalid")
            content = path.read_bytes()
        except M365GraphError:
            raise
        except OSError as exc:
            raise M365GraphError("Outlook draft attachment is unavailable") from exc
        subtype = (
            "application/vnd.ms-excel.sheet.macroEnabled.12"
            if path.suffix.casefold() == ".xlsm"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        if len(content) != size or len(content) >= MAX_DIRECT_ATTACHMENT_BYTES:
            raise M365GraphError("Outlook draft attachment changed during preparation")
        self._request_bytes(
            "POST",
            f"/v1.0/me/messages/{draft}/attachments",
            expected_statuses={201},
            max_bytes=MAX_GRAPH_ATTACHMENT_RESPONSE_BYTES,
            json_body={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": subtype,
                "isInline": False,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            },
        )

    def delete_draft(self, draft_id: str) -> None:
        draft = quote(_graph_id(draft_id, "Draft ID"), safe="")
        self._request_bytes(
            "DELETE",
            f"/v1.0/me/messages/{draft}",
            expected_statuses={204},
            max_bytes=0,
        )


__all__ = [
    "GRAPH_API_ROOT",
    "GRAPH_CONNECT_TIMEOUT_SECONDS",
    "GRAPH_ORIGIN",
    "GRAPH_READ_TIMEOUT_SECONDS",
    "MAX_DIRECT_ATTACHMENT_BYTES",
    "MAX_GRAPH_ATTACHMENT_RESPONSE_BYTES",
    "MAX_GRAPH_MIME_BYTES",
    "GraphDeltaPage",
    "GraphDraft",
    "GraphFolder",
    "GraphHttpClient",
    "M365GraphCursorExpired",
    "M365GraphDraftUncertain",
    "M365GraphError",
    "M365GraphMessageUnavailable",
    "M365GraphReconnectRequired",
    "validate_outlook_web_link",
]
