"""Flask application factory."""

from __future__ import annotations

import atexit
import secrets
from typing import Any

from flask import Flask, Response, jsonify, request

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
from asset_compensation.services import CaseService

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
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    )

    repository = SQLiteCaseRepository(settings.database_path)
    service = CaseService(repository)
    app.extensions["asset_hub"] = {
        "settings": settings,
        "repository": repository,
        "case_service": service,
    }
    atexit.register(repository.close)

    if settings.demo_mode and service.summary().total == 0:
        seed_demo(service)

    app.register_blueprint(blueprint)

    @app.before_request
    def require_shared_demo_access() -> Response | None:
        if not settings.access_user or request.path == "/api/health":
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

    @app.errorhandler(413)
    def too_large(_: Any) -> tuple[Any, int]:
        return _error("Upload exceeds the 25 MB limit", 413)

    @app.errorhandler(404)
    def route_not_found(exc: Any) -> tuple[Any, int] | Any:
        if request.path.startswith("/api/"):
            return _error("Endpoint not found", 404)
        return exc

    return app


def _error(error: object, status: int) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": str(error)}), status
