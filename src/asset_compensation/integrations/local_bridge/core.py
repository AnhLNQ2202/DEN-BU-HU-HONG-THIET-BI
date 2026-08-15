"""Pure helpers and bounded HTTP client for the Classic Outlook bridge.

This module intentionally has no COM dependency so it can be tested on every
platform.  Outlook access is isolated in ``app.py`` and only happens after a
person clicks a visible button (or explicitly enables the five-minute timer).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

Role = Literal["ngan", "tran"]

MAX_EML_BYTES = 2 * 1024 * 1024
MAX_WORKBOOK_BYTES = 25 * 1024 * 1024
INITIAL_LOOKBACK_DAYS = 30
MAX_MAILS_PER_SCAN = 20
HTTP_TIMEOUT = (5, 30)
CLIENT_TYPE = "local_bridge"


class BridgeError(RuntimeError):
    """Safe, user-facing error raised by the bridge."""


@dataclass(frozen=True, slots=True)
class PairingSession:
    token: str
    role: Role
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class SourceReference:
    role: Role
    entry_id: str
    store_id: str
    subject: str = ""


@dataclass(frozen=True, slots=True)
class MailSnapshot:
    entry_id: str
    store_id: str
    received_at: datetime
    subject: str
    sender_name: str
    sender_address: str
    to: str
    cc: str
    sent_at: datetime | None
    transport_headers: str
    html_body: str
    plain_body: str


@dataclass(slots=True)
class ScanCheckpoint:
    """Session-only cursor; nothing from Outlook is persisted to disk."""

    latest_received_at: datetime | None = None
    uploaded_entry_ids: set[str] = field(default_factory=set)

    def record(self, snapshot: MailSnapshot) -> None:
        received_at = as_utc(snapshot.received_at)
        if self.latest_received_at is None or received_at > self.latest_received_at:
            self.latest_received_at = received_at
        self.uploaded_entry_ids.add(snapshot.entry_id)
        # Bound memory even if the bridge is kept open for a long time.
        if len(self.uploaded_entry_ids) > 2_000:
            self.uploaded_entry_ids = {snapshot.entry_id}


@dataclass(frozen=True, slots=True)
class DraftPackage:
    package_id: str
    artifact_handle: str
    subject: str
    body_html: str = ""
    workbook_filename: str = ""
    workbook_content_base64: str = ""


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone().astimezone(UTC)
    return value.astimezone(UTC)


def normalize_origin(raw: str) -> str:
    """Validate a server origin without ever accepting credentials or a path."""

    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError("Địa chỉ server không hợp lệ.")
    if parsed.path not in {"", "/"} or not parsed.hostname:
        raise BridgeError("Chỉ nhập địa chỉ gốc, ví dụ https://ten-app.onrender.com")
    if parsed.scheme not in {"http", "https"}:
        raise BridgeError("Địa chỉ server phải bắt đầu bằng https://")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise BridgeError("Server thật phải dùng https:// để bảo vệ mã kết nối.")
    return value


def subject_eml_filename(subject: str, fallback: str) -> str:
    """Return a bounded, Windows-safe display filename derived from Subject."""

    normalized = unicodedata.normalize("NFKC", str(subject or ""))
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", normalized)
    cleaned = " ".join(cleaned.split())[:80].rstrip(" .-")
    return f"{cleaned}.eml" if cleaned else fallback


def _clean_header(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _transport_value(headers: str, name: str) -> str:
    if not headers:
        return ""
    try:
        parsed = Parser(policy=policy.default).parsestr(headers[:256_000], headersonly=True)
        return _clean_header(parsed.get(name, ""), limit=4_000)
    except (TypeError, ValueError):
        return ""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
    except (TypeError, ValueError):
        return ""


def _base_message(snapshot: MailSnapshot) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = _clean_header(snapshot.subject, limit=500) or "(Không có tiêu đề)"
    from_header = _transport_value(snapshot.transport_headers, "From")
    if not from_header:
        display = _clean_header(snapshot.sender_name, limit=300)
        address = _clean_header(snapshot.sender_address, limit=500)
        from_header = f"{display} <{address}>" if display and address else address or display
    message["From"] = from_header or "unknown@example.invalid"
    message["To"] = _transport_value(snapshot.transport_headers, "To") or _clean_header(
        snapshot.to, limit=4_000
    ) or "undisclosed-recipients:;"
    cc_header = _transport_value(snapshot.transport_headers, "Cc") or _clean_header(
        snapshot.cc, limit=4_000
    )
    if cc_header:
        message["Cc"] = cc_header
    date_header = _transport_value(snapshot.transport_headers, "Date")
    if date_header:
        try:
            message["Date"] = date_header
        except ValueError:
            date_header = ""
    if not date_header:
        when = snapshot.sent_at or snapshot.received_at
        message["Date"] = format_datetime(as_utc(when))
    message_id = _transport_value(snapshot.transport_headers, "Message-ID")
    if re.fullmatch(r"<[^<>\s]{1,980}>", message_id):
        message["Message-ID"] = message_id
    message["X-Asset-Hub-Source"] = "classic-outlook-local-bridge"
    message["X-Asset-Hub-Attachments"] = "omitted-for-minimum-data"
    return message


def _render_message(snapshot: MailSnapshot, *, plain: str, html_body: str) -> bytes:
    message = _base_message(snapshot)
    message.set_content(plain or "(Email không có nội dung chữ.)")
    if html_body:
        # EmailMessage otherwise creates a random multipart boundary during
        # serialization.  That makes the same Outlook item produce different
        # bytes after a Bridge restart, which correctly trips the server's
        # Message-ID/content-conflict guard.  Derive a collision-safe boundary
        # from the exact semantic message instead; Outlook IDs are deliberately
        # excluded so moving an unchanged item does not alter its EML identity.
        seed = message.as_bytes() + b"\0" + html_body.encode("utf-8")
        message.add_alternative(html_body, subtype="html")
        digest = hashlib.sha256(seed).hexdigest()
        boundary = f"===============asset-hub-{digest[:32]}=="
        counter = 0
        while boundary in plain or boundary in html_body:
            counter += 1
            digest = hashlib.sha256(f"{digest}:{counter}".encode()).hexdigest()
            boundary = f"===============asset-hub-{digest[:32]}=="
        message.set_boundary(boundary)
    return message.as_bytes()


def build_minimized_eml(snapshot: MailSnapshot, *, max_bytes: int = MAX_EML_BYTES) -> bytes:
    """Build a valid RFC822 message while deliberately omitting attachments."""

    if max_bytes < 16_384:
        raise BridgeError("Giới hạn email quá nhỏ.")
    plain = (snapshot.plain_body or html_to_text(snapshot.html_body))[:1_500_000]
    html_body = (snapshot.html_body or "")[:1_500_000]
    rendered = _render_message(snapshot, plain=plain, html_body=html_body)
    if len(rendered) <= max_bytes:
        return rendered

    # Very large HTML is replaced by a bounded plain-text representation.  A
    # binary search keeps the MIME valid instead of slicing serialized bytes.
    fallback = plain or html_to_text(html_body)
    low, high = 0, min(len(fallback), max_bytes)
    best = b""
    while low <= high:
        middle = (low + high) // 2
        candidate = _render_message(snapshot, plain=fallback[:middle], html_body="")
        if len(candidate) <= max_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if not best:
        raise BridgeError("Email quá lớn để nạp an toàn (tối đa 2 MB).")
    return best


def select_scan_candidates(
    snapshots: Iterable[MailSnapshot],
    checkpoint: ScanCheckpoint,
    *,
    now: datetime | None = None,
    limit: int = MAX_MAILS_PER_SCAN,
) -> list[MailSnapshot]:
    """Select an exact-folder, bounded batch without changing Outlook items."""

    current = as_utc(now or datetime.now(UTC))
    cutoff = current - timedelta(days=INITIAL_LOOKBACK_DAYS)
    eligible: list[MailSnapshot] = []
    for snapshot in snapshots:
        received_at = as_utc(snapshot.received_at)
        if snapshot.entry_id in checkpoint.uploaded_entry_ids:
            continue
        if checkpoint.latest_received_at is None:
            if received_at < cutoff:
                continue
        elif received_at < checkpoint.latest_received_at:
            continue
        eligible.append(snapshot)

    eligible.sort(key=lambda item: as_utc(item.received_at))
    batch_size = max(0, limit)
    # Always take the oldest unseen messages inside the 30-day window.  This is
    # important on the first run: choosing the newest 20 would advance the
    # cursor past an older backlog and make those messages impossible to drain.
    # Repeated scans therefore walk forward without skipping a gap.
    return eligible[:batch_size]


class _SafeBodySanitizer(HTMLParser):
    _allowed_tags = {
        "a",
        "b",
        "blockquote",
        "br",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
    _void_tags = {"br", "hr"}
    _allowed_attrs = {"align", "colspan", "height", "rowspan", "style", "valign", "width"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self._allowed_tags:
            return
        kept: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if value is None:
                continue
            if name == "href" and tag == "a":
                if value.lower().startswith(("https://", "http://", "mailto:")):
                    kept.append(f' href="{html.escape(value, quote=True)}"')
                continue
            if name not in self._allowed_attrs:
                continue
            if name == "style" and re.search(
                r"url\s*\(|expression\s*\(|javascript:|@import|behavior\s*:", value, re.I
            ):
                continue
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._allowed_tags and tag not in self._void_tags:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def sanitize_reply_html(value: str, *, max_chars: int = 1_000_000) -> str:
    parser = _SafeBodySanitizer()
    try:
        parser.feed((value or "")[:max_chars])
        parser.close()
    except (TypeError, ValueError):
        raise BridgeError("Nội dung draft từ server không hợp lệ.") from None
    sanitized = "".join(parser.parts).strip()
    if not sanitized:
        raise BridgeError("Draft chưa có nội dung để mở trong Outlook.")
    return sanitized


def decode_workbook(filename: str, content_base64: str) -> tuple[str, bytes]:
    safe_name = PurePath(str(filename or "draft.xlsx").replace("\\", "/")).name
    safe_name = re.sub(r"[^A-Za-z0-9_. -]", "_", safe_name)[:120] or "draft.xlsx"
    if not safe_name.lower().endswith((".xlsx", ".xlsm")):
        raise BridgeError("File đính kèm của draft không phải workbook Excel.")
    encoded = str(content_base64 or "")
    if len(encoded) > ((MAX_WORKBOOK_BYTES + 2) // 3) * 4 + 16:
        raise BridgeError("Workbook lớn hơn 25 MB nên không được mở tự động.")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise BridgeError("Workbook từ server bị lỗi.") from None
    if not payload or len(payload) > MAX_WORKBOOK_BYTES:
        raise BridgeError("Workbook trống hoặc lớn hơn 25 MB.")
    # ZIP/Office signature.  This is only a quick corruption check; the server
    # remains responsible for generating the approved template.
    if not payload.startswith(b"PK\x03\x04"):
        raise BridgeError("Workbook từ server không đúng định dạng Excel.")
    return safe_name, payload


def package_from_json(payload: dict[str, Any]) -> DraftPackage:
    workbook = payload.get("workbook") if isinstance(payload.get("workbook"), dict) else {}
    package_id = str(payload.get("package_id") or payload.get("id") or "").strip()
    artifact_handle = str(payload.get("source_eml_handle") or "").strip()
    if not package_id or not artifact_handle:
        raise BridgeError("Gói draft từ server thiếu mã đối chiếu.")
    return DraftPackage(
        package_id=package_id,
        artifact_handle=artifact_handle,
        subject=str(payload.get("subject") or "Draft trả lời")[:500],
        body_html=str(payload.get("body_html") or ""),
        workbook_filename=str(workbook.get("filename") or ""),
        workbook_content_base64=str(workbook.get("content_base64") or ""),
    )


def match_package_source(
    package: DraftPackage,
    sources: Mapping[str, SourceReference],
    expected_role: Role,
    ambiguous_handles: set[str] | frozenset[str] = frozenset(),
) -> SourceReference:
    """Fail closed unless a package names this session's exact role-bound source."""

    source = sources.get(package.artifact_handle)
    if (
        source is None
        or source.role != expected_role
        or package.artifact_handle in ambiguous_handles
    ):
        raise BridgeError(
            "Không tìm thấy mail gốc trong phiên này. Hãy nạp mail bằng Local Bridge trước."
        )
    return source


class HubClient:
    """Small companion API client; its bearer token only exists in memory."""

    def __init__(self, origin: str, expected_role: Role) -> None:
        self.origin = normalize_origin(origin)
        self.expected_role = expected_role
        self._session = requests.Session()
        self._pairing: PairingSession | None = None

    @property
    def paired(self) -> bool:
        return self._pairing is not None

    def clear(self) -> None:
        self._pairing = None
        self._session.close()
        self._session = requests.Session()

    def pair(self, code: str) -> PairingSession:
        clean_code = "".join(str(code or "").split()).upper()
        if not 4 <= len(clean_code) <= 32:
            raise BridgeError("Mã kết nối chưa đúng. Hãy chép lại mã trên Product.")
        payload = self._request_json("POST", "/api/companion/exchange", json={"code": clean_code})
        token = str(payload.get("token") or "")
        role = str(payload.get("role") or "")
        client_type = str(payload.get("client_type") or "")
        if not token or role != self.expected_role or client_type != CLIENT_TYPE:
            raise BridgeError("Mã này không đúng ô Ngan/Tran hoặc không dành cho Local Bridge.")
        try:
            expires = max(0, int(payload.get("expires_in_seconds") or 0))
        except (TypeError, ValueError):
            expires = 0
        self._pairing = PairingSession(
            token=token, role=self.expected_role, expires_in_seconds=expires
        )
        return self._pairing

    def upload_eml(self, filename: str, content: bytes) -> str:
        if not content or len(content) > MAX_EML_BYTES:
            raise BridgeError("Email trống hoặc lớn hơn 2 MB.")
        response = self._request_json(
            "POST",
            "/api/companion/client/emails",
            files={"file": (filename, content, "message/rfc822")},
            authenticated=True,
            headers={"X-Asset-Hub-Upload": "companion-email-v1"},
        )
        source_eml = response.get("source_eml")
        handle = (
            str(source_eml.get("handle") or "").strip()
            if isinstance(source_eml, dict)
            else ""
        )
        if not handle:
            raise BridgeError("Server đã nhận mail nhưng không trả mã đối chiếu.")
        return handle

    def list_draft_packages(self) -> list[DraftPackage]:
        payload = self._request_json(
            "GET", "/api/companion/client/draft-packages", authenticated=True
        )
        raw_items = payload.get("packages")
        if not isinstance(raw_items, list):
            raise BridgeError("Danh sách draft từ server không đúng định dạng.")
        items: list[DraftPackage] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(package_from_json(raw))
            except BridgeError:
                continue
        return items

    def get_draft_package(self, package_id: str) -> DraftPackage:
        safe_id = _safe_package_id(package_id)
        payload = self._request_json(
            "GET", f"/api/companion/client/draft-packages/{safe_id}", authenticated=True
        )
        wrapped = payload.get("package")
        if not isinstance(wrapped, dict):
            raise BridgeError("Chi tiết draft từ server không đúng định dạng.")
        return package_from_json(wrapped)

    def acknowledge_draft(self, package_id: str) -> None:
        safe_id = _safe_package_id(package_id)
        self._request_json(
            "POST",
            f"/api/companion/client/draft-packages/{safe_id}/ack",
            json={},
            authenticated=True,
            headers={"X-Asset-Hub-Action": "companion-ack-v1"},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if authenticated:
            if self._pairing is None:
                raise BridgeError("Hãy bấm Kết nối trước.")
            request_headers["Authorization"] = f"Bearer {self._pairing.token}"
        try:
            response = self._session.request(
                method,
                f"{self.origin}{path}",
                headers=request_headers,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise BridgeError(
                "Không nói chuyện được với Product. Hãy kiểm tra mạng rồi thử lại."
            ) from exc
        if response.is_redirect:
            raise BridgeError("Server chuyển hướng bất thường; hãy kiểm tra đúng địa chỉ Product.")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            message = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(message, dict):
                message = message.get("message")
            safe_message = _clean_header(message, limit=300)
            raise BridgeError(safe_message or f"Product báo lỗi {response.status_code}.")
        if not isinstance(payload, dict):
            raise BridgeError("Product trả dữ liệu không đúng định dạng.")
        return payload


def _safe_package_id(value: str) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", clean):
        raise BridgeError("Mã draft không hợp lệ.")
    return clean
