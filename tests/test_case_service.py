from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asset_compensation.domain import (  # noqa: E402
    CaseStatus,
    CaseType,
    ConcurrencyError,
    InvalidStatusTransition,
    ParsedCase,
    ValidationError,
)
from asset_compensation.repositories import SQLiteCaseRepository  # noqa: E402
from asset_compensation.services import CaseService  # noqa: E402

BASE_TIME = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = BASE_TIME

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def parsed_case(**overrides: object) -> ParsedCase:
    values: dict[str, object] = {
        "case_type": CaseType.DAMAGED,
        "domain": "demo.user",
        "employee_name": "Demo User",
        "asset_code": "DEMO-LAP-001",
        "asset_name": "Demo laptop",
        "received_at": BASE_TIME,
        "amount": 100_000,
        "repair_status": "NOT_REPAIRED",
        "source_file": "demo-message.eml",
        "source_id": "message-demo-001",
        "metadata": {"parser": "test"},
    }
    values.update(overrides)
    return ParsedCase(**values)  # type: ignore[arg-type]


class CaseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.repository = SQLiteCaseRepository(Path(self.tempdir.name) / "cases.sqlite3")
        self.clock = Clock()
        self.service = CaseService(self.repository, clock=self.clock)

    def tearDown(self) -> None:
        self.repository.close()
        self.tempdir.cleanup()

    def test_ingest_is_idempotent_and_stable_when_amount_is_corrected(self) -> None:
        first = self.service.ingest_one(parsed_case(amount=100_000))
        corrected = self.service.ingest_one(parsed_case(amount=125_000))

        self.assertEqual(first.id, corrected.id)
        self.assertEqual(corrected.amount, 125_000)
        self.assertEqual(len(self.service.list_cases()), 1)
        self.assertEqual(len(self.service.status_history(first.id)), 1)

    def test_stable_id_uses_source_identity_not_mutable_display_fields(self) -> None:
        first = parsed_case(employee_name="Original Name", amount=100_000)
        corrected = parsed_case(employee_name="Corrected Name", amount=200_000)

        self.assertEqual(
            CaseService.stable_case_id(first),
            CaseService.stable_case_id(corrected),
        )

    def test_ingest_accepts_parser_contract_and_exact_decimal_amounts(self) -> None:
        parser_result = SimpleNamespace(
            case_type="LOST",
            domain="parser.demo",
            employee_name="Parser Demo",
            asset_code="DEMO-PARSER-001",
            asset_name="Parser asset",
            received_at=BASE_TIME,
            amount=Decimal("123000"),
            residual_value=Decimal("100000"),
            responsibility_fee=Decimal("23000"),
            repair_status=None,
            supplier_number="SUP-DEMO",
            supplier_site="OFFICE",
            supplier_name=None,
            warnings=("Synthetic warning",),
            source_file="parser-demo.eml",
            source_id="parser-message-001",
            metadata={"parser": "external-contract"},
        )

        case = self.service.ingest_one(parser_result)

        self.assertEqual(case.case_type, CaseType.LOST)
        self.assertEqual(case.amount, 123_000)
        self.assertEqual(case.residual_value, 100_000)
        self.assertEqual(case.responsibility_fee, 23_000)

    def test_ingest_rejects_fractional_accounting_units(self) -> None:
        parser_result = SimpleNamespace(
            case_type="DAMAGED",
            domain="parser.demo",
            asset_code="DEMO-PARSER-002",
            received_at=BASE_TIME,
            amount=Decimal("100.50"),
        )

        with self.assertRaises(ValidationError):
            self.service.ingest_one(parser_result)

    def test_valid_transition_chain_is_audited(self) -> None:
        case = self.service.ingest_one(parsed_case())

        for status in (
            CaseStatus.NEEDS_REVIEW,
            CaseStatus.READY_FOR_ACCOUNTING,
            CaseStatus.ACCOUNTED,
            CaseStatus.CLOSED,
        ):
            case = self.service.transition_status(case.id, status, actor="demo.reviewer")

        self.assertEqual(case.status, CaseStatus.CLOSED)
        history = self.service.status_history(case.id)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[-1].from_status, CaseStatus.ACCOUNTED)
        self.assertEqual(history[-1].to_status, CaseStatus.CLOSED)

    def test_invalid_transition_does_not_mutate_or_audit(self) -> None:
        case = self.service.ingest_one(parsed_case())

        with self.assertRaises(InvalidStatusTransition):
            self.service.transition_status(
                case.id,
                CaseStatus.ACCOUNTED,
                actor="demo.reviewer",
            )

        self.assertEqual(self.service.get_case(case.id).status, CaseStatus.NEW)
        self.assertEqual(len(self.service.status_history(case.id)), 1)

    def test_batch_requires_ready_cases(self) -> None:
        case = self.service.ingest_one(parsed_case())
        with self.assertRaises(ValidationError):
            self.service.create_batch("GN-DEMO", [case.id])

        self.service.transition_status(
            case.id,
            CaseStatus.READY_FOR_ACCOUNTING,
            actor="demo.reviewer",
        )
        batch = self.service.create_batch("GN-DEMO", [case.id])
        repeated = self.service.create_batch("GN-DEMO", [case.id])

        self.assertEqual(batch.case_ids, (case.id,))
        self.assertEqual(self.repository.get_batch(batch.id), batch)
        self.assertEqual(repeated.id, batch.id)

    def test_finalize_batch_is_atomic_and_locks_financial_values(self) -> None:
        case = self.service.ingest_one(parsed_case())
        self.service.transition_status(
            case.id,
            CaseStatus.READY_FOR_ACCOUNTING,
            actor="demo.reviewer",
        )

        batch = self.service.finalize_batch(
            "GN-SYNTHETIC",
            [case.id],
            actor="demo.accounting",
            metadata={"output_name": "synthetic.xlsx"},
        )

        accounted = self.service.get_case(case.id)
        self.assertEqual(accounted.status, CaseStatus.ACCOUNTED)
        self.assertEqual(self.repository.get_batch(batch.id), batch)
        self.assertEqual(self.service.status_history(case.id)[-1].to_status, CaseStatus.ACCOUNTED)
        with self.assertRaises(ConcurrencyError):
            self.service.ingest_one(parsed_case(amount=125_000))

    def test_summary_and_filters(self) -> None:
        self.service.ingest(
            [
                parsed_case(),
                parsed_case(
                    case_type=CaseType.LOST,
                    domain="other.user",
                    asset_code="DEMO-CAB-001",
                    source_id="message-demo-002",
                    amount=250_000,
                    warnings=("Manual review",),
                ),
            ]
        )

        summary = self.service.summary()
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.total_amount, 350_000)
        self.assertEqual(summary.warning_count, 1)
        self.assertEqual(
            len(self.service.list_cases(case_type=CaseType.LOST, has_warnings=True)),
            1,
        )

    def test_reset_demo_replaces_existing_data_with_synthetic_cases(self) -> None:
        self.service.ingest_one(parsed_case(domain="old.demo"))

        demo_cases = self.service.reset_demo()

        self.assertEqual(len(demo_cases), 2)
        self.assertEqual(self.service.summary().total, 2)
        self.assertTrue(all(case.metadata.get("demo") is True for case in demo_cases))
        self.assertNotIn("old.demo", {case.domain for case in self.service.list_cases()})


if __name__ == "__main__":
    unittest.main()
