"""HTTP contract tests for read-only compensation previews."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

from asset_compensation.config import Settings
from asset_compensation.web import create_app


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    application = create_app(
        Settings(
            data_dir=tmp_path / "runtime",
            demo_mode=True,
            secret_key="synthetic-preview-test-key",
        )
    )
    application.config.update(TESTING=True)
    yield application
    application.extensions["asset_hub"]["repository"].close()


def _request_asset(**overrides: object) -> dict[str, object]:
    asset: dict[str, object] = {
        "tag_number": "MOU10001",
        "asset_name": "Synthetic mouse",
        "domain": "demo.user",
        "lost_date": "2026-01-01",
        "cost": 500_000,
        "start_date": "2025-01-01",
        "asset_number": "SYNTHETIC-001",
        "book": "Tool",
        "entity": "VNG",
        "cost_center": "0603",
        "product_code": "000",
        "location": "01",
        "physical": True,
        "lookup_status": "MATCHED",
    }
    asset.update(overrides)
    return asset


def test_preview_api_returns_form_friendly_results_without_side_effects(app: Flask) -> None:
    client = app.test_client()
    extension = app.extensions["asset_hub"]
    before_cases = extension["case_service"].summary().total
    before_outputs = tuple(extension["settings"].output_dir.iterdir())

    response = client.post(
        "/api/compensation/preview",
        json={
            "assets": [
                _request_asset(cost=499_999, tag_number="ZZZ10001"),
                _request_asset(
                    tag_number="LAP10001",
                    asset_name="Synthetic laptop",
                    start_date="2026-01-01",
                    cost=1_200_000,
                ),
            ]
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["review_required"] is False
    assert payload["status_counts"] == {"CALCULATED": 1, "EXEMPT": 1}

    exempt, calculated = payload["results"]
    assert exempt["status"] == "EXEMPT"
    assert exempt["exempt"] is True
    assert exempt["total_amount"] == 0
    assert calculated["input"]["tag_number"] == "LAP10001"
    assert calculated["depreciation_group"] == "SIX_YEAR"
    assert calculated["remaining_rate"] == 1.0
    assert calculated["fee_rate"] == 0.3
    assert calculated["total_amount"] == 1_560_000
    assert calculated["formula_explanation"]

    assert extension["case_service"].summary().total == before_cases
    assert tuple(extension["settings"].output_dir.iterdir()) == before_outputs


def test_preview_api_surfaces_review_instead_of_guessing(app: Flask) -> None:
    response = app.test_client().post(
        "/api/compensation/preview",
        json={
            "assets": [
                _request_asset(
                    tag_number="LAP10001",
                    lookup_status="AMBIGUOUS",
                )
            ]
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["review_required"] is True
    assert payload["status_counts"] == {"NEEDS_REVIEW": 1}
    assert payload["results"][0]["total_amount"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"assets": []},
        {"assets": ["not-an-object"]},
        {"assets": [_request_asset(physical="false")]},
        {"assets": [_request_asset(cost="499999.5")]},
    ],
)
def test_preview_api_rejects_invalid_payloads_as_json(app: Flask, body: object) -> None:
    response = app.test_client().post("/api/compensation/preview", json=body)

    assert response.status_code == 400
    assert response.is_json
    assert response.get_json()["ok"] is False


def test_preview_api_limits_batch_size(app: Flask) -> None:
    response = app.test_client().post(
        "/api/compensation/preview",
        json={"assets": [_request_asset()] * 101},
    )

    assert response.status_code == 400
    assert "more than 100" in response.get_json()["error"]
