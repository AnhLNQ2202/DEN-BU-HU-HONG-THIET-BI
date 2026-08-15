"""Safe, optional adapters for rendering EML evidence and composing PDFs.

The module deliberately imports Word COM, WeasyPrint and pypdf only when their
concrete adapters are called. Importing the application therefore never
requires Microsoft Office or an optional PDF backend.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol, cast, runtime_checkable

PdfOverflowPolicy = Literal["fail", "warn"]

_SAFE_INLINE_IMAGE_TYPES = frozenset(
    {"image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp"}
)
_SAFE_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "col",
        "colgroup",
        "dd",
        "div",
        "dl",
        "dt",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
_DROP_WITH_CONTENT_TAGS = frozenset(
    {
        "applet",
        "audio",
        "button",
        "canvas",
        "form",
        "iframe",
        "math",
        "noscript",
        "object",
        "option",
        "script",
        "select",
        "style",
        "svg",
        "textarea",
        "video",
    }
)
_VOID_TAGS = frozenset({"br", "col", "hr", "img"})
_WIDE_TABLE_ASSET_RE = re.compile(r"asset\s*name", re.IGNORECASE)
_WIDE_TABLE_NOTE_RE = re.compile(r"\bnote\b", re.IGNORECASE)
_CID_SOURCE_RE = re.compile(r"^cid:(?P<cid>.+)$", re.IGNORECASE)
_SAFE_DIMENSION_RE = re.compile(r"^(?:\d{1,4}|100%)$")
_SAFE_SPAN_RE = re.compile(r"^\d{1,3}$")
_CLOUD_RENDER_LOCK = Lock()


class PdfAdapterError(RuntimeError):
    """Base class for optional PDF adapter failures."""


class PdfDependencyError(PdfAdapterError):
    """Raised when an explicitly selected optional backend is unavailable."""


class PdfCapabilityError(PdfDependencyError):
    """Raised when the host cannot provide an explicitly requested backend."""


class PdfPageOverflowError(PdfAdapterError):
    """Raised instead of silently discarding pages during normalization."""

    def __init__(self, *, source_pages: int, target_pages: int) -> None:
        self.source_pages = source_pages
        self.target_pages = target_pages
        super().__init__(
            f"PDF has {source_pages} pages, exceeding the {target_pages}-page target; "
            "no pages were discarded"
        )


@dataclass(frozen=True, slots=True)
class PreparedEmlDocument:
    """Sanitized representation passed to a document renderer."""

    subject: str
    html: str
    landscape: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PdfNormalizationResult:
    """Auditable outcome of fixed-page normalization."""

    path: Path
    source_pages: int
    output_pages: int
    padded_pages: int
    truncated_pages: int
    warnings: tuple[str, ...] = ()


@runtime_checkable
class EmlPdfConverter(Protocol):
    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path: ...


@runtime_checkable
class PdfPageNormalizer(Protocol):
    def normalize(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        target_pages: int,
        overflow_policy: PdfOverflowPolicy = "fail",
        overwrite: bool = False,
    ) -> PdfNormalizationResult: ...


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


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
    except (AttributeError, LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return content if isinstance(content, str) else ""


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _message_bodies(message: Message) -> tuple[str | None, str | None]:
    html_parts: list[str] = []
    plain_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type == "text/html":
            html_parts.append(_part_text(part))
        elif content_type == "text/plain":
            plain_parts.append(_part_text(part))
    return (
        max(html_parts, key=len) if html_parts else None,
        max(plain_parts, key=len) if plain_parts else None,
    )


def _inline_images(message: Message) -> tuple[dict[str, str], tuple[str, ...]]:
    images: dict[str, str] = {}
    warnings: list[str] = []
    for part in message.walk():
        raw_cid = str(part.get("Content-ID", "")).strip().strip("<>")
        if not raw_cid:
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in _SAFE_INLINE_IMAGE_TYPES:
            warnings.append("an inline image was omitted because its type is unsafe")
            images[raw_cid] = ""
            images[raw_cid.casefold()] = ""
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            warnings.append("an inline image could not be decoded")
            images[raw_cid] = ""
            images[raw_cid.casefold()] = ""
            continue
        data_uri = f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"
        images[raw_cid] = data_uri
        images[raw_cid.casefold()] = data_uri
    return images, tuple(warnings)


class _EmailHtmlSanitizer(HTMLParser):
    """Allowlist sanitizer that prevents Word from loading active/remote content."""

    def __init__(self, inline_images: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self._inline_images = inline_images
        self._output: list[str] = []
        self._drop_depth = 0
        self.unresolved_cids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if self._drop_depth:
            if name in _DROP_WITH_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if name in _DROP_WITH_CONTENT_TAGS:
            self._drop_depth = 1
            return
        if name not in _SAFE_HTML_TAGS:
            return
        safe_attrs = self._safe_attributes(name, attrs)
        if name == "img" and not any(key == "src" for key, _ in safe_attrs):
            return
        rendered_attrs = "".join(
            f' {key}="{html.escape(value, quote=True)}"' for key, value in safe_attrs
        )
        self._output.append(f"<{name}{rendered_attrs}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        name = tag.casefold()
        if not self._drop_depth and name in _SAFE_HTML_TAGS and name not in _VOID_TAGS:
            self._output.append(f"</{name}>")

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if self._drop_depth:
            if name in _DROP_WITH_CONTENT_TAGS:
                self._drop_depth -= 1
            return
        if name in _SAFE_HTML_TAGS and name not in _VOID_TAGS:
            self._output.append(f"</{name}>")

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self._output.append(html.escape(data))

    def _safe_attributes(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            value = (raw_value or "").strip()
            if name.startswith("on") or name in {"style", "class", "id", "href"}:
                continue
            if tag == "img" and name == "src":
                cid_match = _CID_SOURCE_RE.fullmatch(html.unescape(value))
                if not cid_match:
                    continue
                cid = cid_match.group("cid").strip().strip("<>")
                known_cid = cid if cid in self._inline_images else cid.casefold()
                data_uri = self._inline_images.get(known_cid)
                if data_uri:
                    result.append(("src", data_uri))
                elif known_cid not in self._inline_images:
                    self.unresolved_cids.add(cid)
                continue
            if tag == "img" and name in {"alt", "title"}:
                result.append((name, value[:500]))
                continue
            if tag in {"img", "table", "td", "th", "col"} and name in {
                "height",
                "width",
            }:
                if _SAFE_DIMENSION_RE.fullmatch(value):
                    result.append((name, value))
                continue
            if tag in {"td", "th"} and name in {"colspan", "rowspan"}:
                if _SAFE_SPAN_RE.fullmatch(value):
                    result.append((name, value))
                continue
            if (
                tag == "table"
                and name in {"border", "cellpadding", "cellspacing"}
                and _SAFE_SPAN_RE.fullmatch(value)
            ):
                result.append((name, value))
        return result

    def html(self) -> str:
        return "".join(self._output)


def _is_wide_table(html_body: str | None) -> bool:
    return bool(
        html_body
        and _WIDE_TABLE_ASSET_RE.search(html_body)
        and _WIDE_TABLE_NOTE_RE.search(html_body)
    )


def prepare_eml_bytes(data: bytes) -> PreparedEmlDocument:
    """Parse EML bytes into sanitized HTML without executing or fetching content."""

    try:
        message = cast(EmailMessage, BytesParser(policy=policy.default).parsebytes(data))
    except Exception as exc:
        raise PdfAdapterError("The EML cannot be parsed for PDF rendering") from exc

    subject = _decode_mime_header(str(message.get("Subject", "")))
    html_body, plain_body = _message_bodies(message)
    landscape = _is_wide_table(html_body)
    inline_images, image_warnings = _inline_images(message)
    warnings = list(image_warnings)

    if html_body:
        sanitizer = _EmailHtmlSanitizer(inline_images)
        sanitizer.feed(html_body)
        sanitizer.close()
        body = sanitizer.html()
        if sanitizer.unresolved_cids:
            warnings.append(
                f"{len(sanitizer.unresolved_cids)} inline image reference(s) were unavailable"
            )
    else:
        body = f"<pre>{html.escape(plain_body or '(no message body)')}</pre>"

    header_rows = [
        ("Subject", subject),
        ("From", _decode_mime_header(str(message.get("From", "")))),
        ("To", _decode_mime_header(str(message.get("To", "")))),
        ("Cc", _decode_mime_header(str(message.get("Cc", "")))),
        ("Date", str(message.get("Date", ""))),
        ("Message-ID", str(message.get("Message-ID", ""))),
    ]
    headers = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in header_rows
        if value
    )
    wide_css = (
        "table.mail-body-table{width:100% !important;table-layout:fixed !important;}"
        "body.landscape table:not(.mail-headers){width:100% !important;"
        "table-layout:fixed !important;font-size:8pt;}"
        "body.landscape td,body.landscape th{word-break:break-word;"
        "overflow-wrap:break-word;white-space:normal !important;}"
    )
    page_class = "landscape" if landscape else "portrait"
    document = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:Arial,sans-serif;font-size:10pt;}"
        "table{border-collapse:collapse;}"
        "table.mail-headers{width:100%;margin-bottom:12pt;}"
        "table.mail-headers th{width:90pt;text-align:left;color:#444;}"
        "td,th{padding:2pt 4pt;}img{max-width:100%;height:auto;}"
        "pre{white-space:pre-wrap;word-break:break-word;}"
        f"{wide_css}</style></head>"
        f"<body class='{page_class}'>"
        f"<table class='mail-headers'>{headers}</table>{body}</body></html>"
    )
    return PreparedEmlDocument(
        subject=subject,
        html=document,
        landscape=landscape,
        warnings=tuple(warnings),
    )


def prepare_eml_document(source: str | Path) -> PreparedEmlDocument:
    """Prepare a local EML for rendering; no external resource is opened."""

    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    return prepare_eml_bytes(source_path.read_bytes())


def _safe_eml_html(source: Path) -> str:
    """Backward-compatible internal wrapper used by older adapter callers."""

    return prepare_eml_document(source).html


def _start_word_application() -> object:
    if os.name != "nt":
        raise PdfCapabilityError(
            "EML-to-PDF requires Microsoft Word desktop on Windows; "
            "this cloud/server host does not provide that capability"
        )
    try:
        import win32com.client as win32  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PdfCapabilityError(
            "EML-to-PDF requires Microsoft Word and the optional pywin32 package"
        ) from exc
    try:
        return win32.DispatchEx("Word.Application")
    except Exception as exc:
        raise PdfCapabilityError(
            "Microsoft Word desktop automation is unavailable on this host"
        ) from exc


class WordPdfConverter:
    """Convert one EML with a private, hidden Word instance.

    The prepared HTML contains no scripts, forms, remote resources or unsafe
    inline image formats. This adapter never opens a visible window and never
    sends mail.
    """

    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path:
        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _check_destination(destination_path, overwrite)
        prepared = prepare_eml_document(source_path)

        with tempfile.TemporaryDirectory(prefix="asset-eml-pdf-") as temp_dir:
            temp_root = Path(temp_dir)
            html_path = temp_root / "message.html"
            pdf_path = temp_root / "message.pdf"
            html_path.write_text(prepared.html, encoding="utf-8")

            word = _start_word_application()
            document = None
            try:
                word.Visible = False  # type: ignore[attr-defined]
                word.DisplayAlerts = 0  # type: ignore[attr-defined]
                document = word.Documents.Open(  # type: ignore[attr-defined]
                    str(html_path),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                )
                if prepared.landscape:
                    document.PageSetup.Orientation = 1
                    document.PageSetup.LeftMargin = 18
                    document.PageSetup.RightMargin = 18
                    document.PageSetup.TopMargin = 24
                    document.PageSetup.BottomMargin = 24
                    for table in document.Tables:
                        table.AutoFitBehavior(2)
                document.ExportAsFixedFormat(OutputFileName=str(pdf_path), ExportFormat=17)
            except PdfAdapterError:
                raise
            except Exception as exc:
                raise PdfAdapterError("Word could not export the EML evidence") from exc
            finally:
                if document is not None:
                    with suppress(Exception):
                        document.Close(False)
                with suppress(Exception):
                    word.Quit()  # type: ignore[attr-defined]

            if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                raise PdfAdapterError("Word did not produce a PDF output")
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


def _load_weasyprint() -> tuple[type[object], type[object]]:
    """Load the pinned cloud renderer without making it a core dependency."""

    try:
        from weasyprint import HTML
        from weasyprint.urls import URLFetcher
    except (ImportError, OSError) as exc:
        raise PdfCapabilityError(
            "Cloud EML-to-PDF requires the 'pdf' extra and the native Pango "
            "libraries documented for WeasyPrint"
        ) from exc
    return HTML, URLFetcher


@contextmanager
def _deterministic_cloud_render() -> Iterator[None]:
    """Serialize font subsetting and give FontTools a stable timestamp."""

    with _CLOUD_RENDER_LOCK:
        previous = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "0"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = previous


class WeasyPrintPdfConverter:
    """Render a sanitized EML to PDF on Linux/cloud hosts.

    Only ``data:`` resources embedded by :func:`prepare_eml_bytes` are
    available to the renderer. HTTP(S), local files and every other scheme are
    denied by the renderer itself. The limits bound the input, page count and
    published output; application upload limits should normally be lower.
    """

    @staticmethod
    def is_available() -> bool:
        """Return whether both the renderer and PDF verifier can be loaded."""

        try:
            _load_weasyprint()
            _load_pypdf()
        except PdfDependencyError:
            return False
        return True

    def __init__(
        self,
        *,
        max_source_bytes: int = 25 * 1024 * 1024,
        max_output_bytes: int = 32 * 1024 * 1024,
        max_pages: int = 100,
        image_dpi: int = 150,
    ) -> None:
        for name, value in (
            ("max_source_bytes", max_source_bytes),
            ("max_output_bytes", max_output_bytes),
            ("max_pages", max_pages),
            ("image_dpi", image_dpi),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        self._max_source_bytes = max_source_bytes
        self._max_output_bytes = max_output_bytes
        self._max_pages = max_pages
        self._image_dpi = image_dpi

    def convert_eml(
        self, source: str | Path, destination: str | Path, *, overwrite: bool = False
    ) -> Path:
        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _check_destination(destination_path, overwrite)

        with source_path.open("rb") as source_stream:
            source_bytes = source_stream.read(self._max_source_bytes + 1)
        if len(source_bytes) > self._max_source_bytes:
            raise PdfAdapterError(
                f"EML exceeds the {self._max_source_bytes}-byte cloud rendering limit"
            )

        prepared = prepare_eml_bytes(source_bytes)
        page_size = "A4 landscape" if prepared.landscape else "A4 portrait"
        page_margin = "10mm" if prepared.landscape else "12mm"
        renderer_css = (
            "<style>"
            f"@page{{size:{page_size};margin:{page_margin};}}"
            'body{font-family:"DejaVu Sans",Arial,sans-serif;}'
            "</style>"
        )
        rendered_html = prepared.html.replace(
            "</head>", f"{renderer_css}</head>", 1
        )
        html_type, fetcher_type = _load_weasyprint()
        fetcher = fetcher_type(
            allowed_protocols={"data"},
            allow_redirects=False,
            fail_on_errors=True,
        )
        pdf_identifier = hashlib.sha256(rendered_html.encode("utf-8")).digest()

        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.stem}.",
            suffix=".pdf",
            dir=destination_path.parent,
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
        try:
            try:
                with _deterministic_cloud_render():
                    html_document = html_type(string=rendered_html, url_fetcher=fetcher)
                    rendered_document = html_document.render()  # type: ignore[attr-defined]
                    page_count = len(rendered_document.pages)  # type: ignore[attr-defined]
                    if page_count < 1:
                        raise PdfAdapterError("Cloud renderer produced a PDF with no pages")
                    if page_count > self._max_pages:
                        raise PdfAdapterError(
                            f"Rendered PDF has {page_count} pages, exceeding the "
                            f"{self._max_pages}-page cloud rendering limit"
                        )
                    rendered_document.write_pdf(  # type: ignore[attr-defined]
                        temp_path,
                        pdf_identifier=pdf_identifier,
                        optimize_images=True,
                        dpi=self._image_dpi,
                    )
            except PdfAdapterError:
                raise
            except Exception as exc:
                raise PdfAdapterError(
                    "Cloud renderer could not export the EML evidence"
                ) from exc

            output_size = temp_path.stat().st_size
            if output_size < 1:
                raise PdfAdapterError("Cloud renderer did not produce a PDF output")
            if output_size > self._max_output_bytes:
                raise PdfAdapterError(
                    f"Rendered PDF exceeds the {self._max_output_bytes}-byte output limit"
                )

            reader_type, _ = _load_pypdf()
            try:
                with temp_path.open("rb") as pdf_stream:
                    verified_pages = len(reader_type(pdf_stream).pages)  # type: ignore[attr-defined]
            except Exception as exc:
                raise PdfAdapterError("Cloud renderer produced an invalid PDF") from exc
            if verified_pages != page_count:
                raise PdfAdapterError(
                    "Cloud renderer PDF verification returned an inconsistent page count"
                )

            _commit_temp_file(temp_path, destination_path, overwrite=overwrite)
        finally:
            temp_path.unlink(missing_ok=True)
        return destination_path


def _load_pypdf() -> tuple[type[object], type[object]]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise PdfDependencyError("Install the 'pdf' or 'windows' extra to process PDFs") from exc
    return PdfReader, PdfWriter


class PypdfPageNormalizer:
    """Pad or explicitly truncate a PDF to an exact number of pages."""

    def normalize(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        target_pages: int,
        overflow_policy: PdfOverflowPolicy = "fail",
        overwrite: bool = False,
    ) -> PdfNormalizationResult:
        if target_pages < 1:
            raise ValueError("target_pages must be at least 1")
        if overflow_policy not in {"fail", "warn"}:
            raise ValueError("overflow_policy must be 'fail' or 'warn'")
        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        _check_destination(destination_path, overwrite)
        reader_type, writer_type = _load_pypdf()
        writer = writer_type()
        warnings: list[str] = []
        padded_pages = 0
        truncated_pages = 0

        with source_path.open("rb") as source_stream:
            reader = reader_type(source_stream)
            source_pages = len(reader.pages)  # type: ignore[attr-defined]
            if source_pages > target_pages and overflow_policy == "fail":
                raise PdfPageOverflowError(
                    source_pages=source_pages, target_pages=target_pages
                )
            take = min(source_pages, target_pages)
            for index in range(take):
                writer.add_page(reader.pages[index])  # type: ignore[attr-defined]
            if source_pages > target_pages:
                truncated_pages = source_pages - target_pages
                warnings.append(
                    f"{truncated_pages} overflow page(s) were explicitly omitted "
                    f"from a {source_pages}-page PDF"
                )
            elif source_pages < target_pages:
                padded_pages = target_pages - source_pages
                if source_pages:
                    last_page = reader.pages[source_pages - 1]  # type: ignore[attr-defined]
                    width = float(last_page.mediabox.width)
                    height = float(last_page.mediabox.height)
                else:
                    width, height = 595.28, 841.89
                for _ in range(padded_pages):
                    writer.add_blank_page(width=width, height=height)

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
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

        try:
            _commit_temp_file(temp_path, destination_path, overwrite=overwrite)
        finally:
            temp_path.unlink(missing_ok=True)
        return PdfNormalizationResult(
            path=destination_path,
            source_pages=source_pages,
            output_pages=target_pages,
            padded_pages=padded_pages,
            truncated_pages=truncated_pages,
            warnings=tuple(warnings),
        )


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
        reader_type, writer_type = _load_pypdf()
        destination_path = Path(destination)
        _check_destination(destination_path, overwrite)
        writer = writer_type()

        with ExitStack() as stack:
            for source in sources:
                source_path = Path(source)
                if not source_path.is_file():
                    raise FileNotFoundError(source_path)
                stream = stack.enter_context(source_path.open("rb"))
                reader = reader_type(stream)
                for page in reader.pages:  # type: ignore[attr-defined]
                    writer.add_page(page)

            with tempfile.NamedTemporaryFile(
                prefix=f".{destination_path.stem}.",
                suffix=".pdf",
                dir=destination_path.parent,
                delete=False,
            ) as temp_handle:
                temp_path = Path(temp_handle.name)
            try:
                with temp_path.open("wb") as output_stream:
                    writer.write(output_stream)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

        try:
            _commit_temp_file(temp_path, destination_path, overwrite=overwrite)
        finally:
            temp_path.unlink(missing_ok=True)
        return destination_path
