"""Render and package the two same-origin Outlook companion downloads."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from asset_compensation.domain import ValidationError

from .local_bridge import DOWNLOAD_FILES as BRIDGE_DOWNLOAD_FILES
from .local_bridge import PACKAGE_DIRECTORY as BRIDGE_DIRECTORY
from .local_bridge import PACKAGE_PLACEHOLDER
from .outlook_addin import PACKAGE_DIRECTORY as ADDIN_DIRECTORY

ADDIN_PUBLIC_FILES = frozenset(
    {
        "logo.png",
        "logo-16.png",
        "logo-32.png",
        "logo-64.png",
        "logo-80.png",
        "logo-128.png",
        "taskpane.css",
        "taskpane.html",
        "taskpane.js",
    }
)
_MAX_ASSET_BYTES = 512 * 1024
_MAX_PACKAGE_SOURCE_BYTES = 2 * 1024 * 1024
_BRIDGE_ARCHIVE_ROOT = "asset-hub-outlook-bridge"
_SAFE_NETLOC_RE = re.compile(r"[A-Za-z0-9.:[\]-]+")


def canonical_public_origin(value: str) -> str:
    """Return one safe same-origin base URL for generated client packages."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(character) < 32 for character in raw):
        raise ValidationError("The public Product address is invalid")
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").casefold()
    local_host = hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme not in ({"http", "https"} if local_host else {"https"}):
        raise ValidationError("Outlook companion downloads require an HTTPS Product address")
    if (
        not hostname
        or not _SAFE_NETLOC_RE.fullmatch(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValidationError("The public Product address is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("The public Product address has an invalid port") from exc
    if port is not None and not local_host:
        raise ValidationError("The public Product address cannot use a custom port")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def render_outlook_addin_manifest(origin: str) -> bytes:
    """Render the installable manifest without accepting arbitrary template paths."""

    safe_origin = canonical_public_origin(origin)
    if urlsplit(safe_origin).scheme != "https":
        raise ValidationError("The Outlook Add-in manifest requires a public HTTPS address")
    template = _read_bounded(ADDIN_DIRECTORY / "manifest.xml.template")
    try:
        text = template.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - committed asset invariant
        raise ValidationError("The Outlook Add-in manifest is unavailable") from exc
    rendered = text.replace("__ASSET_HUB_ORIGIN__", safe_origin)
    if "__ASSET_HUB_ORIGIN__" in rendered or safe_origin not in rendered:
        raise ValidationError("The Outlook Add-in manifest is unavailable")
    return rendered.encode("utf-8")


def resolve_outlook_addin_asset(filename: str) -> Path:
    """Resolve one exact public task-pane asset inside the packaged directory."""

    if filename not in ADDIN_PUBLIC_FILES:
        raise ValidationError("Outlook Add-in asset is unavailable")
    root = ADDIN_DIRECTORY.resolve()
    candidate = ADDIN_DIRECTORY / filename
    resolved = candidate.resolve()
    if (
        resolved.parent != root
        or candidate.is_symlink()
        or not resolved.is_file()
        or resolved.stat().st_size < 1
        or resolved.stat().st_size > _MAX_ASSET_BYTES
    ):
        raise ValidationError("Outlook Add-in asset is unavailable")
    return resolved


def build_local_bridge_zip(origin: str) -> bytes:
    """Return a deterministic ZIP whose GUI is pinned to this Product origin."""

    safe_origin = canonical_public_origin(origin)
    source_total = 0
    rendered_files: list[tuple[str, bytes]] = []
    root = BRIDGE_DIRECTORY.resolve()
    for filename in BRIDGE_DOWNLOAD_FILES:
        candidate = BRIDGE_DIRECTORY / filename
        resolved = candidate.resolve()
        if resolved.parent != root or candidate.is_symlink() or not resolved.is_file():
            raise ValidationError("The Local Bridge download is unavailable")
        payload = _read_bounded(resolved)
        source_total += len(payload)
        if source_total > _MAX_PACKAGE_SOURCE_BYTES:
            raise ValidationError("The Local Bridge download exceeds the safe size limit")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - committed asset invariant
            raise ValidationError("The Local Bridge package contains an invalid text file") from exc
        rendered = text.replace(PACKAGE_PLACEHOLDER, safe_origin)
        if PACKAGE_PLACEHOLDER in rendered:
            raise ValidationError("The Local Bridge package could not be configured")
        rendered_files.append((filename, rendered.encode("utf-8")))

    archive = io.BytesIO()
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as bundle:
        for filename, payload in rendered_files:
            info = zipfile.ZipInfo(f"{_BRIDGE_ARCHIVE_ROOT}/{filename}")
            info.date_time = (2024, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            bundle.writestr(info, payload)
    return archive.getvalue()


def _read_bounded(path: Path) -> bytes:
    size = path.stat().st_size
    if size < 1 or size > _MAX_ASSET_BYTES:
        raise ValidationError("A packaged Outlook companion asset is unavailable")
    return path.read_bytes()
