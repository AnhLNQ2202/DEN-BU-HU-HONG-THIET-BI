# Mail evidence and PDF services

This module preserves the useful behavior of the original
`ghep_mail_pdf_word.py` tool while moving file retention, EML rendering, Word
automation, cloud rendering, page normalization and batch publication behind
explicit ports. The thin authenticated HTTP adapter is documented in
[TRAN_API.md](TRAN_API.md).

## Original-function mapping

| Original behavior | Product contract |
| --- | --- |
| Decode the mail subject and choose HTML/plain content | `prepare_eml_bytes()` / `prepare_eml_document()` return `PreparedEmlDocument` |
| Replace `cid:` images | Safe raster CID parts are embedded as data URIs; unavailable or unsafe parts produce warnings |
| Render the original HTML | HTML is allowlisted first: scripts, forms, event handlers, remote URLs, active SVG and untrusted CSS are removed |
| Detect the wide Asset Name/NOTE table | `PreparedEmlDocument.landscape` is true and the Word adapter applies landscape, narrow margins and table auto-fit |
| Convert one EML through Word | `WordPdfConverter.convert_eml()` uses one hidden private Word instance and never opens a visible window or sends mail |
| Convert one EML in Linux/Render | `WeasyPrintPdfConverter.convert_eml()` renders the same sanitized document with Pango; it allows embedded `data:` images only and cannot fetch HTTP(S) or local files |
| Force a fixed number of pages | `PypdfPageNormalizer.normalize()` pads short PDFs; overflow fails by default or returns an explicit truncation warning under `overflow_policy="warn"` |
| Write numbered per-mail PDFs | `MailPdfService.create_batch()` writes them below the batch's `individual/` directory |
| Merge the normalized PDFs | The same service writes `chungtu_<batch>.pdf`, preserving every page supplied to the merger |

The unsafe legacy behavior that silently dropped pages above `--pages` is not
carried forward. A caller must deliberately choose warning/truncation mode.

## Raw EML retention

Raw evidence is **not retained by default**. To make later PDF generation
possible, application composition may create an opt-in private store:

```python
from asset_compensation.services import MailArtifactStore

store = MailArtifactStore(
    settings.data_dir / "private-mail-artifacts",
    enabled=True,
)
```

Only pass `EmailPayload` values returned by `EmailUploadService.validate()` to
`retain_validated()`. The store:

- removes client paths and returns a safe display basename;
- addresses the stored EML by its full SHA-256 digest;
- returns an opaque `eml-sha256-...` retrieval handle;
- uses an app-managed private directory and best-effort `0700`/`0600` modes;
- writes atomically, deduplicates identical bytes and refuses integrity drift;
- never creates the storage directory while disabled.

`delete(handle)` removes exactly one selected artifact. `clear_managed()` is
reserved for an explicitly authorized disposable-test reset: it removes only
the store's exact two-hex-shard/full-SHA-256 `.eml` pattern, never follows
symlinks and leaves every unknown file or directory untouched. Neither cleanup
operation runs automatically.

Raw EML, generated PDFs and their handles are operational data. Keep the
storage root below the configured data directory, outside Git, and behind the
same authorization controls as case data.

## PDF capabilities

Cloud EML rendering, PDF validation, normalization and merge use the
cross-platform PDF extra:

```powershell
python -m pip install -e ".[pdf]"
```

Word rendering additionally requires Windows, Microsoft Word desktop and the
Windows extra:

```powershell
python -m pip install -e ".[windows]"
```

Linux cloud services such as Render cannot run Microsoft Word COM automation.
Use `WeasyPrintPdfConverter` there. The production Docker image installs the
Pango libraries listed by the official WeasyPrint Debian installation guide,
the pinned WeasyPrint 68 release and a DejaVu font with Vietnamese glyphs.
Selecting `WordPdfConverter` on Linux still raises `PdfCapabilityError`; there
is no silent backend switch with different layout.

The cloud adapter accepts the same `EmlPdfConverter` contract as the Windows
adapter. Its constructor defaults to a 25 MiB source limit, a 32 MiB generated
PDF limit, 100 rendered pages and 150 DPI embedded images. It renders to a
private temporary file, validates the result with `pypdf`, and atomically
publishes it without clobbering an existing destination. A content-derived PDF
identifier plus a serialized `SOURCE_DATE_EPOCH` scope keep font subsetting and
repeated output deterministic without changing the process environment after
each render. Its URL fetcher allows
only `data:` resources produced from safe CID raster images, disables redirects
and treats resource errors as failures. WeasyPrint does not execute JavaScript;
the shared preparation step also removes scripts, active tags, source CSS,
links, remote images and local-file references before rendering.

Word and WeasyPrint are different layout engines, so exact pagination can
differ. Use Word on an approved Windows worker when pixel-level legacy fidelity
is required; use WeasyPrint for the safe cloud preview and merge workflow.

References: [WeasyPrint 68 installation](https://doc.courtbouillon.org/weasyprint/v68.0/first_steps.html#installation)
and [WeasyPrint 68 URL fetcher/API](https://doc.courtbouillon.org/weasyprint/v68.0/api_reference.html).

## Service examples

Individual full-length PDF:

```python
from asset_compensation.adapters import WordPdfConverter
from asset_compensation.services import MailPdfService

pdfs = MailPdfService(store, WordPdfConverter())
path = pdfs.create_individual(artifact_handle, output_path)
```

The equivalent Render/Linux composition is:

```python
from asset_compensation.adapters.pdf import WeasyPrintPdfConverter
from asset_compensation.services import MailPdfService

pdfs = MailPdfService(store, WeasyPrintPdfConverter())
path = pdfs.create_individual(artifact_handle, output_path)
```

Fixed two-page individual PDFs plus one merged batch:

```python
result = pdfs.create_batch(
    artifact_handles,
    settings.output_dir / "mail-pdf-batches",
    batch_name="GN-DEMO-001",
    pages_per_mail=2,
    overflow_policy="fail",
)
```

Batch work is staged first and published only after all individual conversion,
normalization and merge steps succeed. Existing output files/directories are
never overwritten by these service methods.
