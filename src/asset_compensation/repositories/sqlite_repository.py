"""Transactional SQLite persistence for compensation cases."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

from asset_compensation.domain import (
    AccountingBatch,
    Case,
    CaseNotFoundError,
    CaseStatus,
    CaseSummary,
    CaseType,
    ConcurrencyError,
    RepositoryError,
    StatusEvent,
    as_utc,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    case_type TEXT NOT NULL CHECK (case_type IN ('DAMAGED', 'LOST')),
    status TEXT NOT NULL CHECK (
        status IN ('NEW', 'NEEDS_REVIEW', 'READY_FOR_ACCOUNTING', 'ACCOUNTED', 'CLOSED')
    ),
    domain TEXT NOT NULL COLLATE NOCASE,
    employee_name TEXT,
    asset_code TEXT NOT NULL COLLATE NOCASE,
    asset_name TEXT,
    received_at TEXT NOT NULL,
    amount INTEGER CHECK (amount IS NULL OR amount >= 0),
    residual_value INTEGER CHECK (residual_value IS NULL OR residual_value >= 0),
    responsibility_fee INTEGER CHECK (responsibility_fee IS NULL OR responsibility_fee >= 0),
    repair_status TEXT,
    supplier_number TEXT,
    supplier_site TEXT,
    supplier_name TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    source_file TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_type ON cases(case_type);
CREATE INDEX IF NOT EXISTS idx_cases_domain ON cases(domain);
CREATE INDEX IF NOT EXISTS idx_cases_received_at ON cases(received_at DESC);

CREATE TABLE IF NOT EXISTS status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    from_status TEXT CHECK (
        from_status IS NULL OR
        from_status IN ('NEW', 'NEEDS_REVIEW', 'READY_FOR_ACCOUNTING', 'ACCOUNTED', 'CLOSED')
    ),
    to_status TEXT NOT NULL CHECK (
        to_status IN ('NEW', 'NEEDS_REVIEW', 'READY_FOR_ACCOUNTING', 'ACCOUNTED', 'CLOSED')
    ),
    changed_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_status_events_case
    ON status_events(case_id, changed_at, id);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (batch_id, case_id),
    UNIQUE (batch_id, position)
);

CREATE INDEX IF NOT EXISTS idx_batch_items_case ON batch_items(case_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_items_case ON batch_items(case_id);
"""


def _timestamp(value: datetime | str) -> str:
    return as_utc(value).isoformat(timespec="microseconds")


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        # Invalid historical JSON should not make the whole repository unreadable.
        return default


class SQLiteCaseRepository:
    """SQLite repository with one serialized connection per repository instance."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteCaseRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def _upsert(self, cursor: sqlite3.Cursor, case: Case) -> Case:
        previous = cursor.execute(
            "SELECT * FROM cases WHERE id = ?", (case.id,)
        ).fetchone()

        if previous is None:
            stored = case
            cursor.execute(
                """
                INSERT INTO cases (
                    id, case_type, status, domain, employee_name, asset_code, asset_name,
                    received_at, amount, residual_value, responsibility_fee, repair_status,
                    supplier_number, supplier_site, supplier_name, warnings_json, source_file,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._case_values(stored),
            )
            cursor.execute(
                """
                INSERT INTO status_events
                    (case_id, from_status, to_status, changed_at, actor, note)
                VALUES (?, NULL, ?, ?, 'system', 'case created')
                """,
                (stored.id, stored.status.value, _timestamp(stored.created_at)),
            )
            return stored

        previous_case = self._row_to_case(previous)
        if previous_case.status in {CaseStatus.ACCOUNTED, CaseStatus.CLOSED}:
            frozen_candidate = replace(
                case,
                status=previous_case.status,
                created_at=previous_case.created_at,
                updated_at=previous_case.updated_at,
            )
            if frozen_candidate != previous_case:
                raise ConcurrencyError(
                    f"Case {case.id!r} is {previous_case.status.value}; "
                    "financial and source fields are immutable after accounting"
                )
            return previous_case

        # Ingest refreshes parser-owned fields but cannot roll workflow state backwards.
        stored = replace(
            case,
            status=previous_case.status,
            created_at=previous_case.created_at,
        )
        cursor.execute(
            """
            UPDATE cases SET
                case_type = ?, status = ?, domain = ?, employee_name = ?, asset_code = ?,
                asset_name = ?, received_at = ?, amount = ?, residual_value = ?,
                responsibility_fee = ?, repair_status = ?, supplier_number = ?,
                supplier_site = ?, supplier_name = ?, warnings_json = ?, source_file = ?,
                metadata_json = ?, created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            self._case_values(stored)[1:] + (stored.id,),
        )
        return stored

    @staticmethod
    def _case_values(case: Case) -> tuple[Any, ...]:
        return (
            case.id,
            case.case_type.value,
            case.status.value,
            case.domain,
            case.employee_name,
            case.asset_code,
            case.asset_name,
            _timestamp(case.received_at),
            case.amount,
            case.residual_value,
            case.responsibility_fee,
            case.repair_status,
            case.supplier_number,
            case.supplier_site,
            case.supplier_name,
            _dump_json(list(case.warnings)),
            case.source_file,
            _dump_json(dict(case.metadata)),
            _timestamp(case.created_at),
            _timestamp(case.updated_at),
        )

    def upsert_case(self, case: Case) -> Case:
        return self.upsert_many([case])[0]

    def upsert_many(self, cases: Sequence[Case]) -> list[Case]:
        if not cases:
            return []
        with self._transaction() as cursor:
            return [self._upsert(cursor, case) for case in cases]

    def replace_all(self, cases: Sequence[Case]) -> list[Case]:
        """Atomically replace cases, events, and batches with the supplied cases."""

        with self._transaction() as cursor:
            cursor.execute("DELETE FROM batch_items")
            cursor.execute("DELETE FROM batches")
            cursor.execute("DELETE FROM status_events")
            cursor.execute("DELETE FROM cases")
            return [self._upsert(cursor, case) for case in cases]

    def delete_all(self) -> None:
        self.replace_all([])

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def require_case(self, case_id: str) -> Case:
        case = self.get_case(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case {case_id!r} was not found")
        return case

    def list_cases(
        self,
        *,
        case_type: CaseType | str | None = None,
        status: CaseStatus | str | None = None,
        domain: str | None = None,
        has_warnings: bool | None = None,
        source_file: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Case]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_type is not None:
            clauses.append("case_type = ?")
            params.append(
                case_type.value
                if isinstance(case_type, CaseType)
                else CaseType(str(case_type).upper()).value
            )
        if status is not None:
            clauses.append("status = ?")
            params.append(
                status.value
                if isinstance(status, CaseStatus)
                else CaseStatus(str(status).upper()).value
            )
        if domain is not None:
            clauses.append("domain = ? COLLATE NOCASE")
            params.append(domain.strip())
        if has_warnings is not None:
            clauses.append("warnings_json <> '[]'" if has_warnings else "warnings_json = '[]'")
        if source_file is not None:
            clauses.append("source_file = ?")
            params.append(source_file)

        sql = "SELECT * FROM cases"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at DESC, id ASC"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit cannot be negative")
            sql += " LIMIT ?"
            params.append(limit)
        elif offset:
            sql += " LIMIT -1"
        if offset:
            if offset < 0:
                raise ValueError("offset cannot be negative")
            sql += " OFFSET ?"
            params.append(offset)

        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._row_to_case(row) for row in rows]

    def update_status(
        self,
        case_id: str,
        new_status: CaseStatus | str,
        *,
        actor: str,
        note: str | None = None,
        changed_at: datetime | str | None = None,
        expected_status: CaseStatus | str | None = None,
    ) -> Case:
        target = (
            new_status
            if isinstance(new_status, CaseStatus)
            else CaseStatus(str(new_status).upper())
        )
        expected = (
            expected_status
            if isinstance(expected_status, CaseStatus)
            else CaseStatus(str(expected_status).upper())
            if expected_status is not None
            else None
        )
        timestamp = as_utc(changed_at or utc_now())
        actor = actor.strip()
        if not actor:
            raise ValueError("actor is required")

        with self._transaction() as cursor:
            row = cursor.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
            if row is None:
                raise CaseNotFoundError(f"Case {case_id!r} was not found")
            current = CaseStatus(row["status"])
            if expected is not None and current is not expected:
                raise ConcurrencyError(
                    f"Case {case_id!r} is {current.value}, expected {expected.value}"
                )
            if current is target:
                unchanged = cursor.execute(
                    "SELECT * FROM cases WHERE id = ?", (case_id,)
                ).fetchone()
                return self._row_to_case(unchanged)

            cursor.execute(
                "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, _timestamp(timestamp), case_id),
            )
            cursor.execute(
                """
                INSERT INTO status_events
                    (case_id, from_status, to_status, changed_at, actor, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (case_id, current.value, target.value, _timestamp(timestamp), actor, note),
            )
            updated = cursor.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            return self._row_to_case(updated)

    def status_history(self, case_id: str) -> list[StatusEvent]:
        self.require_case(case_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM status_events WHERE case_id = ? ORDER BY changed_at, id",
                (case_id,),
            ).fetchall()
        return [
            StatusEvent(
                id=row["id"],
                case_id=row["case_id"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                changed_at=row["changed_at"],
                actor=row["actor"],
                note=row["note"],
            )
            for row in rows
        ]

    def summary(self) -> CaseSummary:
        with self._lock:
            aggregate = self._connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(amount), 0) AS total_amount,
                       COALESCE(SUM(CASE WHEN warnings_json <> '[]' THEN 1 ELSE 0 END), 0)
                           AS warning_count
                FROM cases
                """
            ).fetchone()
            type_rows = self._connection.execute(
                "SELECT case_type, COUNT(*) AS count FROM cases GROUP BY case_type"
            ).fetchall()
            status_rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM cases GROUP BY status"
            ).fetchall()
        by_type = {item: 0 for item in CaseType}
        by_type.update({CaseType(row["case_type"]): row["count"] for row in type_rows})
        by_status = {item: 0 for item in CaseStatus}
        by_status.update({CaseStatus(row["status"]): row["count"] for row in status_rows})
        return CaseSummary(
            total=aggregate["total"],
            total_amount=aggregate["total_amount"],
            warning_count=aggregate["warning_count"],
            by_type=by_type,
            by_status=by_status,
        )

    def create_batch(self, batch: AccountingBatch) -> AccountingBatch:
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    "INSERT INTO batches (id, name, created_at, metadata_json) VALUES (?, ?, ?, ?)",
                    (
                        batch.id,
                        batch.name,
                        _timestamp(batch.created_at),
                        _dump_json(dict(batch.metadata)),
                    ),
                )
                cursor.executemany(
                    "INSERT INTO batch_items (batch_id, case_id, position) VALUES (?, ?, ?)",
                    [
                        (batch.id, case_id, position)
                        for position, case_id in enumerate(batch.case_ids)
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"Could not create batch {batch.name!r}: {exc}") from exc
        return batch

    def create_accounting_batch(
        self,
        batch: AccountingBatch,
        *,
        actor: str,
        note: str | None = None,
        changed_at: datetime | str | None = None,
    ) -> AccountingBatch:
        """Persist a batch and mark every case accounted in one transaction."""

        actor = actor.strip()
        if not actor:
            raise ValueError("actor is required")
        timestamp = as_utc(changed_at or utc_now())
        try:
            with self._transaction() as cursor:
                cursor.execute(
                    "INSERT INTO batches (id, name, created_at, metadata_json) VALUES (?, ?, ?, ?)",
                    (
                        batch.id,
                        batch.name,
                        _timestamp(batch.created_at),
                        _dump_json(dict(batch.metadata)),
                    ),
                )
                for position, case_id in enumerate(batch.case_ids):
                    row = cursor.execute(
                        "SELECT status FROM cases WHERE id = ?",
                        (case_id,),
                    ).fetchone()
                    if row is None:
                        raise CaseNotFoundError(f"Case {case_id!r} was not found")
                    current = CaseStatus(row["status"])
                    if current is not CaseStatus.READY_FOR_ACCOUNTING:
                        raise ConcurrencyError(
                            f"Case {case_id!r} is {current.value}, expected READY_FOR_ACCOUNTING"
                        )
                    cursor.execute(
                        "INSERT INTO batch_items (batch_id, case_id, position) VALUES (?, ?, ?)",
                        (batch.id, case_id, position),
                    )
                    cursor.execute(
                        "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                        (CaseStatus.ACCOUNTED.value, _timestamp(timestamp), case_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO status_events
                            (case_id, from_status, to_status, changed_at, actor, note)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            case_id,
                            CaseStatus.READY_FOR_ACCOUNTING.value,
                            CaseStatus.ACCOUNTED.value,
                            _timestamp(timestamp),
                            actor,
                            note,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"Could not finalize batch {batch.name!r}: {exc}") from exc
        return batch

    def get_batch(self, batch_id: str) -> AccountingBatch | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            item_rows = self._connection.execute(
                "SELECT case_id FROM batch_items WHERE batch_id = ? ORDER BY position",
                (batch_id,),
            ).fetchall()
        return AccountingBatch(
            id=row["id"],
            name=row["name"],
            case_ids=tuple(item["case_id"] for item in item_rows),
            created_at=row["created_at"],
            metadata=_load_json(row["metadata_json"], {}),
        )

    def list_batches(self) -> list[AccountingBatch]:
        with self._lock:
            ids = [
                row["id"]
                for row in self._connection.execute(
                    "SELECT id FROM batches ORDER BY created_at DESC, id"
                ).fetchall()
            ]
        return [batch for batch_id in ids if (batch := self.get_batch(batch_id)) is not None]

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Case:
        return Case(
            id=row["id"],
            case_type=row["case_type"],
            status=row["status"],
            domain=row["domain"],
            employee_name=row["employee_name"],
            asset_code=row["asset_code"],
            asset_name=row["asset_name"],
            received_at=row["received_at"],
            amount=row["amount"],
            residual_value=row["residual_value"],
            responsibility_fee=row["responsibility_fee"],
            repair_status=row["repair_status"],
            supplier_number=row["supplier_number"],
            supplier_site=row["supplier_site"],
            supplier_name=row["supplier_name"],
            warnings=tuple(_load_json(row["warnings_json"], [])),
            source_file=row["source_file"],
            metadata=_load_json(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
