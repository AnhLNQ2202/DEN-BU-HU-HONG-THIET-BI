"""Resolution tests for approved defaults, presets, and review stops."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from asset_compensation.adapters import (
    CcdcClassification,
    CcdcWorkbookIndex,
    FaGlRecord,
    FaGlWorkbookIndex,
)
from asset_compensation.domain import (
    CompensationStatus,
    DepreciationGroup,
    ReferenceStatus,
)
from asset_compensation.services import TranAssetRequest, TranWorkflowService


def _record(
    tag: str,
    *,
    cost: int | None = 1_000_000,
    start_date: date | None = date(2025, 1, 1),
) -> FaGlRecord:
    return FaGlRecord(
        tag_number=tag,
        asset_number="SYN-001",
        start_date=start_date,
        cost=None if cost is None else Decimal(cost),
        book="Asset",
        entity="VNGS",
        cost_center="1234",
        product_code="567",
        location="89",
        company_name="Synthetic Company",
        source_file="synthetic-fa-gl.xlsx",
        source_sheet="VNGS-Asset",
        source_row=3,
    )


def _fa_index(*records: FaGlRecord) -> FaGlWorkbookIndex:
    grouped: dict[str, tuple[FaGlRecord, ...]] = {}
    for record in records:
        grouped[record.tag_number] = (*grouped.get(record.tag_number, ()), record)
    return FaGlWorkbookIndex(grouped, Path("synthetic-fa-gl.xlsx"))


def _ccdc() -> CcdcWorkbookIndex:
    return CcdcWorkbookIndex(
        classifications={
            "ZZZ": (
                CcdcClassification(
                    barcode="ZZZ",
                    product_type="Other/Spe.part",
                    group_type="Hardware",
                    group=DepreciationGroup.FOUR_YEAR,
                    physical=True,
                    source_sheet="Define",
                    source_row=2,
                ),
            ),
            "SRV": (
                CcdcClassification(
                    barcode="SRV",
                    product_type="Cloud product",
                    group_type="Service",
                    group=None,
                    physical=False,
                    source_sheet="Define",
                    source_row=3,
                ),
            ),
        },
        start_dates={"ADA10001": (date(2024, 1, 1), date(2023, 1, 1))},
        source_path=Path("synthetic-ccdc.xlsx"),
    )


def test_matched_fa_gl_fields_are_static_and_calculated() -> None:
    request = TranAssetRequest(
        tag_number="LAP10001",
        asset_name="Synthetic laptop",
        domain="demo.user",
        lost_date=date(2026, 1, 1),
    )

    result = TranWorkflowService().resolve(request, _fa_index(_record("LAP10001")))

    assert result.ready is True
    assert result.asset is not None
    assert result.asset.asset_number == "SYN-001"
    assert result.asset.book == "Asset"
    assert result.asset.entity == "VNGS"
    assert result.preview is not None
    assert result.preview.status is CompensationStatus.CALCULATED
    assert any("synthetic-fa-gl.xlsx" in note for note in result.notes)


def test_missing_fa_gl_uses_approved_adapter_defaults_and_oldest_ccdc_date() -> None:
    request = TranAssetRequest(
        tag_number="ADA10001",
        asset_name="Synthetic Lenovo adapter",
        domain="demo.user",
        lost_date=date(2026, 1, 1),
    )

    result = TranWorkflowService().resolve(request, _fa_index(), ccdc=_ccdc())

    assert result.ready is True
    assert result.fa_status is ReferenceStatus.NOT_FOUND
    assert result.asset is not None
    assert result.asset.asset_number == "ko có trên ORC"
    assert result.asset.start_date == date(2023, 1, 1)
    assert result.asset.cost == Decimal("1060000")
    assert (result.asset.book, result.asset.entity) == ("Tool", "VNG")
    assert (
        result.asset.cost_center,
        result.asset.product_code,
        result.asset.location,
    ) == ("0603", "000", "01")


def test_missing_non_adapter_cost_stops_for_confirmation() -> None:
    request = TranAssetRequest(
        tag_number="MOU10001",
        asset_name="Synthetic mouse",
        domain="demo.user",
        lost_date=date(2026, 1, 1),
        confirmed_start_date=date(2025, 1, 1),
    )

    result = TranWorkflowService().resolve(request, _fa_index())

    assert result.ready is False
    assert result.asset is None
    assert any("Cost is absent" in issue for issue in result.issues)


def test_zero_fa_cost_needs_confirmation_and_accepts_explicit_approved_cost() -> None:
    fa_gl = _fa_index(_record("ADA10001", cost=0))
    unresolved = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="ADA10001",
            asset_name="Synthetic Apple adapter",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
        ),
        fa_gl,
    )
    resolved = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="ADA10001",
            asset_name="Synthetic Apple adapter",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
            confirmed_cost=2_044_545,
        ),
        fa_gl,
    )

    assert unresolved.ready is False
    assert any("blank or zero" in issue for issue in unresolved.issues)
    assert resolved.ready is True
    assert resolved.asset is not None
    assert resolved.asset.cost == Decimal("2044545")
    assert any("explicit user confirmation" in note for note in resolved.notes)


def test_unknown_barcode_can_use_define_or_explicit_user_confirmation() -> None:
    fa_gl = _fa_index(_record("ZZZ10001"), _record("QQQ10001"))
    from_define = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="ZZZ10001",
            asset_name="Synthetic peripheral",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
        ),
        fa_gl,
        ccdc=_ccdc(),
    )
    manual = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="QQQ10001",
            asset_name="Synthetic legacy hardware",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
            physical=True,
            confirmed_group="SIX_YEAR",
            confirmed_fee_rate="30%",
            classification_confirmed=True,
        ),
        fa_gl,
    )

    assert from_define.ready is True
    assert from_define.preview is not None
    assert from_define.preview.depreciation_group is DepreciationGroup.FOUR_YEAR
    assert from_define.preview.fee_rate == Decimal("0.05")
    assert manual.ready is True
    assert manual.preview is not None
    assert manual.preview.depreciation_group is DepreciationGroup.SIX_YEAR
    assert manual.preview.fee_rate == Decimal("0.30")


def test_unverified_unknown_barcode_never_defaults_to_four_year_five_percent() -> None:
    result = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="QQQ10001",
            asset_name="Synthetic unknown hardware",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
            physical=True,
        ),
        _fa_index(_record("QQQ10001")),
    )

    assert result.ready is False
    assert result.preview is None
    assert any("confirm both group and fee" in issue for issue in result.issues)


def test_verified_nonphysical_ccdc_item_is_listed_without_fa_cost_or_date() -> None:
    result = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="SRV10001",
            asset_name="Synthetic cloud service",
            domain="demo.user",
            lost_date=date(2026, 1, 1),
        ),
        _fa_index(),
        ccdc=_ccdc(),
    )

    assert result.ready is True
    assert result.asset is not None
    assert result.asset.physical is False
    assert result.asset.cost is None
    assert result.asset.start_date is None
    assert result.preview is not None
    assert result.preview.status is CompensationStatus.NOT_APPLICABLE


def test_loss_date_defaults_to_injected_today() -> None:
    result = TranWorkflowService().resolve(
        TranAssetRequest(
            tag_number="MOU10001",
            asset_name="Synthetic mouse",
            domain="demo.user",
        ),
        _fa_index(_record("MOU10001", start_date=date(2025, 1, 1))),
        today=date(2026, 2, 3),
    )

    assert result.asset is not None
    assert result.asset.lost_date == date(2026, 2, 3)
