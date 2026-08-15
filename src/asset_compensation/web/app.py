"""Flask application factory."""

from __future__ import annotations

import atexit
import importlib.util
import os
import secrets
from threading import RLock
from typing import Any

from flask import Flask, Response, jsonify, request

from asset_compensation.adapters import PdfCapabilityError, WordPdfConverter
from asset_compensation.adapters.pdf import WeasyPrintPdfConverter
from asset_compensation.config import Settings
from asset_compensation.demo import seed_demo
from asset_compensation.domain import (
    AssetCompensationError,
    CaseNotFoundError,
    ConcurrencyError,
    InvalidStatusTransition,
    RepositoryError,
    ValidationError,
)
from asset_compensation.repositories import SQLiteCaseRepository
from asset_compensation.services import (
    DAMAGED_NO_REPAIR_POLICY,
    DAMAGED_REPAIR_POLICY,
    LOST_DEPRECIATION_ASSET_POLICY,
    LOST_DEPRECIATION_OTHER_POLICY,
    LOST_FALLBACK_POLICY,
    LOST_RESPONSIBILITY_POLICY,
    PREPAYMENT_POLICY,
    AccountingPolicyResolver,
    CaseService,
    CompensationService,
    M365ConnectionService,
    M365MailSyncService,
    M365OAuthConfig,
    M365OAuthStateError,
    M365OutlookDraftService,
    M365ProviderError,
    M365ReconnectRequired,
    M365ServiceError,
    M365UnavailableError,
    MailArtifactStore,
    MailPdfService,
    TestDataService,
    TranWorkflowService,
)
from asset_compensation.services.email_upload_service import EmailUploadService
from asset_compensation.services.supplier_upload_service import SupplierUploadService
from asset_compensation.services.tran_reference_upload_service import (
    TranReferenceUploadService,
)

from .routes import blueprint


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    if bool(settings.access_user) != bool(settings.access_password):
        raise RuntimeError(
            "ASSET_HUB_ACCESS_USER and ASSET_HUB_ACCESS_PASSWORD must be configured together"
        )
    settings.ensure_directories()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=settings.secret_key,
        JSON_SORT_KEYS=False,
        # Two 50 MiB Tran reference files plus multipart framing are the
        # largest accepted request; every upload service enforces tighter
        # per-file and aggregate limits before parsing.
        MAX_CONTENT_LENGTH=106 * 1024 * 1024,
    )

    repository = SQLiteCaseRepository(settings.database_path)
    service = CaseService(repository)
    supplier_upload_service = SupplierUploadService(settings.reference_dir)
    mail_artifact_store = MailArtifactStore(
        settings.mail_artifact_dir,
        enabled=settings.retain_raw_eml,
    )
    email_upload_service = EmailUploadService()
    m365_connection_service = M365ConnectionService(M365OAuthConfig.from_settings(settings))
    m365_mail_sync_service = M365MailSyncService(
        m365_connection_service,
        email_upload_service,
        service,
        mail_artifact_store,
        retain_raw_eml=settings.retain_raw_eml,
    )
    tran_reference_upload_service = TranReferenceUploadService(settings.reference_dir)
    if settings.prepayment_gl is None and settings.damaged_debit_gl != settings.lost_debit_gl:
        raise RuntimeError(
            "Configure ASSET_HUB_PREPAYMENT_GL when DAMAGED and LOST debit accounts differ"
        )
    prepayment_gl = settings.prepayment_gl or settings.damaged_debit_gl
    accounting_policy_resolver = AccountingPolicyResolver(
        {
            PREPAYMENT_POLICY: prepayment_gl,
            DAMAGED_REPAIR_POLICY: (
                settings.damaged_repair_credit_gl or settings.damaged_credit_gl
            ),
            DAMAGED_NO_REPAIR_POLICY: (
                settings.damaged_no_repair_credit_gl or settings.damaged_credit_gl
            ),
            LOST_DEPRECIATION_ASSET_POLICY: (
                settings.lost_depreciation_asset_gl_template or settings.lost_credit_gl
            ),
            LOST_DEPRECIATION_OTHER_POLICY: (
                settings.lost_depreciation_other_gl_template or settings.lost_credit_gl
            ),
            LOST_RESPONSIBILITY_POLICY: (
                settings.lost_responsibility_gl_template or settings.lost_credit_gl
            ),
            LOST_FALLBACK_POLICY: settings.lost_credit_gl,
        }
    )
    pypdf_available = importlib.util.find_spec("pypdf") is not None
    word_pdf_available = bool(os.name == "nt" and importlib.util.find_spec("win32com") is not None)
    try:
        cloud_pdf_available = bool(
            importlib.util.find_spec("weasyprint") is not None
            and pypdf_available
            and WeasyPrintPdfConverter.is_available()
        )
    except (OSError, PdfCapabilityError):
        cloud_pdf_available = False
    if word_pdf_available:
        pdf_converter = WordPdfConverter()
        pdf_backend = "word-windows"
    else:
        pdf_converter = WeasyPrintPdfConverter()
        pdf_backend = "weasyprint-cloud" if cloud_pdf_available else None
    mail_pdf_available = bool(settings.retain_raw_eml and pdf_backend)
    app.extensions["asset_hub"] = {
        "settings": settings,
        "repository": repository,
        "case_service": service,
        "compensation_service": CompensationService(),
        "accounting_policy_resolver": accounting_policy_resolver,
        "mutation_lock": RLock(),
        "supplier_upload_service": supplier_upload_service,
        "email_upload_service": email_upload_service,
        "mail_artifact_store": mail_artifact_store,
        "mail_pdf_service": MailPdfService(mail_artifact_store, pdf_converter),
        "mail_pdf_available": mail_pdf_available,
        "pdf_backend": pdf_backend,
        "word_pdf_available": word_pdf_available,
        "pypdf_available": pypdf_available,
        "tran_reference_upload_service": tran_reference_upload_service,
        "tran_workflow_service": TranWorkflowService(),
        "m365_connection_service": m365_connection_service,
        "m365_mail_sync_service": m365_mail_sync_service,
        "m365_outlook_draft_service": M365OutlookDraftService(m365_connection_service),
        "test_data_service": TestDataService(
            settings,
            repository,
            service,
            supplier_upload_service,
        ),
    }
    atexit.register(repository.close)

    if settings.demo_mode and service.summary().total == 0:
        seed_demo(service)

    app.register_blueprint(blueprint)

    @app.before_request
    def require_shared_demo_access() -> Response | None:
        # The Entra cross-site return cannot reliably carry cached HTTP Basic
        # credentials. This one callback remains guarded by its opaque session
        # cookie plus short-lived, one-time MSAL state; all other M365 routes
        # still require the shared staging gate when it is configured.
        if not settings.access_user or request.path in {
            "/api/health",
            "/api/m365/callback",
        }:
            return None
        auth = request.authorization
        valid = bool(
            auth
            and secrets.compare_digest(auth.username or "", settings.access_user)
            and secrets.compare_digest(auth.password or "", settings.access_password or "")
        )
        if valid:
            return None
        response = Response("Authentication required", status=401)
        response.headers["WWW-Authenticate"] = 'Basic realm="Asset Compensation Hub"'
        return response

    @app.after_request
    def security_headers(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'",
        )
        if request.path.startswith("/api/m365/") or request.path == ("/api/tran/outlook-drafts"):
            response.headers["Cache-Control"] = "private, no-store"
        if request.path == "/api/m365/callback":
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(CaseNotFoundError)
    def not_found(exc: CaseNotFoundError) -> tuple[Any, int]:
        return _error(exc, 404)

    @app.errorhandler(InvalidStatusTransition)
    @app.errorhandler(ConcurrencyError)
    def conflict(exc: AssetCompensationError) -> tuple[Any, int]:
        return _error(exc, 409)

    @app.errorhandler(ValidationError)
    def bad_request(exc: ValidationError) -> tuple[Any, int]:
        return _error(exc, 400)

    @app.errorhandler(RepositoryError)
    def repository_error(exc: RepositoryError) -> tuple[Any, int]:
        app.logger.exception("Repository operation failed")
        return _error(exc, 409)

    @app.errorhandler(M365UnavailableError)
    def m365_unavailable(exc: M365UnavailableError) -> tuple[Any, int]:
        return jsonify({"ok": False, "error": str(exc), "capability_available": False}), 503

    @app.errorhandler(M365ReconnectRequired)
    def m365_reconnect(exc: M365ReconnectRequired) -> tuple[Any, int]:
        return jsonify({"ok": False, "error": str(exc), "reconnect_required": True}), 401

    @app.errorhandler(M365OAuthStateError)
    def m365_oauth_error(exc: M365OAuthStateError) -> tuple[Any, int]:
        return _error(exc, 400)

    @app.errorhandler(M365ProviderError)
    def m365_provider_error(exc: M365ProviderError) -> tuple[Any, int]:
        return _error(exc, 502)

    @app.errorhandler(M365ServiceError)
    def m365_bad_request(exc: M365ServiceError) -> tuple[Any, int]:
        return _error(exc, 400)

    @app.errorhandler(413)
    def too_large(_: Any) -> tuple[Any, int]:
        return _error("Upload exceeds the server request-size limit", 413)

    @app.errorhandler(404)
    def route_not_found(exc: Any) -> tuple[Any, int] | Any:
        if request.path.startswith("/api/"):
            return _error("Endpoint not found", 404)
        return exc

    return app


def _error(error: object, status: int) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": str(error)}), status
