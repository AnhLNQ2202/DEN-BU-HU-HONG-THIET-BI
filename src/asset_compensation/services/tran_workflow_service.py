"""Reference resolution for the seven-step TranNNB lost-asset workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from asset_compensation.adapters.tran_reference_xlsx import (
    CcdcWorkbookIndex,
    FaGlRecord,
    FaGlWorkbookIndex,
)
from asset_compensation.domain import (
    CompensationAsset,
    CompensationPreview,
    DepreciationGroup,
    ReferenceStatus,
    ValidationError,
)
from asset_compensation.domain.compensation import (
    MAX_ASSET_NAME_CHARS,
    MAX_ASSET_TAG_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_NUMERIC_INPUT_CHARS,
    MAX_SAFE_VND,
)

from .compensation_service import CompensationService, known_fee, known_group

_NO_ORC_ASSET_NUMBER = "ko có trên ORC"
_DEFAULT_COST_CENTER = "0603"
_DEFAULT_PRODUCT_CODE = "000"
_DEFAULT_LOCATION = "01"
_LENOVO_ADAPTER_COST = Decimal("1060000")
_APPLE_ADAPTER_COST = Decimal("2044545")


def _required_text(value: object, field_name: str, max_length: int) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValidationError(f"{field_name} is required")
    if len(result) > max_length or any(
        ord(character) < 32 or ord(character) == 127 for character in result
    ):
        raise ValidationError(f"{field_name} exceeds the safe text limit")
    return result


def _optional_date(value: date | datetime | str | None, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{field_name} must use YYYY-MM-DD") from exc


def _optional_cost(value: Decimal | int | str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError("confirmed_cost must be a positive whole VND amount")
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError("confirmed_cost must be a positive whole VND amount")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("confirmed_cost must be a positive whole VND amount") from exc
    if (
        not result.is_finite()
        or result <= 0
        or result > MAX_SAFE_VND
        or result != result.to_integral_value()
    ):
        raise ValidationError("confirmed_cost must be a positive whole VND amount")
    return result


def _optional_group(value: DepreciationGroup | str | None) -> DepreciationGroup | None:
    if value in (None, ""):
        return None
    if isinstance(value, DepreciationGroup):
        return value
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError("confirmed_group must be FOUR_YEAR or SIX_YEAR")
    normalized = text.upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "4": DepreciationGroup.FOUR_YEAR,
        "4_YEAR": DepreciationGroup.FOUR_YEAR,
        "FOUR_YEAR": DepreciationGroup.FOUR_YEAR,
        "6": DepreciationGroup.SIX_YEAR,
        "6_YEAR": DepreciationGroup.SIX_YEAR,
        "SIX_YEAR": DepreciationGroup.SIX_YEAR,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValidationError("confirmed_group must be FOUR_YEAR or SIX_YEAR") from exc


def _optional_fee(value: Decimal | int | float | str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise ValidationError("confirmed_fee_rate must be 0.05 or 0.30")
    percentage = text.endswith("%")
    if percentage:
        text = text[:-1]
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError("confirmed_fee_rate must be 0.05 or 0.30") from exc
    if percentage or result in {Decimal("5"), Decimal("30")}:
        result /= Decimal("100")
    if result not in {Decimal("0.05"), Decimal("0.30")}:
        raise ValidationError("confirmed_fee_rate must be 0.05 or 0.30")
    return result


@dataclass(frozen=True, slots=True)
class TranAssetRequest:
    """The four email fields plus explicit operator confirmations."""

    tag_number: str
    asset_name: str
    domain: str
    lost_date: date | datetime | str | None = None
    physical: bool | None = None
    confirmed_cost: Decimal | int | str | None = None
    confirmed_start_date: date | datetime | str | None = None
    confirmed_group: DepreciationGroup | str | None = None
    confirmed_fee_rate: Decimal | int | float | str | None = None
    classification_confirmed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tag_number",
            _required_text(
                self.tag_number, "tag_number", MAX_ASSET_TAG_CHARS
            ).upper(),
        )
        object.__setattr__(
            self,
            "asset_name",
            _required_text(
                self.asset_name, "asset_name", MAX_ASSET_NAME_CHARS
            ),
        )
        object.__setattr__(
            self,
            "domain",
            _required_text(self.domain, "domain", MAX_DOMAIN_CHARS),
        )
        object.__setattr__(self, "lost_date", _optional_date(self.lost_date, "lost_date"))
        object.__setattr__(
            self,
            "confirmed_start_date",
            _optional_date(self.confirmed_start_date, "confirmed_start_date"),
        )
        object.__setattr__(self, "confirmed_cost", _optional_cost(self.confirmed_cost))
        object.__setattr__(self, "confirmed_group", _optional_group(self.confirmed_group))
        object.__setattr__(
            self, "confirmed_fee_rate", _optional_fee(self.confirmed_fee_rate)
        )
        if self.physical is not None and not isinstance(self.physical, bool):
            raise ValidationError("physical must be a boolean or null")
        if not isinstance(self.classification_confirmed, bool):
            raise ValidationError("classification_confirmed must be a boolean")

    @property
    def barcode(self) -> str:
        return self.tag_number[:3]


@dataclass(frozen=True, slots=True)
class TranResolution:
    """Auditable resolution; an asset/preview exists only when inputs are complete."""

    request: TranAssetRequest
    asset: CompensationAsset | None
    preview: CompensationPreview | None
    notes: tuple[str, ...]
    issues: tuple[str, ...]
    fa_status: ReferenceStatus
    classification_status: ReferenceStatus

    @property
    def ready(self) -> bool:
        return self.asset is not None and self.preview is not None and not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset.to_dict() if self.asset else None,
            "preview": self.preview.to_dict() if self.preview else None,
            "notes": list(self.notes),
            "issues": list(self.issues),
            "fa_status": self.fa_status.value,
            "classification_status": self.classification_status.value,
            "ready": self.ready,
        }


def _adapter_preset(asset_name: str) -> tuple[Decimal | None, str | None]:
    normalized = asset_name.casefold()
    if "lenovo" in normalized:
        return _LENOVO_ADAPTER_COST, "Adapter Lenovo preset = 1,060,000 VND."
    if "macbook" in normalized or "apple" in normalized:
        return _APPLE_ADAPTER_COST, "Adapter Macbook/Apple preset = 2,044,545 VND."
    return None, None


class TranWorkflowService:
    """Resolve FA&GL/CCDC evidence and invoke the pure compensation calculator."""

    def __init__(self, calculator: CompensationService | None = None) -> None:
        self._calculator = calculator or CompensationService()

    def resolve(
        self,
        request: TranAssetRequest,
        fa_gl: FaGlWorkbookIndex,
        *,
        ccdc: CcdcWorkbookIndex | None = None,
        today: date | None = None,
    ) -> TranResolution:
        lost_date = request.lost_date or today or date.today()
        lookup = fa_gl.lookup(request.tag_number)
        notes: list[str] = []
        issues: list[str] = []

        classification_status = ReferenceStatus.NOT_FOUND
        ccdc_group: DepreciationGroup | None = None
        ccdc_physical: bool | None = None
        if ccdc is not None:
            classification = ccdc.classification(request.barcode)
            classification_status = classification.status
            if classification.status is ReferenceStatus.MATCHED:
                assert classification.classification is not None
                ccdc_group = classification.classification.group
                ccdc_physical = classification.classification.physical
                notes.append(
                    "Phân loại từ file CCDC, sheet "
                    f"{classification.classification.source_sheet}, barcode {request.barcode}."
                )
            elif classification.status is ReferenceStatus.AMBIGUOUS:
                issues.append("CCDC returns conflicting classifications for this barcode.")

        mapped_group = known_group(request.barcode)
        if mapped_group is not None and classification_status is ReferenceStatus.NOT_FOUND:
            classification_status = ReferenceStatus.MATCHED
            notes.append(
                f"Barcode {request.barcode} dùng mapping đã được duyệt trong IT.POL.01."
            )
        physical = request.physical
        if physical is None:
            if ccdc_physical is not None:
                physical = ccdc_physical
            elif mapped_group is not None:
                physical = True

        record: FaGlRecord | None = lookup.record
        if lookup.status is ReferenceStatus.AMBIGUOUS and physical is not False:
            issues.append("Tag number has multiple FA&GL matches; select one before calculation.")
        if record is None:
            asset_number = _NO_ORC_ASSET_NUMBER
            book = "Tool"
            entity = "VNG"
            cost_center = _DEFAULT_COST_CENTER
            product_code = _DEFAULT_PRODUCT_CODE
            location = _DEFAULT_LOCATION
            notes.append(
                "Ko có trên ORC; dùng mặc định Sổ=Tool, Entity=VNG, Cost center=0603, "
                "Product code=000, Location=01; entity thật chưa tra được."
            )
            start_date = request.confirmed_start_date
            if start_date is None and ccdc is not None:
                start_date = ccdc.earliest_start_date(request.tag_number)
                if start_date is not None:
                    notes.append(
                        "Ngày đưa vào sử dụng lấy từ CCDC/BC Xuatkho cột B/E; "
                        "nếu có nhiều dòng thì lấy ngày cũ nhất."
                    )
            if start_date is None and physical is not False:
                issues.append(
                    "Start date is absent from FA&GL and BC Xuatkho; user confirmation is required."
                )
            cost = request.confirmed_cost
            if cost is not None:
                notes.append("Cost supplied through explicit user confirmation.")
            elif request.barcode == "ADA" and physical is not False:
                cost, preset_note = _adapter_preset(request.asset_name)
                if preset_note:
                    notes.append(preset_note + " Applied because FA&GL has no match.")
            if cost is None and physical is not False:
                issues.append(
                    "Cost is absent from FA&GL and has no approved automatic preset; "
                    "user confirmation is required."
                )
        else:
            asset_number = record.asset_number
            book = record.book
            entity = record.entity
            cost_center = record.cost_center
            product_code = record.product_code
            location = record.location
            notes.append(record.source_note)
            start_date = record.start_date or request.confirmed_start_date
            if record.start_date is None and request.confirmed_start_date is not None:
                notes.append("Missing FA&GL start date replaced by explicit user confirmation.")
            if start_date is None and physical is not False:
                issues.append("FA&GL start date is empty; user confirmation is required.")
            cost = record.cost
            if cost in (None, Decimal("0")) and physical is not False:
                if request.confirmed_cost is None:
                    issues.append(
                        "FA&GL cost is blank or zero; an approved cost confirmation is required."
                    )
                else:
                    cost = request.confirmed_cost
                    notes.append("Blank/zero FA&GL cost replaced by explicit user confirmation.")

        group = request.confirmed_group
        fee_rate = request.confirmed_fee_rate
        classification_confirmed = request.classification_confirmed
        if physical is False:
            group = None
            fee_rate = None
            classification_confirmed = False
        elif mapped_group is None and ccdc_group is not None:
            group = ccdc_group
            fee_rate = known_fee(request.barcode)
            classification_confirmed = True
        elif mapped_group is None and not (
            classification_confirmed and group is not None and fee_rate is not None
        ):
            issues.append(
                "Barcode is absent from approved Define/CMDB mappings; confirm both group and fee."
            )

        if physical is None:
            issues.append("Physical/non-physical classification requires verification.")
        if issues or (physical is not False and (cost is None or start_date is None)):
            return TranResolution(
                request=request,
                asset=None,
                preview=None,
                notes=tuple(notes),
                issues=tuple(dict.fromkeys(issues)),
                fa_status=lookup.status,
                classification_status=classification_status,
            )

        asset = CompensationAsset(
            tag_number=request.tag_number,
            asset_name=request.asset_name,
            domain=request.domain,
            lost_date=lost_date,
            cost=cost,
            start_date=start_date,
            asset_number=asset_number,
            book=book,
            entity=entity,
            cost_center=cost_center,
            product_code=product_code,
            location=location,
            group=group,
            fee_rate=fee_rate,
            physical=physical,
            lookup_status=(
                lookup.status if physical is False else ReferenceStatus.MATCHED
            ),
            classification_confirmed=classification_confirmed,
        )
        preview = self._calculator.preview(asset)
        preview_issues = preview.reasons if preview.review_required else ()
        return TranResolution(
            request=request,
            asset=asset,
            preview=preview,
            notes=tuple(notes),
            issues=tuple(preview_issues),
            fa_status=lookup.status,
            classification_status=classification_status,
        )

    def resolve_many(
        self,
        requests: list[TranAssetRequest] | tuple[TranAssetRequest, ...],
        fa_gl: FaGlWorkbookIndex,
        *,
        ccdc: CcdcWorkbookIndex | None = None,
        today: date | None = None,
    ) -> tuple[TranResolution, ...]:
        if not requests:
            raise ValidationError("At least one TranNNB asset is required")
        return tuple(
            self.resolve(request, fa_gl, ccdc=ccdc, today=today) for request in requests
        )
