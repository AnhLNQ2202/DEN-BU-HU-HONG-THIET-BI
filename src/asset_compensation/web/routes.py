"""Thin HTTP routes for the dashboard and JSON API."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from asset_compensation.adapters import (
    AccountingExportError,
    AccountingTemplateAdapter,
    CcdcWorkbookIndex,
    FaGlWorkbookIndex,
    GlAccountPair,
    OutputExistsError,
    PdfAdapterError,
    PdfCapabilityError,
    PdfDependencyError,
    PdfPageOverflowError,
    TranMailDraftBuilder,
    TranMailError,
    TranReferenceError,
    TranWorkbookAdapter,
    TranWorkbookError,
    build_tran_mail_table,
)
from asset_compensation.demo import seed_demo
from asset_compensation.domain import Case, CaseStatus, CaseType, ValidationError
from asset_compensation.parsers import (
    SupplierLoadError,
    load_supplier_directory,
    normalize_domain,
)
from asset_compensation.services import (
    MailArtifactError,
    TranAssetRequest,
    safe_eml_basename,
)
from asset_compensation.services.email_upload_service import EmailUpload
from asset_compensation.services.ingestion_service import EmailPayload, ingest_eml_payloads
from asset_compensation.services.supplier_upload_service import SupplierUpload
from asset_compensation.services.tran_reference_upload_service import TranReferenceUpload

blueprint = Blueprint("asset_hub", __name__)
_BATCH_NAME_RE = re.compile(r"GN2\d{6}")
_OPAQUE_ID_RE = re.compile(r"[0-9a-f]{32}")
_ARTIFACT_HANDLE_RE = re.compile(r"eml-sha256-[0-9a-f]{64}")
_TRAN_REQUEST_FIELDS = frozenset(
    {
        "tag_number",
        "asset_name",
        "domain",
        "lost_date",
        "physical",
        "confirmed_cost",
        "confirmed_start_date",
        "confirmed_group",
        "confirmed_fee_rate",
        "classification_confirmed",
    }
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _dependencies() -> tuple[Any, Any, Any]:
    extension = current_app.extensions["asset_hub"]
    return extension["settings"], extension["repository"], extension["case_service"]


def _compensation_service() -> Any:
    return current_app.extensions["asset_hub"]["compensation_service"]


def _extension(name: str) -> Any:
    return current_app.extensions["asset_hub"][name]


def _serialized_mutation(handler: Callable[_P, _R]) -> Callable[_P, _R]:
    """Serialize state-changing routes against staging cleanup in this process."""

    @wraps(handler)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _extension("mutation_lock"):
            return handler(*args, **kwargs)

    return locked


def _require_upload_header(expected: str) -> None:
    if request.headers.get("X-Asset-Hub-Upload") != expected:
        raise ValidationError("The required upload request header is missing or invalid")


def _current_supplier_reference(settings: Any) -> tuple[Any, frozenset[str]]:
    upload_service = _extension("supplier_upload_service")
    directory, raw_ambiguous, status = upload_service.snapshot()
    if status["configured"]:
        ambiguous = frozenset(
            normalize_domain(domain) for domain in raw_ambiguous if domain
        )
        return directory, ambiguous
    if settings.supplier_file is not None:
        return load_supplier_directory(settings.supplier_file), frozenset()
    return None, frozenset()


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("A JSON object is required")
    return data


def _bounded_integer_arg(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_boolean_arg(name: str) -> bool | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValidationError(f"{name} must be true or false")


def _batch_name(value: object) -> str:
    name = str(value or "").strip().upper()
    if not _BATCH_NAME_RE.fullmatch(name):
        raise ValidationError("batch_name must use GN2 + DDMMYY, for example GN2250526")
    try:
        datetime.strptime(name[3:], "%d%m%y")
    except ValueError as exc:
        raise ValidationError("batch_name contains an invalid calendar date") from exc
    return name


def _require_exact_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str] | set[str],
    required: frozenset[str] | set[str] = frozenset(),
) -> None:
    unknown = set(data) - set(allowed)
    missing = set(required) - set(data)
    if unknown:
        raise ValidationError("Unsupported request fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValidationError("Missing request fields: " + ", ".join(sorted(missing)))


def _optional_iso_date(value: object, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{field_name} must use YYYY-MM-DD") from exc


def _tran_requests(data: dict[str, Any], *, maximum: int = 100) -> list[TranAssetRequest]:
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValidationError("assets must be a non-empty list")
    if len(raw_assets) > maximum:
        raise ValidationError(f"A TranNNB request cannot contain more than {maximum} assets")
    requests: list[TranAssetRequest] = []
    for index, item in enumerate(raw_assets, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"assets item {index} must be a JSON object")
        unknown = set(item) - _TRAN_REQUEST_FIELDS
        if unknown:
            raise ValidationError(
                f"assets item {index} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        try:
            requests.append(
                TranAssetRequest(
                    tag_number=item.get("tag_number", ""),
                    asset_name=item.get("asset_name", ""),
                    domain=item.get("domain", ""),
                    lost_date=item.get("lost_date"),
                    physical=item.get("physical"),
                    confirmed_cost=item.get("confirmed_cost"),
                    confirmed_start_date=item.get("confirmed_start_date"),
                    confirmed_group=item.get("confirmed_group"),
                    confirmed_fee_rate=item.get("confirmed_fee_rate"),
                    classification_confirmed=item.get(
                        "classification_confirmed", False
                    ),
                )
            )
        except ValidationError as exc:
            raise ValidationError(f"assets item {index}: {exc}") from exc
    return requests


def _tran_reference_status(settings: Any) -> dict[str, Any]:
    managed = _extension("tran_reference_upload_service").snapshot()
    fa_path = managed.fa_gl_path or settings.fa_gl_reference
    ccdc_path = managed.ccdc_path or settings.ccdc_reference
    return {
        "fa_gl": {
            "configured": fa_path is not None,
            "available": bool(
                fa_path
                and fa_path.is_file()
                and fa_path.suffix.casefold() in {".xlsx", ".xlsm"}
            ),
            "source": (
                "uploaded"
                if managed.fa_gl_path is not None
                else "configured"
                if settings.fa_gl_reference is not None
                else None
            ),
        },
        "ccdc": {
            "configured": ccdc_path is not None,
            "available": bool(
                ccdc_path
                and ccdc_path.is_file()
                and ccdc_path.suffix.casefold() in {".xlsx", ".xlsm"}
            ),
            "source": (
                "uploaded"
                if managed.ccdc_path is not None
                else "configured"
                if settings.ccdc_reference is not None
                else None
            ),
        },
        "managed_updated_at": managed.status.get("updated_at"),
    }


def _load_tran_references(settings: Any) -> tuple[FaGlWorkbookIndex, CcdcWorkbookIndex | None]:
    managed = _extension("tran_reference_upload_service").snapshot()
    fa_path = managed.fa_gl_path or settings.fa_gl_reference
    ccdc_path = managed.ccdc_path or settings.ccdc_reference
    if fa_path is None:
        raise ValidationError("FA&GL reference is not configured")
    try:
        fa_gl = FaGlWorkbookIndex.from_path(fa_path)
    except (OSError, TranReferenceError, ValueError) as exc:
        raise ValidationError("FA&GL reference is unavailable or invalid") from exc
    if ccdc_path is None:
        return fa_gl, None
    try:
        return fa_gl, CcdcWorkbookIndex.from_path(ccdc_path)
    except (OSError, TranReferenceError, ValueError) as exc:
        raise ValidationError("CCDC reference is unavailable or invalid") from exc


def _resolve_tran(data: dict[str, Any], *, maximum: int = 100) -> tuple[Any, ...]:
    settings, _, _ = _dependencies()
    fa_gl, ccdc = _load_tran_references(settings)
    return _extension("tran_workflow_service").resolve_many(
        _tran_requests(data, maximum=maximum),
        fa_gl,
        ccdc=ccdc,
    )


def _tran_template(settings: Any) -> Path:
    template = settings.effective_tran_template
    if (
        template is None
        or not template.is_file()
        or template.suffix.casefold() not in {".xlsx", ".xlsm"}
    ):
        raise ValidationError("TranNNB workbook template is unavailable or invalid")
    return template


def _opaque_id(value: object, field_name: str) -> str:
    identifier = str(value or "")
    if not _OPAQUE_ID_RE.fullmatch(identifier):
        raise ValidationError(f"{field_name} is invalid")
    return identifier


def _managed_output(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if (
        resolved.parent != resolved_root
        or path.is_symlink()
        or not resolved.is_file()
    ):
        raise ValidationError("Generated output is unavailable")
    return resolved


def _private_download(
    path: Path,
    *,
    download_name: str,
    mimetype: str | None = None,
) -> Any:
    """Return a sensitive generated artifact without browser/proxy storage."""

    response = send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _unavailable(message: str) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": message, "capability_available": False}), 503


def _capabilities(settings: Any) -> dict[str, Any]:
    reference_status = _tran_reference_status(settings)
    template = settings.effective_tran_template
    template_available = bool(
        template
        and template.is_file()
        and template.suffix.casefold() in {".xlsx", ".xlsm"}
    )
    retention = bool(settings.retain_raw_eml)
    mail_pdf = bool(_extension("mail_pdf_available"))
    pypdf = bool(_extension("pypdf_available"))
    return {
        "test_reset": settings.allow_test_reset,
        "demo_reset": settings.demo_mode,
        "raw_eml_retention": retention,
        "source_eml_download": retention,
        "tran_reference_upload": True,
        "tran_lookup": reference_status["fa_gl"]["available"],
        "tran_workbook_export": template_available,
        "tran_draft": bool(
            retention and template_available and settings.draft_from_address
        ),
        "mail_pdf_individual": mail_pdf,
        "mail_pdf_batch": bool(mail_pdf and pypdf),
        "mail_pdf_backend": _extension("pdf_backend") if mail_pdf else None,
    }


def _case_issue(case: Case, index: int, warning: str) -> dict[str, Any]:
    lowered = warning.casefold()
    severity = "high" if any(
        token in lowered for token in ("mismatch", "missing", "multiple", "duplicate", "lệch")
    ) else "medium"
    return {
        "id": f"{case.id}:{index}",
        "case_id": case.id,
        "asset_code": case.asset_code,
        "severity": severity,
        "title": warning,
        "description": f"{case.domain} · {case.asset_code}",
    }


def _case_dict(case: Case) -> dict[str, Any]:
    payload = case.to_dict()
    handle = case.metadata.get("mail_artifact_handle")
    filename = case.metadata.get("mail_artifact_filename")
    if (
        isinstance(handle, str)
        and _ARTIFACT_HANDLE_RE.fullmatch(handle)
        and isinstance(filename, str)
    ):
        with suppress(MailArtifactError):
            safe_filename = safe_eml_basename(filename)
            payload["source_eml"] = {
                "handle": handle,
                "filename": safe_filename,
                "download_url": f"/api/mail-artifacts/{handle}/download",
            }
    return payload


def _batch_dict(batch: Any, service: Any, settings: Any) -> dict[str, Any]:
    cases = [service.get_case(case_id) for case_id in batch.case_ids]
    metadata = dict(batch.metadata)
    output_name = metadata.get("output_name")
    output_exists = (
        isinstance(output_name, str)
        and output_name == str(output_name).replace("\\", "/").rsplit("/", 1)[-1]
        and (settings.output_dir / output_name).is_file()
    )
    return {
        "id": batch.id,
        "batch_name": batch.name,
        "name": batch.name,
        "case_ids": list(batch.case_ids),
        "case_count": len(cases),
        "total_amount": sum(case.amount or 0 for case in cases),
        "created_at": batch.created_at.isoformat(),
        "download_url": f"/api/batches/{batch.id}/download" if output_exists else None,
        "metadata": metadata,
    }


@blueprint.get("/")
def index() -> str:
    return render_template("index.html")


@blueprint.get("/api/health")
def health() -> Any:
    return jsonify(
        {
            "ok": True,
            "service": "asset-compensation-hub",
        }
    )


@blueprint.get("/api/dashboard")
def dashboard() -> Any:
    settings, repository, service = _dependencies()
    cases = service.list_cases()
    issues = [
        _case_issue(case, index, warning)
        for case in cases
        for index, warning in enumerate(case.warnings, start=1)
    ]
    batches = [_batch_dict(batch, service, settings) for batch in repository.list_batches()]
    return jsonify(
        {
            "ok": True,
            "summary": service.summary().to_dict(),
            "cases": [_case_dict(case) for case in cases],
            "issues": issues,
            "batches": batches,
            "capabilities": _capabilities(settings),
        }
    )


@blueprint.get("/api/capabilities")
def capabilities() -> Any:
    settings, _, _ = _dependencies()
    return jsonify(
        {
            "ok": True,
            "capabilities": _capabilities(settings),
            "tran_references": _tran_reference_status(settings),
        }
    )


@blueprint.get("/api/cases")
def list_cases() -> Any:
    _, _, service = _dependencies()
    raw_type = request.args.get("case_type") or None
    raw_status = request.args.get("status") or None
    try:
        case_type = CaseType(raw_type.upper()) if raw_type else None
        status = CaseStatus(raw_status.upper()) if raw_status else None
    except ValueError as exc:
        raise ValidationError("Unsupported case_type or status filter") from exc
    cases = service.list_cases(
        case_type=case_type,
        status=status,
        domain=request.args.get("domain") or None,
        has_warnings=_optional_boolean_arg("has_warnings"),
        limit=_bounded_integer_arg("limit", 500, minimum=1, maximum=500),
        offset=_bounded_integer_arg("offset", 0, minimum=0, maximum=1_000_000),
    )
    return jsonify({"ok": True, "cases": [_case_dict(case) for case in cases]})


@blueprint.get("/api/cases/<case_id>")
def case_detail(case_id: str) -> Any:
    _, _, service = _dependencies()
    case = service.get_case(case_id)
    history = [event.to_dict() for event in service.status_history(case_id)]
    return jsonify({"ok": True, "case": _case_dict(case), "history": history})


@blueprint.post("/api/compensation/preview")
def compensation_preview() -> Any:
    """Preview policy calculations without persisting or exporting anything."""

    data = _json_body()
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValidationError("assets must be a non-empty list")
    if len(raw_assets) > 100:
        raise ValidationError("A preview cannot contain more than 100 assets")
    if not all(isinstance(item, dict) for item in raw_assets):
        raise ValidationError("Each assets item must be a JSON object")

    calculator = _compensation_service()
    results = [calculator.preview_mapping(item).to_dict() for item in raw_assets]
    status_counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    return jsonify(
        {
            "ok": True,
            "count": len(results),
            "review_required": any(result["review_required"] for result in results),
            "status_counts": status_counts,
            "results": results,
        }
    )


@blueprint.patch("/api/cases/<case_id>/status")
@_serialized_mutation
def update_case_status(case_id: str) -> Any:
    _, _, service = _dependencies()
    data = _json_body()
    target = data.get("status")
    if not target:
        raise ValidationError("status is required")
    case = service.transition_status(
        case_id,
        target,
        actor=str(data.get("actor") or "dashboard"),
        note=str(data["note"]) if data.get("note") else None,
    )
    return jsonify({"ok": True, "case": _case_dict(case)})


@blueprint.post("/api/demo/reset")
@_serialized_mutation
def reset_demo() -> Any:
    settings, _, service = _dependencies()
    if not settings.demo_mode:
        raise ValidationError("Demo reset is disabled")
    cases = seed_demo(service)
    return jsonify({"ok": True, "cases": [_case_dict(case) for case in cases]})


def _clear_managed_tran_outputs(settings: Any) -> int:
    """Remove only server-named Tran/mail-PDF outputs; preserve unknown files."""

    output_root = settings.output_dir.resolve()
    count = 0
    tran_root = settings.tran_output_dir
    if tran_root.exists():
        if tran_root.is_symlink() or tran_root.resolve().parent != output_root:
            raise ValidationError("Managed Tran output storage is unsafe")
        for candidate in tran_root.iterdir():
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.resolve().parent == tran_root.resolve()
                and re.fullmatch(
                    r"(?:workbook-[0-9a-f]{32}\.(?:xlsx|xlsm)|draft-[0-9a-f]{32}\.eml)",
                    candidate.name,
                    re.IGNORECASE,
                )
            ):
                candidate.unlink()
                count += 1

    pdf_root = settings.mail_pdf_output_dir
    if not pdf_root.exists():
        return count
    if pdf_root.is_symlink() or pdf_root.resolve().parent != output_root:
        raise ValidationError("Managed mail PDF storage is unsafe")
    standalone = pdf_root / "standalone"
    if standalone.exists():
        if standalone.is_symlink() or standalone.resolve().parent != pdf_root.resolve():
            raise ValidationError("Managed standalone PDF storage is unsafe")
        for candidate in standalone.iterdir():
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.resolve().parent == standalone.resolve()
                and re.fullmatch(r"mail-[0-9a-f]{32}\.pdf", candidate.name, re.IGNORECASE)
            ):
                candidate.unlink()
                count += 1
        with suppress(OSError):
            standalone.rmdir()

    for directory in pdf_root.iterdir():
        if not re.fullmatch(r"batch-[0-9a-f]{32}", directory.name, re.IGNORECASE):
            continue
        if directory.is_symlink() or directory.resolve().parent != pdf_root.resolve():
            raise ValidationError("Managed mail PDF batch storage is unsafe")
        if not directory.is_dir():
            continue
        individual = directory / "individual"
        if individual.exists():
            if individual.is_symlink() or individual.resolve().parent != directory.resolve():
                raise ValidationError("Managed mail PDF batch storage is unsafe")
            for candidate in individual.iterdir():
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and candidate.resolve().parent == individual.resolve()
                    and re.fullmatch(
                        r"\d{2}_[^/\\\x00-\x1f]+\.pdf",
                        candidate.name,
                        re.IGNORECASE,
                    )
                ):
                    candidate.unlink()
                    count += 1
            with suppress(OSError):
                individual.rmdir()
        merged = directory / f"chungtu_{directory.name}.pdf"
        if (
            merged.is_file()
            and not merged.is_symlink()
            and merged.resolve().parent == directory.resolve()
        ):
            merged.unlink()
            count += 1
        with suppress(OSError):
            directory.rmdir()
    with suppress(OSError):
        pdf_root.rmdir()
    return count


@blueprint.post("/api/test-data/clear")
@_serialized_mutation
def clear_test_data() -> Any:
    settings, _, _ = _dependencies()
    if not settings.allow_test_reset:
        raise ValidationError("Test data clearing is disabled")
    if request.headers.get("X-Asset-Hub-Action") != "clear-test-data-v1":
        raise ValidationError("The required test-data action header is missing or invalid")
    if _json_body() != {"confirm": "CLEAR_TEST_DATA"}:
        raise ValidationError("Exact test-data clearing confirmation is required")

    counts = _extension("test_data_service").clear()
    reference_counts = _extension("tran_reference_upload_service").clear()
    artifact_count = _extension("mail_artifact_store").clear_managed()
    managed_output_count = _clear_managed_tran_outputs(settings)
    return jsonify(
        {
            "ok": True,
            "message": "Test data cleared",
            **counts,
            **reference_counts,
            "mail_artifact_count": artifact_count,
            "tran_managed_output_count": managed_output_count,
        }
    )


@blueprint.get("/api/suppliers/status")
def supplier_upload_status() -> Any:
    return jsonify({"ok": True, "status": _extension("supplier_upload_service").status()})


@blueprint.post("/api/suppliers/upload")
@_serialized_mutation
def upload_suppliers() -> Any:
    _require_upload_header("supplier-v1")
    if request.mimetype != "multipart/form-data":
        raise ValidationError("Supplier upload must use multipart/form-data")
    expected = {"active_file", "inactive_file"}
    if set(request.files.keys()) != expected or request.form:
        raise ValidationError("Upload exactly active_file and inactive_file")
    active_files = request.files.getlist("active_file")
    inactive_files = request.files.getlist("inactive_file")
    if len(active_files) != 1 or len(inactive_files) != 1:
        raise ValidationError("Upload exactly one active_file and one inactive_file")

    active_file = active_files[0]
    inactive_file = inactive_files[0]
    result = _extension("supplier_upload_service").upload(
        SupplierUpload(
            filename=active_file.filename or "",
            content_type=active_file.content_type,
            stream=active_file.stream,
        ),
        SupplierUpload(
            filename=inactive_file.filename or "",
            content_type=inactive_file.content_type,
            stream=inactive_file.stream,
        ),
    )
    payload = result.to_dict()
    return jsonify(
        {
            "ok": True,
            "message": "Supplier directory uploaded and activated",
            **payload,
        }
    ), 201


@blueprint.post("/api/emails/upload")
@_serialized_mutation
def upload_emails() -> Any:
    _require_upload_header("email-v1")
    if request.mimetype != "multipart/form-data":
        raise ValidationError("Email upload must use multipart/form-data")
    if set(request.files.keys()) != {"files"} or request.form:
        raise ValidationError("Upload one or more EML files using the files field")

    file_storages = request.files.getlist("files")
    uploads = [
        EmailUpload(
            filename=item.filename or "",
            content_type=item.content_type,
            stream=item.stream,
        )
        for item in file_storages
    ]
    payloads = _extension("email_upload_service").validate(uploads)
    settings, _, service = _dependencies()
    artifact_metadata: dict[str, dict[str, str]] = {}
    retained: tuple[Any, ...] = ()
    if settings.retain_raw_eml:
        try:
            retained_payloads = tuple(
                EmailPayload(
                    filename=safe_eml_basename(storage.filename or payload.filename),
                    data=payload.data,
                )
                for storage, payload in zip(file_storages, payloads, strict=True)
            )
            retained = _extension("mail_artifact_store").retain_many(retained_payloads)
        except MailArtifactError as exc:
            raise ValidationError("Could not retain validated source email") from exc
        for item in retained:
            artifact_metadata.setdefault(
                item.content_sha256,
                {
                    "mail_artifact_handle": item.handle,
                    "mail_artifact_filename": item.safe_filename,
                },
            )
    try:
        try:
            supplier_directory, ambiguous_domains = _current_supplier_reference(settings)
        except (OSError, SupplierLoadError) as exc:
            raise ValidationError(
                "Could not load the configured supplier reference"
            ) from exc
        result = ingest_eml_payloads(
            service,
            payloads,
            supplier_directory=supplier_directory,
            ambiguous_supplier_domains=ambiguous_domains,
            artifact_metadata_by_sha256=artifact_metadata or None,
        )
    except Exception:
        for item in retained:
            if item.created:
                with suppress(MailArtifactError, OSError):
                    _extension("mail_artifact_store").delete(item.handle)
        raise
    referenced_handles = {
        str(case.metadata.get("mail_artifact_handle"))
        for case in result.cases
        if case.metadata.get("mail_artifact_handle")
    }
    live_handles = {
        str(case.metadata.get("mail_artifact_handle"))
        for case in service.list_cases()
        if case.metadata.get("mail_artifact_handle")
    }
    for item in retained:
        if item.handle not in live_handles:
            try:
                _extension("mail_artifact_store").delete(item.handle)
            except (MailArtifactError, OSError) as exc:
                raise ValidationError(
                    "Could not remove an unclassified retained source email"
                ) from exc
    cases = [
        {
            "id": case.id,
            "case_type": case.case_type.value,
            **(
                {
                    "source_eml": {
                        "handle": case.metadata["mail_artifact_handle"],
                        "filename": case.metadata["mail_artifact_filename"],
                        "download_url": (
                            "/api/mail-artifacts/"
                            f"{case.metadata['mail_artifact_handle']}/download"
                        ),
                    }
                }
                if case.metadata.get("mail_artifact_handle")
                and case.metadata.get("mail_artifact_filename")
                else {}
            ),
        }
        for case in result.cases
    ]
    case_types: dict[str, int] = {}
    for case in result.cases:
        key = case.case_type.value
        case_types[key] = case_types.get(key, 0) + 1
    case_label = "case" if len(cases) == 1 else "cases"
    email_label = "email" if len(payloads) == 1 else "emails"
    return jsonify(
        {
            "ok": True,
            "message": (
                f"Created {len(cases)} {case_label} from "
                f"{len(payloads)} uploaded {email_label}"
            ),
            "received_count": len(payloads),
            "ingested": len(cases),
            "case_ids": [case["id"] for case in cases],
            "case_types": case_types,
            "cases": cases,
            "retained_source_count": len(referenced_handles),
            "warnings": list(result.warnings),
            "unknown_files": list(result.unknown_files),
            "skipped_files": list(result.skipped_files),
        }
    )


@blueprint.post("/api/ingest")
@_serialized_mutation
def ingest() -> Any:
    settings, _, service = _dependencies()
    from asset_compensation.services.ingestion_service import ingest_eml_directory

    try:
        supplier_directory, ambiguous_domains = _current_supplier_reference(settings)
    except (OSError, SupplierLoadError) as exc:
        raise ValidationError(f"Could not load supplier reference: {exc}") from exc
    result = ingest_eml_directory(
        service,
        settings.inbox_dir,
        supplier_directory=supplier_directory,
        ambiguous_supplier_domains=ambiguous_domains,
    )
    return jsonify(
        {
            "ok": True,
            "ingested": len(result.cases),
            "cases": [_case_dict(case) for case in result.cases],
            "warnings": list(result.warnings),
            "unknown_files": list(result.unknown_files),
        }
    )


@blueprint.get("/api/tran/references/status")
def tran_reference_status() -> Any:
    settings, _, _ = _dependencies()
    return jsonify({"ok": True, "status": _tran_reference_status(settings)})


@blueprint.post("/api/tran/references/upload")
@_serialized_mutation
def upload_tran_references() -> Any:
    _require_upload_header("tran-reference-v1")
    if request.mimetype != "multipart/form-data":
        raise ValidationError("Tran reference upload must use multipart/form-data")
    if not set(request.files).issubset({"fa_gl_file", "ccdc_file"}):
        raise ValidationError("Upload only fa_gl_file and optional ccdc_file")
    if set(request.form) - {"clear_ccdc"}:
        raise ValidationError("Unsupported Tran reference upload fields")
    fa_files = request.files.getlist("fa_gl_file")
    ccdc_files = request.files.getlist("ccdc_file")
    if len(fa_files) != 1 or len(ccdc_files) > 1:
        raise ValidationError(
            "Upload exactly one fa_gl_file and at most one ccdc_file"
        )
    raw_clear = request.form.get("clear_ccdc")
    if raw_clear not in {None, "true"}:
        raise ValidationError("clear_ccdc must be omitted or exactly true")
    clear_ccdc = raw_clear == "true"
    if ccdc_files and clear_ccdc:
        raise ValidationError("ccdc_file and clear_ccdc cannot be supplied together")

    fa_file = fa_files[0]
    ccdc_file = ccdc_files[0] if ccdc_files else None
    status = _extension("tran_reference_upload_service").upload(
        TranReferenceUpload(
            filename=fa_file.filename or "",
            content_type=fa_file.content_type,
            stream=fa_file.stream,
        ),
        (
            TranReferenceUpload(
                filename=ccdc_file.filename or "",
                content_type=ccdc_file.content_type,
                stream=ccdc_file.stream,
            )
            if ccdc_file is not None
            else None
        ),
        clear_ccdc=clear_ccdc,
    )
    settings, _, _ = _dependencies()
    return jsonify(
        {
            "ok": True,
            "message": "TranNNB references uploaded and activated",
            "managed": status,
            "status": _tran_reference_status(settings),
        }
    ), 201


@blueprint.post("/api/tran/resolve")
@_serialized_mutation
def resolve_tran_assets() -> Any:
    data = _json_body()
    _require_exact_keys(data, allowed={"assets"}, required={"assets"})
    resolutions = _resolve_tran(data)
    ready = all(item.ready for item in resolutions)
    return jsonify(
        {
            "ok": True,
            "count": len(resolutions),
            "ready": ready,
            "review_required": not ready,
            "results": [item.to_dict() for item in resolutions],
            "mail_table_html": (
                build_tran_mail_table(resolutions) if ready else None
            ),
        }
    )


def _export_tran_workbook(data: dict[str, Any]) -> tuple[Any, Path, str]:
    settings, _, _ = _dependencies()
    resolutions = _resolve_tran(data)
    processing_date = _optional_iso_date(
        data.get("processing_date"), "processing_date"
    ) or date.today()
    year_sheet = data.get("year_sheet")
    if year_sheet is not None:
        if (
            not isinstance(year_sheet, str)
            or not year_sheet.strip()
            or len(year_sheet.strip()) > 31
            or any(ord(character) < 32 for character in year_sheet)
        ):
            raise ValidationError("year_sheet must be a valid non-empty sheet name")
        year_sheet = year_sheet.strip()
    template = _tran_template(settings)
    settings.tran_output_dir.mkdir(parents=True, exist_ok=True)
    output_id = uuid4().hex
    destination = settings.tran_output_dir / f"workbook-{output_id}{template.suffix.lower()}"
    try:
        result = TranWorkbookAdapter(template).export(
            resolutions,
            destination,
            processing_date=processing_date,
            year_sheet=year_sheet,
        )
    except (OSError, OutputExistsError, TranWorkbookError) as exc:
        raise ValidationError("Could not export the TranNNB workbook") from exc
    return (resolutions, result.path, output_id)


@blueprint.post("/api/tran/workbooks")
@_serialized_mutation
def export_tran_workbook() -> Any:
    data = _json_body()
    _require_exact_keys(
        data,
        allowed={"assets", "processing_date", "year_sheet"},
        required={"assets"},
    )
    resolutions, path, output_id = _export_tran_workbook(data)
    return jsonify(
        {
            "ok": True,
            "output_id": output_id,
            "asset_count": len(resolutions),
            "download_url": f"/api/tran/workbooks/{output_id}/download",
            "format": path.suffix.lower().removeprefix("."),
        }
    ), 201


@blueprint.get("/api/tran/workbooks/<output_id>/download")
def download_tran_workbook(output_id: str) -> Any:
    settings, _, _ = _dependencies()
    identifier = _opaque_id(output_id, "output_id")
    for suffix in (".xlsx", ".xlsm"):
        candidate = settings.tran_output_dir / f"workbook-{identifier}{suffix}"
        if candidate.is_file():
            path = _managed_output(candidate, settings.tran_output_dir)
            mimetype = (
                "application/vnd.ms-excel.sheet.macroEnabled.12"
                if suffix == ".xlsm"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            return _private_download(
                path,
                download_name=f"tran-compensation-{identifier[:12]}{suffix}",
                mimetype=mimetype,
            )
    raise ValidationError("Generated workbook is unavailable")


@blueprint.post("/api/tran/drafts")
@_serialized_mutation
def create_tran_draft() -> Any:
    settings, _, _ = _dependencies()
    if not settings.retain_raw_eml:
        return _unavailable("Raw EML retention is disabled")
    if not settings.draft_from_address:
        return _unavailable("Draft sender address is not configured")
    data = _json_body()
    _require_exact_keys(
        data,
        allowed={
            "assets",
            "mail_artifact_handle",
            "body_intro",
            "processing_date",
            "year_sheet",
        },
        required={"assets", "mail_artifact_handle", "body_intro"},
    )
    handle = str(data.get("mail_artifact_handle") or "")
    if not _ARTIFACT_HANDLE_RE.fullmatch(handle):
        raise ValidationError("mail_artifact_handle is invalid")
    body_intro = data.get("body_intro")
    if not isinstance(body_intro, str) or not body_intro.strip() or len(body_intro) > 10_000:
        raise ValidationError("body_intro must contain 1 to 10,000 characters")
    try:
        original = _extension("mail_artifact_store").read_bytes(handle)
    except MailArtifactError as exc:
        raise ValidationError("Retained source email is unavailable") from exc

    resolutions: Any = None
    workbook_path: Path | None = None
    try:
        resolutions, workbook_path, output_id = _export_tran_workbook(data)
        draft_path = settings.tran_output_dir / f"draft-{output_id}.eml"
        draft = TranMailDraftBuilder().build(
            original,
            resolutions,
            workbook_path,
            draft_path,
            from_address=settings.draft_from_address,
            body_intro=body_intro.strip(),
        )
    except (OSError, OutputExistsError, TranMailError) as exc:
        if workbook_path is not None:
            workbook_path.unlink(missing_ok=True)
        raise ValidationError("Could not create the unsent TranNNB draft") from exc
    return jsonify(
        {
            "ok": True,
            "output_id": output_id,
            "asset_count": len(resolutions),
            "workbook_download_url": f"/api/tran/workbooks/{output_id}/download",
            "draft_download_url": f"/api/tran/drafts/{output_id}/download",
            "subject": draft.subject,
            "to_count": len(draft.to),
            "cc_count": len(draft.cc),
            "sent": False,
        }
    ), 201


@blueprint.get("/api/tran/drafts/<output_id>/download")
def download_tran_draft(output_id: str) -> Any:
    settings, _, _ = _dependencies()
    identifier = _opaque_id(output_id, "output_id")
    path = _managed_output(
        settings.tran_output_dir / f"draft-{identifier}.eml",
        settings.tran_output_dir,
    )
    return _private_download(
        path,
        download_name=f"tran-reply-draft-{identifier[:12]}.eml",
        mimetype="message/rfc822",
    )


@blueprint.get("/api/mail-artifacts/<handle>/download")
def download_mail_artifact(handle: str) -> Any:
    settings, _, service = _dependencies()
    if not settings.retain_raw_eml:
        return _unavailable("Raw EML retention is disabled")
    if not _ARTIFACT_HANDLE_RE.fullmatch(handle):
        raise ValidationError("Mail artifact handle is invalid")
    try:
        path = _extension("mail_artifact_store").resolve(handle)
    except MailArtifactError as exc:
        raise ValidationError("Retained source email is unavailable") from exc
    filename = "source-email.eml"
    for case in service.list_cases():
        if case.metadata.get("mail_artifact_handle") == handle:
            candidate = case.metadata.get("mail_artifact_filename")
            if isinstance(candidate, str):
                with suppress(MailArtifactError):
                    filename = safe_eml_basename(candidate)
            break
    response = _private_download(
        path,
        download_name=filename,
        mimetype="message/rfc822",
    )
    return response


@blueprint.post("/api/mail-pdfs/individual")
@_serialized_mutation
def create_mail_pdf() -> Any:
    settings, _, _ = _dependencies()
    if not settings.retain_raw_eml or not _extension("mail_pdf_available"):
        return _unavailable(
            "EML-to-PDF requires opt-in retention and an available safe PDF renderer"
        )
    data = _json_body()
    _require_exact_keys(
        data,
        allowed={"mail_artifact_handle"},
        required={"mail_artifact_handle"},
    )
    handle = str(data.get("mail_artifact_handle") or "")
    if not _ARTIFACT_HANDLE_RE.fullmatch(handle):
        raise ValidationError("mail_artifact_handle is invalid")
    output_id = uuid4().hex
    root = settings.mail_pdf_output_dir / "standalone"
    root.mkdir(parents=True, exist_ok=True)
    try:
        _extension("mail_pdf_service").create_individual(
            handle,
            root / f"mail-{output_id}.pdf",
        )
    except PdfCapabilityError:
        return _unavailable("Mail PDF conversion is unavailable on this host")
    except (MailArtifactError, PdfAdapterError, OSError) as exc:
        raise ValidationError("Could not create the individual mail PDF") from exc
    return jsonify(
        {
            "ok": True,
            "output_id": output_id,
            "download_url": f"/api/mail-pdfs/individual/{output_id}/download",
        }
    ), 201


@blueprint.get("/api/mail-pdfs/individual/<output_id>/download")
def download_mail_pdf(output_id: str) -> Any:
    settings, _, _ = _dependencies()
    identifier = _opaque_id(output_id, "output_id")
    root = settings.mail_pdf_output_dir / "standalone"
    path = _managed_output(root / f"mail-{identifier}.pdf", root)
    return _private_download(
        path,
        download_name=f"mail-evidence-{identifier[:12]}.pdf",
        mimetype="application/pdf",
    )


@blueprint.post("/api/mail-pdfs/batches")
@_serialized_mutation
def create_mail_pdf_batch() -> Any:
    settings, _, _ = _dependencies()
    if (
        not settings.retain_raw_eml
        or not _extension("mail_pdf_available")
        or not _extension("pypdf_available")
    ):
        return _unavailable(
            "Batch EML-to-PDF requires retention, a safe renderer, and pypdf"
        )
    data = _json_body()
    _require_exact_keys(
        data,
        allowed={
            "mail_artifact_handles",
            "pages_per_mail",
            "overflow_policy",
            "batch_name",
        },
        required={"mail_artifact_handles"},
    )
    raw_batch_name = data.get("batch_name")
    output_batch_name = (
        _batch_name(raw_batch_name) if raw_batch_name not in (None, "") else None
    )
    handles = data.get("mail_artifact_handles")
    if (
        not isinstance(handles, list)
        or not handles
        or len(handles) > 20
        or not all(
            isinstance(item, str) and _ARTIFACT_HANDLE_RE.fullmatch(item)
            for item in handles
        )
    ):
        raise ValidationError("mail_artifact_handles must contain 1 to 20 valid handles")
    if len(set(handles)) != len(handles):
        raise ValidationError("mail_artifact_handles cannot contain duplicates")
    pages_per_mail = data.get("pages_per_mail", 2)
    if isinstance(pages_per_mail, bool) or not isinstance(pages_per_mail, int):
        raise ValidationError("pages_per_mail must be an integer")
    if not 1 <= pages_per_mail <= 10:
        raise ValidationError("pages_per_mail must be between 1 and 10")
    overflow_policy = data.get("overflow_policy", "fail")
    if overflow_policy not in {"fail", "warn"}:
        raise ValidationError("overflow_policy must be fail or warn")

    batch_id = uuid4().hex
    try:
        result = _extension("mail_pdf_service").create_batch(
            handles,
            settings.mail_pdf_output_dir,
            batch_name=f"batch-{batch_id}",
            pages_per_mail=pages_per_mail,
            overflow_policy=overflow_policy,
        )
    except (PdfCapabilityError, PdfDependencyError):
        return _unavailable("Mail PDF batch capability is unavailable on this host")
    except PdfPageOverflowError as exc:
        raise ValidationError(str(exc)) from exc
    except (MailArtifactError, PdfAdapterError, OSError, ValueError) as exc:
        raise ValidationError("Could not create the mail PDF batch") from exc
    return jsonify(
        {
            "ok": True,
            "batch_id": batch_id,
            "output_name": (
                f"chungtu_{output_batch_name}.pdf"
                if output_batch_name
                else "chungtu_gop.pdf"
            ),
            "merged_download_url": (
                f"/api/mail-pdfs/batches/{batch_id}/merged"
                + (f"?batch_name={output_batch_name}" if output_batch_name else "")
            ),
            "items": [
                {
                    "index": index,
                    "source_pages": item.source_pages,
                    "output_pages": item.output_pages,
                    "padded_pages": item.padded_pages,
                    "truncated_pages": item.truncated_pages,
                    "warnings": list(item.warnings),
                    "download_url": (
                        f"/api/mail-pdfs/batches/{batch_id}/items/{index}"
                    ),
                }
                for index, item in enumerate(result.items, start=1)
            ],
            "warnings": list(result.warnings),
        }
    ), 201


def _mail_pdf_batch_directory(settings: Any, batch_id: str) -> Path:
    identifier = _opaque_id(batch_id, "batch_id")
    root = settings.mail_pdf_output_dir.resolve()
    candidate = settings.mail_pdf_output_dir / f"batch-{identifier}"
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or candidate.resolve().parent != root
    ):
        raise ValidationError("Mail PDF batch is unavailable")
    return candidate.resolve()


@blueprint.get("/api/mail-pdfs/batches/<batch_id>/merged")
def download_mail_pdf_batch(batch_id: str) -> Any:
    settings, _, _ = _dependencies()
    directory = _mail_pdf_batch_directory(settings, batch_id)
    merged = directory / f"chungtu_{directory.name}.pdf"
    if merged.is_symlink() or not merged.is_file() or merged.resolve().parent != directory:
        raise ValidationError("Merged mail PDF is unavailable")
    raw_batch_name = request.args.get("batch_name")
    download_name = (
        f"chungtu_{_batch_name(raw_batch_name)}.pdf"
        if raw_batch_name
        else "chungtu_gop.pdf"
    )
    return _private_download(
        merged,
        download_name=download_name,
        mimetype="application/pdf",
    )


@blueprint.get("/api/mail-pdfs/batches/<batch_id>/items/<int:item_index>")
def download_mail_pdf_batch_item(batch_id: str, item_index: int) -> Any:
    settings, _, _ = _dependencies()
    directory = _mail_pdf_batch_directory(settings, batch_id)
    individual = directory / "individual"
    if individual.is_symlink() or not individual.is_dir():
        raise ValidationError("Mail PDF batch item is unavailable")
    files = sorted(
        path
        for path in individual.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and re.fullmatch(r"\d{2}_[^/\\\x00-\x1f]+\.pdf", path.name, re.IGNORECASE)
        and path.resolve().parent == individual.resolve()
    )
    if item_index < 1 or item_index > len(files):
        raise ValidationError("Mail PDF batch item is unavailable")
    path = files[item_index - 1]
    return _private_download(
        path,
        download_name=f"mail-evidence-{batch_id[:12]}-{item_index:02d}.pdf",
        mimetype="application/pdf",
    )


@blueprint.post("/api/batches")
@_serialized_mutation
def create_batch() -> Any:
    settings, repository, service = _dependencies()
    data = _json_body()
    _require_exact_keys(
        data,
        allowed={"batch_name", "name", "case_ids", "actor", "invoice_start"},
        required={"case_ids"},
    )
    batch_name = _batch_name(data.get("batch_name") or data.get("name"))
    invoice_start = data.get("invoice_start", 1)
    if (
        isinstance(invoice_start, bool)
        or not isinstance(invoice_start, int)
        or not 0 <= invoice_start <= 999_999
    ):
        raise ValidationError("invoice_start must be an integer between 0 and 999999")
    raw_case_ids = data.get("case_ids")
    if not isinstance(raw_case_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_case_ids
    ):
        raise ValidationError("case_ids must be a list of strings")
    case_ids = [item.strip() for item in raw_case_ids]
    if len(case_ids) > 500:
        raise ValidationError("A batch cannot contain more than 500 cases")
    actor = str(data.get("actor") or "dashboard").strip()
    if not actor:
        raise ValidationError("actor is required")

    existing = next(
        (batch for batch in repository.list_batches() if batch.name == batch_name),
        None,
    )
    if existing is not None:
        if tuple(case_ids) != existing.case_ids:
            raise ValidationError(f"Batch name {batch_name} is already in use")
        if int(existing.metadata.get("invoice_start", 1)) != invoice_start:
            raise ValidationError(
                f"Batch name {batch_name} already uses a different invoice_start"
            )
        return jsonify(
            {
                "ok": True,
                "message": f"Batch {batch_name} already exists",
                "batch": _batch_dict(existing, service, settings),
            }
        )

    cases = [service.get_case(case_id) for case_id in case_ids]
    not_ready = [
        case.id for case in cases if case.status is not CaseStatus.READY_FOR_ACCOUNTING
    ]
    if not_ready:
        raise ValidationError(
            "Only READY_FOR_ACCOUNTING cases can enter a batch: " + ", ".join(not_ready)
        )

    policy_results = _extension("accounting_policy_resolver").resolve_many(cases)
    resolved_cases = [result.case for result in policy_results]

    batch_date = datetime.strptime(batch_name[3:], "%d%m%y").date()
    output_path = None
    try:
        exporter = AccountingTemplateAdapter(
            {
                CaseType.DAMAGED.value: GlAccountPair(
                    settings.damaged_debit_gl,
                    settings.damaged_credit_gl,
                ),
                CaseType.LOST.value: GlAccountPair(
                    settings.lost_debit_gl,
                    settings.lost_credit_gl,
                ),
            },
            template_path=settings.accounting_template,
            invoice_start=invoice_start,
            org_id=settings.accounting_org_id,
        )
        output_name = (
            f"hachtoan_gop_{batch_date:%m.%Y}_{batch_name}"
            f"{exporter.output_suffix}"
        )
        output_path = settings.output_dir / output_name
        export_result = exporter.export(
            resolved_cases,
            output_path,
            batch_name=batch_name,
            invoice_date=batch_date,
            expected_total=sum(case.amount or 0 for case in cases),
        )
    except (AccountingExportError, OSError) as exc:
        raise ValidationError(f"Could not export accounting workbook: {exc}") from exc

    try:
        batch = service.finalize_batch(
            batch_name,
            case_ids,
            actor=actor,
            metadata={
                "output_name": output_name,
                "format": "accounting-template-v1",
                "template_suffix": exporter.output_suffix,
                "journal_line_count": export_result.journal_line_count,
                "invoice_start": invoice_start,
                "accounting_policy_keys": sorted(
                    {key for result in policy_results for key in result.policy_keys}
                ),
            },
        )
    except Exception:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        raise

    response = _batch_dict(batch, service, settings)
    return jsonify(
        {
            "ok": True,
            "message": f"Created {batch_name} with {len(cases)} cases",
            "batch": response,
        }
    ), 201


@blueprint.get("/api/batches/<batch_id>/download")
def download_batch(batch_id: str) -> Any:
    settings, repository, _ = _dependencies()
    batch = repository.get_batch(batch_id)
    if batch is None:
        raise ValidationError("Batch was not found")
    output_name = batch.metadata.get("output_name")
    if not isinstance(output_name, str) or output_name != output_name.replace("\\", "/").rsplit(
        "/", 1
    )[-1]:
        raise ValidationError("Batch output is not available")
    output_path = settings.output_dir / output_name
    resolved = output_path.resolve()
    if resolved.parent != settings.output_dir.resolve() or not resolved.is_file():
        raise ValidationError("Batch output is not available")
    return _private_download(resolved, download_name=resolved.name)
