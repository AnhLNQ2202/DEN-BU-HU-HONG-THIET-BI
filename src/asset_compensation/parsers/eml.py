"""Pure, standard-library parser for asset-compensation EML messages."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .contracts import CaseKind, ParsedCase


class EmlParseError(ValueError):
    """Raised when an EML cannot be classified as a compensation case."""


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "br",
        "div",
        "p",
        "li",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


_ASSET_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9-]{1,30}\d[A-Z0-9-]{0,20}\b", re.IGNORECASE)
_MONEY_TOKEN = r"-?\d[\d.,]*"
_DATE_TOKEN = r"\d{1,2}[/-]\d{1,2}[/-]\d{4}"


def decode_mime_header(value: str | None) -> str:
    """Decode an RFC 2047 header without depending on parser policy quirks."""

    if not value:
        return ""
    return str(make_header(decode_header(value)))


def parse_money(value: str) -> Decimal:
    """Parse whole-unit VND values written with either dot or comma groups."""

    cleaned = re.sub(r"(?i)(?:vnd|vnđ|đ|₫)", "", value).strip().replace(" ", "")
    if re.fullmatch(r"-?\d+", cleaned):
        normalized = cleaned
    else:
        grouped = re.fullmatch(
            r"(?P<sign>-?)\d{1,3}(?P<separator>[.,])\d{3}"
            r"(?:(?P=separator)\d{3})*",
            cleaned,
        )
        if grouped is None:
            raise EmlParseError("Email contains an invalid monetary value")
        normalized = grouped.group("sign") + re.sub(r"[.,]", "", cleaned.lstrip("-"))
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise EmlParseError("Email contains an invalid monetary value") from exc


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _part_content(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except (AttributeError, LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def _message_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_part_content(part))
        elif content_type == "text/html":
            html.append(_part_content(part))

    if plain:
        return _normalize_text(max(plain, key=len))
    if html:
        extractor = _TextExtractor()
        extractor.feed(max(html, key=len))
        return _normalize_text(extractor.text())
    return ""


def _case_type(subject: str, body: str) -> CaseKind:
    subject_folded = _fold(subject)
    body_folded = _fold(body)
    lost_score = 0
    damaged_score = 0

    if re.search(r"\b(that lac|mat thiet bi|mat tai san)\b", subject_folded):
        lost_score += 8
    if re.search(r"\b(that lac|ngay that lac|phi den bu trach nhiem)\b", body_folded):
        lost_score += 4
    if "mat thiet bi" in body_folded or "mat tai san" in body_folded:
        lost_score += 3

    if "hu hong" in subject_folded:
        damaged_score += 8
    if "tinh trang khac phuc" in body_folded or "chi phi sua chua" in body_folded:
        damaged_score += 4
    if "hu hong" in body_folded:
        damaged_score += 2

    if lost_score == damaged_score:
        raise EmlParseError("Cannot distinguish LOST from DAMAGED mail")
    return "LOST" if lost_score > damaged_score else "DAMAGED"


def _first_group(patterns: Iterable[str], value: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _asset_code(subject: str, body: str) -> str:
    labelled = _first_group(
        (
            r"(?:Mã thiết bị|Mã tài sản)\s*:\s*([A-Z][A-Z0-9-]*\d[A-Z0-9-]*)",
            r"Asset\s+Name\s*:\s*([A-Z][A-Z0-9-]*\d[A-Z0-9-]*)",
        ),
        body,
    )
    if labelled:
        return labelled.upper()
    subject_match = _ASSET_CODE_RE.search(subject)
    if subject_match:
        return subject_match.group(0).upper()
    body_match = _ASSET_CODE_RE.search(body)
    return body_match.group(0).upper() if body_match else ""


def _subject_domain(subject: str) -> str | None:
    match = re.search(
        r"-\s*([A-Za-z][A-Za-z0-9._-]{1,63})\s*(?:-\s*Đang làm việc\s*)?$",
        subject,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _domain(body: str, subject: str) -> str:
    labelled = _first_group(
        (
            r"(?:Người dùng|Người quản lý thiết bị|"
            r"Người sử dụng thiết bị)\s*:?\s*"
            r"([A-Za-z][A-Za-z0-9._-]{1,63})",
            r"\buser\s+([A-Za-z][A-Za-z0-9._-]{1,63})\b",
        ),
        body,
    )
    return labelled or _subject_domain(subject) or ""


def _lost_row(body: str, asset_code: str) -> dict[str, str]:
    if not asset_code:
        return {}
    pattern = re.compile(
        rf"\b{re.escape(asset_code)}\b\s+"
        rf"(?P<asset_name>.+?)\s+"
        rf"(?P<domain>[A-Za-z][A-Za-z0-9._-]{{1,63}})\s+"
        rf"(?P<start>{_DATE_TOKEN})\s+"
        rf"(?P<lost>{_DATE_TOKEN})\s+"
        rf"(?P<original>{_MONEY_TOKEN})\s+"
        rf"(?P<residual>{_MONEY_TOKEN})\s+"
        rf"(?P<fee>{_MONEY_TOKEN})\s+"
        rf"(?P<total>{_MONEY_TOKEN})\b",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.groupdict() if match else {}


def _total_block(body: str) -> tuple[Decimal, Decimal, Decimal] | None:
    match = re.search(
        rf"\bTotal\s*:\s*({_MONEY_TOKEN})\s+({_MONEY_TOKEN})\s+({_MONEY_TOKEN})",
        body,
        re.IGNORECASE,
    )
    if not match:
        return None
    return tuple(parse_money(value) for value in match.groups())  # type: ignore[return-value]


def _damaged_amount(body: str) -> Decimal | None:
    value = _first_group(
        (
            rf"Chi phí đền bù\s*:\s*({_MONEY_TOKEN})",
            rf"Số tiền user chịu trách nhiệm\s*:\s*({_MONEY_TOKEN})",
        ),
        body,
    )
    return parse_money(value) if value else None


def _asset_name(body: str, lost_row: dict[str, str]) -> str | None:
    if lost_row:
        return re.sub(r"\s+", " ", lost_row["asset_name"]).strip()
    return _first_group(
        (
            r"(?:Model|Tên thiết bị)\s*:\s*([^\n]+)",
            r"Product\s+Name\s*:\s*([^\n]+)",
        ),
        body,
    )


def _repair_status(body: str, case_type: CaseKind) -> str | None:
    if case_type == "LOST":
        return None
    folded = _fold(body)
    if "thiet bi khong sua chua" in folded:
        return "NOT_REPAIRED"
    if "thiet bi co sua chua" in folded:
        return "REPAIRED"
    return "UNKNOWN"


def _received_at(message: Message) -> datetime | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _employee_name(message: Message, domain: str) -> str | None:
    if not domain:
        return None
    address_header_names = {"from", "to", "cc"}
    raw_headers = [
        decode_mime_header(value)
        for name, value in message.raw_items()
        if name.casefold() in address_header_names
    ]
    address_pattern = re.compile(
        rf'(?:"(?P<quoted>[^"]+)"|(?P<plain>[^,<]+?))\s*'
        rf"<\s*{re.escape(domain)}@[^>]+>",
        re.IGNORECASE,
    )
    for raw_header in raw_headers:
        match = address_pattern.search(raw_header)
        if match:
            display_name = (match.group("quoted") or match.group("plain")).strip()
            if display_name:
                return decode_mime_header(display_name)

    addresses = getaddresses(raw_headers)
    for display_name, address in addresses:
        if address.partition("@")[0].casefold() == domain.casefold() and display_name:
            return decode_mime_header(display_name)
    return None


def _confirmed(body: str) -> bool:
    folded = _fold(body)
    patterns = (
        r"nguoi dung\s*:?.{0,80}dong y den bu",
        r"minh ok voi chi phi den bu",
        r"(?m)^\s*anh confirm nhe\s*[.!]?\s*$",
        r"\b(?:please|kindly)\s+confirm\b",
    )
    return any(re.search(pattern, folded, re.DOTALL) for pattern in patterns)


class EmlParser:
    """Parse an EML into one compensation candidate without side effects."""

    def parse(self, path: str | Path) -> ParsedCase:
        source = Path(path)
        return self.parse_bytes(source.read_bytes(), source_file=str(source))

    def parse_bytes(self, data: bytes, *, source_file: str | None = None) -> ParsedCase:
        try:
            message = BytesParser(policy=policy.default).parsebytes(data)
        except Exception as exc:  # malformed parser inputs vary by policy implementation
            raise EmlParseError(f"Invalid EML: {exc}") from exc

        subject = decode_mime_header(str(message.get("Subject", "")))
        body = _message_body(message)
        case_type = _case_type(subject, body)
        asset_code = _asset_code(subject, body)
        domain = _domain(body, subject)
        lost_row = _lost_row(body, asset_code) if case_type == "LOST" else {}
        if not domain and lost_row:
            domain = lost_row["domain"]

        warnings: list[str] = []
        if not asset_code:
            warnings.append("missing_asset_code")
        if not domain:
            warnings.append("missing_domain")

        residual_value: Decimal | None = None
        responsibility_fee: Decimal | None = None
        amount: Decimal | None
        metadata: dict[str, object] = {
            "subject": subject,
            "message_id": str(message.get("Message-ID", "")).strip() or None,
            "sender": str(message.get("From", "")).strip() or None,
            "confirmed": _confirmed(body),
        }

        if case_type == "LOST":
            if lost_row:
                metadata.update(
                    {
                        "usage_start": lost_row["start"],
                        "loss_date": lost_row["lost"],
                        "original_value": parse_money(lost_row["original"]),
                    }
                )
                residual_value = parse_money(lost_row["residual"])
                responsibility_fee = parse_money(lost_row["fee"])
                amount = parse_money(lost_row["total"])
            else:
                total = _total_block(body)
                if total:
                    residual_value, responsibility_fee, amount = total
                else:
                    amount = None
        else:
            amount = _damaged_amount(body)

        if amount is None:
            warnings.append("missing_amount")
        if metadata["confirmed"] and "qua thoi gian xu ly den bu" in _fold(body):
            warnings.append("confirmation_conflicts_with_deadline_forward")

        request_match = re.search(r"\b(?:DEMO-)?RE(?:Q)?-\d+\b", body, re.IGNORECASE)
        order_match = re.search(r"(?:#|DEMO-ORDER-)(\d{4,})\b", body, re.IGNORECASE)
        if request_match:
            metadata["request_id"] = request_match.group(0).upper()
        if order_match:
            metadata["order_id"] = order_match.group(1)

        source_id = str(message.get("Message-ID", "")).strip(" <>")
        if not source_id:
            source_id = hashlib.sha256(data).hexdigest()

        return ParsedCase(
            case_type=case_type,
            domain=domain,
            asset_code=asset_code,
            received_at=_received_at(message),
            employee_name=_employee_name(message, domain),
            asset_name=_asset_name(body, lost_row),
            amount=amount,
            residual_value=residual_value,
            responsibility_fee=responsibility_fee,
            repair_status=_repair_status(body, case_type),
            warnings=tuple(warnings),
            source_file=source_file,
            source_id=source_id,
            metadata=metadata,
        )


def parse_eml(path: str | Path) -> ParsedCase:
    """Convenience wrapper used by simple callers."""

    return EmlParser().parse(path)
