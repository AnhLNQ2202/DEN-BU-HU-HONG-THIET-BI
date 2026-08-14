"""Synthetic tests for semantic accounting-policy resolution."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

from asset_compensation.adapters import AccountingTemplateAdapter
from asset_compensation.domain import ValidationError
from asset_compensation.services import (
    DAMAGED_NO_REPAIR_POLICY,
    DAMAGED_REPAIR_POLICY,
    LOST_DEPRECIATION_ASSET_POLICY,
    LOST_DEPRECIATION_OTHER_POLICY,
    LOST_FALLBACK_POLICY,
    LOST_RESPONSIBILITY_POLICY,
    PREPAYMENT_POLICY,
    AccountingPolicyResolver,
)


def _resolver(**overrides: str) -> AccountingPolicyResolver:
    policies = {
        PREPAYMENT_POLICY: "DEMO.PREPAY",
        DAMAGED_REPAIR_POLICY: "DEMO.DAMAGED.REPAIR",
        DAMAGED_NO_REPAIR_POLICY: "DEMO.DAMAGED.NO_REPAIR",
        LOST_DEPRECIATION_ASSET_POLICY: (
            "DEMO.{cost_center}.LOST.ASSET.{product_code}.{location}"
        ),
        LOST_DEPRECIATION_OTHER_POLICY: (
            "DEMO.{cost_center}.LOST.OTHER.{product_code}.{location}"
        ),
        LOST_RESPONSIBILITY_POLICY: (
            "DEMO.{cost_center}.LOST.FEE.{product_code}.{location}"
        ),
        LOST_FALLBACK_POLICY: "DEMO.LOST.FALLBACK",
    }
    policies.update(overrides)
    return AccountingPolicyResolver(policies)


def _damaged(case_id: str, repair_status: str) -> dict[str, object]:
    return {
        "id": case_id,
        "case_type": "DAMAGED",
        "domain": "demo.damaged",
        "asset_code": "DEMO-LAP-001",
        "amount": 100_000,
        "repair_status": repair_status,
        "supplier_number": "00101",
        "supplier_site": "OFFICE",
        "supplier_name": "Synthetic Supplier",
        "metadata": {"employee_inactive": True},
    }


def _lost() -> dict[str, object]:
    return {
        "id": "lost-1",
        "case_type": "LOST",
        "domain": "demo.lost",
        "asset_code": "DEMO-CAB-001",
        "amount": 250_000,
        "residual_value": 200_000,
        "responsibility_fee": 50_000,
        "supplier_number": "102",
        "supplier_site": "0022",
        "supplier_name": "Synthetic Supplier",
        "metadata": {
            "employee_inactive": True,
            "prepayment_policy_key": PREPAYMENT_POLICY,
            "credit_components": [
                {
                    "policy_key": LOST_DEPRECIATION_ASSET_POLICY,
                    "amount": 200_000,
                    "cost_center": "0603",
                    "product_code": "000",
                    "location": "01",
                    "entity_non_vng": True,
                },
                {
                    "policy_key": LOST_RESPONSIBILITY_POLICY,
                    "amount": 50_000,
                    "cost_center": "0603",
                    "product_code": "000",
                    "location": "01",
                    "entity_non_vng": False,
                },
            ],
        },
    }


def test_resolves_semantic_components_and_keeps_leading_zero_dimensions() -> None:
    result = _resolver().resolve(_lost())
    metadata = result.case["metadata"]

    assert result.policy_keys == (
        PREPAYMENT_POLICY,
        LOST_DEPRECIATION_ASSET_POLICY,
        LOST_RESPONSIBILITY_POLICY,
    )
    assert metadata["debit_gl"] == "DEMO.PREPAY"
    assert metadata["prepayment_highlight"] == "green"
    assert metadata["credit_lines"] == [
        {
            "account": "DEMO.0603.LOST.ASSET.000.01",
            "amount": 200_000,
            "highlight": "green",
        },
        {
            "account": "DEMO.0603.LOST.FEE.000.01",
            "amount": 50_000,
            "highlight": None,
        },
    ]


def test_damaged_repair_policy_and_original_type_order_are_fail_closed() -> None:
    results = _resolver().resolve_many(
        [_lost(), _damaged("damaged-repair", "REPAIRED"), _damaged("damaged-no", "NOT_REPAIRED")]
    )

    assert [item.case["id"] for item in results] == [
        "damaged-repair",
        "damaged-no",
        "lost-1",
    ]
    assert results[0].case["metadata"]["credit_gl"] == "DEMO.DAMAGED.REPAIR"
    assert results[1].case["metadata"]["credit_gl"] == "DEMO.DAMAGED.NO_REPAIR"

    with pytest.raises(ValidationError, match="repair status"):
        _resolver().resolve(_damaged("damaged-unknown", "UNKNOWN"))


def test_unknown_policy_or_invalid_dimension_never_falls_back_silently() -> None:
    unknown = _lost()
    unknown["metadata"]["credit_components"][0]["policy_key"] = "UNCONFIGURED"
    with pytest.raises(ValidationError, match="not configured"):
        _resolver().resolve(unknown)

    invalid = _lost()
    invalid["metadata"]["credit_components"][0]["cost_center"] = "=BAD"
    with pytest.raises(ValidationError, match="cost_center"):
        _resolver().resolve(invalid)


def test_zero_value_case_is_rejected_before_accounting_export() -> None:
    zero = _lost()
    zero["amount"] = 0
    zero["residual_value"] = 0
    zero["responsibility_fee"] = 0
    zero["metadata"]["credit_components"] = []

    with pytest.raises(ValidationError, match="greater than zero"):
        _resolver().resolve(zero)


def test_export_applies_legacy_yellow_and_green_review_signals(tmp_path: Path) -> None:
    resolved = _resolver().resolve_many([_lost(), _damaged("damaged-1", "REPAIRED")])
    output = tmp_path / "semantic-accounting.xlsx"
    AccountingTemplateAdapter(
        {
            "DAMAGED": ("DEMO.DEFAULT.DEBIT", "DEMO.DEFAULT.DAMAGED"),
            "LOST": ("DEMO.DEFAULT.DEBIT", "DEMO.DEFAULT.LOST"),
        }
    ).export(
        [item.case for item in resolved],
        output,
        batch_name="DEMO-BATCH-",
        invoice_date=date(2026, 8, 14),
    )

    workbook = load_workbook(output, data_only=False)
    try:
        sheet = workbook.active
        # DAMAGED is sorted first and inactive, so both of its rows are yellow.
        assert sheet["A2"].fill.fgColor.rgb.endswith("FFFF00")
        assert sheet["A3"].fill.fgColor.rgb.endswith("FFFF00")
        # LOST prepayment and its non-VNG component are green; inactive fallback is yellow.
        assert sheet["A4"].fill.fgColor.rgb.endswith("92D050")
        assert sheet["A5"].fill.fgColor.rgb.endswith("92D050")
        assert sheet["A6"].fill.fgColor.rgb.endswith("FFFF00")
        assert sheet["M4"].value == "DEMO.PREPAY"
        assert sheet["M5"].value == "DEMO.0603.LOST.ASSET.000.01"
        assert sheet["M6"].value == "DEMO.0603.LOST.FEE.000.01"
    finally:
        workbook.close()
