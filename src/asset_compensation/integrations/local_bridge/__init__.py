"""Downloadable, interactive bridge for Classic Outlook on Windows.

The bridge deliberately lives outside the server runtime.  It uses Outlook's
local COM automation only while the signed-in Windows user is running the GUI.
"""

from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
PACKAGE_PLACEHOLDER = "__ASSET_HUB_ORIGIN__"
DOWNLOAD_FILES = (
    "app.py",
    "core.py",
    "install.ps1",
    "start.cmd",
    "requirements.txt",
    "README.txt",
)

__all__ = ["DOWNLOAD_FILES", "PACKAGE_DIRECTORY", "PACKAGE_PLACEHOLDER"]

