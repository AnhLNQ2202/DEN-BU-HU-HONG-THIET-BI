"""Application service for case ingestion and workflow operations."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from asset_compensation.domain import (
    AccountingBatch,
    Case,
    CaseNotFoundError,
    CaseStatus,
    CaseSummary,
    CaseType,
    InvalidStatusTransition,
    ParsedCase,
    StatusEvent,
    ValidationError,
    utc_now,
)


class CaseRepository(Protocol):
    def upsert_many(self, cases: list[Case]) -> list[Case]: ...

    def replace_all(self, cases: list[Case]) -> list[Case]: ...

    def get_case(self, case_id: str) -> Case | None: ...

    def list_cases(self, **filters: object) -> list[Case]: ...

    def update_status(
        self,
        case_id: str,
        new_status: CaseStatus,
        *,
        actor: str,
        note: str | None,
        changed_at: datetime,
        expected_status: CaseStatus,
    ) -> Case: ...

    def status_history(self, case_id: str) -> list[StatusEvent]: ...

    def summary(self) -> CaseSummary: ...

    def create_batch(self, batch: AccountingBatch) -> AccountingBatch: ...

    def create_accounting_batch(
        self,
        batch: AccountingBatch,
        *,
        actor: str,
        note: str | None,
        changed_at: datetime,
    ) -> AccountingBatch: ...

    def get_batch(self, batch_id: str) -> AccountingBatch | None: ...


ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.NEW: frozenset({CaseStatus.NEEDS_REVIEW, CaseStatus.READY_FOR_ACCOUNTING}),
    CaseStatus.NEEDS_REVIEW: frozenset({CaseStatus.READY_FOR_ACCOUNTING}),
    CaseStatus.READY_FOR_ACCOUNTING: frozenset({CaseStatus.NEEDS_REVIEW, CaseStatus.ACCOUNTED}),
    CaseStatus.ACCOUNTED: frozenset({CaseStatus.CLOSED}),
    CaseStatus.CLOSED: frozenset(),
}


def _identity_part(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized.strip()).casefold()


def _integer_amount(value: object, field_name: str) -> int | None:
    """Convert parser Decimal values to integer accounting units without rounding."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a numeric amount")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a numeric amount") from exc
    if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
        raise ValidationError(f"{field_name} must use whole accounting units")
    result = int(decimal_value)
    if result < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    return result


def _attribute(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _coerce_parsed_case(value: object) -> ParsedCase:
    """Accept the domain DTO or a parser contract exposing the same attributes."""

    if isinstance(value, ParsedCase):
        return value
    required = ("case_type", "domain", "asset_code", "received_at")
    missing = [name for name in required if _attribute(value, name) in (None, "")]
    if missing:
        raise ValidationError("Parsed case is missing: " + ", ".join(missing))
    return ParsedCase(
        case_type=_attribute(value, "case_type"),
        domain=_attribute(value, "domain"),
        asset_code=_attribute(value, "asset_code"),
        received_at=_attribute(value, "received_at"),
        employee_name=_attribute(value, "employee_name"),
        asset_name=_attribute(value, "asset_name"),
        amount=_integer_amount(_attribute(value, "amount"), "amount"),
        residual_value=_integer_amount(_attribute(value, "residual_value"), "residual_value"),
        responsibility_fee=_integer_amount(
            _attribute(value, "responsibility_fee"), "responsibility_fee"
        ),
        repair_status=_attribute(value, "repair_status"),
        supplier_number=_attribute(value, "supplier_number"),
        supplier_site=_attribute(value, "supplier_site"),
        supplier_name=_attribute(value, "supplier_name"),
        warnings=_attribute(value, "warnings", ()) or (),
        source_file=_attribute(value, "source_file"),
        source_id=_attribute(value, "source_id"),
        metadata=_attribute(value, "metadata", {}) or {},
    )


class CaseService:
    def __init__(
        self,
        repository: CaseRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.clock = clock

    @staticmethod
    def stable_case_id(parsed: ParsedCase) -> str:
        """Build a deterministic ID without including mutable financial fields."""

        metadata = dict(parsed.metadata)
        source_identity = (
            parsed.source_id
            or metadata.get("message_id")
            or metadata.get("internet_message_id")
            or metadata.get("source_id")
            or parsed.source_file
            or parsed.received_at.isoformat()
        )
        payload = "|".join(
            (
                parsed.case_type.value,
                _identity_part(parsed.domain),
                _identity_part(parsed.asset_code),
                _identity_part(source_identity),
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()
        prefix = "DMG" if parsed.case_type is CaseType.DAMAGED else "LOST"
        return f"{prefix}-{parsed.received_at:%Y%m}-{digest}"

    def _materialize(self, parsed: ParsedCase, timestamp: datetime) -> Case:
        return Case(
            id=self.stable_case_id(parsed),
            case_type=parsed.case_type,
            status=CaseStatus.NEW,
            domain=parsed.domain,
            employee_name=parsed.employee_name,
            asset_code=parsed.asset_code,
            asset_name=parsed.asset_name,
            received_at=parsed.received_at,
            amount=parsed.amount,
            residual_value=parsed.residual_value,
            responsibility_fee=parsed.responsibility_fee,
            repair_status=parsed.repair_status,
            supplier_number=parsed.supplier_number,
            supplier_site=parsed.supplier_site,
            supplier_name=parsed.supplier_name,
            warnings=parsed.warnings,
            source_file=parsed.source_file,
            metadata=parsed.metadata,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def ingest(self, parsed_cases: Iterable[object]) -> list[Case]:
        """Idempotently persist parser results in one SQLite transaction."""

        timestamp = self.clock()
        unique: dict[str, Case] = {}
        for value in parsed_cases:
            parsed = _coerce_parsed_case(value)
            case = self._materialize(parsed, timestamp)
            unique[case.id] = case
        return self.repository.upsert_many(list(unique.values()))

    def ingest_one(self, parsed_case: object) -> Case:
        return self.ingest([parsed_case])[0]

    def get_case(self, case_id: str) -> Case:
        case = self.repository.get_case(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case {case_id!r} was not found")
        return case

    def detail(self, case_id: str) -> Case:
        """Return one case, raising when the identifier is unknown."""

        return self.get_case(case_id)

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
        return self.repository.list_cases(
            case_type=case_type,
            status=status,
            domain=domain,
            has_warnings=has_warnings,
            source_file=source_file,
            limit=limit,
            offset=offset,
        )

    def transition_status(
        self,
        case_id: str,
        new_status: CaseStatus | str,
        *,
        actor: str,
        note: str | None = None,
    ) -> Case:
        current = self.get_case(case_id)
        try:
            target = (
                new_status
                if isinstance(new_status, CaseStatus)
                else CaseStatus(str(new_status).upper())
            )
        except ValueError as exc:
            raise InvalidStatusTransition(f"Unknown target status: {new_status!r}") from exc
        actor = actor.strip()
        if not actor:
            raise ValidationError("actor is required for a status transition")
        if target is current.status:
            return current
        if target not in ALLOWED_TRANSITIONS[current.status]:
            raise InvalidStatusTransition(
                f"Cannot move {case_id} from {current.status.value} to {target.value}"
            )
        return self.repository.update_status(
            case_id,
            target,
            actor=actor,
            note=note,
            changed_at=self.clock(),
            expected_status=current.status,
        )

    def update_status(
        self,
        case_id: str,
        new_status: CaseStatus | str,
        *,
        actor: str,
        note: str | None = None,
    ) -> Case:
        """Compatibility alias for the validated workflow operation."""

        return self.transition_status(case_id, new_status, actor=actor, note=note)

    def status_history(self, case_id: str) -> list[StatusEvent]:
        return self.repository.status_history(case_id)

    def summary(self) -> CaseSummary:
        return self.repository.summary()

    def create_batch(
        self,
        name: str,
        case_ids: Iterable[str],
        *,
        metadata: dict[str, object] | None = None,
    ) -> AccountingBatch:
        batch, exists = self._prepare_batch(name, case_ids, metadata=metadata)
        return batch if exists else self.repository.create_batch(batch)

    def _prepare_batch(
        self,
        name: str,
        case_ids: Iterable[str],
        *,
        metadata: dict[str, object] | None = None,
    ) -> tuple[AccountingBatch, bool]:
        requested_ids = tuple(case_ids)
        if not requested_ids:
            raise ValidationError("At least one case is required")
        unique_ids = tuple(dict.fromkeys(requested_ids))
        if len(unique_ids) != len(requested_ids):
            raise ValidationError("A case may appear only once in an accounting batch")
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValidationError("batch name is required")
        digest_input = cleaned_name.casefold() + "|" + "|".join(sorted(unique_ids))
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16].upper()
        batch = AccountingBatch(
            id=f"BATCH-{digest}",
            name=cleaned_name,
            case_ids=unique_ids,
            created_at=self.clock(),
            metadata=metadata or {},
        )
        existing = self.repository.get_batch(batch.id)
        if existing is not None:
            return existing, True
        cases = [self.get_case(case_id) for case_id in unique_ids]
        not_ready = [
            case.id for case in cases if case.status is not CaseStatus.READY_FOR_ACCOUNTING
        ]
        if not_ready:
            raise ValidationError(
                "Only READY_FOR_ACCOUNTING cases can enter a batch: " + ", ".join(not_ready)
            )
        return batch, False

    def finalize_batch(
        self,
        name: str,
        case_ids: Iterable[str],
        *,
        actor: str,
        metadata: dict[str, object] | None = None,
    ) -> AccountingBatch:
        """Atomically persist an accounting batch and lock its cases as accounted."""

        actor = actor.strip()
        if not actor:
            raise ValidationError("actor is required for batch finalization")
        batch, exists = self._prepare_batch(name, case_ids, metadata=metadata)
        if exists:
            return batch
        return self.repository.create_accounting_batch(
            batch,
            actor=actor,
            note=f"Exported in batch {batch.name}",
            changed_at=self.clock(),
        )

    def reset_demo(self, parsed_cases: Iterable[object] | None = None) -> list[Case]:
        """Atomically reset storage to deterministic, synthetic demo records."""

        demo = list(parsed_cases) if parsed_cases is not None else self._default_demo_cases()
        timestamp = self.clock()
        unique: dict[str, Case] = {}
        for value in demo:
            parsed = _coerce_parsed_case(value)
            case = self._materialize(parsed, timestamp)
            unique[case.id] = case
        return self.repository.replace_all(list(unique.values()))

    def clear(self) -> None:
        self.repository.replace_all([])

    def _default_demo_cases(self) -> list[ParsedCase]:
        now = self.clock()
        return [
            ParsedCase(
                case_type=CaseType.DAMAGED,
                domain="demo.damaged",
                employee_name="Demo Employee A",
                asset_code="DEMO-LAP-001",
                asset_name="Demo laptop",
                received_at=now,
                amount=120_000,
                repair_status="NOT_REPAIRED",
                source_file="demo-damaged.eml",
                source_id="demo-damaged-001",
                metadata={"demo": True},
            ),
            ParsedCase(
                case_type=CaseType.LOST,
                domain="demo.lost",
                employee_name="Demo Employee B",
                asset_code="DEMO-CAB-001",
                asset_name="Demo accessory",
                received_at=now,
                residual_value=80_000,
                responsibility_fee=20_000,
                amount=100_000,
                warnings=("Demo warning",),
                source_file="demo-lost.eml",
                source_id="demo-lost-001",
                metadata={"demo": True},
            ),
        ]
