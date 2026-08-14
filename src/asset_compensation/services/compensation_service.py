"""Pure implementation of the TranNNB compensation policy."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from asset_compensation.domain.compensation import (
    CompensationAsset,
    CompensationPreview,
    CompensationStatus,
    DepreciationGroup,
    ReferenceStatus,
)

FOUR_YEAR_BARCODES = frozenset(
    {
        "ADA",
        "BAT",
        "CAB",
        "CDW",
        "COL",
        "EHD",
        "GAM",
        "HEA",
        "IHD",
        "IPO",
        "KEY",
        "MOU",
        "NET",
        "PEN",
        "POW",
        "RAM",
        "SWA",
        "TAB",
        "UPS",
        "USB",
        "VGA",
        "WRI",
    }
)

SIX_YEAR_BARCODES = frozenset(
    {
        "APT",
        "CAM",
        "CPU",
        "FIW",
        "LAP",
        "LEN",
        "MOD",
        "MON",
        "NAL",
        "NAP",
        "NAS",
        "PHO",
        "PJP",
        "PRI",
        "PRJ",
        "ROU",
        "SCA",
        "SHR",
        "SWI",
        "TPC",
    }
)

COMPANY_DATA_BARCODES = frozenset({"CPU", "EHD", "IHD", "LAP", "PHO", "TPC", "USB"})

FOUR_YEAR_RATES = (
    Decimal("0.50"),
    Decimal("0.30"),
    Decimal("0.10"),
    Decimal("0.10"),
)
SIX_YEAR_RATES = (
    Decimal("0.20"),
    Decimal("0.20"),
    Decimal("0.20"),
    Decimal("0.10"),
    Decimal("0.10"),
    Decimal("0.10"),
)
FIVE_PERCENT = Decimal("0.05")
THIRTY_PERCENT = Decimal("0.30")
EXEMPTION_THRESHOLD = Decimal("500000")


def days360_european(start: date, end: date) -> int:
    """Match Excel ``DAYS360(start, end, TRUE)`` for valid ordered dates."""

    start_day = min(start.day, 30)
    end_day = min(end.day, 30)
    return (
        (end.year - start.year) * 360
        + (end.month - start.month) * 30
        + end_day
        - start_day
    )


def rounded_usage_months(start: date, end: date) -> int:
    """Convert European 30/360 days to months using Excel-style half-up rounding."""

    months = Decimal(days360_european(start, end)) / Decimal("30")
    return int(months.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def remaining_rate(group: DepreciationGroup, usage_months: int) -> Decimal:
    """Return the residual-use percentage defined by the approved policy schedule."""

    current_year = usage_months // 12 + 1
    months_used_in_year = usage_months % 12
    remaining_months = 12 - months_used_in_year
    fraction = Decimal(remaining_months) / Decimal("12")

    if group is DepreciationGroup.SIX_YEAR:
        if current_year >= 7:
            return Decimal("0.10")
        current_index = current_year - 1
        # The approved operational rule adds a flat year-7 10% so the schedule
        # totals 100%, even though the six printed annual rates total only 90%.
        return (
            SIX_YEAR_RATES[current_index] * fraction
            + sum(SIX_YEAR_RATES[current_index + 1 :], start=Decimal("0"))
            + Decimal("0.10")
        )

    if current_year >= 5:
        return Decimal("0.10")
    current_index = current_year - 1
    if current_year == 4:
        # Historical implementation includes the following flat 10% year.
        return FOUR_YEAR_RATES[current_index] * fraction + Decimal("0.10")
    return FOUR_YEAR_RATES[current_index] * fraction + sum(
        FOUR_YEAR_RATES[current_index + 1 :], start=Decimal("0")
    )


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _known_group(barcode: str) -> DepreciationGroup | None:
    if barcode in FOUR_YEAR_BARCODES:
        return DepreciationGroup.FOUR_YEAR
    if barcode in SIX_YEAR_BARCODES:
        return DepreciationGroup.SIX_YEAR
    return None


def _known_fee(barcode: str) -> Decimal:
    return THIRTY_PERCENT if barcode in COMPANY_DATA_BARCODES else FIVE_PERCENT


class CompensationService:
    """Calculate deterministic, side-effect-free compensation previews."""

    def preview(self, asset: CompensationAsset) -> CompensationPreview:
        if not asset.physical:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NOT_APPLICABLE,
                reasons=("Only physical IT assets are covered by IT.POL.01.",),
                formula_explanation="No calculation: the item is not a physical IT asset.",
            )

        if asset.lookup_status is not ReferenceStatus.MATCHED:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NEEDS_REVIEW,
                reasons=(
                    "Reference lookup did not produce exactly one verified match; "
                    "manual review is required.",
                ),
                formula_explanation="No calculation was made from unresolved reference data.",
            )

        usage_months = rounded_usage_months(asset.start_date, asset.lost_date)
        day_count = days360_european(asset.start_date, asset.lost_date)

        # Mandatory policy ordering: decide the per-asset exemption before any
        # depreciation-group or responsibility-fee calculation.
        if asset.cost < EXEMPTION_THRESHOLD and usage_months >= 12:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.EXEMPT,
                reasons=(
                    "Cost is below 500,000 VND and the asset has been used for at least "
                    "12 months.",
                ),
                usage_months=usage_months,
                remaining_value=0,
                fee_value=0,
                total_amount=0,
                formula_explanation=(
                    f"DAYS360_EU={day_count}; ROUND_HALF_UP({day_count}/30)="
                    f"{usage_months}. Exemption applied before schedule and fee calculations."
                ),
            )

        approved_group = _known_group(asset.barcode)
        if approved_group is None:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NEEDS_REVIEW,
                reasons=(
                    f"Barcode {asset.barcode or '(missing)'} is not in the approved "
                    "Define/CMDB mapping.",
                ),
                usage_months=usage_months,
                formula_explanation="No depreciation group or fee was guessed.",
            )

        if asset.group is not None and asset.group is not approved_group:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NEEDS_REVIEW,
                reasons=(
                    f"Provided group {asset.group.value} conflicts with approved "
                    f"{asset.barcode} mapping {approved_group.value}.",
                ),
                usage_months=usage_months,
                depreciation_group=approved_group,
                formula_explanation="No calculation was made from conflicting classifications.",
            )

        if asset.cost < EXEMPTION_THRESHOLD and approved_group is DepreciationGroup.SIX_YEAR:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NEEDS_REVIEW,
                reasons=(
                    "A sub-500,000 VND asset used under 12 months normally uses the four-year "
                    "schedule, but this barcode belongs to the six-year group.",
                ),
                usage_months=usage_months,
                depreciation_group=approved_group,
                formula_explanation="Manual review is required for this threshold exception.",
            )

        applied_group = (
            DepreciationGroup.FOUR_YEAR
            if asset.cost < EXEMPTION_THRESHOLD
            else approved_group
        )
        approved_fee = _known_fee(asset.barcode)
        if asset.fee_rate is not None and asset.fee_rate != approved_fee:
            return CompensationPreview(
                input=asset,
                status=CompensationStatus.NEEDS_REVIEW,
                reasons=(
                    f"Provided fee_rate {asset.fee_rate} conflicts with approved "
                    f"{asset.barcode} fee {approved_fee}.",
                ),
                usage_months=usage_months,
                depreciation_group=applied_group,
                fee_rate=approved_fee,
                formula_explanation="No calculation was made from conflicting fee rules.",
            )

        applied_rate = remaining_rate(applied_group, usage_months)
        remaining_value = _money(asset.cost * applied_rate)
        fee_value = _money(asset.cost * approved_fee)
        total = remaining_value + fee_value
        schedule_label = "six-year + flat year-7 10%" if (
            applied_group is DepreciationGroup.SIX_YEAR
        ) else "four-year"
        return CompensationPreview(
            input=asset,
            status=CompensationStatus.CALCULATED,
            usage_months=usage_months,
            depreciation_group=applied_group,
            remaining_rate=applied_rate,
            remaining_value=remaining_value,
            fee_rate=approved_fee,
            fee_value=fee_value,
            total_amount=total,
            formula_explanation=(
                f"DAYS360_EU={day_count}; ROUND_HALF_UP({day_count}/30)={usage_months}. "
                f"{schedule_label} remaining rate={applied_rate}; "
                f"ROUND_HALF_UP({int(asset.cost)} x {applied_rate})={remaining_value}; "
                f"ROUND_HALF_UP({int(asset.cost)} x {approved_fee})={fee_value}; "
                f"total={total}."
            ),
        )

    def preview_mapping(
        self,
        data: dict[str, Any],
        *,
        default_loss_date: date | None = None,
    ) -> CompensationPreview:
        """Validate a form-shaped mapping and calculate one preview."""

        lost_date = data.get("lost_date") or default_loss_date or date.today()
        asset = CompensationAsset(
            tag_number=data.get("tag_number", ""),
            asset_name=data.get("asset_name", ""),
            domain=data.get("domain", ""),
            lost_date=lost_date,
            cost=data.get("cost", ""),
            start_date=data.get("start_date", ""),
            asset_number=data.get("asset_number"),
            book=data.get("book"),
            entity=data.get("entity"),
            cost_center=data.get("cost_center"),
            product_code=data.get("product_code"),
            location=data.get("location"),
            group=data.get("group"),
            fee_rate=data.get("fee_rate"),
            physical=data.get("physical", True),
            lookup_status=data.get("lookup_status", ReferenceStatus.MATCHED),
        )
        return self.preview(asset)
