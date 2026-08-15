"""Orchestrate pure parsers and the case service without filesystem side effects in either."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from asset_compensation.domain import Case, ValidationError
from asset_compensation.domain import ParsedCase as DomainParsedCase
from asset_compensation.parsers import (
    EmlParseError,
    EmlParser,
    EmlSkipError,
    SupplierRecord,
    normalize_domain,
)
from asset_compensation.parsers.contracts import ParsedCase as ParserCase

from .case_service import CaseService

_MAX_SAFE_VND = 9_007_199_254_740_991


@dataclass(frozen=True, slots=True)
class IngestionReport:
    cases: tuple[Case, ...]
    warnings: tuple[str, ...]
    unknown_files: tuple[str, ...]
    skipped_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailPayload:
    """One validated in-memory EML with a display-safe source filename."""

    filename: str
    data: bytes


def _vnd(value: Decimal | int | None, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a numeric VND amount") from exc
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise ValidationError(f"{field_name} must use whole VND units")
    if decimal < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    if decimal > _MAX_SAFE_VND:
        raise ValidationError(f"{field_name} exceeds the supported VND limit")
    return int(decimal)


def _received_at(parsed: ParserCase, fallback: datetime) -> datetime:
    if parsed.received_at is not None:
        return parsed.received_at
    return fallback


def _metadata_value(value: object, field_name: str = "metadata") -> object:
    """Make parser metadata JSON-safe without rounding exact money values."""

    if isinstance(value, Decimal):
        return _vnd(value, field_name)
    if isinstance(value, Mapping):
        return {
            str(key): _metadata_value(item, f"{field_name}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _metadata_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _to_domain_case(
    parsed: ParserCase,
    source_name: str,
    received_at_fallback: datetime,
    supplier_directory: Mapping[str, SupplierRecord] | None,
    ambiguous_supplier_domains: frozenset[str] = frozenset(),
) -> DomainParsedCase:
    warnings = list(parsed.warnings)
    normalized_domain = normalize_domain(parsed.domain)
    supplier = (
        supplier_directory.get(normalized_domain) if supplier_directory else None
    )
    metadata_value = _metadata_value(parsed.metadata)
    if not isinstance(metadata_value, dict):
        raise ValidationError("Parser metadata must be a mapping")
    metadata = metadata_value
    source_marks_inactive = metadata.get("employee_inactive") is True

    if supplier_directory is None:
        metadata["supplier_lookup_status"] = "NOT_CONFIGURED"
    elif supplier is None:
        if normalized_domain in ambiguous_supplier_domains:
            metadata["supplier_lookup_status"] = "AMBIGUOUS"
            warnings.append(
                "Supplier domain is ambiguous across multiple "
                "Supplier Number/Employee Number records"
            )
        else:
            metadata["supplier_lookup_status"] = "NOT_FOUND"
            warnings.append("Supplier domain was not found in the configured directory")
    else:
        metadata["supplier_active"] = supplier.active
        if supplier.active is False:
            metadata["supplier_lookup_status"] = "INACTIVE"
            metadata["employee_inactive"] = True
            metadata["employee_status_source"] = "SUPPLIER_DIRECTORY"
            warnings.append("Supplier record is inactive")
        elif supplier.active is True:
            metadata["supplier_lookup_status"] = "ACTIVE"
        else:
            metadata["supplier_lookup_status"] = "MATCHED"
    if source_marks_inactive:
        metadata["employee_inactive"] = True
        metadata["employee_status_source"] = "EMAIL_SUBJECT"
    if not parsed.domain:
        raise ValidationError("Parser could not determine an employee domain")
    if not parsed.asset_code:
        raise ValidationError("Parser could not determine an asset code")
    if parsed.received_at is None:
        warnings.append("Email Date header was missing or invalid; upload time was used")

    return DomainParsedCase(
        case_type=parsed.case_type,
        domain=parsed.domain,
        employee_name=parsed.employee_name,
        asset_code=parsed.asset_code,
        asset_name=parsed.asset_name,
        received_at=_received_at(parsed, received_at_fallback),
        amount=_vnd(parsed.amount, "amount"),
        residual_value=_vnd(parsed.residual_value, "residual_value"),
        responsibility_fee=_vnd(parsed.responsibility_fee, "responsibility_fee"),
        repair_status=parsed.repair_status,
        supplier_number=supplier.supplier_number if supplier else parsed.supplier_number,
        supplier_site=supplier.supplier_site if supplier else parsed.supplier_site,
        supplier_name=supplier.supplier_name if supplier else parsed.supplier_name,
        warnings=tuple(dict.fromkeys(warnings)),
        source_file=source_name,
        source_id=parsed.source_id,
        metadata=metadata,
    )


def ingest_eml_directory(
    service: CaseService,
    directory: str | Path,
    *,
    supplier_directory: Mapping[str, SupplierRecord] | None = None,
    ambiguous_supplier_domains: frozenset[str] = frozenset(),
) -> IngestionReport:
    """Parse every top-level EML and persist valid cases idempotently."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    parser = EmlParser()
    candidates: list[DomainParsedCase] = []
    warnings: list[str] = []
    unknown: list[str] = []
    skipped: list[str] = []

    for source in sorted(root.glob("*.eml"), key=lambda item: item.name.casefold()):
        try:
            parsed_cases = parser.parse_many(source)
            fallback = datetime.fromtimestamp(source.stat().st_mtime, tz=UTC)
            candidates.extend(
                _to_domain_case(
                    parsed,
                    source.name,
                    fallback,
                    supplier_directory,
                    ambiguous_supplier_domains,
                )
                for parsed in parsed_cases
            )
        except EmlSkipError as exc:
            skipped.append(source.name)
            warnings.append(f"{source.name}: skipped ({exc.reason})")
        except (
            EmlParseError,
            InvalidOperation,
            ValidationError,
            OSError,
            UnicodeError,
        ) as exc:
            unknown.append(source.name)
            warnings.append(f"{source.name}: {exc}")

    persisted = service.ingest(candidates)
    return IngestionReport(
        tuple(persisted), tuple(warnings), tuple(unknown), tuple(skipped)
    )


def ingest_eml_payloads(
    service: CaseService,
    payloads: tuple[EmailPayload, ...] | list[EmailPayload],
    *,
    supplier_directory: Mapping[str, SupplierRecord] | None = None,
    ambiguous_supplier_domains: frozenset[str] = frozenset(),
    uploaded_at: datetime | None = None,
    artifact_metadata_by_sha256: Mapping[str, Mapping[str, object]] | None = None,
) -> IngestionReport:
    """Parse validated EML bytes and persist every valid case in one transaction."""

    parser = EmlParser()
    fallback = uploaded_at or datetime.now(UTC)
    candidates: list[DomainParsedCase] = []
    warnings: list[str] = []
    unknown: list[str] = []
    skipped: list[str] = []
    known_content: dict[str, str] = {}
    for case in service.list_cases():
        message_id_hash = str(case.metadata.get("message_id_sha256") or "")
        if not message_id_hash and case.metadata.get("message_id"):
            legacy_message_id = (
                str(case.metadata["message_id"]).strip(" <>").casefold().encode("utf-8")
            )
            message_id_hash = hashlib.sha256(legacy_message_id).hexdigest()
        content_sha = str(case.metadata.get("content_sha256") or "")
        if message_id_hash and content_sha:
            known_content[message_id_hash] = content_sha
    for payload in payloads:
        try:
            parsed_cases = parser.parse_bytes_many(
                payload.data, source_file=payload.filename
            )
            content_sha = hashlib.sha256(payload.data).hexdigest()
            artifact_metadata: dict[str, str] = {}
            if artifact_metadata_by_sha256 is not None:
                raw_artifact = artifact_metadata_by_sha256.get(content_sha)
                if raw_artifact is not None:
                    handle = raw_artifact.get("mail_artifact_handle")
                    filename = raw_artifact.get("mail_artifact_filename")
                    if handle != f"eml-sha256-{content_sha}":
                        raise ValidationError("Mail artifact handle does not match EML content")
                    if not isinstance(filename, str) or not re.fullmatch(
                        r"[^/\\\x00-\x1f]{1,255}\.eml", filename, flags=re.IGNORECASE
                    ):
                        raise ValidationError("Mail artifact filename is invalid")
                    artifact_metadata = {
                        "mail_artifact_handle": handle,
                        "mail_artifact_filename": filename,
                    }
            message_id = str(parsed_cases[0].source_id or "").strip(" <>").casefold()
            message_id_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
            if (
                message_id
                and message_id_hash in known_content
                and known_content[message_id_hash] != content_sha
            ):
                unknown.append(payload.filename)
                warnings.append(
                    f"{payload.filename}: Message-ID matches different content; "
                    "manual review required"
                )
                continue
            payload_candidates: list[DomainParsedCase] = []
            for parsed in parsed_cases:
                safe_metadata = {
                    key: value
                    for key, value in parsed.metadata.items()
                    if key not in {"message_id", "sender", "subject"}
                }
                safe_parsed = replace(
                    parsed,
                    metadata={
                        **safe_metadata,
                        "message_id_sha256": message_id_hash,
                        "content_sha256": content_sha,
                        **artifact_metadata,
                    },
                )
                payload_candidates.append(
                    _to_domain_case(
                        safe_parsed,
                        payload.filename,
                        fallback,
                        supplier_directory,
                        ambiguous_supplier_domains,
                    )
                )
            candidates.extend(payload_candidates)
            if message_id:
                known_content[message_id_hash] = content_sha
        except EmlSkipError as exc:
            skipped.append(payload.filename)
            warnings.append(f"{payload.filename}: skipped ({exc.reason})")
        except (EmlParseError, InvalidOperation, ValidationError, UnicodeError):
            unknown.append(payload.filename)
            warnings.append(
                f"{payload.filename}: email could not be classified or is missing required fields"
            )

    persisted = service.ingest(candidates)
    warnings.extend(
        f"{case.id}: {warning}" for case in persisted for warning in case.warnings
    )
    return IngestionReport(
        tuple(persisted), tuple(warnings), tuple(unknown), tuple(skipped)
    )
