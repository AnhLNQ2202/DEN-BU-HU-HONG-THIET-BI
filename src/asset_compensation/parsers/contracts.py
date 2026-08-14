"""File-format-neutral contracts returned by ingestion parsers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

CaseKind = Literal["DAMAGED", "LOST"]


@dataclass(frozen=True, slots=True)
class ParsedCase:
    """A candidate case extracted from a source document.

    This deliberately mirrors the public attributes of the domain ``Case``
    without importing it.  The application service can therefore validate and
    convert parser output while the parser remains pure and independently
    testable.
    """

    case_type: CaseKind
    domain: str
    asset_code: str
    received_at: datetime | None
    employee_name: str | None = None
    asset_name: str | None = None
    amount: Decimal | None = None
    residual_value: Decimal | None = None
    responsibility_fee: Decimal | None = None
    repair_status: str | None = None
    supplier_number: str | None = None
    supplier_site: str | None = None
    supplier_name: str | None = None
    warnings: tuple[str, ...] = ()
    source_file: str | None = None
    source_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SupplierRecord:
    """Supplier lookup data keyed by a normalized user domain."""

    domain: str
    supplier_number: str
    supplier_site: str
    supplier_name: str | None = None
    active: bool | None = None
    source_row: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
