"""Domain types for policy-based lost-asset compensation previews."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .exceptions import ValidationError

MAX_ASSET_TAG_CHARS = 255
MAX_ASSET_NAME_CHARS = 1_024
MAX_DOMAIN_CHARS = 253
MAX_ASSET_METADATA_CHARS = 1_024
MAX_NUMERIC_INPUT_CHARS = 32
MAX_SAFE_VND = Decimal("9007199254740991")


class DepreciationGroup(StrEnum):
    """Approved compensation schedules from IT.POL.01."""

    FOUR_YEAR = "FOUR_YEAR"
    SIX_YEAR = "SIX_YEAR"


class ReferenceStatus(StrEnum):
    """Whether upstream reference data produced one unambiguous match."""

    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


class CompensationStatus(StrEnum):
    """Outcome of a read-only calculation preview."""

    CALCULATED = "CALCULATED"
    EXEMPT = "EXEMPT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


def _bounded_text(value: object, field_name: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_length or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ValidationError(f"{field_name} exceeds the safe text limit")
    return text


def _required_text(value: object, field_name: str, max_length: int) -> str:
    text = _bounded_text(value, field_name, max_length)
    if not text:
        raise ValidationError(f"{field_name} is required")
    return text


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    text = _bounded_text(value, field_name, MAX_ASSET_METADATA_CHARS)
    return text or None


def _date(value: date | str, field_name: str) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError(f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must use YYYY-MM-DD") from exc


def _optional_date(value: date | str | None, field_name: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    return _date(value, field_name)


def _money(value: Decimal | int | str | None, field_name: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a non-negative whole VND amount")
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError(f"{field_name} must be a non-negative whole VND amount")
    try:
        amount = Decimal(text)
    except Exception as exc:
        raise ValidationError(f"{field_name} must be a non-negative whole VND amount") from exc
    if (
        not amount.is_finite()
        or amount < 0
        or amount > MAX_SAFE_VND
        or amount != amount.to_integral_value()
    ):
        raise ValidationError(f"{field_name} must be a non-negative whole VND amount")
    return amount


def _group(value: DepreciationGroup | str | None) -> DepreciationGroup | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, DepreciationGroup):
        return value
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError("group must be FOUR_YEAR or SIX_YEAR")
    normalized = text.upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "4": DepreciationGroup.FOUR_YEAR,
        "4_YEAR": DepreciationGroup.FOUR_YEAR,
        "4_YEARS": DepreciationGroup.FOUR_YEAR,
        "FOUR_YEAR": DepreciationGroup.FOUR_YEAR,
        "6": DepreciationGroup.SIX_YEAR,
        "6_YEAR": DepreciationGroup.SIX_YEAR,
        "6_YEARS": DepreciationGroup.SIX_YEAR,
        "SIX_YEAR": DepreciationGroup.SIX_YEAR,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValidationError("group must be FOUR_YEAR or SIX_YEAR") from exc


def _fee_rate(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError("fee_rate must be 0.05 or 0.30")
    is_percentage = text.endswith("%")
    if is_percentage:
        text = text[:-1].strip()
    try:
        rate = Decimal(text)
    except Exception as exc:
        raise ValidationError("fee_rate must be 0.05 or 0.30") from exc
    if is_percentage or rate in {Decimal("5"), Decimal("30")}:
        rate /= Decimal("100")
    if rate not in {Decimal("0.05"), Decimal("0.30")}:
        raise ValidationError("fee_rate must be 0.05 or 0.30")
    return rate


def _reference_status(value: ReferenceStatus | str | None) -> ReferenceStatus | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, ReferenceStatus):
        return value
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError(
            "lookup_status must be MATCHED, NOT_FOUND, or AMBIGUOUS"
        )
    try:
        return ReferenceStatus(text.upper())
    except ValueError as exc:
        raise ValidationError(
            "lookup_status must be MATCHED, NOT_FOUND, or AMBIGUOUS"
        ) from exc


@dataclass(frozen=True, slots=True)
class CompensationAsset:
    """Validated inputs required for one calculation preview."""

    tag_number: str
    asset_name: str
    domain: str
    lost_date: date | str
    cost: Decimal | int | str | None
    start_date: date | str | None
    asset_number: str | None = None
    book: str | None = None
    entity: str | None = None
    cost_center: str | None = None
    product_code: str | None = None
    location: str | None = None
    group: DepreciationGroup | str | None = None
    fee_rate: Decimal | int | float | str | None = None
    physical: bool | None = None
    lookup_status: ReferenceStatus | str | None = None
    classification_confirmed: bool = False

    def __post_init__(self) -> None:
        tag_number = _required_text(
            self.tag_number, "tag_number", MAX_ASSET_TAG_CHARS
        ).upper()
        object.__setattr__(self, "tag_number", tag_number)
        object.__setattr__(
            self,
            "asset_name",
            _required_text(self.asset_name, "asset_name", MAX_ASSET_NAME_CHARS),
        )
        object.__setattr__(
            self,
            "domain",
            _required_text(self.domain, "domain", MAX_DOMAIN_CHARS),
        )
        object.__setattr__(self, "lost_date", _date(self.lost_date, "lost_date"))
        object.__setattr__(self, "start_date", _optional_date(self.start_date, "start_date"))
        object.__setattr__(self, "cost", _money(self.cost, "cost"))
        for field_name in (
            "asset_number",
            "book",
            "entity",
            "cost_center",
            "product_code",
            "location",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "group", _group(self.group))
        object.__setattr__(self, "fee_rate", _fee_rate(self.fee_rate))
        if self.physical is not None and not isinstance(self.physical, bool):
            raise ValidationError("physical must be a boolean or null")
        object.__setattr__(self, "lookup_status", _reference_status(self.lookup_status))
        if not isinstance(self.classification_confirmed, bool):
            raise ValidationError("classification_confirmed must be a boolean")
        if self.start_date is not None and self.lost_date < self.start_date:
            raise ValidationError("lost_date cannot be earlier than start_date")

    @property
    def barcode(self) -> str:
        return self.tag_number[:3]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag_number": self.tag_number,
            "asset_name": self.asset_name,
            "domain": self.domain,
            "lost_date": self.lost_date.isoformat(),
            "cost": int(self.cost) if self.cost is not None else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "asset_number": self.asset_number,
            "book": self.book,
            "entity": self.entity,
            "cost_center": self.cost_center,
            "product_code": self.product_code,
            "location": self.location,
            "group": self.group.value if self.group else None,
            "fee_rate": float(self.fee_rate) if self.fee_rate is not None else None,
            "physical": self.physical,
            "lookup_status": self.lookup_status.value if self.lookup_status else None,
            "classification_confirmed": self.classification_confirmed,
        }


@dataclass(frozen=True, slots=True)
class CompensationPreview:
    """Auditable result for one input without any external side effects."""

    input: CompensationAsset
    status: CompensationStatus
    reasons: tuple[str, ...] = ()
    usage_months: int | None = None
    depreciation_group: DepreciationGroup | None = None
    remaining_rate: Decimal | None = None
    remaining_value: int | None = None
    fee_rate: Decimal | None = None
    fee_value: int | None = None
    total_amount: int | None = None
    formula_explanation: str = ""

    @property
    def review_required(self) -> bool:
        return self.status is CompensationStatus.NEEDS_REVIEW

    @property
    def exempt(self) -> bool:
        return self.status is CompensationStatus.EXEMPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input.to_dict(),
            "status": self.status.value,
            "review_required": self.review_required,
            "reasons": list(self.reasons),
            "usage_months": self.usage_months,
            "depreciation_group": (
                self.depreciation_group.value if self.depreciation_group else None
            ),
            "remaining_rate": (
                float(self.remaining_rate) if self.remaining_rate is not None else None
            ),
            "remaining_value": self.remaining_value,
            "fee_rate": float(self.fee_rate) if self.fee_rate is not None else None,
            "fee_value": self.fee_value,
            "total_amount": self.total_amount,
            "exempt": self.exempt,
            "formula_explanation": self.formula_explanation,
        }
