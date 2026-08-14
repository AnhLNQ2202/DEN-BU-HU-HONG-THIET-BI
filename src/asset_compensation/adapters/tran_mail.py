"""HTML table and downloadable RFC822 draft builders for TranNNB outputs."""

from __future__ import annotations

import html
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses
from pathlib import Path
from typing import TYPE_CHECKING

from asset_compensation.domain import CompensationStatus, ValidationError

from .accounting_xlsx import OutputExistsError
from .tran_workbook import TRAN_SENT_HEADERS

if TYPE_CHECKING:
    from asset_compensation.services.tran_workflow_service import TranResolution


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


def _resolution_values(resolution: TranResolution) -> tuple[object, ...]:
    asset = resolution.asset
    preview = resolution.preview
    if asset is None or preview is None or preview.status is CompensationStatus.NEEDS_REVIEW:
        raise TranMailError("Every mail-table item must be fully resolved")
    remaining: object = preview.remaining_value
    if preview.status is CompensationStatus.EXEMPT:
        remaining = "Không tính đền bù"
    elif preview.status is CompensationStatus.NOT_APPLICABLE:
        remaining = "Không áp dụng"
    return (
        asset.tag_number,
        asset.asset_name,
        asset.domain,
        _date_text(asset.start_date),
        _date_text(asset.lost_date),
        _money(asset.cost),
        _money(remaining),
        _money(preview.fee_value),
        _money(preview.total_amount),
        preview.usage_months if preview.usage_months is not None else "",
        asset.book or "",
        asset.entity or "",
        asset.cost_center or "",
        asset.product_code or "",
        asset.location or "",
    )


def build_tran_mail_table(resolutions: Iterable[TranResolution]) -> str:
    """Return an escaped, inline-styled HTML copy of the request-only Sent out table."""

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
    cell_style = "border:1px solid #777;padding:5px 7px;font-family:Arial;font-size:10pt;"
    header_style = cell_style + "background:#d9eaf7;text-align:center;font-weight:600;"
    parts = ['<table role="table" style="border-collapse:collapse;border-spacing:0;">']
    parts.append("<thead><tr>")
    parts.extend(
        f'<th scope="col" style="{header_style}">{html.escape(header)}</th>'
        for header in TRAN_SENT_HEADERS
    )
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(
            f'<td style="{cell_style}">{html.escape(_mail_text(value))}</td>' for value in row
        )
        parts.append("</tr>")
    total_cells: list[object] = ["", "Total:", "", "", "", ""]
    total_cells.extend((_money(total_remaining), _money(total_fee), _money(total_amount)))
    total_cells.extend(("", "", "", "", "", ""))
    parts.append("<tr>")
    parts.extend(
        f'<td style="{header_style}">{html.escape(_mail_text(value))}</td>'
        for value in total_cells
    )
    parts.append("</tr></tbody></table>")
    return "".join(parts)


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
        message.set_content(intro)
        html_table = build_tran_mail_table(resolutions)
        html_intro = html.escape(intro).replace("\n", "<br>")
        message.add_alternative(
            '<div style="font-family:Arial;font-size:10pt;">'
            f"<p>{html_intro}</p>{html_table}</div>",
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
