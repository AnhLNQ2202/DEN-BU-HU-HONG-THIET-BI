"""Pure, standard-library parser for asset-compensation EML messages.

The original desktop tool has two independent mail contracts:

* damaged-asset messages contain one or more ``Người dùng`` blocks;
* lost-asset messages contain an HTML asset table and are grouped by domain.

``EmlParser.parse_many`` is the lossless API. ``parse``/``parse_bytes`` and
``parse_eml`` keep the historic single-case facade for existing callers.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .contracts import CaseKind, CreditComponent, ParsedCase


class EmlParseError(ValueError):
    """Raised when an EML cannot be classified as a compensation case."""


class EmlSkipError(EmlParseError):
    """Raised when an original-tool exclusion rule intentionally skips a mail."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Email skipped by source rule: {reason}")
        self.reason = reason


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


class _TableExtractor(HTMLParser):
    """Extract raw cell text while preserving leading zeroes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table = []
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if tag == "table":
            if self._table_depth == 1 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
                self._row = None
                self._cell = None
            if self._table_depth:
                self._table_depth -= 1
            return
        if self._table_depth != 1:
            return
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(_normalize_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._table is not None and self._row is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._cell is not None and not self._ignored_depth:
            self._cell.append(data)


_ASSET_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9-]{1,30}\d[A-Z0-9-]{0,20}\b", re.IGNORECASE)
_MONEY_TOKEN = r"-?\d[\d.,]*"
_DATE_TOKEN = r"\d{1,2}[/-]\d{1,2}[/-]\d{4}"
_DOMAIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")

_DAMAGED_DOMAIN_RE = re.compile(
    r"(?:Người\s*dùng|Nguoi\s*dung)\s*:?\s*([^\s,;\n]+)", re.IGNORECASE
)
_DAMAGED_AMOUNT_RE = re.compile(
    r"(?:Chi\s*phí\s*đền\s*bù|Chi\s*phi\s*den\s*bu)\s*:?\s*([\d.,]+)",
    re.IGNORECASE,
)
_REPAIR_AMOUNT_RE = re.compile(
    r"(?:Chi\s*phí\s*sửa\s*chữa|Chi\s*phi\s*sua\s*chua)\s*:?\s*([\d.,]+)",
    re.IGNORECASE,
)
_REPAIRED_ASSET_RE = re.compile(
    r"(?:Thiết\s*bị\s*có\s*sửa\s*chữa|Thiet\s*bi\s*co\s*sua\s*chua)"
    r"\s*:?\s*([A-Za-z0-9\-/]+)",
    re.IGNORECASE,
)
_NOT_REPAIRED_ASSET_RE = re.compile(
    r"(?:Thiết\s*bị\s*không\s*sửa\s*chữa|Thiet\s*bi\s*khong\s*sua\s*chua)"
    r"\s*:?\s*([A-Za-z0-9\-/]+)",
    re.IGNORECASE,
)

PREPAYMENT_POLICY = "ASSET_COMPENSATION_PREPAYMENT"
DAMAGED_REPAIR_POLICY = "DAMAGED_REPAIR"
DAMAGED_NO_REPAIR_POLICY = "DAMAGED_NO_REPAIR"
LOST_DEPRECIATION_ASSET_POLICY = "LOST_DEPRECIATION_ASSET"
LOST_DEPRECIATION_OTHER_POLICY = "LOST_DEPRECIATION_OTHER"
LOST_RESPONSIBILITY_POLICY = "LOST_RESPONSIBILITY"


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


def _html_text(value: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(value)
    extractor.close()
    return _normalize_text(extractor.text())


def _part_content(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except (AttributeError, LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def _message_parts(message: Message) -> tuple[str, str]:
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
    return (
        _normalize_text(max(plain, key=len)) if plain else "",
        max(html, key=len) if html else "",
    )


def _skip_reason(subject: str, body: str) -> str | None:
    haystack = f"{subject} {body}".casefold()
    # The source script used a raw substring check. Preserve its intended MOU
    # rule without false positives for ordinary values such as "mouse" or
    # "amount" that happen to contain the same three letters.
    if re.search(r"(?<![-\w])mou(?![-\w])", haystack):
        return "MOU"
    folded = _fold(haystack)
    if re.search(r"loi\s+ky\s+thuat\s*-\s*khong\s+den\s+bu", folded):
        return "TECHNICAL_NO_COMPENSATION"
    return None


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


def _clean_domain(value: str) -> str:
    cleaned = value.strip().strip(".,;:<>()[]{}")
    if "@" in cleaned:
        cleaned = cleaned.partition("@")[0]
    return cleaned if _DOMAIN_RE.fullmatch(cleaned) else ""


def _subject_domain(subject: str) -> str | None:
    match = re.search(
        r"-\s*([A-Za-z][A-Za-z0-9._-]{0,63})\s*(?:-\s*Đang làm việc\s*)?$",
        subject,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _domain(body: str, subject: str) -> str:
    labelled = _first_group(
        (
            r"(?:Người dùng|Người quản lý thiết bị|Người sử dụng thiết bị)"
            r"\s*:?\s*([A-Za-z][A-Za-z0-9._-]{0,63})",
            r"\buser\s+([A-Za-z][A-Za-z0-9._-]{0,63})\b",
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
        rf"(?P<domain>[A-Za-z][A-Za-z0-9._-]{{0,63}})\s+"
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


def _asset_name(body: str, lost_row: Mapping[str, str] | None = None) -> str | None:
    if lost_row:
        return re.sub(r"\s+", " ", lost_row["asset_name"]).strip()
    return _first_group(
        (
            r"(?:Model|Tên thiết bị)\s*:\s*([^\n]+)",
            r"Product\s+Name\s*:\s*([^\n]+)",
        ),
        body,
    )


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
        rf'(?:(?:"(?P<quoted>[^"]+)")|(?P<plain>[^,<]+?))\s*'
        rf"<\s*{re.escape(domain)}@[^>]+>",
        re.IGNORECASE,
    )
    for raw_header in raw_headers:
        match = address_pattern.search(raw_header)
        if match:
            display_name = (match.group("quoted") or match.group("plain")).strip()
            if display_name:
                return decode_mime_header(display_name)
    for display_name, address in getaddresses(raw_headers):
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


def _source_id(message: Message, data: bytes) -> str:
    value = str(message.get("Message-ID", "")).strip(" <>")
    return value or hashlib.sha256(data).hexdigest()


def _base_metadata(message: Message, subject: str, body: str) -> dict[str, object]:
    metadata: dict[str, object] = {
        "subject": subject,
        "message_id": str(message.get("Message-ID", "")).strip() or None,
        "sender": str(message.get("From", "")).strip() or None,
        "confirmed": _confirmed(body),
    }
    request_match = re.search(r"\b(?:DEMO-)?RE(?:Q)?-\d+\b", body, re.IGNORECASE)
    order_match = re.search(r"(?:#|DEMO-ORDER-)(\d{4,})\b", body, re.IGNORECASE)
    if request_match:
        metadata["request_id"] = request_match.group(0).upper()
    if order_match:
        metadata["order_id"] = order_match.group(1)
    if re.search(r"nghỉ\s*việc|nghi\s*viec", subject, re.IGNORECASE):
        metadata["employee_inactive"] = True
        metadata["employee_status_source"] = "EMAIL_SUBJECT"
    return metadata


def _table_amount(value: str) -> Decimal | None:
    folded = _fold(value)
    if not any(char.isdigit() for char in value) or "khong tinh den bu" in folded:
        return None
    try:
        return parse_money(value)
    except EmlParseError:
        return None


def _column(headers: list[str], *alternatives: tuple[str, ...]) -> int | None:
    folded = [_fold(value) for value in headers]
    for tokens in alternatives:
        wanted = tuple(_fold(token) for token in tokens)
        for index, value in enumerate(folded):
            if all(token in value for token in wanted):
                return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None and index < len(row) else ""


def _initial_lost_notice_rows(html: str) -> list[dict[str, str]]:
    """Read the small Vietnamese asset table from the first IT loss notice."""

    extractor = _TableExtractor()
    extractor.feed(html)
    extractor.close()
    expected_headers = {
        "ten thiet bi",
        "ma thiet bi",
        "tinh trang",
        "ghi chu",
    }
    for table in extractor.tables:
        for header_index, header_candidate in enumerate(table):
            folded_headers = [
                _fold(_normalize_text(value)).strip() for value in header_candidate
            ]
            if (
                len(folded_headers) != len(expected_headers)
                or set(folded_headers) != expected_headers
            ):
                continue
            columns = {name: folded_headers.index(name) for name in expected_headers}
            parsed_rows: list[dict[str, str]] = []
            for row in table[header_index + 1 :]:
                if len(row) != len(folded_headers):
                    raise EmlParseError(
                        "Initial lost-device table contains an invalid asset row"
                    )
                asset_name = _cell(row, columns["ten thiet bi"])
                asset_code = _cell(row, columns["ma thiet bi"]).upper()
                reported_status = _cell(row, columns["tinh trang"])
                if (
                    not asset_name
                    or _ASSET_CODE_RE.fullmatch(asset_code) is None
                    or re.search(r"\b(?:that lac|mat)\b", _fold(reported_status)) is None
                ):
                    raise EmlParseError(
                        "Initial lost-device table contains an invalid asset row"
                    )
                parsed_rows.append(
                    {
                        "asset_code": asset_code,
                        "asset_name": asset_name,
                        "reported_status": reported_status,
                        "source_note": _cell(row, columns["ghi chu"]),
                    }
                )
            if parsed_rows:
                return parsed_rows
            raise EmlParseError(
                "Initial lost-device table does not contain any asset rows"
            )
    return []


def _asset_table(html: str) -> tuple[list[dict[str, object]], int] | None:
    extractor = _TableExtractor()
    extractor.feed(html)
    extractor.close()
    selected: tuple[list[str], list[list[str]]] | None = None
    for table in extractor.tables:
        for index, header_candidate in enumerate(table):
            folded = [_fold(value) for value in header_candidate]
            if any("asset name" in value for value in folded) and any(
                "note" in value for value in folded
            ):
                selected = (header_candidate, table[index + 1 :])
                break
        if selected:
            break
    if selected is None:
        return None

    headers, data_rows = selected
    columns = {
        "asset": _column(headers, ("asset name",)),
        "product": _column(headers, ("product name",)),
        "domain": _column(headers, ("domain",)),
        "residual": _column(headers, ("khau hao",)),
        "fee": _column(headers, ("trach nhiem",)),
        "total": _column(headers, ("tong so tien",), ("tong",)),
        "note": _column(headers, ("note",)),
        "entity": _column(headers, ("entity",)),
        "cost_center": _column(headers, ("cost center",)),
        "product_code": _column(headers, ("product code",)),
        "location": _column(headers, ("location",)),
        "usage_start": _column(headers, ("ngay bat dau su dung",), ("start date",)),
        "loss_date": _column(headers, ("ngay that lac",), ("ngay mat",), ("loss date",)),
        "original_value": _column(headers, ("nguyen gia",), ("original value",)),
    }
    required = ("asset", "domain", "residual", "fee", "total", "note")
    if any(columns[name] is None for name in required):
        raise EmlParseError("Lost-asset table is missing a required source column")

    parsed_rows: list[dict[str, object]] = []
    skipped = 0
    for row in data_rows:
        asset_code = _cell(row, columns["asset"])
        if not asset_code or _fold(asset_code).startswith("total"):
            continue
        domain = _clean_domain(_cell(row, columns["domain"]))
        if not domain:
            continue
        residual = _table_amount(_cell(row, columns["residual"]))
        fee = _table_amount(_cell(row, columns["fee"]))
        total = _table_amount(_cell(row, columns["total"]))
        if residual is None or fee is None or total is None:
            skipped += 1
            continue
        product_name = _cell(row, columns["product"])
        entity = _cell(row, columns["entity"])
        parsed_rows.append(
            {
                "asset_code": asset_code,
                "asset_name": product_name or asset_code,
                "domain": domain,
                "residual_value": residual,
                "responsibility_fee": fee,
                "total": total,
                "note": _cell(row, columns["note"]),
                "entity": entity,
                "entity_non_vng": bool(entity) and entity.strip().upper() != "VNG",
                "cost_center": _cell(row, columns["cost_center"]).lstrip("'") or "0000",
                "product_code": _cell(row, columns["product_code"]).lstrip("'") or "000",
                "location": _cell(row, columns["location"]).lstrip("'") or "01",
                "usage_start": _cell(row, columns["usage_start"]),
                "loss_date": _cell(row, columns["loss_date"]),
                "original_value": _table_amount(_cell(row, columns["original_value"])),
            }
        )
    return parsed_rows, skipped


def _credit_components(rows: list[dict[str, object]]) -> tuple[CreditComponent, ...]:
    depreciation: dict[tuple[str, str, str, str], list[object]] = {}
    responsibility: dict[tuple[str, str, str], list[object]] = {}
    for row in rows:
        non_vng = bool(row["entity_non_vng"])
        dep_key = (
            str(row["note"]).casefold(),
            str(row["cost_center"]),
            str(row["product_code"]),
            str(row["location"]),
        )
        dep = depreciation.setdefault(dep_key, [Decimal(0), False])
        dep[0] = Decimal(dep[0]) + Decimal(row["residual_value"])
        dep[1] = bool(dep[1]) or non_vng
        fee_key = (
            str(row["cost_center"]),
            str(row["product_code"]),
            str(row["location"]),
        )
        fee = responsibility.setdefault(fee_key, [Decimal(0), False])
        fee[0] = Decimal(fee[0]) + Decimal(row["responsibility_fee"])
        fee[1] = bool(fee[1]) or non_vng

    components: list[CreditComponent] = []
    for (note, cost_center, product_code, location), (amount, non_vng) in depreciation.items():
        exact_amount = Decimal(amount)
        if exact_amount == 0:
            continue
        components.append(
            CreditComponent(
                policy_key=(
                    LOST_DEPRECIATION_ASSET_POLICY
                    if note == "asset"
                    else LOST_DEPRECIATION_OTHER_POLICY
                ),
                amount=exact_amount,
                cost_center=cost_center,
                product_code=product_code,
                location=location,
                entity_non_vng=bool(non_vng),
            )
        )
    for (cost_center, product_code, location), (amount, non_vng) in responsibility.items():
        exact_amount = Decimal(amount)
        if exact_amount == 0:
            continue
        components.append(
            CreditComponent(
                policy_key=LOST_RESPONSIBILITY_POLICY,
                amount=exact_amount,
                cost_center=cost_center,
                product_code=product_code,
                location=location,
                entity_non_vng=bool(non_vng),
            )
        )
    return tuple(components)


def _lost_table_cases(
    message: Message,
    subject: str,
    body: str,
    table_rows: list[dict[str, object]],
    skipped_rows: int,
    *,
    source_file: str | None,
    source_id: str,
) -> tuple[ParsedCase, ...]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in table_rows:
        grouped.setdefault(str(row["domain"]).casefold(), []).append(row)

    cases: list[ParsedCase] = []
    for rows in grouped.values():
        domain = str(rows[0]["domain"])
        asset_codes = tuple(dict.fromkeys(str(row["asset_code"]) for row in rows))
        asset_names = tuple(dict.fromkeys(str(row["asset_name"]) for row in rows))
        residual = sum((Decimal(row["residual_value"]) for row in rows), Decimal(0))
        fee = sum((Decimal(row["responsibility_fee"]) for row in rows), Decimal(0))
        total = sum((Decimal(row["total"]) for row in rows), Decimal(0))
        components = _credit_components(rows)
        credit_total = sum((component["amount"] for component in components), Decimal(0))
        warnings: list[str] = []
        if credit_total.copy_abs() != total:
            warnings.append("Prepayment total does not reconcile to credit components")
        if any(bool(row["entity_non_vng"]) for row in rows):
            warnings.append("One or more assets belong to an entity other than VNG")
        if skipped_rows:
            warnings.append(f"{skipped_rows} non-compensable asset row(s) were skipped")

        metadata = _base_metadata(message, subject, body)
        metadata.update(
            {
                "prepayment_policy_key": PREPAYMENT_POLICY,
                "credit_components": list(components),
                "credit_total": credit_total,
                "asset_count": len(rows),
                "source_table_row_count": len(table_rows) + skipped_rows,
                "non_compensable_row_count": skipped_rows,
                "entity_non_vng": any(bool(row["entity_non_vng"]) for row in rows),
                "credit_mismatch": credit_total.copy_abs() != total,
                "asset_rows": [dict(row) for row in rows],
            }
        )
        usage_dates = tuple(
            dict.fromkeys(str(row["usage_start"]) for row in rows if row["usage_start"])
        )
        loss_dates = tuple(
            dict.fromkeys(str(row["loss_date"]) for row in rows if row["loss_date"])
        )
        original_values = [row["original_value"] for row in rows]
        if len(usage_dates) == 1:
            metadata["usage_start"] = usage_dates[0]
        elif len(usage_dates) > 1:
            warnings.append("Grouped assets have multiple usage-start dates")
        if len(loss_dates) == 1:
            metadata["loss_date"] = loss_dates[0]
        elif len(loss_dates) > 1:
            warnings.append("Grouped assets have multiple loss dates")
        if original_values and all(value is not None for value in original_values):
            metadata["original_value"] = sum(
                (Decimal(value) for value in original_values), Decimal(0)
            )

        cases.append(
            ParsedCase(
                case_type="LOST",
                domain=domain,
                asset_code=", ".join(asset_codes),
                received_at=_received_at(message),
                employee_name=_employee_name(message, domain),
                asset_name=", ".join(asset_names),
                amount=total,
                residual_value=residual,
                responsibility_fee=fee,
                warnings=tuple(warnings),
                source_file=source_file,
                source_id=source_id,
                metadata=metadata,
            )
        )
    return tuple(cases)


def _damaged_records(body: str, subject: str) -> list[dict[str, object]]:
    anchors = list(_DAMAGED_DOMAIN_RE.finditer(body))
    records: list[dict[str, object]] = []
    for index, anchor in enumerate(anchors):
        domain = _clean_domain(anchor.group(1))
        block_end = anchors[index + 1].start() if index + 1 < len(anchors) else len(body)
        block = body[anchor.start() : block_end]
        amount_match = _DAMAGED_AMOUNT_RE.search(block)
        if not amount_match:
            continue
        amount = parse_money(amount_match.group(1))
        repair_match = _REPAIR_AMOUNT_RE.search(block)
        repair_cost = parse_money(repair_match.group(1)) if repair_match else Decimal(0)
        repaired_assets = _REPAIRED_ASSET_RE.findall(block)
        not_repaired_assets = _NOT_REPAIRED_ASSET_RE.findall(block)
        explicit = [(asset, True) for asset in repaired_assets]
        explicit.extend((asset, False) for asset in not_repaired_assets)
        if not explicit:
            fallback = _asset_code(subject, block) or "N/A"
            explicit = [(fallback, repair_cost > 0)]
        for asset, repaired in explicit:
            records.append(
                {
                    "domain": domain,
                    "asset_code": str(asset).upper(),
                    "amount": amount,
                    "repair_cost": repair_cost,
                    "repaired": repaired,
                    "block": block,
                }
            )
    return records


def _damaged_cases(
    message: Message,
    subject: str,
    body: str,
    records: list[dict[str, object]],
    *,
    source_file: str | None,
    source_id: str,
) -> tuple[ParsedCase, ...]:
    cases: list[ParsedCase] = []
    for index, record in enumerate(records, start=1):
        repaired = bool(record["repaired"])
        warnings: list[str] = []
        if record["asset_code"] == "N/A":
            warnings.append("Asset code was not present in the source email")
        metadata = _base_metadata(message, subject, body)
        metadata.update(
            {
                "prepayment_policy_key": PREPAYMENT_POLICY,
                "credit_components": [
                    CreditComponent(
                        policy_key=(
                            DAMAGED_REPAIR_POLICY if repaired else DAMAGED_NO_REPAIR_POLICY
                        ),
                        amount=Decimal(record["amount"]),
                        cost_center="",
                        product_code="",
                        location="",
                        entity_non_vng=False,
                    )
                ],
                "repair_cost": Decimal(record["repair_cost"]),
                "source_record_index": index,
                "source_record_count": len(records),
            }
        )
        cases.append(
            ParsedCase(
                case_type="DAMAGED",
                domain=str(record["domain"]),
                asset_code=str(record["asset_code"]),
                received_at=_received_at(message),
                employee_name=_employee_name(message, str(record["domain"])),
                asset_name=_asset_name(str(record["block"])),
                amount=Decimal(record["amount"]),
                repair_status="REPAIRED" if repaired else "NOT_REPAIRED",
                warnings=tuple(warnings),
                source_file=source_file,
                source_id=source_id,
                metadata=metadata,
            )
        )
    return tuple(cases)


def _fallback_case(
    message: Message,
    subject: str,
    body: str,
    *,
    source_file: str | None,
    source_id: str,
    initial_lost_rows: list[dict[str, str]] | None = None,
) -> ParsedCase:
    case_type = _case_type(subject, body)
    asset_code = _asset_code(subject, body)
    domain = _domain(body, subject)
    lost_row = _lost_row(body, asset_code) if case_type == "LOST" else {}
    if not domain and lost_row:
        domain = lost_row["domain"]

    residual_value: Decimal | None = None
    responsibility_fee: Decimal | None = None
    metadata = _base_metadata(message, subject, body)
    notice_asset_name: str | None = None
    if case_type == "LOST" and initial_lost_rows:
        source_rows = [
            {
                **row,
                "domain": domain,
            }
            for row in initial_lost_rows
        ]
        asset_code = ", ".join(
            dict.fromkeys(row["asset_code"] for row in initial_lost_rows)
        )
        notice_asset_name = ", ".join(
            dict.fromkeys(row["asset_name"] for row in initial_lost_rows)
        )
        metadata.update(
            {
                "asset_count": len(source_rows),
                "source_table_row_count": len(source_rows),
                "source_table_kind": "INITIAL_LOSS_NOTICE",
                "asset_rows": source_rows,
            }
        )
    warnings: list[str] = []
    if not asset_code:
        warnings.append("missing_asset_code")
    if not domain:
        warnings.append("missing_domain")
    amount: Decimal | None
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
        metadata["prepayment_policy_key"] = PREPAYMENT_POLICY
        metadata["credit_dimensions_complete"] = False
    else:
        amount_value = _first_group(
            (
                rf"Chi phí đền bù\s*:\s*({_MONEY_TOKEN})",
                rf"Số tiền user chịu trách nhiệm\s*:\s*({_MONEY_TOKEN})",
            ),
            body,
        )
        amount = parse_money(amount_value) if amount_value else None
    if amount is None:
        warnings.append("missing_amount")
    if metadata["confirmed"] and "qua thoi gian xu ly den bu" in _fold(body):
        warnings.append("confirmation_conflicts_with_deadline_forward")

    repair_status: str | None = None
    if case_type == "DAMAGED":
        folded = _fold(body)
        if "thiet bi khong sua chua" in folded:
            repair_status = "NOT_REPAIRED"
        elif "thiet bi co sua chua" in folded:
            repair_status = "REPAIRED"
        else:
            repair_status = "UNKNOWN"
    return ParsedCase(
        case_type=case_type,
        domain=domain,
        asset_code=asset_code,
        received_at=_received_at(message),
        employee_name=_employee_name(message, domain),
        asset_name=notice_asset_name or _asset_name(body, lost_row),
        amount=amount,
        residual_value=residual_value,
        responsibility_fee=responsibility_fee,
        repair_status=repair_status,
        warnings=tuple(warnings),
        source_file=source_file,
        source_id=source_id,
        metadata=metadata,
    )


class EmlParser:
    """Parse EML messages without filesystem or persistence side effects."""

    def parse_many(self, path: str | Path) -> tuple[ParsedCase, ...]:
        source = Path(path)
        return self.parse_bytes_many(source.read_bytes(), source_file=str(source))

    def parse(self, path: str | Path) -> ParsedCase:
        """Backward-compatible facade returning the first parsed case."""

        return self.parse_many(path)[0]

    def parse_bytes_many(
        self,
        data: bytes,
        *,
        source_file: str | None = None,
    ) -> tuple[ParsedCase, ...]:
        try:
            message = BytesParser(policy=policy.default).parsebytes(data)
        except Exception as exc:  # malformed parser inputs vary by policy implementation
            raise EmlParseError(f"Invalid EML: {exc}") from exc

        subject = decode_mime_header(str(message.get("Subject", "")))
        plain, html = _message_parts(message)
        html_text = _html_text(html) if html else ""
        body = plain or html_text
        reason = _skip_reason(subject, f"{plain}\n{html_text}")
        if reason:
            raise EmlSkipError(reason)

        source_id = _source_id(message, data)
        initial_lost_rows: list[dict[str, str]] = []
        if html:
            table = _asset_table(html)
            if table is not None:
                rows, skipped = table
                if rows:
                    return _lost_table_cases(
                        message,
                        subject,
                        body,
                        rows,
                        skipped,
                        source_file=source_file,
                        source_id=source_id,
                    )
                if skipped:
                    raise EmlSkipError("NO_COMPENSABLE_ASSETS")
            initial_lost_rows = _initial_lost_notice_rows(html)

        damaged = _damaged_records(body, subject)
        if damaged:
            return _damaged_cases(
                message,
                subject,
                body,
                damaged,
                source_file=source_file,
                source_id=source_id,
            )
        return (
            _fallback_case(
                message,
                subject,
                body,
                source_file=source_file,
                source_id=source_id,
                initial_lost_rows=initial_lost_rows,
            ),
        )

    def parse_bytes(self, data: bytes, *, source_file: str | None = None) -> ParsedCase:
        """Backward-compatible facade returning the first parsed case."""

        return self.parse_bytes_many(data, source_file=source_file)[0]


def parse_eml(path: str | Path) -> ParsedCase:
    """Backward-compatible convenience wrapper returning the first case."""

    return EmlParser().parse(path)


def parse_eml_many(path: str | Path) -> tuple[ParsedCase, ...]:
    """Parse every record/domain represented by one EML."""

    return EmlParser().parse_many(path)
