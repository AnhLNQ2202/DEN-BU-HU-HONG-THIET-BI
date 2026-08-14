from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asset_compensation.domain import (  # noqa: E402
    AccountingBatch,
    Case,
    CaseNotFoundError,
    CaseStatus,
    CaseType,
    ConcurrencyError,
    RepositoryError,
)
from asset_compensation.repositories import SQLiteCaseRepository  # noqa: E402

NOW = datetime(2026, 8, 14, 3, 0, tzinfo=UTC)


def make_case(case_id: str, **overrides: object) -> Case:
    values: dict[str, object] = {
        "id": case_id,
        "case_type": CaseType.DAMAGED,
        "status": CaseStatus.NEW,
        "domain": "demo.user",
        "employee_name": "Demo User",
        "asset_code": "DEMO-001",
        "asset_name": "Demo asset",
        "received_at": NOW,
        "amount": 100_000,
        "warnings": (),
        "source_file": "demo.eml",
        "metadata": {"test": True},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Case(**values)  # type: ignore[arg-type]


class SQLiteCaseRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.repository = SQLiteCaseRepository(Path(self.tempdir.name) / "cases.sqlite3")

    def tearDown(self) -> None:
        self.repository.close()
        self.tempdir.cleanup()

    def test_upsert_get_filter_and_summary(self) -> None:
        damaged = make_case("DMG-1")
        lost = make_case(
            "LOST-1",
            case_type=CaseType.LOST,
            domain="another.user",
            asset_code="DEMO-002",
            amount=250_000,
            warnings=("Needs manual review",),
        )

        stored = self.repository.upsert_many([damaged, lost])

        self.assertEqual([case.id for case in stored], ["DMG-1", "LOST-1"])
        self.assertEqual(self.repository.get_case("DMG-1"), damaged)
        self.assertEqual(
            [case.id for case in self.repository.list_cases(case_type=CaseType.LOST)],
            ["LOST-1"],
        )
        self.assertEqual(
            [case.id for case in self.repository.list_cases(has_warnings=True)],
            ["LOST-1"],
        )
        summary = self.repository.summary()
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.total_amount, 350_000)
        self.assertEqual(summary.warning_count, 1)
        self.assertEqual(summary.by_type[CaseType.DAMAGED], 1)
        self.assertEqual(summary.by_type[CaseType.LOST], 1)

    def test_upsert_preserves_workflow_status_and_creation_event(self) -> None:
        original = self.repository.upsert_case(make_case("DMG-1"))
        transitioned = self.repository.update_status(
            original.id,
            CaseStatus.NEEDS_REVIEW,
            actor="reviewer",
            note="Review requested",
            changed_at=NOW,
            expected_status=CaseStatus.NEW,
        )
        refreshed_input = make_case(
            "DMG-1",
            amount=125_000,
            status=CaseStatus.NEW,
            updated_at=datetime(2026, 8, 14, 4, 0, tzinfo=UTC),
        )

        refreshed = self.repository.upsert_case(refreshed_input)
        history = self.repository.status_history(original.id)

        self.assertEqual(transitioned.status, CaseStatus.NEEDS_REVIEW)
        self.assertEqual(refreshed.status, CaseStatus.NEEDS_REVIEW)
        self.assertEqual(refreshed.amount, 125_000)
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0].from_status)
        self.assertEqual(history[1].from_status, CaseStatus.NEW)
        self.assertEqual(history[1].to_status, CaseStatus.NEEDS_REVIEW)

    def test_status_update_uses_optimistic_expected_status(self) -> None:
        self.repository.upsert_case(make_case("DMG-1"))

        with self.assertRaises(ConcurrencyError):
            self.repository.update_status(
                "DMG-1",
                CaseStatus.ACCOUNTED,
                actor="system",
                changed_at=NOW,
                expected_status=CaseStatus.READY_FOR_ACCOUNTING,
            )

        self.assertEqual(self.repository.require_case("DMG-1").status, CaseStatus.NEW)
        self.assertEqual(len(self.repository.status_history("DMG-1")), 1)

    def test_batch_creation_is_atomic_when_a_case_is_missing(self) -> None:
        self.repository.upsert_case(make_case("DMG-1"))
        bad_batch = AccountingBatch(
            id="BATCH-BAD",
            name="Bad batch",
            case_ids=("DMG-1", "MISSING"),
            created_at=NOW,
        )

        with self.assertRaises(RepositoryError):
            self.repository.create_batch(bad_batch)

        self.assertIsNone(self.repository.get_batch("BATCH-BAD"))
        self.assertEqual(self.repository.list_batches(), [])

    def test_replace_all_clears_cases_events_and_batches_atomically(self) -> None:
        self.repository.upsert_case(make_case("DMG-1"))
        self.repository.create_batch(
            AccountingBatch(
                id="BATCH-1",
                name="Demo batch",
                case_ids=("DMG-1",),
                created_at=NOW,
            )
        )

        replacement = make_case("LOST-2", case_type=CaseType.LOST, asset_code="DEMO-002")
        self.repository.replace_all([replacement])

        self.assertIsNone(self.repository.get_case("DMG-1"))
        self.assertEqual(self.repository.get_case("LOST-2"), replacement)
        self.assertEqual(self.repository.list_batches(), [])
        self.assertEqual(len(self.repository.status_history("LOST-2")), 1)

    def test_accounted_case_rejects_source_or_financial_mutation(self) -> None:
        original = self.repository.upsert_case(make_case("DMG-LOCKED"))
        self.repository.update_status(
            original.id,
            CaseStatus.READY_FOR_ACCOUNTING,
            actor="demo.reviewer",
            changed_at=NOW,
            expected_status=CaseStatus.NEW,
        )
        self.repository.create_accounting_batch(
            AccountingBatch(
                id="BATCH-LOCKED",
                name="Synthetic locked batch",
                case_ids=(original.id,),
                created_at=NOW,
            ),
            actor="demo.accounting",
            changed_at=NOW,
        )

        with self.assertRaises(ConcurrencyError):
            self.repository.upsert_case(make_case("DMG-LOCKED", amount=999_000))

        stored = self.repository.require_case(original.id)
        self.assertEqual(stored.status, CaseStatus.ACCOUNTED)
        self.assertEqual(stored.amount, 100_000)

    def test_accounting_finalization_rolls_back_every_change_on_failure(self) -> None:
        ready = self.repository.upsert_case(make_case("DMG-READY"))
        self.repository.update_status(
            ready.id,
            CaseStatus.READY_FOR_ACCOUNTING,
            actor="demo.reviewer",
            changed_at=NOW,
            expected_status=CaseStatus.NEW,
        )
        bad_batch = AccountingBatch(
            id="BATCH-ROLLBACK",
            name="Synthetic rollback batch",
            case_ids=(ready.id, "MISSING"),
            created_at=NOW,
        )

        with self.assertRaises(CaseNotFoundError):
            self.repository.create_accounting_batch(
                bad_batch,
                actor="demo.accounting",
                changed_at=NOW,
            )

        self.assertIsNone(self.repository.get_batch(bad_batch.id))
        self.assertEqual(
            self.repository.require_case(ready.id).status,
            CaseStatus.READY_FOR_ACCOUNTING,
        )
        self.assertEqual(len(self.repository.status_history(ready.id)), 2)


if __name__ == "__main__":
    unittest.main()
