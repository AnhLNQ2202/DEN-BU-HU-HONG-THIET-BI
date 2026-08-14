"""Auditable, no-clobber XLSX export for accounting batches."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


class AccountingExportError(RuntimeError):
    """Base class for accounting export failures."""


class AccountingValidationError(AccountingExportError, ValueError):
    """Raised before any destination is changed when accounting data is invalid."""


class OutputExistsError(AccountingExportError, FileExistsError):
    """Raised when overwrite was not explicitly authorized."""


@dataclass(frozen=True, slots=True)
class GlAccountPair:
    debit: str
    credit: str


@dataclass(frozen=True, slots=True)
class AccountingExportResult:
    path: Path
    case_count: int
    journal_line_count: int
    source_total: Decimal
    debit_total: Decimal
    credit_total: Decimal


@dataclass(frozen=True, slots=True)
class _PreparedCase:
    case_id: str
    case_type: str
    domain: str
    employee_name: str
    asset_code: str
    asset_name: str
    received_at: datetime | date | None
    amount: Decimal
    residual_value: Decimal | None
    responsibility_fee: Decimal | None
    repair_status: str
    supplier_number: str
    supplier_site: str
    source_file: str
    warnings: tuple[str, ...]
    debit_gl: str
    credit_lines: tuple[tuple[str, Decimal], ...]


_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_DANGEROUS_EXCEL_PREFIXES = ("=", "+", "-", "@")


def _case_value(case: object, name: str, default: Any = None) -> Any:
    if isinstance(case, Mapping):
        return case.get(name, default)
    return getattr(case, name, default)


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().upper()


def _decimal(value: object, *, field_name: str, allow_none: bool = False) -> Decimal | None:
    if value in (None, ""):
        if allow_none:
            return None
        raise AccountingValidationError(f"{field_name} is required")
    if isinstance(value, bool):
        raise AccountingValidationError(f"{field_name} must be a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AccountingValidationError(f"Invalid {field_name}: {value!r}") from exc
    if not result.is_finite():
        raise AccountingValidationError(f"{field_name} must be finite")
    if result != result.to_integral_value():
        raise AccountingValidationError(f"{field_name} must use whole VND units")
    return result


def _excel_text(value: object) -> str:
    text = str(value or "")
    if text.lstrip().startswith(_DANGEROUS_EXCEL_PREFIXES):
        return "'" + text
    return text


def _account_pair(value: GlAccountPair | tuple[str, str]) -> GlAccountPair:
    if isinstance(value, GlAccountPair):
        pair = value
    else:
        try:
            debit, credit = value
        except (TypeError, ValueError) as exc:
            raise AccountingValidationError("GL mapping must contain debit/credit pairs") from exc
        pair = GlAccountPair(str(debit), str(credit))
    debit = pair.debit.strip()
    credit = pair.credit.strip()
    if not _ACCOUNT_RE.fullmatch(debit) or not _ACCOUNT_RE.fullmatch(credit):
        raise AccountingValidationError(f"Invalid GL account pair: {pair!r}")
    if debit == credit:
        raise AccountingValidationError(f"Debit and credit GL cannot be the same: {debit}")
    return GlAccountPair(debit=debit, credit=credit)


def _credit_lines(
    value: object,
    fallback: GlAccountPair,
    amount: Decimal,
) -> tuple[tuple[str, Decimal], ...]:
    if value in (None, ""):
        return ((fallback.credit, amount),)
    if not isinstance(value, (list, tuple)):
        raise AccountingValidationError("credit_lines must be a list of account/amount pairs")

    lines: list[tuple[str, Decimal]] = []
    for index, raw_line in enumerate(value, start=1):
        if isinstance(raw_line, Mapping):
            account = str(raw_line.get("account") or "").strip()
            raw_amount = raw_line.get("amount")
        else:
            try:
                account_value, raw_amount = raw_line
            except (TypeError, ValueError) as exc:
                raise AccountingValidationError(
                    f"Invalid credit line at position {index}"
                ) from exc
            account = str(account_value).strip()
        if not _ACCOUNT_RE.fullmatch(account):
            raise AccountingValidationError(f"Invalid credit GL account: {account!r}")
        line_amount = _decimal(
            raw_amount,
            field_name=f"credit_lines[{index}].amount",
        )
        assert line_amount is not None
        if line_amount <= 0:
            raise AccountingValidationError("Credit line amounts must be greater than zero")
        lines.append((account, line_amount))
    if not lines:
        raise AccountingValidationError("At least one credit line is required")
    if sum((line_amount for _, line_amount in lines), Decimal(0)) != amount:
        raise AccountingValidationError("Credit lines do not reconcile to case amount")
    return tuple(lines)


class AccountingXlsxAdapter:
    """Create a double-entry workbook from Case-like objects.

    ``gl_mapping`` is mandatory by design: accounting accounts are policy,
    not a file-format default.  Each value may be a ``GlAccountPair`` or a
    ``(debit, credit)`` tuple.  Case objects may be dataclasses or mappings.
    """

    def __init__(self, gl_mapping: Mapping[str, GlAccountPair | tuple[str, str]]) -> None:
        if not gl_mapping:
            raise AccountingValidationError("At least one GL mapping is required")
        self._gl_mapping = {
            _enum_value(case_type): _account_pair(pair)
            for case_type, pair in gl_mapping.items()
        }

    def _prepare_case(self, case: object) -> _PreparedCase:
        case_type = _enum_value(_case_value(case, "case_type"))
        if case_type not in {"DAMAGED", "LOST"}:
            raise AccountingValidationError(f"Unsupported case type: {case_type!r}")
        gl = self._gl_mapping.get(case_type)
        if gl is None:
            raise AccountingValidationError(f"Missing GL mapping for {case_type}")

        amount = _decimal(_case_value(case, "amount"), field_name="amount")
        assert amount is not None
        if amount <= 0:
            raise AccountingValidationError("amount must be greater than zero")

        metadata = _case_value(case, "metadata", {}) or {}
        has_gl_override = isinstance(metadata, Mapping) and (
            metadata.get("debit_gl") or metadata.get("credit_gl")
        )
        if has_gl_override:
            if not metadata.get("debit_gl") or not metadata.get("credit_gl"):
                raise AccountingValidationError(
                    "Both debit_gl and credit_gl overrides are required"
                )
            gl = _account_pair((str(metadata["debit_gl"]), str(metadata["credit_gl"])))
        raw_credit_lines = metadata.get("credit_lines") if isinstance(metadata, Mapping) else None

        case_id = str(
            _case_value(case, "id")
            or _case_value(case, "source_id")
            or f"{case_type}:{_case_value(case, 'asset_code')}:{_case_value(case, 'domain')}"
        ).strip()
        if not case_id:
            raise AccountingValidationError("case id/source_id is required")

        received_at = _case_value(case, "received_at")
        if received_at is not None and not isinstance(received_at, (datetime, date)):
            raise AccountingValidationError("received_at must be a date/datetime or None")
        if isinstance(received_at, datetime) and received_at.tzinfo is not None:
            received_at = received_at.astimezone(UTC).replace(tzinfo=None)

        warnings = tuple(str(value) for value in (_case_value(case, "warnings", ()) or ()))
        residual_value = _decimal(
            _case_value(case, "residual_value"),
            field_name="residual_value",
            allow_none=True,
        )
        responsibility_fee = _decimal(
            _case_value(case, "responsibility_fee"),
            field_name="responsibility_fee",
            allow_none=True,
        )
        if residual_value is not None and residual_value < 0:
            raise AccountingValidationError("residual_value cannot be negative")
        if responsibility_fee is not None and responsibility_fee < 0:
            raise AccountingValidationError("responsibility_fee cannot be negative")
        if (
            case_type == "LOST"
            and residual_value is not None
            and responsibility_fee is not None
            and residual_value + responsibility_fee != amount
        ):
            raise AccountingValidationError(
                f"LOST case {case_id}: residual + responsibility fee does not equal amount"
            )

        return _PreparedCase(
            case_id=case_id,
            case_type=case_type,
            domain=_excel_text(_case_value(case, "domain")),
            employee_name=_excel_text(_case_value(case, "employee_name")),
            asset_code=_excel_text(_case_value(case, "asset_code")),
            asset_name=_excel_text(_case_value(case, "asset_name")),
            received_at=received_at,
            amount=amount,
            residual_value=residual_value,
            responsibility_fee=responsibility_fee,
            repair_status=_excel_text(_case_value(case, "repair_status")),
            supplier_number=_excel_text(_case_value(case, "supplier_number")),
            supplier_site=_excel_text(_case_value(case, "supplier_site")),
            source_file=_excel_text(_case_value(case, "source_file")),
            warnings=warnings,
            debit_gl=gl.debit,
            credit_lines=_credit_lines(raw_credit_lines, gl, amount),
        )

    def export(
        self,
        cases: Iterable[object],
        destination: str | Path,
        *,
        expected_total: Decimal | int | None = None,
        overwrite: bool = False,
    ) -> AccountingExportResult:
        prepared = [self._prepare_case(case) for case in cases]
        if not prepared:
            raise AccountingValidationError("Cannot export an empty accounting batch")
        case_ids = [case.case_id for case in prepared]
        duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
        if duplicates:
            raise AccountingValidationError(f"Duplicate case ids: {', '.join(duplicates)}")

        source_total = sum((case.amount for case in prepared), Decimal(0))
        expected = (
            source_total
            if expected_total is None
            else _decimal(expected_total, field_name="expected_total")
        )
        assert expected is not None
        if source_total != expected:
            raise AccountingValidationError(
                f"Source total {source_total} does not match expected total {expected}"
            )

        debit_total = sum((case.amount for case in prepared), Decimal(0))
        credit_total = sum(
            (line_amount for case in prepared for _, line_amount in case.credit_lines),
            Decimal(0),
        )
        if debit_total != credit_total or debit_total != source_total:
            raise AccountingValidationError("Journal does not reconcile to source total")

        destination_path = Path(destination)
        if destination_path.exists() and not overwrite:
            raise OutputExistsError(f"Destination already exists: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        workbook = self._build_workbook(prepared, source_total, expected)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.stem}.",
            suffix=".xlsx",
            dir=destination_path.parent,
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
        try:
            workbook.save(temp_path)
            self._verify_saved_workbook(temp_path, len(prepared))
            self._commit(temp_path, destination_path, overwrite=overwrite)
        finally:
            temp_path.unlink(missing_ok=True)

        return AccountingExportResult(
            path=destination_path,
            case_count=len(prepared),
            journal_line_count=sum(1 + len(case.credit_lines) for case in prepared),
            source_total=source_total,
            debit_total=debit_total,
            credit_total=credit_total,
        )

    @staticmethod
    def _commit(temp_path: Path, destination: Path, *, overwrite: bool) -> None:
        if overwrite:
            os.replace(temp_path, destination)
            return
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

    @staticmethod
    def _build_workbook(
        cases: list[_PreparedCase], source_total: Decimal, expected_total: Decimal
    ) -> Workbook:
        workbook = Workbook()
        cases_sheet = workbook.active
        cases_sheet.title = "Cases"
        journal_sheet = workbook.create_sheet("Journal")
        checks_sheet = workbook.create_sheet("Checks")

        title_fill = PatternFill("solid", fgColor="17365D")
        header_fill = PatternFill("solid", fgColor="D9EAF7")
        pass_fill = PatternFill("solid", fgColor="E2F0D9")
        thin_gray = Side(style="thin", color="B7C9D6")
        header_border = Border(bottom=thin_gray)

        case_headers = [
            "Case ID",
            "Case Type",
            "Domain",
            "Employee",
            "Asset Code",
            "Asset Name",
            "Received At (UTC)",
            "Amount (VND)",
            "Residual Value (VND)",
            "Responsibility Fee (VND)",
            "Repair Status",
            "Supplier Number",
            "Supplier Site",
            "Source File",
            "Debit GL",
            "Credit GL",
            "Warning Count",
        ]
        cases_sheet.merge_cells(
            start_row=1, start_column=1, end_row=1, end_column=len(case_headers)
        )
        cases_sheet.cell(1, 1, "Asset Compensation - Source Cases")
        cases_sheet.cell(2, 1, "Source Total (VND)")
        cases_sheet.cell(2, 2, int(source_total))
        cases_sheet.append(case_headers)
        for case in cases:
            cases_sheet.append(
                [
                    _excel_text(case.case_id),
                    case.case_type,
                    case.domain,
                    case.employee_name,
                    case.asset_code,
                    case.asset_name,
                    case.received_at,
                    int(case.amount),
                    int(case.residual_value) if case.residual_value is not None else None,
                    int(case.responsibility_fee) if case.responsibility_fee is not None else None,
                    case.repair_status,
                    case.supplier_number,
                    case.supplier_site,
                    case.source_file,
                    case.debit_gl,
                    ", ".join(account for account, _ in case.credit_lines),
                    len(case.warnings),
                ]
            )

        journal_headers = [
            "Case ID",
            "Line",
            "GL Account",
            "Debit (VND)",
            "Credit (VND)",
            "Domain",
            "Asset Code",
            "Narrative",
        ]
        journal_sheet.merge_cells(
            start_row=1, start_column=1, end_row=1, end_column=len(journal_headers)
        )
        journal_sheet.cell(1, 1, "Balanced Journal Lines")
        journal_sheet.cell(2, 1, "Each case produces one debit and one credit line")
        journal_sheet.append(journal_headers)
        journal_line_count = 0
        for case in cases:
            narrative = _excel_text(
                f"{case.case_type} compensation - {case.asset_code} - {case.domain}"
            )
            journal_line_count += 1
            journal_sheet.append(
                [
                    _excel_text(case.case_id),
                    1,
                    case.debit_gl,
                    int(case.amount),
                    None,
                    case.domain,
                    case.asset_code,
                    narrative,
                ]
            )
            for line_number, (credit_gl, credit_amount) in enumerate(
                case.credit_lines,
                start=2,
            ):
                journal_line_count += 1
                journal_sheet.append(
                    [
                        _excel_text(case.case_id),
                        line_number,
                        credit_gl,
                        None,
                        int(credit_amount),
                        case.domain,
                        case.asset_code,
                        narrative,
                    ]
                )

        last_journal_row = 3 + journal_line_count
        checks_sheet.append(["Accounting Export Checks", "Value"])
        checks_sheet.append(["Source case total (VND)", int(source_total)])
        checks_sheet.append(["Expected total (VND)", int(expected_total)])
        checks_sheet.append(
            ["Journal debit total (VND)", f"=SUM('Journal'!D4:D{last_journal_row})"]
        )
        checks_sheet.append(
            ["Journal credit total (VND)", f"=SUM('Journal'!E4:E{last_journal_row})"]
        )
        checks_sheet.append(["Source total delta", "=B2-B3"])
        checks_sheet.append(
            [
                "Journal reconciliation delta",
                "=MAX(ABS(B4-B5),ABS(B2-B4),ABS(B2-B5))",
            ]
        )
        checks_sheet.append(["MODEL STATUS", '=IF(AND(B6=0,B7=0),"PASS","FAIL")'])

        for sheet, header_row in ((cases_sheet, 3), (journal_sheet, 3), (checks_sheet, 1)):
            max_column = sheet.max_column
            for cell in sheet[header_row]:
                cell.fill = header_fill
                cell.font = Font(bold=True, color="17365D")
                cell.border = header_border
                cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.freeze_panes = f"A{header_row + 1}"
            sheet.auto_filter.ref = f"A{header_row}:{sheet.cell(header_row, max_column).coordinate}"
            sheet.sheet_view.showGridLines = False

        for sheet in (cases_sheet, journal_sheet):
            sheet.cell(1, 1).fill = title_fill
            sheet.cell(1, 1).font = Font(bold=True, color="FFFFFF", size=14)
            sheet.cell(1, 1).alignment = Alignment(horizontal="left", vertical="center")

        for row in range(4, cases_sheet.max_row + 1):
            cases_sheet.cell(row, 7).number_format = "yyyy-mm-dd hh:mm"
            for column in (8, 9, 10):
                cases_sheet.cell(row, column).number_format = "#,##0"
                cases_sheet.cell(row, column).alignment = Alignment(horizontal="right")
        cases_sheet.cell(2, 2).number_format = "#,##0"
        for row in range(4, journal_sheet.max_row + 1):
            for column in (4, 5):
                journal_sheet.cell(row, column).number_format = "#,##0"
                journal_sheet.cell(row, column).alignment = Alignment(horizontal="right")
        for row in range(2, 8):
            checks_sheet.cell(row, 2).number_format = "#,##0"
        checks_sheet.cell(8, 1).font = Font(bold=True)
        checks_sheet.cell(8, 2).fill = pass_fill
        checks_sheet.cell(8, 2).font = Font(bold=True, color="006100")

        widths = {
            "A": 28,
            "B": 16,
            "C": 18,
            "D": 28,
            "E": 16,
            "F": 32,
            "G": 21,
            "H": 18,
            "I": 22,
            "J": 25,
            "K": 18,
            "L": 20,
            "M": 18,
            "N": 42,
            "O": 16,
            "P": 16,
            "Q": 15,
        }
        for column, width in widths.items():
            cases_sheet.column_dimensions[column].width = width
        for column, width in zip("ABCDEFGH", (28, 10, 16, 18, 18, 18, 16, 52), strict=True):
            journal_sheet.column_dimensions[column].width = width
        checks_sheet.column_dimensions["A"].width = 32
        checks_sheet.column_dimensions["B"].width = 22
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.calculation.calcMode = "auto"
        return workbook

    @staticmethod
    def _verify_saved_workbook(path: Path, case_count: int) -> None:
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            if workbook.sheetnames != ["Cases", "Journal", "Checks"]:
                raise AccountingExportError("Saved workbook has unexpected sheets")
            if workbook["Cases"].max_row != case_count + 3:
                raise AccountingExportError("Saved workbook case count does not reconcile")
            journal_rows = workbook["Journal"].max_row - 3
            if journal_rows < case_count * 2:
                raise AccountingExportError("Saved workbook has too few journal lines")
            if workbook["Checks"]["B8"].value != '=IF(AND(B6=0,B7=0),"PASS","FAIL")':
                raise AccountingExportError("Saved workbook is missing the model-status check")
        finally:
            workbook.close()
