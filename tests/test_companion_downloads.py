"""Distribution and hosting contracts for both Outlook companion options."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from asset_compensation.config import Settings
from asset_compensation.domain import ValidationError
from asset_compensation.integrations import (
    build_local_bridge_zip,
    canonical_public_origin,
    render_outlook_addin_manifest,
)
from asset_compensation.integrations.local_bridge import DOWNLOAD_FILES
from asset_compensation.web import create_app

_ORIGIN = "https://hub.example.test"
_BASIC = {
    "Authorization": "Basic "
    + base64.b64encode(b"demo-team:synthetic-secret").decode("ascii")
}


def _app(tmp_path: Path, *, public_origin: str | None = _ORIGIN):
    app = create_app(
        Settings(
            data_dir=tmp_path / "runtime",
            demo_mode=False,
            public_origin=public_origin,
            access_user="demo-team",
            access_password="synthetic-secret",
        )
    )
    app.config.update(TESTING=True)
    return app


@pytest.mark.parametrize(
    "value",
    [
        "http://hub.example.test",
        "https://user:password@hub.example.test",
        "https://hub.example.test/path",
        "https://hub.example.test?next=evil",
        "https://hub.example.test:8443",
        'https://hub.example.test\".evil.test',
    ],
)
def test_public_origin_rejects_untrusted_shapes(value: str) -> None:
    with pytest.raises(ValidationError):
        canonical_public_origin(value)


def test_public_origin_allows_https_and_local_development() -> None:
    assert canonical_public_origin(f"{_ORIGIN}/") == _ORIGIN
    assert canonical_public_origin("http://127.0.0.1:5000/") == "http://127.0.0.1:5000"
    with pytest.raises(ValidationError, match="manifest requires"):
        render_outlook_addin_manifest("http://127.0.0.1:5000")
    assert build_local_bridge_zip("http://127.0.0.1:5000")


def test_manifest_and_bridge_render_only_the_configured_origin() -> None:
    manifest = render_outlook_addin_manifest(_ORIGIN)
    assert b"__ASSET_HUB_ORIGIN__" not in manifest
    assert manifest.count(_ORIGIN.encode("ascii")) >= 8
    ElementTree.fromstring(manifest)

    first = build_local_bridge_zip(_ORIGIN)
    assert first == build_local_bridge_zip(_ORIGIN)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        expected = {
            f"asset-hub-outlook-bridge/{filename}" for filename in DOWNLOAD_FILES
        }
        assert set(archive.namelist()) == expected
        combined = b"\n".join(archive.read(name) for name in sorted(expected))
    assert b"__ASSET_HUB_ORIGIN__" not in combined
    assert _ORIGIN.encode("ascii") in combined
    assert b"__pycache__" not in combined
    assert b"site-packages" not in combined


def test_downloads_are_basic_gated_and_ignore_request_host(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        client = app.test_client()
        paths = (
            "/api/companion/downloads/outlook-addin-manifest.xml",
            "/api/companion/downloads/local-bridge.zip",
        )
        for path in paths:
            assert client.get(path, base_url="https://evil.example.test").status_code == 401

        manifest = client.get(
            paths[0],
            headers=_BASIC,
            base_url="https://evil.example.test",
        )
        assert manifest.status_code == 200
        assert manifest.mimetype == "application/xml"
        assert "attachment" in manifest.headers["Content-Disposition"]
        assert "private" in manifest.headers["Cache-Control"]
        assert "no-store" in manifest.headers["Cache-Control"]
        assert _ORIGIN.encode("ascii") in manifest.data
        assert b"evil.example.test" not in manifest.data

        bridge = client.get(paths[1], headers=_BASIC)
        assert bridge.status_code == 200
        assert bridge.mimetype == "application/zip"
        assert "attachment" in bridge.headers["Content-Disposition"]
        assert "no-store" in bridge.headers["Cache-Control"]
        with zipfile.ZipFile(io.BytesIO(bridge.data)) as archive:
            assert len(archive.namelist()) == len(DOWNLOAD_FILES)
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_only_exact_taskpane_assets_are_public_and_embeddable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        client = app.test_client()
        for filename in (
            "taskpane.html",
            "taskpane.css",
            "taskpane.js",
            "logo.png",
            "logo-16.png",
            "logo-32.png",
            "logo-64.png",
            "logo-80.png",
            "logo-128.png",
        ):
            response = client.get(f"/outlook-addin/{filename}")
            assert response.status_code == 200
            assert response.headers.get("X-Frame-Options") is None
            assert "https://appsforoffice.microsoft.com" in response.headers[
                "Content-Security-Policy"
            ]
            assert "frame-ancestors" in response.headers["Content-Security-Policy"]
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"

        denied = client.get("/outlook-addin/manifest.xml.template")
        assert denied.status_code == 401
        assert denied.headers["X-Frame-Options"] == "DENY"
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_remote_download_fails_closed_without_configured_origin(tmp_path: Path) -> None:
    app = _app(tmp_path, public_origin=None)
    try:
        response = app.test_client().get(
            "/api/companion/downloads/local-bridge.zip",
            headers=_BASIC,
            base_url="https://untrusted.example.test",
        )
        assert response.status_code == 400
        assert response.get_json()["ok"] is False
    finally:
        app.extensions["asset_hub"]["repository"].close()


def test_pairing_is_disabled_without_raw_mail_retention_but_downloads_remain(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    try:
        client = app.test_client()
        capabilities = client.get("/api/capabilities", headers=_BASIC).get_json()[
            "capabilities"
        ]
        assert capabilities["companion_pairing"] is False
        assert capabilities["outlook_addin"] is True
        assert capabilities["local_bridge"] is True

        pairing = client.post(
            "/api/companion/pairings",
            json={"role": "ngan", "client_type": "outlook_addin"},
            headers={**_BASIC, "X-Asset-Hub-Action": "companion-pair-v1"},
        )
        assert pairing.status_code == 503
        assert pairing.get_json()["capability_available"] is False
        assert (
            client.get(
                "/api/companion/downloads/outlook-addin-manifest.xml",
                headers=_BASIC,
            ).status_code
            == 200
        )
    finally:
        app.extensions["asset_hub"]["repository"].close()
