"""Application orchestration for individual and merged mail-evidence PDFs."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from asset_compensation.adapters.pdf import (
    EmlPdfConverter,
    PdfMerger,
    PdfNormalizationResult,
    PdfOverflowPolicy,
    PdfPageNormalizer,
    PypdfMerger,
    PypdfPageNormalizer,
)

from .mail_artifact_service import MailArtifactHandle, MailArtifactStore, safe_eml_basename


@dataclass(frozen=True, slots=True)
class MailPdfItemResult:
    """One normalized PDF in a published evidence batch."""

    artifact_handle: str
    source_filename: str
    path: Path
    source_pages: int
    output_pages: int
    padded_pages: int
    truncated_pages: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MailPdfBatchResult:
    """Atomically published individual PDFs and their merged document."""

    directory: Path
    merged_path: Path
    items: tuple[MailPdfItemResult, ...]
    warnings: tuple[str, ...] = ()


class MailPdfService:
    """Convert retained EML artifacts without opening Word or mail windows."""

    def __init__(
        self,
        artifact_store: MailArtifactStore,
        converter: EmlPdfConverter,
        *,
        normalizer: PdfPageNormalizer | None = None,
        merger: PdfMerger | None = None,
        max_batch_items: int = 20,
    ) -> None:
        if max_batch_items < 1:
            raise ValueError("max_batch_items must be positive")
        self._artifact_store = artifact_store
        self._converter = converter
        self._normalizer = normalizer or PypdfPageNormalizer()
        self._merger = merger or PypdfMerger()
        self._max_batch_items = max_batch_items

    def create_individual(
        self,
        artifact: str | MailArtifactHandle,
        destination: str | Path,
    ) -> Path:
        """Create a full-length PDF for one retained EML without overwriting."""

        handle, _ = self._artifact_reference(artifact)
        source = self._artifact_store.resolve(handle)
        return self._converter.convert_eml(source, destination, overwrite=False)

    def create_batch(
        self,
        artifacts: list[str | MailArtifactHandle] | tuple[str | MailArtifactHandle, ...],
        destination_root: str | Path,
        *,
        batch_name: str,
        pages_per_mail: int = 2,
        overflow_policy: PdfOverflowPolicy = "fail",
    ) -> MailPdfBatchResult:
        """Publish fixed-page individual PDFs plus one merged PDF as a batch.

        Work is completed in a private sibling staging directory. The final
        directory is made visible only after every conversion and merge
        succeeds. Existing batch names are never overwritten.
        """

        if not artifacts:
            raise ValueError("At least one mail artifact is required")
        if len(artifacts) > self._max_batch_items:
            raise ValueError(
                f"A PDF batch cannot contain more than {self._max_batch_items} emails"
            )
        if pages_per_mail < 1:
            raise ValueError("pages_per_mail must be at least 1")
        if overflow_policy not in {"fail", "warn"}:
            raise ValueError("overflow_policy must be 'fail' or 'warn'")
        references = [self._artifact_reference(artifact) for artifact in artifacts]
        handles = [handle for handle, _ in references]
        if len(set(handles)) != len(handles):
            raise ValueError("A PDF batch cannot contain a duplicate mail artifact")

        safe_batch = self._safe_batch_name(batch_name)
        root = Path(destination_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        final_directory = root / safe_batch
        reservation_key = sha256(safe_batch.encode("utf-8")).hexdigest()[:16]
        reservation = root / f".mp-{reservation_key}.lock"
        try:
            reservation_handle = reservation.open("xb")
        except FileExistsError as exc:
            raise FileExistsError(f"PDF batch is already being created: {safe_batch}") from exc

        staging: Path | None = None
        try:
            with reservation_handle:
                reservation_handle.write(str(os.getpid()).encode("ascii"))
                reservation_handle.flush()
                os.fsync(reservation_handle.fileno())
            if final_directory.exists():
                raise FileExistsError(f"PDF batch already exists: {final_directory}")
            staging = Path(
                tempfile.mkdtemp(prefix=".mp-", suffix=".tmp", dir=root)
            )
            raw_directory = staging / ".raw"
            individual_directory = staging / "individual"
            raw_directory.mkdir()
            individual_directory.mkdir()

            staged_items: list[tuple[str, str, PdfNormalizationResult]] = []
            normalized_paths: list[Path] = []
            all_warnings: list[str] = []
            for index, (handle, source_filename) in enumerate(references, start=1):
                source = self._artifact_store.resolve(handle)
                raw_pdf = raw_directory / f"{index:02d}.pdf"
                self._converter.convert_eml(source, raw_pdf, overwrite=False)
                output_name = self._individual_pdf_name(index, source_filename)
                normalized_path = individual_directory / output_name
                normalized = self._normalizer.normalize(
                    raw_pdf,
                    normalized_path,
                    target_pages=pages_per_mail,
                    overflow_policy=overflow_policy,
                    overwrite=False,
                )
                normalized_paths.append(normalized_path)
                staged_items.append((handle, source_filename, normalized))
                all_warnings.extend(
                    f"email {index}: {warning}" for warning in normalized.warnings
                )

            merged_name = f"chungtu_{safe_batch}.pdf"
            staged_merged = staging / merged_name
            self._merger.merge(normalized_paths, staged_merged, overwrite=False)
            self._remove_staging_subdirectory(raw_directory, staging)

            if final_directory.exists():
                raise FileExistsError(f"PDF batch already exists: {final_directory}")
            staging.rename(final_directory)
            staging = None

            published_items = tuple(
                MailPdfItemResult(
                    artifact_handle=handle,
                    source_filename=source_filename,
                    path=final_directory / "individual" / normalized.path.name,
                    source_pages=normalized.source_pages,
                    output_pages=normalized.output_pages,
                    padded_pages=normalized.padded_pages,
                    truncated_pages=normalized.truncated_pages,
                    warnings=normalized.warnings,
                )
                for handle, source_filename, normalized in staged_items
            )
            return MailPdfBatchResult(
                directory=final_directory,
                merged_path=final_directory / merged_name,
                items=published_items,
                warnings=tuple(all_warnings),
            )
        finally:
            if staging is not None:
                self._remove_staging_subdirectory(staging, root)
            reservation.unlink(missing_ok=True)

    @staticmethod
    def _artifact_reference(
        artifact: str | MailArtifactHandle,
    ) -> tuple[str, str]:
        if isinstance(artifact, MailArtifactHandle):
            return artifact.handle, safe_eml_basename(artifact.safe_filename)
        handle = str(artifact)
        digest = handle.removeprefix("eml-sha256-")
        return handle, f"mail-{digest[:12]}.eml"

    @staticmethod
    def _safe_batch_name(batch_name: str) -> str:
        raw = str(batch_name or "").strip()
        safe = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in raw
        )
        safe = re.sub(r"_+", "_", safe).strip(" ._")[:80].rstrip(" ._")
        if not safe or safe in {".", ".."}:
            raise ValueError("batch_name must contain a safe visible character")
        return Path(safe_eml_basename(f"{safe}.eml")).stem

    @staticmethod
    def _individual_pdf_name(index: int, source_filename: str) -> str:
        safe_source = safe_eml_basename(source_filename)
        return f"{index:02d}_{Path(safe_source).stem}.pdf"

    @staticmethod
    def _remove_staging_subdirectory(target: Path, allowed_parent: Path) -> None:
        resolved_target = target.resolve()
        resolved_parent = allowed_parent.resolve()
        if resolved_target == resolved_parent or not resolved_target.is_relative_to(
            resolved_parent
        ):
            raise RuntimeError("Refusing to clean a path outside the PDF staging directory")
        shutil.rmtree(resolved_target, ignore_errors=True)
