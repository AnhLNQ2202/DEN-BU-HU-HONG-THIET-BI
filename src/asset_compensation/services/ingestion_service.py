"""Orchestrate pure parsers and the case service without filesystem side effects in either."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from asset_compensation.domain import Case, ValidationError
from asset_compensation.domain import ParsedCase as DomainParsedCase
from asset_compensation.parsers import EmlParseError, EmlParser, SupplierRecord
from asset_compensation.parsers.contracts import ParsedCase as ParserCase

from .case_service import CaseService


@dataclass(frozen=True, slots=True)
class IngestionReport:
    cases: tuple[Case, ...]
    warnings: tuple[str, ...]
    unknown_files: tuple[str, ...]


def _vnd(value: Decimal | int | None, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a numeric VND amount") from exc
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise ValidationError(f"{field_name} must use whole VND units")
    if decimal < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    return int(decimal)


def _received_at(parsed: ParserCase, source: Path) -> datetime:
    if parsed.received_at is not None:
        return parsed.received_at
    return datetime.fromtimestamp(source.stat().st_mtime, tz=UTC)


def _to_domain_case(
    parsed: ParserCase,
    source: Path,
    supplier_directory: Mapping[str, SupplierRecord] | None,
) -> DomainParsedCase:
    warnings = list(parsed.warnings)
    supplier = supplier_directory.get(parsed.domain.casefold()) if supplier_directory else None
    if supplier_directory is not None and supplier is None:
        warnings.append("Supplier domain was not found in the configured directory")
    if supplier is not None and supplier.active is False:
        warnings.append("Supplier record is inactive")
    if not parsed.domain:
        raise ValidationError("Parser could not determine an employee domain")
    if not parsed.asset_code:
        raise ValidationError("Parser could not determine an asset code")

    return DomainParsedCase(
        case_type=parsed.case_type,
        domain=parsed.domain,
        employee_name=parsed.employee_name,
        asset_code=parsed.asset_code,
        asset_name=parsed.asset_name,
        received_at=_received_at(parsed, source),
        amount=_vnd(parsed.amount, "amount"),
        residual_value=_vnd(parsed.residual_value, "residual_value"),
        responsibility_fee=_vnd(parsed.responsibility_fee, "responsibility_fee"),
        repair_status=parsed.repair_status,
        supplier_number=supplier.supplier_number if supplier else parsed.supplier_number,
        supplier_site=supplier.supplier_site if supplier else parsed.supplier_site,
        supplier_name=supplier.supplier_name if supplier else parsed.supplier_name,
        warnings=tuple(dict.fromkeys(warnings)),
        source_file=source.name,
        source_id=parsed.source_id,
        metadata=dict(parsed.metadata),
    )


def ingest_eml_directory(
    service: CaseService,
    directory: str | Path,
    *,
    supplier_directory: Mapping[str, SupplierRecord] | None = None,
) -> IngestionReport:
    """Parse every top-level EML and persist valid cases idempotently."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    parser = EmlParser()
    candidates: list[DomainParsedCase] = []
    warnings: list[str] = []
    unknown: list[str] = []

    for source in sorted(root.glob("*.eml"), key=lambda item: item.name.casefold()):
        try:
            parsed = parser.parse(source)
            candidates.append(_to_domain_case(parsed, source, supplier_directory))
        except (EmlParseError, ValidationError, OSError, UnicodeError) as exc:
            unknown.append(source.name)
            warnings.append(f"{source.name}: {exc}")

    persisted = service.ingest(candidates)
    return IngestionReport(tuple(persisted), tuple(warnings), tuple(unknown))
