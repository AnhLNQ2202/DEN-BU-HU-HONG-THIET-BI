"""Core domain models used by parsers, repositories, and services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .exceptions import ValidationError


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def as_utc(value: datetime | str) -> datetime:
    """Normalize an ISO string or datetime to an aware UTC datetime."""

    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"Invalid ISO datetime: {value!r}") from exc
    if not isinstance(value, datetime):
        raise ValidationError(f"Expected datetime or ISO string, got {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class CaseType(StrEnum):
    DAMAGED = "DAMAGED"
    LOST = "LOST"


class CaseStatus(StrEnum):
    NEW = "NEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_FOR_ACCOUNTING = "READY_FOR_ACCOUNTING"
    ACCOUNTED = "ACCOUNTED"
    CLOSED = "CLOSED"


def _case_type(value: CaseType | str) -> CaseType:
    try:
        return value if isinstance(value, CaseType) else CaseType(str(value).upper())
    except ValueError as exc:
        raise ValidationError(f"Unsupported case type: {value!r}") from exc


def _case_status(value: CaseStatus | str) -> CaseStatus:
    try:
        return value if isinstance(value, CaseStatus) else CaseStatus(str(value).upper())
    except ValueError as exc:
        raise ValidationError(f"Unsupported case status: {value!r}") from exc


def _optional_amount(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer amount or None")
    if value < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    return value


def _clean_required(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValidationError(f"{field_name} is required")
    return cleaned


def _clean_warnings(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    # Preserve source ordering while eliminating empty and duplicate warnings.
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


@dataclass(frozen=True, slots=True)
class ParsedCase:
    """Parser output before persistence assigns a stable case ID and timestamps."""

    case_type: CaseType | str
    domain: str
    asset_code: str
    received_at: datetime | str
    employee_name: str | None = None
    asset_name: str | None = None
    amount: int | None = None
    residual_value: int | None = None
    responsibility_fee: int | None = None
    repair_status: str | None = None
    supplier_number: str | None = None
    supplier_site: str | None = None
    supplier_name: str | None = None
    warnings: tuple[str, ...] | list[str] = ()
    source_file: str | None = None
    source_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_type", _case_type(self.case_type))
        object.__setattr__(self, "domain", _clean_required(self.domain, "domain"))
        object.__setattr__(self, "asset_code", _clean_required(self.asset_code, "asset_code"))
        object.__setattr__(self, "received_at", as_utc(self.received_at))
        for name in ("amount", "residual_value", "responsibility_fee"):
            object.__setattr__(self, name, _optional_amount(getattr(self, name), name))
        object.__setattr__(self, "warnings", _clean_warnings(self.warnings))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class Case:
    """Persisted compensation case."""

    id: str
    case_type: CaseType | str
    status: CaseStatus | str
    domain: str
    asset_code: str
    received_at: datetime | str
    employee_name: str | None = None
    asset_name: str | None = None
    amount: int | None = None
    residual_value: int | None = None
    responsibility_fee: int | None = None
    repair_status: str | None = None
    supplier_number: str | None = None
    supplier_site: str | None = None
    supplier_name: str | None = None
    warnings: tuple[str, ...] | list[str] = ()
    source_file: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | str = field(default_factory=utc_now)
    updated_at: datetime | str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_required(self.id, "id"))
        object.__setattr__(self, "case_type", _case_type(self.case_type))
        object.__setattr__(self, "status", _case_status(self.status))
        object.__setattr__(self, "domain", _clean_required(self.domain, "domain"))
        object.__setattr__(self, "asset_code", _clean_required(self.asset_code, "asset_code"))
        object.__setattr__(self, "received_at", as_utc(self.received_at))
        object.__setattr__(self, "created_at", as_utc(self.created_at))
        object.__setattr__(self, "updated_at", as_utc(self.updated_at))
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at cannot be earlier than created_at")
        for name in ("amount", "residual_value", "responsibility_fee"):
            object.__setattr__(self, name, _optional_amount(getattr(self, name), name))
        object.__setattr__(self, "warnings", _clean_warnings(self.warnings))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_type": self.case_type.value,
            "status": self.status.value,
            "domain": self.domain,
            "employee_name": self.employee_name,
            "asset_code": self.asset_code,
            "asset_name": self.asset_name,
            "received_at": self.received_at.isoformat(),
            "amount": self.amount,
            "residual_value": self.residual_value,
            "responsibility_fee": self.responsibility_fee,
            "repair_status": self.repair_status,
            "supplier_number": self.supplier_number,
            "supplier_site": self.supplier_site,
            "supplier_name": self.supplier_name,
            "warnings": list(self.warnings),
            "source_file": self.source_file,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class StatusEvent:
    id: int | None
    case_id: str
    from_status: CaseStatus | str | None
    to_status: CaseStatus | str
    changed_at: datetime | str
    actor: str = "system"
    note: str | None = None

    def __post_init__(self) -> None:
        if self.from_status is not None:
            object.__setattr__(self, "from_status", _case_status(self.from_status))
        object.__setattr__(self, "to_status", _case_status(self.to_status))
        object.__setattr__(self, "changed_at", as_utc(self.changed_at))
        object.__setattr__(self, "actor", _clean_required(self.actor, "actor"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "from_status": self.from_status.value if self.from_status else None,
            "to_status": self.to_status.value,
            "changed_at": self.changed_at.isoformat(),
            "actor": self.actor,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CaseSummary:
    total: int
    total_amount: int
    warning_count: int
    by_type: Mapping[CaseType, int]
    by_status: Mapping[CaseStatus, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "total_amount": self.total_amount,
            "warning_count": self.warning_count,
            "by_type": {key.value: value for key, value in self.by_type.items()},
            "by_status": {key.value: value for key, value in self.by_status.items()},
        }


@dataclass(frozen=True, slots=True)
class AccountingBatch:
    id: str
    name: str
    case_ids: tuple[str, ...] | list[str]
    created_at: datetime | str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_required(self.id, "batch id"))
        object.__setattr__(self, "name", _clean_required(self.name, "batch name"))
        case_ids = tuple(dict.fromkeys(_clean_required(item, "case id") for item in self.case_ids))
        if not case_ids:
            raise ValidationError("An accounting batch must contain at least one case")
        object.__setattr__(self, "case_ids", case_ids)
        object.__setattr__(self, "created_at", as_utc(self.created_at))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "case_ids": list(self.case_ids),
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }
