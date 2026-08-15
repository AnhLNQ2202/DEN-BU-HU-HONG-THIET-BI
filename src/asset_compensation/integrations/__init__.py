"""Optional integrations that run outside the web server."""

from .distribution import (
    ADDIN_PUBLIC_FILES,
    build_local_bridge_zip,
    canonical_public_origin,
    render_outlook_addin_manifest,
    resolve_outlook_addin_asset,
)

__all__ = [
    "ADDIN_PUBLIC_FILES",
    "build_local_bridge_zip",
    "canonical_public_origin",
    "render_outlook_addin_manifest",
    "resolve_outlook_addin_asset",
]
