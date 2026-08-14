"""Thin HTTP routes for the dashboard and JSON API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from asset_compensation.adapters import (
    AccountingExportError,
    AccountingTemplateAdapter,
    GlAccountPair,
)
from asset_compensation.demo import seed_demo
from asset_compensation.domain import Case, CaseStatus, CaseType, ValidationError
from asset_compensation.parsers import SupplierLoadError, load_supplier_directory

blueprint = Blueprint("asset_hub", __name__)
_BATCH_NAME_RE = re.compile(r"GN2\d{6}")


def _dependencies() -> tuple[Any, Any, Any]:
    extension = current_app.extensions["asset_hub"]
    return extension["settings"], extension["repository"], extension["case_service"]


def _compensation_service() -> Any:
    return current_app.extensions["asset_hub"]["compensation_service"]


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
    settings, _, service = _dependencies()
    return jsonify(
        {
            "ok": True,
            "service": "asset-compensation-hub",
            "demo_mode": settings.demo_mode,
            "case_count": service.summary().total,
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
            "cases": [case.to_dict() for case in cases],
            "issues": issues,
            "batches": batches,
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
    return jsonify({"ok": True, "cases": [case.to_dict() for case in cases]})


@blueprint.get("/api/cases/<case_id>")
def case_detail(case_id: str) -> Any:
    _, _, service = _dependencies()
    case = service.get_case(case_id)
    history = [event.to_dict() for event in service.status_history(case_id)]
    return jsonify({"ok": True, "case": case.to_dict(), "history": history})


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
    return jsonify({"ok": True, "case": case.to_dict()})


@blueprint.post("/api/demo/reset")
def reset_demo() -> Any:
    settings, _, service = _dependencies()
    if not settings.demo_mode:
        raise ValidationError("Demo reset is disabled")
    cases = seed_demo(service)
    return jsonify({"ok": True, "cases": [case.to_dict() for case in cases]})


@blueprint.post("/api/ingest")
def ingest() -> Any:
    settings, _, service = _dependencies()
    from asset_compensation.services.ingestion_service import ingest_eml_directory

    try:
        supplier_directory = (
            load_supplier_directory(settings.supplier_file)
            if settings.supplier_file is not None
            else None
        )
    except (OSError, SupplierLoadError) as exc:
        raise ValidationError(f"Could not load supplier reference: {exc}") from exc
    result = ingest_eml_directory(
        service,
        settings.inbox_dir,
        supplier_directory=supplier_directory,
    )
    return jsonify(
        {
            "ok": True,
            "ingested": len(result.cases),
            "cases": [case.to_dict() for case in result.cases],
            "warnings": list(result.warnings),
            "unknown_files": list(result.unknown_files),
        }
    )


@blueprint.post("/api/batches")
def create_batch() -> Any:
    settings, repository, service = _dependencies()
    data = _json_body()
    batch_name = _batch_name(data.get("batch_name") or data.get("name"))
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
            org_id=settings.accounting_org_id,
        )
        output_name = (
            f"hachtoan_gop_{batch_date:%m.%Y}_{batch_name}"
            f"{exporter.output_suffix}"
        )
        output_path = settings.output_dir / output_name
        export_result = exporter.export(
            cases,
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
    return send_file(resolved, as_attachment=True, download_name=resolved.name)
