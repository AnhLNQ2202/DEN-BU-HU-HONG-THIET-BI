"""Narrow, opt-in cleanup of application-owned staging data."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from threading import RLock
from typing import Protocol

from asset_compensation.config import Settings
from asset_compensation.domain import AccountingBatch, Case, StatusEvent, ValidationError

from .supplier_upload_service import SupplierUploadService


class TestDataRepository(Protocol):
    """Repository operations required by staging cleanup."""

    def list_batches(self) -> list[AccountingBatch]: ...

    def delete_all(self) -> None: ...


class TestDataCaseService(Protocol):
    """Read operations used to report exact cleanup counts."""

    def list_cases(self, **filters: object) -> list[Case]: ...

    def status_history(self, case_id: str) -> list[StatusEvent]: ...


class TestDataService:
    """Clear only records and generated artifacts owned by this application."""

    def __init__(
        self,
        settings: Settings,
        repository: TestDataRepository,
        case_service: TestDataCaseService,
        supplier_service: SupplierUploadService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.case_service = case_service
        self.supplier_service = supplier_service
        self._lock = RLock()

    def clear(self) -> dict[str, int]:
        """Remove staged cases, batches, their named outputs and uploaded Suppliers."""

        with self._lock:
            cases = self.case_service.list_cases()
            batches = self.repository.list_batches()
            event_count = sum(
                len(self.case_service.status_history(case.id)) for case in cases
            )
            output_paths = self._validated_output_paths(batches)

            # Validate the Supplier pointer before making any changes. The actual
            # deletion revalidates its private paths while holding the service lock.
            self.supplier_service.status()

            output_count = 0
            for path in output_paths:
                if path.exists() or path.is_symlink():
                    try:
                        path.unlink()
                    except OSError as exc:
                        raise ValidationError(
                            "Could not remove a generated accounting output"
                        ) from exc
                    output_count += 1

            supplier_counts = self.supplier_service.clear()
            self.repository.delete_all()
            return {
                "case_count": len(cases),
                "event_count": event_count,
                "batch_count": len(batches),
                "output_file_count": output_count,
                **supplier_counts,
            }

    def _validated_output_paths(
        self, batches: Iterable[AccountingBatch]
    ) -> tuple[Path, ...]:
        output_root = self.settings.output_dir.resolve()
        targets: dict[Path, None] = {}
        for batch in batches:
            output_name = batch.metadata.get("output_name")
            if output_name is None:
                continue
            if not isinstance(output_name, str) or not self._safe_basename(output_name):
                raise ValidationError("A batch contains an unsafe output name")
            candidate = self.settings.output_dir / output_name
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValidationError("A batch contains an unsafe output path") from exc
            if resolved.parent != output_root:
                raise ValidationError("A batch contains an unsafe output path")
            if candidate.exists() and not candidate.is_file():
                raise ValidationError("A batch output path is not a file")
            targets[candidate] = None
        return tuple(targets)

    @staticmethod
    def _safe_basename(value: str) -> bool:
        return bool(
            value
            and value not in {".", ".."}
            and len(value) <= 255
            and "/" not in value
            and "\\" not in value
            and Path(value).name == value
            and not any(ord(character) < 32 for character in value)
        )
