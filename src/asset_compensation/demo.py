"""Synthetic hackathon data. No record in this module identifies a real person."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from asset_compensation.domain import CaseStatus, CaseType, ParsedCase
from asset_compensation.services import CaseService


def demo_cases(now: datetime | None = None) -> list[ParsedCase]:
    anchor = now or datetime.now(UTC)
    return [
        ParsedCase(
            case_type=CaseType.DAMAGED,
            domain="demo.an",
            employee_name="Nguyễn Minh An (Demo)",
            asset_code="DEMO-LAP-014",
            asset_name="Laptop 14-inch",
            received_at=anchor - timedelta(days=9),
            amount=180_000,
            repair_status="NOT_REPAIRED",
            supplier_number="D-1024",
            supplier_site="OFFICE",
            source_file="demo-laptop-damaged.eml",
            source_id="demo-message-001",
            metadata={"demo": True, "reference": "DEMO-REQ-1001"},
        ),
        ParsedCase(
            case_type=CaseType.LOST,
            domain="demo.binh",
            employee_name="Trần Gia Bình (Demo)",
            asset_code="DEMO-MOU-207",
            asset_name="Wireless mouse",
            received_at=anchor - timedelta(days=5),
            residual_value=228_000,
            responsibility_fee=14_000,
            amount=242_000,
            supplier_number="D-2048",
            supplier_site="OFFICE",
            source_file="demo-mouse-lost.eml",
            source_id="demo-message-002",
            metadata={
                "demo": True,
                "entity": "DEMO",
                "cost_center": "0603",
                "product_code": "000",
                "location": "01",
                "note": "Tool",
            },
        ),
        ParsedCase(
            case_type=CaseType.LOST,
            domain="demo.chi",
            employee_name="Lê Minh Chi (Demo)",
            asset_code="DEMO-CAB-031",
            asset_name="USB-C multiport adapter",
            received_at=anchor - timedelta(days=3),
            residual_value=132_000,
            responsibility_fee=66_000,
            amount=198_000,
            supplier_number="D-4096",
            supplier_site="OFFICE",
            warnings=("Location mismatch: email=01, asset register=66",),
            source_file="demo-adapter-lost.eml",
            source_id="demo-message-003",
            metadata={
                "demo": True,
                "entity": "DEMO",
                "cost_center": "1808",
                "product_code": "509",
                "location": "66",
                "note": "Tool",
            },
        ),
        ParsedCase(
            case_type=CaseType.DAMAGED,
            domain="demo.dung",
            employee_name="Phạm Hoàng Dũng (Demo)",
            asset_code="DEMO-LAP-118",
            asset_name="Business laptop",
            received_at=anchor - timedelta(days=1),
            amount=1_260_000,
            repair_status="REPAIRED",
            supplier_number="D-8192",
            supplier_site="OFFICE",
            source_file="demo-water-damage.eml",
            source_id="demo-message-004",
            metadata={"demo": True, "reference": "DEMO-REQ-1004"},
        ),
        ParsedCase(
            case_type=CaseType.DAMAGED,
            domain="demo.ha",
            employee_name="Vũ Thanh Hà (Demo)",
            asset_code="DEMO-PHO-009",
            asset_name="Company phone",
            received_at=anchor - timedelta(hours=8),
            amount=320_000,
            repair_status="UNKNOWN",
            warnings=("Supplier domain has multiple candidate records",),
            source_file="demo-phone-damaged.eml",
            source_id="demo-message-005",
            metadata={"demo": True},
        ),
    ]


def seed_demo(service: CaseService) -> list[object]:
    """Reset and distribute synthetic cases across realistic workflow states."""

    cases = service.reset_demo(demo_cases())
    by_asset = {case.asset_code: case for case in cases}

    for asset in ("DEMO-LAP-014", "DEMO-MOU-207", "DEMO-LAP-118"):
        service.transition_status(
            by_asset[asset].id,
            CaseStatus.READY_FOR_ACCOUNTING,
            actor="demo",
            note="Synthetic approval for the hackathon walkthrough",
        )

    for asset in ("DEMO-CAB-031", "DEMO-PHO-009"):
        service.transition_status(
            by_asset[asset].id,
            CaseStatus.NEEDS_REVIEW,
            actor="demo",
            note="Synthetic data-quality issue requires review",
        )

    return service.list_cases()
