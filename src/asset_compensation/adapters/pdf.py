"""Optional PDF adapter interfaces with lazy Windows dependencies."""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from asset_compensation.parsers.eml import decode_mime_header


class PdfAdapterError(RuntimeError):
    """Base class for optional PDF adapter failures."""


class PdfDependencyError(PdfAdapterError):
    """Raised when an explicitly selected optional backend is unavailable."""


@runtime_checkable
class EmlPdfConverter(Protocol):
    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path: ...


@runtime_checkable
class PdfMerger(Protocol):
    def merge(
        self,
        sources: list[str | Path],
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path: ...


def _check_destination(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Destination already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def _commit_temp_file(temp_path: Path, destination: Path, *, overwrite: bool) -> None:
    """Commit a completed temp file without a check-then-overwrite race."""

    if overwrite:
        os.replace(temp_path, destination)
        return
    try:
        os.link(temp_path, destination)
    except FileExistsError as exc:
        raise FileExistsError(f"Destination already exists: {destination}") from exc
    except OSError:
        created = False
        try:
            with temp_path.open("rb") as source, destination.open("xb") as target:
                created = True
                shutil.copyfileobj(source, target)
        except FileExistsError as exc:
            raise FileExistsError(f"Destination already exists: {destination}") from exc
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise


def _plain_part(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h[1-6])>", "\n", content)
        content = re.sub(r"(?s)<[^>]+>", " ", content)
        content = html.unescape(content)
    return str(content)


def _safe_eml_html(source: Path) -> str:
    message = cast(
        EmailMessage,
        BytesParser(policy=policy.default).parsebytes(source.read_bytes()),
    )
    subject = decode_mime_header(str(message.get("Subject", "")))
    header_rows = [
        ("Subject", subject),
        ("From", str(message.get("From", ""))),
        ("To", str(message.get("To", ""))),
        ("Cc", str(message.get("Cc", ""))),
        ("Date", str(message.get("Date", ""))),
        ("Message-ID", str(message.get("Message-ID", ""))),
    ]
    headers = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in header_rows
        if value
    )
    body = html.escape(_plain_part(message))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:Arial,sans-serif;font-size:10pt;}"
        "table{border-collapse:collapse;width:100%;margin-bottom:12pt;}"
        "th{width:90pt;text-align:left;color:#444;}td,th{padding:2pt 4pt;}"
        "pre{white-space:pre-wrap;word-break:break-word;}</style></head><body>"
        f"<table>{headers}</table><pre>{body}</pre></body></html>"
    )


class WordPdfConverter:
    """Convert one EML with a private Word instance and no page truncation.

    ``pywin32`` is imported only when this adapter is called.  Importing the
    package and running the cross-platform test suite therefore never starts
    or requires Microsoft Word.
    """

    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path:
        if os.name != "nt":
            raise PdfDependencyError("Word PDF conversion is available only on Windows")
        try:
            import win32com.client as win32  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PdfDependencyError("Install the 'windows' extra to use Word PDF export") from exc

        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _check_destination(destination_path, overwrite)

        with tempfile.TemporaryDirectory(prefix="asset-eml-pdf-") as temp_dir:
            temp_root = Path(temp_dir)
            html_path = temp_root / "message.html"
            pdf_path = temp_root / "message.pdf"
            html_path.write_text(_safe_eml_html(source_path), encoding="utf-8")

            word = win32.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            document = None
            try:
                document = word.Documents.Open(
                    str(html_path), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False
                )
                document.ExportAsFixedFormat(OutputFileName=str(pdf_path), ExportFormat=17)
            except Exception as exc:
                raise PdfAdapterError(f"Word failed to export {source_path.name}: {exc}") from exc
            finally:
                if document is not None:
                    document.Close(False)
                word.Quit()

            with tempfile.NamedTemporaryFile(
                prefix=f".{destination_path.stem}.",
                suffix=".pdf",
                dir=destination_path.parent,
                delete=False,
            ) as temp_handle:
                temp_output = Path(temp_handle.name)
            try:
                shutil.copyfile(pdf_path, temp_output)
                _commit_temp_file(temp_output, destination_path, overwrite=overwrite)
            finally:
                temp_output.unlink(missing_ok=True)
        return destination_path


class PypdfMerger:
    """Merge every page from every input; no implicit page limit exists."""

    def merge(
        self,
        sources: list[str | Path],
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        if not sources:
            raise ValueError("At least one source PDF is required")
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError as exc:
            raise PdfDependencyError("Install the 'windows' extra to use PDF merging") from exc

        destination_path = Path(destination)
        _check_destination(destination_path, overwrite)
        writer = PdfWriter()
        for source in sources:
            source_path = Path(source)
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            reader = PdfReader(source_path)
            for page in reader.pages:
                writer.add_page(page)

        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.stem}.",
            suffix=".pdf",
            dir=destination_path.parent,
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
        try:
            with temp_path.open("wb") as stream:
                writer.write(stream)
            _commit_temp_file(temp_path, destination_path, overwrite=overwrite)
        finally:
            temp_path.unlink(missing_ok=True)
        return destination_path
