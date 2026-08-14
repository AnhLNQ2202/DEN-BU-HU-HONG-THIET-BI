"""Strict supplier-directory loaders for CSV, Excel and Oracle BIP HTML exports."""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .contracts import SupplierRecord


class SupplierLoadError(ValueError):
    """Raised when supplier source data does not satisfy the contract."""


class DuplicateSupplierDomainError(SupplierLoadError):
    """Raised instead of silently applying last-write-wins semantics."""

    def __init__(self, domain: str, first_row: int | None, duplicate_row: int | None) -> None:
        self.domain = domain
        self.first_row = first_row
        self.duplicate_row = duplicate_row
        super().__init__(
            f"Duplicate supplier domain {domain!r} at rows {first_row} and {duplicate_row}"
        )


_ALIASES = {
    "domain": {"domain", "user", "username", "email", "user domain"},
    "employee_number": {
        "employee number",
        "employee no",
        "employee code",
        "employee id",
        "ma nhan vien",
    },
    "supplier_number": {
        "supplier number",
        "supplier no",
        "supplier code",
        "vendor number",
        "vendor code",
        "ma nha cung cap",
    },
    "supplier_site": {"supplier site", "vendor site", "site", "dia diem nha cung cap"},
    "supplier_name": {"supplier name", "vendor name", "name", "ten nha cung cap"},
    "active": {"active", "is active", "status", "trang thai"},
}
_SUPPLIER_NAME_DOMAIN_RE = re.compile(r"-\s*([A-Za-z][A-Za-z0-9._]{1,63})\s*$")
_MAX_SUPPLIER_COLUMNS = 32
_MAX_SUPPLIER_DATA_ROWS = 20_000
_MAX_SUPPLIER_CELLS = 640_000


def normalize_domain(value: object) -> str:
    domain = str(value or "").strip().casefold()
    if "@" in domain:
        domain = domain.partition("@")[0]
    return domain


def _fold_header(value: object) -> str:
    text = str(value or "").strip().casefold().replace("đ", "d")
    normalized = unicodedata.normalize("NFKD", text)
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).split()
    )


def _canonical_headers(headers: Iterable[object]) -> dict[str, int]:
    folded = [_fold_header(value) for value in headers]
    result: dict[str, int] = {}
    for canonical, aliases in _ALIASES.items():
        normalized_aliases = {_fold_header(alias) for alias in aliases}
        matches = [index for index, header in enumerate(folded) if header in normalized_aliases]
        if len(matches) > 1:
            raise SupplierLoadError(f"Ambiguous columns for {canonical!r}: {matches}")
        if matches:
            result[canonical] = matches[0]
    missing = {"supplier_number", "supplier_site"} - result.keys()
    if "domain" not in result and "supplier_name" not in result:
        missing.add("domain/supplier_name")
    if missing:
        raise SupplierLoadError(f"Missing supplier columns: {', '.join(sorted(missing))}")
    return result


def _as_bool(value: object) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    folded = _fold_header(value)
    if folded in {"1", "true", "yes", "y", "active", "hoat dong"}:
        return True
    if folded in {"0", "false", "no", "n", "inactive", "khong hoat dong"}:
        return False
    raise SupplierLoadError(f"Unsupported active value: {value!r}")


def _row_value(row: list[object], columns: Mapping[str, int], name: str) -> object:
    index = columns.get(name)
    return row[index] if index is not None and index < len(row) else None


def _records_from_rows(
    rows: Iterable[Iterable[object]],
    *,
    active_override: bool | None = None,
    reject_duplicate_domains: bool = True,
    find_header: bool = False,
    skip_unresolved_domains: bool = False,
    warnings: list[str] | None = None,
) -> list[SupplierRecord]:
    iterator = iter(rows)
    columns: dict[str, int] | None = None
    header_row_number = 0
    cell_count = 0
    for candidate_row_number, raw_headers in enumerate(iterator, start=1):
        headers = list(raw_headers)
        if len(headers) > _MAX_SUPPLIER_COLUMNS:
            raise SupplierLoadError("Supplier source exceeds the 32-column limit")
        cell_count += len(headers)
        if cell_count > _MAX_SUPPLIER_CELLS:
            raise SupplierLoadError("Supplier source exceeds the 640,000-cell limit")
        try:
            columns = _canonical_headers(headers)
        except SupplierLoadError:
            if find_header and candidate_row_number < 100:
                continue
            raise
        header_row_number = candidate_row_number
        break
    if columns is None:
        raise SupplierLoadError("Supplier source is empty or has no supported header row")

    records: list[SupplierRecord] = []
    seen: dict[str, int] = {}
    unresolved_count = 0
    data_row_count = 0
    for row_number, raw_row in enumerate(iterator, start=header_row_number + 1):
        row = list(raw_row)
        if not any(value not in (None, "") for value in row):
            continue
        data_row_count += 1
        if data_row_count > _MAX_SUPPLIER_DATA_ROWS:
            raise SupplierLoadError("Supplier source exceeds the 20,000-row limit")
        if len(row) > _MAX_SUPPLIER_COLUMNS:
            raise SupplierLoadError("Supplier source exceeds the 32-column limit")
        cell_count += len(row)
        if cell_count > _MAX_SUPPLIER_CELLS:
            raise SupplierLoadError("Supplier source exceeds the 640,000-cell limit")

        employee_number = str(_row_value(row, columns, "employee_number") or "").strip()
        employee_number = employee_number.lstrip("'")
        supplier_name_value = _row_value(row, columns, "supplier_name")
        supplier_name = (
            str(supplier_name_value).strip()
            if supplier_name_value not in (None, "")
            else None
        )
        domain_value = _row_value(row, columns, "domain")
        if domain_value in (None, "") and supplier_name:
            suffix = _SUPPLIER_NAME_DOMAIN_RE.search(supplier_name)
            domain_value = suffix.group(1) if suffix else None
        domain = normalize_domain(domain_value)
        if not domain:
            if skip_unresolved_domains:
                unresolved_count += 1
                continue
            raise SupplierLoadError(f"Missing domain at row {row_number}")
        if reject_duplicate_domains and domain in seen:
            raise DuplicateSupplierDomainError(domain, seen[domain], row_number)
        seen[domain] = row_number

        supplier_number = str(_row_value(row, columns, "supplier_number") or "").strip()
        supplier_number = supplier_number.lstrip("'")
        supplier_site = str(_row_value(row, columns, "supplier_site") or "").strip()
        supplier_site = supplier_site.lstrip("'")
        if not supplier_number or not supplier_site:
            raise SupplierLoadError(f"Missing supplier number/site at row {row_number}")

        records.append(
            SupplierRecord(
                domain=domain,
                supplier_number=supplier_number,
                supplier_site=supplier_site,
                supplier_name=supplier_name,
                active=(
                    active_override
                    if active_override is not None
                    else _as_bool(_row_value(row, columns, "active"))
                ),
                source_row=row_number,
                metadata={"employee_number": employee_number or domain},
            )
        )
    if unresolved_count and warnings is not None:
        warnings.append(
            f"Skipped {unresolved_count} rows without a safe Supplier Name domain suffix"
        )
    return records


def _mapping_rows(rows: Iterable[Mapping[str, Any]]) -> Iterable[list[object]]:
    materialized = list(rows)
    if not materialized:
        return []
    headers = list(materialized[0].keys())
    return [headers, *[[row.get(header) for header in headers] for row in materialized]]


class _OracleBipTableParser(HTMLParser):
    """Collect table rows from the constrained HTML emitted by Oracle BI Publisher."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._header_found = False
        self._data_rows = 0
        self._cell_count = 0
        self._preamble_rows = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        folded = tag.casefold()
        if folded == "tr":
            self._row = []
        elif folded in {"td", "th"} and self._row is not None:
            self._cell = []
        elif folded == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif folded == "tr" and self._row is not None:
            if self._row:
                if len(self._row) > _MAX_SUPPLIER_COLUMNS:
                    raise SupplierLoadError("Supplier source exceeds the 32-column limit")
                self._cell_count += len(self._row)
                if self._cell_count > _MAX_SUPPLIER_CELLS:
                    raise SupplierLoadError("Supplier source exceeds the 640,000-cell limit")
                if not self._header_found:
                    self._preamble_rows += 1
                    try:
                        _canonical_headers(self._row)
                    except SupplierLoadError:
                        if self._preamble_rows > 100:
                            raise SupplierLoadError(
                                "Supplier source has no supported header in its first 100 rows"
                            ) from None
                    else:
                        self._header_found = True
                else:
                    self._data_rows += 1
                    if self._data_rows > _MAX_SUPPLIER_DATA_ROWS:
                        raise SupplierLoadError("Supplier source exceeds the 20,000-row limit")
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _oracle_bip_rows(path: Path) -> list[list[str]]:
    with path.open("rb") as stream:
        prefix = stream.read(65536)
    try:
        folded = prefix.decode("utf-8-sig", errors="strict").lstrip().casefold()
    except UnicodeDecodeError as exc:
        raise SupplierLoadError("Oracle BI Publisher export must be valid UTF-8 HTML") from exc
    if not (folded.startswith("<html") or folded.startswith("<!doctype html")):
        raise SupplierLoadError("Legacy .xls must be an Oracle BI Publisher HTML export")
    if "oracle bi publisher" not in folded:
        raise SupplierLoadError("Legacy .xls is missing the Oracle BI Publisher signature")
    try:
        parser = _OracleBipTableParser()
        with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
            while chunk := stream.read(65536):
                parser.feed(chunk)
        parser.close()
    except (UnicodeDecodeError, OSError) as exc:
        raise SupplierLoadError("Oracle BI Publisher export must be valid UTF-8 HTML") from exc
    return parser.rows


def load_supplier_records(
    source: str | Path | Iterable[Mapping[str, Any]],
    *,
    active_override: bool | None = None,
    reject_duplicate_domains: bool = True,
    skip_unresolved_domains: bool = False,
    warnings: list[str] | None = None,
) -> list[SupplierRecord]:
    """Load supplier rows and fail fast on duplicate normalized domains."""

    if not isinstance(source, (str, Path)):
        return _records_from_rows(
            _mapping_rows(source),
            active_override=active_override,
            reject_duplicate_domains=reject_duplicate_domains,
            skip_unresolved_domains=skip_unresolved_domains,
            warnings=warnings,
        )

    path = Path(source)
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _records_from_rows(
                csv.reader(stream),
                active_override=active_override,
                reject_duplicate_domains=reject_duplicate_domains,
                skip_unresolved_domains=skip_unresolved_domains,
                warnings=warnings,
            )
    if suffix == ".xlsx":
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            worksheet = workbook.active
            return _records_from_rows(
                worksheet.iter_rows(values_only=True),
                active_override=active_override,
                reject_duplicate_domains=reject_duplicate_domains,
                find_header=True,
                skip_unresolved_domains=skip_unresolved_domains,
                warnings=warnings,
            )
        finally:
            workbook.close()
    if suffix == ".xls":
        return _records_from_rows(
            _oracle_bip_rows(path),
            active_override=active_override,
            reject_duplicate_domains=reject_duplicate_domains,
            find_header=True,
            skip_unresolved_domains=skip_unresolved_domains,
            warnings=warnings,
        )
    raise SupplierLoadError(f"Unsupported supplier source: {path.suffix or '<no extension>'}")


def load_supplier_directory(
    source: str | Path | Iterable[Mapping[str, Any]],
) -> dict[str, SupplierRecord]:
    records = load_supplier_records(source)
    return {record.domain: record for record in records}
