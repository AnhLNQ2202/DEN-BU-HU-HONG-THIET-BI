"""Static Outlook Office.js add-in assets.

The Flask integration serves these files from ``/outlook-addin/`` and renders
``manifest.xml.template`` with the current public HTTPS origin.
"""

from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent

__all__ = ["PACKAGE_DIRECTORY"]
