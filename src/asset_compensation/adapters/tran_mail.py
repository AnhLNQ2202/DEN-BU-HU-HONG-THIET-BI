"""HTML table and downloadable RFC822 draft builders for TranNNB outputs."""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import format_datetime, getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from asset_compensation.domain import CompensationStatus, ValidationError

from .accounting_xlsx import OutputExistsError
from .m365_contract import MAX_GRAPH_BODY_CHARS, MAX_TRAN_OUTLOOK_PREFIX_CHARS

if TYPE_CHECKING:
    from asset_compensation.services.tran_workflow_service import TranResolution


# These labels are the approved mail-facing template. They intentionally differ
# from the internal ``Sent out`` workbook headers, so the two contracts must not
# share a constant.
TRAN_MAIL_HEADERS = (
    "Asset Name",
    "Product Name",
    "Domain",
    "Ngày bắt đầu sử dụng",
    "Ngày thất lạc/mất",
    "Nguyên giá ban đầu (vnd)",
    "Mức khấu hao sử dụng còn lại (vnd)",
    "Phí đền bù trách nhiệm (vnd)",
    "Tổng số tiền đền bù (vnd)",
    "Thời gian đã sử dụng (tháng)",
    "NOTE",
    "Entity",
    "Cost center",
    "Product code",
    "Location",
)
_MAIL_MONEY_COLUMNS = frozenset({5, 6, 7, 8})
_MAIL_DATE_COLUMNS = frozenset({3, 4})
_MAIL_CENTER_COLUMNS = frozenset({2, 9, 10, 11, 12, 13, 14})
_MAIL_TOTAL_COLUMN = 8
_MAIL_CELL_STYLE = (
    "border:1px solid #000;padding:4px 8px;"
    "font-family:Arial,sans-serif;font-size:12px;"
)
_MAIL_HEADER_BACKGROUND = "#9CC2E5"
_MAIL_TOTAL_BACKGROUND = "#FFFF00"
_MAX_QUOTED_SOURCE_CHARS = 100_000
_MAX_SOURCE_HTML_CHARS = 500_000
_MAX_QUOTED_HEADER_CHARS = 2_000
_SOURCE_TRUNCATED_MARKER = "\n[quoted source truncated]"
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]{1,480}@[^<>\s]{1,480}>")
_OUTLOOK_QUOTE_TRUNCATED_MARKER = (
    "\n[Outlook quoted thread truncated for safe draft size]"
)
_HTML_DROP_WITH_CONTENT = frozenset(
    {
        "applet",
        "embed",
        "form",
        "head",
        "iframe",
        "math",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
    }
)
_HTML_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class TranMailError(ValidationError):
    """Raised when a safe draft cannot be constructed."""


@dataclass(frozen=True, slots=True)
class TranMailDraftResult:
    path: Path
    subject: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    attachment_name: str


def _mail_text(value: object) -> str:
    return str(value or "").strip()


def _header_text(value: object, field_name: str) -> str:
    text = _mail_text(value)
    if not text or "\r" in text or "\n" in text:
        raise TranMailError(f"{field_name} must be a single non-empty header value")
    return text


def _body_text(value: object, field_name: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise TranMailError(f"{field_name} must be non-empty")
    if len(text) > 10_000:
        raise TranMailError(f"{field_name} cannot exceed 10,000 characters")
    if any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or ord(character) == 127
        for character in text
    ):
        raise TranMailError(f"{field_name} contains unsupported control characters")
    return text


def _money(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return f"{int(value):,}"


def _date_text(value: object) -> str:
    return value.strftime("%d/%m/%Y") if hasattr(value, "strftime") else _mail_text(value)


def _clean_display_text(value: object) -> str:
    text = _mail_text(value)
    return text[1:] if text.startswith("'") else text


def _mail_cell_text(column: int, value: object) -> str:
    if column in _MAIL_MONEY_COLUMNS:
        return _money(value)
    if column in _MAIL_DATE_COLUMNS:
        return _date_text(value)
    return _clean_display_text(value)


def _mail_cell_alignment(column: int) -> str:
    if column in _MAIL_CENTER_COLUMNS:
        return "center"
    if column in _MAIL_MONEY_COLUMNS or column in _MAIL_DATE_COLUMNS:
        return "right"
    return "left"


def _resolution_values(resolution: TranResolution) -> tuple[object, ...]:
    asset = resolution.asset
    preview = resolution.preview
    if asset is None or preview is None or preview.status is CompensationStatus.NEEDS_REVIEW:
        raise TranMailError("Every mail-table item must be fully resolved")
    remaining: object = preview.remaining_value
    fee_value: object = preview.fee_value
    total_amount: object = preview.total_amount
    if preview.status is CompensationStatus.EXEMPT:
        remaining = "Không tính đền bù"
        fee_value = None
        total_amount = None
    elif preview.status is CompensationStatus.NOT_APPLICABLE:
        remaining = "Không áp dụng"
        fee_value = None
        total_amount = None
    return (
        asset.tag_number,
        asset.asset_name,
        asset.domain,
        _date_text(asset.start_date),
        _date_text(asset.lost_date),
        _money(asset.cost),
        _money(remaining),
        _money(fee_value),
        _money(total_amount),
        preview.usage_months if preview.usage_months is not None else "",
        asset.book or "",
        asset.entity or "",
        asset.cost_center or "",
        asset.product_code or "",
        asset.location or "",
    )


def build_tran_mail_table(resolutions: Iterable[TranResolution]) -> str:
    """Return the approved, escaped 15-column TranNNB mail table."""

    prepared = list(resolutions)
    if not prepared:
        raise TranMailError("Cannot build an empty compensation mail table")
    rows = [_resolution_values(item) for item in prepared]
    total_remaining = sum(
        item.preview.remaining_value or 0
        for item in prepared
        if item.preview is not None
        and item.preview.status is CompensationStatus.CALCULATED
    )
    total_fee = sum(item.preview.fee_value or 0 for item in prepared if item.preview)
    total_amount = sum(item.preview.total_amount or 0 for item in prepared if item.preview)
    header_style = (
        f"{_MAIL_CELL_STYLE}background:{_MAIL_HEADER_BACKGROUND};"
        "font-weight:bold;text-align:center;"
    )
    parts = ['<table role="table" style="border-collapse:collapse;">', "<tr>"]
    parts.extend(
        f'<td role="columnheader" style="{header_style}">{html.escape(header)}</td>'
        for header in TRAN_MAIL_HEADERS
    )
    parts.append("</tr>")
    for row in rows:
        parts.append("<tr>")
        for column, value in enumerate(row):
            background = (
                f"background:{_MAIL_TOTAL_BACKGROUND};"
                if column == _MAIL_TOTAL_COLUMN
                else ""
            )
            alignment = _mail_cell_alignment(column)
            cell_text = html.escape(_mail_cell_text(column, value))
            parts.append(
                f'<td style="{_MAIL_CELL_STYLE}{background}'
                f'text-align:{alignment};">{cell_text}</td>'
            )
        parts.append("</tr>")
    total_cells: list[object] = ["", "Total:", "", "", "", ""]
    total_cells.extend((_money(total_remaining), _money(total_fee), _money(total_amount)))
    total_cells.extend(("", "", "", "", "", ""))
    parts.append("<tr>")
    for column, value in enumerate(total_cells):
        background = (
            f"background:{_MAIL_TOTAL_BACKGROUND};"
            if column == _MAIL_TOTAL_COLUMN
            else ""
        )
        alignment = "right" if column in {6, 7, 8} else "left"
        parts.append(
            f'<td style="{_MAIL_CELL_STYLE}{background}font-weight:bold;'
            f'text-align:{alignment};">{html.escape(_mail_text(value))}</td>'
        )
    parts.append("</tr></table>")
    return "".join(parts)


def extract_source_message_id(original_eml: bytes) -> str:
    """Return the single exact RFC 822 Message-ID needed for Graph lookup."""

    if not original_eml:
        raise TranMailError("Source email is required")
    try:
        headers = BytesHeaderParser(policy=policy.default).parsebytes(original_eml)
    except Exception as exc:  # email parser exception types vary for malformed input
        raise TranMailError("Source email headers are invalid") from exc
    values = headers.get_all("Message-ID", [])
    if len(values) != 1:
        raise TranMailError("Source email must contain exactly one Message-ID header")
    value = str(values[0]).strip()
    if not _MESSAGE_ID_RE.fullmatch(value) or "\r" in value or "\n" in value:
        raise TranMailError("Source email Message-ID is invalid")
    return value


def build_tran_outlook_body(
    resolutions: Iterable[TranResolution],
    body_intro: str,
    quoted_body: str,
    quoted_content_type: str,
) -> str:
    """Prepend approved Tran content to the quote created by Outlook Graph."""

    intro = _body_text(body_intro, "body_intro")
    if not isinstance(quoted_body, str) or len(quoted_body) > MAX_GRAPH_BODY_CHARS:
        raise TranMailError("Outlook quoted body is invalid")
    html_intro = html.escape(intro).replace("\n", "<br>")
    table = build_tran_mail_table(resolutions)
    prefix = (
        '<div style="font-family:Arial,sans-serif;font-size:13px;color:#000;">'
        f"{html_intro}<br><br>{table}<br><br></div>"
    )
    if len(prefix) > MAX_TRAN_OUTLOOK_PREFIX_CHARS:
        raise TranMailError("Approved Outlook draft content exceeds the safe limit")
    quoted_html = _bounded_outlook_quote(
        quoted_body,
        quoted_content_type,
        MAX_GRAPH_BODY_CHARS - len(prefix),
    )
    result = prefix + quoted_html
    if len(result) > MAX_GRAPH_BODY_CHARS:  # pragma: no cover - invariant guard
        raise TranMailError("Outlook draft body exceeds the safe limit")
    return result


def _bounded_outlook_quote(
    quoted_body: str,
    quoted_content_type: str,
    budget: int,
) -> str:
    wrapper_start = '<div style="white-space:pre-wrap;">'
    wrapper_end = "</div>"
    content_type = str(quoted_content_type or "").casefold()
    has_unsafe_controls = any(
        (ord(character) < 32 and character not in {"\n", "\r", "\t"})
        or ord(character) == 127
        for character in quoted_body
    )
    if content_type == "html" and not has_unsafe_controls and len(quoted_body) <= budget:
        return quoted_body
    if content_type == "text" and not has_unsafe_controls:
        escaped = html.escape(quoted_body)
        candidate = f"{wrapper_start}{escaped}{wrapper_end}"
        if len(candidate) <= budget:
            return candidate
        visible_text = quoted_body
    elif content_type == "html":
        extractor = _SafeHtmlTextExtractor()
        try:
            extractor.feed(quoted_body[:_MAX_SOURCE_HTML_CHARS])
            extractor.close()
        except Exception:
            visible_text = ""
        else:
            visible_text = extractor.text()
    elif content_type == "text":
        visible_text = quoted_body
    else:
        raise TranMailError("Outlook quoted body type is invalid")

    normalized = "".join(
        character
        for character in visible_text.replace("\r\n", "\n").replace("\r", "\n")
        if (ord(character) >= 32 and ord(character) != 127)
        or character in {"\n", "\t"}
    )
    marker = _OUTLOOK_QUOTE_TRUNCATED_MARKER
    fixed = len(wrapper_start) + len(wrapper_end)
    if fixed + len(html.escape(marker)) > budget:
        raise TranMailError("Outlook quoted-body budget is invalid")
    low, high = 0, len(normalized)
    while low < high:
        midpoint = (low + high + 1) // 2
        escaped = html.escape(normalized[:midpoint].rstrip() + marker)
        if fixed + len(escaped) <= budget:
            low = midpoint
        else:
            high = midpoint - 1
    escaped = html.escape(normalized[:low].rstrip() + marker)
    return f"{wrapper_start}{escaped}{wrapper_end}"


def _bounded_source_text(value: object, limit: int) -> str:
    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(normalized) > limit
    safe = "".join(
        character
        for character in normalized[: limit + 1]
        if (ord(character) >= 32 and ord(character) != 127)
        or character in {"\n", "\t"}
    )
    if truncated or len(safe) > limit:
        return safe[:limit].rstrip() + _SOURCE_TRUNCATED_MARKER
    return safe.strip()


def _bounded_source_header(value: object) -> str:
    return re.sub(r"\s+", " ", _bounded_source_text(value, _MAX_QUOTED_HEADER_CHARS))


class _SafeHtmlTextExtractor(HTMLParser):
    """Extract bounded visible text without preserving HTML or resource attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._length = 0
        self._drop_stack: list[str] = []

    def _append(self, value: str) -> None:
        if not value or self._length >= _MAX_QUOTED_SOURCE_CHARS:
            return
        remaining = _MAX_QUOTED_SOURCE_CHARS - self._length
        chunk = value[:remaining]
        self._parts.append(chunk)
        self._length += len(chunk)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.casefold()
        if self._drop_stack:
            if name in _HTML_DROP_WITH_CONTENT:
                self._drop_stack.append(name)
            return
        if name in _HTML_DROP_WITH_CONTENT:
            self._drop_stack.append(name)
            return
        if name in _HTML_BLOCK_TAGS:
            self._append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.casefold()
        if (
            not self._drop_stack
            and name not in _HTML_DROP_WITH_CONTENT
            and name in _HTML_BLOCK_TAGS
        ):
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if self._drop_stack:
            if name == self._drop_stack[-1]:
                self._drop_stack.pop()
            return
        if name in _HTML_BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self._drop_stack:
            self._append(data)

    def text(self) -> str:
        value = "".join(self._parts).replace("\xa0", " ")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return _bounded_source_text(value, _MAX_QUOTED_SOURCE_CHARS)


def _message_part_text(part: Message) -> str:
    try:
        content = part.get_content()
    except (AttributeError, LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return content if isinstance(content, str) else ""


def _source_body_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None and not message.is_multipart():
        part = message
    if part is None:
        return ""
    content = _message_part_text(part)
    if part.get_content_type().casefold() != "text/html":
        return _bounded_source_text(content, _MAX_QUOTED_SOURCE_CHARS)
    extractor = _SafeHtmlTextExtractor()
    try:
        extractor.feed(content[:_MAX_SOURCE_HTML_CHARS])
        extractor.close()
    except Exception:
        return ""
    return extractor.text()


def _quoted_source(message: EmailMessage) -> tuple[str, str]:
    header_rows = (
        ("From", message.get("From")),
        ("Sent", message.get("Date")),
        ("To", message.get("To")),
        ("Cc", message.get("Cc")),
        ("Subject", message.get("Subject")),
    )
    prepared_headers = tuple(
        (label, text)
        for label, value in header_rows
        if (text := _bounded_source_header(value))
    )
    body = _source_body_text(message)
    plain_headers = "\n".join(f"{label}: {value}" for label, value in prepared_headers)
    plain = "\n\n--- Original message ---\n" + plain_headers
    if body:
        plain += f"\n\n{body}"

    html_headers = "".join(
        f"<div><strong>{html.escape(label)}:</strong> {html.escape(value)}</div>"
        for label, value in prepared_headers
    )
    html_body = (
        '<div style="margin-top:8px;white-space:pre-wrap;">'
        f"{html.escape(body)}</div>"
        if body
        else ""
    )
    quoted_html = (
        '<div style="margin-top:16px;border-top:1px solid #999;padding-top:8px;'
        'font-family:Arial,sans-serif;font-size:12px;color:#000;">'
        f"{html_headers}{html_body}</div>"
    )
    return plain, quoted_html


def _deduplicated_addresses(values: list[str], excluded: set[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen = set(excluded)
    for display_name, address in getaddresses(values):
        normalized = address.strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(f"{display_name} <{address}>" if display_name else address)
    return tuple(result)


def _atomic_write(payload: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}.",
        suffix=".eml",
        dir=destination.parent,
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temp_path, destination)
        except FileExistsError as exc:
            raise OutputExistsError(f"Destination already exists: {destination}") from exc
        except OSError:
            try:
                with temp_path.open("rb") as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
            except FileExistsError as exc:
                raise OutputExistsError(f"Destination already exists: {destination}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


class TranMailDraftBuilder:
    """Create an unsent reply-all draft; this adapter has no send capability."""

    def build(
        self,
        original_eml: bytes,
        resolutions: Iterable[TranResolution],
        attachment_path: str | Path,
        destination: str | Path,
        *,
        from_address: str,
        body_intro: str,
        now: datetime | None = None,
    ) -> TranMailDraftResult:
        sender = _header_text(from_address, "from_address")
        intro = _body_text(body_intro, "body_intro")
        if not original_eml:
            raise TranMailError("original_eml is required for a reply-all draft")
        original = BytesParser(policy=policy.default).parsebytes(original_eml)
        original_subject = _mail_text(original.get("Subject"))
        if not original_subject:
            raise TranMailError("Original email has no Subject")
        reply_source = _mail_text(original.get("Reply-To") or original.get("From"))
        excluded = {address.casefold() for _, address in getaddresses([sender]) if address}
        to = _deduplicated_addresses(
            [reply_source, *original.get_all("To", [])], excluded
        )
        to_addresses = {address.casefold() for _, address in getaddresses(list(to))}
        cc = _deduplicated_addresses(
            original.get_all("Cc", []), excluded | to_addresses
        )
        if not to:
            raise TranMailError("Reply-all draft has no recipient after excluding the sender")

        attachment = Path(attachment_path).resolve()
        if not attachment.is_file() or attachment.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise TranMailError("attachment_path must be an existing .xlsx or .xlsm output")
        destination_path = Path(destination).resolve()
        if destination_path.suffix.lower() != ".eml":
            raise TranMailError("Draft destination must use the .eml suffix")
        if destination_path.exists():
            raise OutputExistsError(f"Destination already exists: {destination_path}")

        subject = original_subject if original_subject.casefold().startswith("re:") else (
            f"Re: {original_subject}"
        )
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = sender
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message["Date"] = format_datetime(now or datetime.now(UTC))
        message["X-Unsent"] = "1"
        message_id = _mail_text(original.get("Message-ID"))
        if message_id:
            message["In-Reply-To"] = message_id
            references = _mail_text(original.get("References"))
            message["References"] = f"{references} {message_id}".strip()
        quoted_plain, quoted_html = _quoted_source(original)
        message.set_content(intro + quoted_plain)
        html_table = build_tran_mail_table(resolutions)
        html_intro = html.escape(intro).replace("\n", "<br>")
        message.add_alternative(
            '<div style="font-family:Arial,sans-serif;font-size:13px;color:#000;">'
            f"{html_intro}<br><br>{html_table}<br></div>{quoted_html}",
            subtype="html",
        )
        subtype = (
            "vnd.ms-excel.sheet.macroEnabled.12"
            if attachment.suffix.lower() == ".xlsm"
            else "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        message.add_attachment(
            attachment.read_bytes(),
            maintype="application",
            subtype=subtype,
            filename=attachment.name,
        )
        _atomic_write(message.as_bytes(), destination_path)
        return TranMailDraftResult(
            path=destination_path,
            subject=subject,
            to=to,
            cc=cc,
            attachment_name=attachment.name,
        )
