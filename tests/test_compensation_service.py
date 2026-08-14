"""Deterministic policy tests for the TranNNB calculation service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from asset_compensation.domain import (
    CompensationAsset,
    CompensationStatus,
    DepreciationGroup,
    ReferenceStatus,
    ValidationError,
)
from asset_compensation.services import (
    CompensationService,
    days360_european,
    remaining_rate,
    rounded_usage_months,
)


def _asset(**overrides: object) -> CompensationAsset:
    values: dict[str, object] = {
        "tag_number": "MOU10001",
        "asset_name": "Synthetic mouse",
        "domain": "demo.user",
        "start_date": date(2026, 1, 1),
        "lost_date": date(2026, 1, 1),
        "cost": 1_000_000,
    }
    values.update(overrides)
    return CompensationAsset(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start", "end", "expected_days"),
    [
        (date(2026, 1, 1), date(2026, 1, 1), 0),
        (date(2026, 1, 31), date(2026, 2, 28), 28),
        (date(2026, 2, 28), date(2026, 3, 31), 32),
        (date(2025, 12, 31), date(2026, 1, 31), 30),
    ],
)
def test_days360_uses_european_method(start: date, end: date, expected_days: int) -> None:
    assert days360_european(start, end) == expected_days


def test_usage_months_rounds_half_up_instead_of_bankers_or_roundup() -> None:
    start = date(2026, 1, 1)

    assert rounded_usage_months(start, date(2026, 1, 15)) == 0  # 14 / 30
    assert rounded_usage_months(start, date(2026, 1, 16)) == 1  # 15 / 30
    assert rounded_usage_months(start, date(2026, 1, 30)) == 1  # 29 / 30


@pytest.mark.parametrize(
    ("group", "months", "expected"),
    [
        (DepreciationGroup.FOUR_YEAR, 0, Decimal("1.00")),
        (DepreciationGroup.FOUR_YEAR, 14, Decimal("0.45")),
        (DepreciationGroup.FOUR_YEAR, 36, Decimal("0.20")),
        (DepreciationGroup.FOUR_YEAR, 48, Decimal("0.10")),
        (DepreciationGroup.SIX_YEAR, 0, Decimal("1.00")),
        (DepreciationGroup.SIX_YEAR, 60, Decimal("0.20")),
        (DepreciationGroup.SIX_YEAR, 72, Decimal("0.10")),
    ],
)
def test_schedule_boundaries(
    group: DepreciationGroup,
    months: int,
    expected: Decimal,
) -> None:
    assert remaining_rate(group, months) == expected


def test_six_year_schedule_includes_approved_extra_year_seven_ten_percent() -> None:
    rate = remaining_rate(DepreciationGroup.SIX_YEAR, 4)

    assert rate == Decimal("0.20") * Decimal(8) / Decimal(12) + Decimal("0.80")


@pytest.mark.parametrize(
    ("tag_number", "group", "fee_rate"),
    [
        ("LAP10001", DepreciationGroup.SIX_YEAR, Decimal("0.30")),
        ("MON10001", DepreciationGroup.SIX_YEAR, Decimal("0.05")),
        ("MOU10001", DepreciationGroup.FOUR_YEAR, Decimal("0.05")),
        ("USB10001", DepreciationGroup.FOUR_YEAR, Decimal("0.30")),
        ("TPC10001", DepreciationGroup.SIX_YEAR, Decimal("0.30")),
        ("SWA10001", DepreciationGroup.FOUR_YEAR, Decimal("0.05")),
    ],
)
def test_known_barcodes_select_approved_group_and_fee(
    tag_number: str,
    group: DepreciationGroup,
    fee_rate: Decimal,
) -> None:
    result = CompensationService().preview(_asset(tag_number=tag_number))

    assert result.status is CompensationStatus.CALCULATED
    assert result.depreciation_group is group
    assert result.fee_rate == fee_rate


def test_sub_500k_exemption_is_checked_before_barcode_classification() -> None:
    result = CompensationService().preview(
        _asset(
            tag_number="ZZZ10001",
            cost=499_999,
            start_date=date(2025, 1, 1),
            lost_date=date(2026, 1, 1),
        )
    )

    assert result.status is CompensationStatus.EXEMPT
    assert result.exempt is True
    assert result.usage_months == 12
    assert result.depreciation_group is None
    assert (result.remaining_value, result.fee_value, result.total_amount) == (0, 0, 0)


def test_exactly_500k_is_not_exempt() -> None:
    result = CompensationService().preview(
        _asset(
            cost=500_000,
            start_date=date(2025, 1, 1),
            lost_date=date(2026, 1, 1),
        )
    )

    assert result.status is CompensationStatus.CALCULATED
    assert result.exempt is False
    assert result.remaining_rate == Decimal("0.50")
    assert result.remaining_value == 250_000
    assert result.fee_value == 25_000
    assert result.total_amount == 275_000


def test_sub_500k_under_twelve_months_uses_four_year_schedule() -> None:
    result = CompensationService().preview(
        _asset(
            cost=499_999,
            start_date=date(2025, 1, 1),
            lost_date=date(2025, 12, 1),
        )
    )

    assert result.status is CompensationStatus.CALCULATED
    assert result.usage_months == 11
    assert result.depreciation_group is DepreciationGroup.FOUR_YEAR
    assert result.remaining_value == 270_833
    assert result.fee_value == 25_000
    assert result.total_amount == 295_833


def test_sub_500k_six_year_exception_requires_manual_review() -> None:
    result = CompensationService().preview(
        _asset(
            tag_number="LAP10001",
            cost=499_999,
            start_date=date(2025, 1, 1),
            lost_date=date(2025, 12, 1),
        )
    )

    assert result.status is CompensationStatus.NEEDS_REVIEW
    assert result.review_required is True
    assert "six-year group" in result.reasons[0]
    assert result.total_amount is None


def test_unknown_barcode_and_ambiguous_lookup_are_not_guessed() -> None:
    service = CompensationService()

    unknown = service.preview(_asset(tag_number="ZZZ10001"))
    ambiguous = service.preview(
        _asset(tag_number="LAP10001", lookup_status=ReferenceStatus.AMBIGUOUS)
    )

    assert unknown.status is CompensationStatus.NEEDS_REVIEW
    assert unknown.depreciation_group is None
    assert unknown.total_amount is None
    assert ambiguous.status is CompensationStatus.NEEDS_REVIEW
    assert ambiguous.usage_months is None
    assert ambiguous.total_amount is None


def test_nonphysical_item_is_listed_but_not_calculated() -> None:
    result = CompensationService().preview(
        _asset(tag_number="ZZZ10001", asset_name="Synthetic cloud license", physical=False)
    )

    assert result.status is CompensationStatus.NOT_APPLICABLE
    assert result.review_required is False
    assert result.total_amount is None
    assert "physical IT asset" in result.reasons[0]


def test_conflicting_manual_group_or_fee_requires_review() -> None:
    service = CompensationService()

    wrong_group = service.preview(_asset(group="SIX_YEAR"))
    wrong_fee = service.preview(_asset(fee_rate="30%"))

    assert wrong_group.status is CompensationStatus.NEEDS_REVIEW
    assert wrong_fee.status is CompensationStatus.NEEDS_REVIEW


def test_money_is_rounded_half_up_with_decimal_arithmetic() -> None:
    result = CompensationService().preview(_asset(cost=500_010))

    assert result.remaining_value == 500_010
    assert result.fee_value == 25_001
    assert result.total_amount == 525_011


@pytest.mark.parametrize(
    "overrides",
    [
        {"cost": -1},
        {"cost": "10.5"},
        {"lost_date": "31/01/2026"},
        {"start_date": date(2026, 2, 1), "lost_date": date(2026, 1, 1)},
        {"physical": "false"},
        {"fee_rate": "0.10"},
    ],
)
def test_invalid_inputs_fail_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _asset(**overrides)
