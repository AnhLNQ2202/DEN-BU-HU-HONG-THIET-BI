# TranNNB, mail evidence, and PDF API

All routes except `/api/health` use the application's normal authentication.
Responses never contain configured server paths or client-side upload paths.
Generated workbooks, drafts, retained EML, and PDFs are operational data below
`ASSET_HUB_DATA_DIR`; they are ignored by Git.

## Capabilities

`GET /api/capabilities` returns the same capability booleans embedded in
`GET /api/dashboard`, plus data-minimized Tran reference status. Important keys:

- `raw_eml_retention` and `source_eml_download`;
- `tran_reference_upload`, `tran_lookup`, and `tran_workbook_export`;
- `tran_draft`;
- `mail_pdf_individual`, `mail_pdf_batch`, and `mail_pdf_backend`.

The PDF backend is `word-windows` when the Windows adapter is available and
otherwise `weasyprint-cloud` when the sandboxed cloud renderer and PDF verifier
are available. A disabled operation returns HTTP `503` with
`capability_available: false`; the server never silently substitutes another
output format.

## Reference workbooks

`GET /api/tran/references/status` reports whether FA&GL and optional CCDC are
configured and available, and whether each source is `uploaded` or
`configured`. It does not return a filename or path.

```http
POST /api/tran/references/upload
Content-Type: multipart/form-data
X-Asset-Hub-Upload: tran-reference-v1
```

The multipart request contains exactly one `fa_gl_file` and optionally one
`ccdc_file`. Both must be `.xlsx` and are limited to 50 MiB each. To retain the
currently uploaded CCDC, omit both CCDC fields. To remove it from the new
managed version, omit `ccdc_file` and send the exact text field
`clear_ccdc=true`.

Before activation, the service bounds ZIP entries, expansion and compression
ratio; rejects encryption, unsafe paths, duplicate entries, VBA, external
links, ActiveX, OLE and embedded objects; and validates the workbook through
the FA&GL/CCDC indexes. It then switches one atomic version pointer. A failed
replacement leaves the previous version active. Client filenames are never
used as storage names.

An administrator may instead configure read-only external files with
`ASSET_HUB_FA_GL_REFERENCE` and `ASSET_HUB_CCDC_REFERENCE`. A managed upload
takes precedence. CCDC is optional; FA&GL is required for resolution.

## Resolve and export

`POST /api/tran/resolve` accepts:

```json
{
  "assets": [
    {
      "tag_number": "MOU10001",
      "asset_name": "Synthetic mouse",
      "domain": "demo.user",
      "lost_date": "2026-08-14",
      "physical": true,
      "confirmed_cost": 1000000,
      "confirmed_start_date": "2025-01-01",
      "confirmed_group": "FOUR_YEAR",
      "confirmed_fee_rate": "5%",
      "classification_confirmed": true
    }
  ]
}
```

Confirmation fields are optional and only used by the fail-closed rules
documented in [TRAN_WORKFLOW.md](TRAN_WORKFLOW.md). The response contains each
resolution, its provenance notes/issues, `ready`, and an escaped
`mail_table_html` only when every item is ready. The request is read-only.

`POST /api/tran/workbooks` accepts the same `assets`, optional
`processing_date` (`YYYY-MM-DD`), and optional `year_sheet`. It creates a new
output from the approved external Tran template or the bundled clean template;
it never overwrites the source or an existing output. The `201` response
returns an opaque `output_id` and
`GET /api/tran/workbooks/<output_id>/download` URL.

## Raw EML and unsent drafts

Raw EML retention is opt-in with `ASSET_HUB_RETAIN_RAW_EML=true`. When enabled,
`POST /api/emails/upload` content-addresses every validated EML and attaches
only these values to each resulting case:

```json
{
  "source_eml": {
    "handle": "eml-sha256-<64 lowercase hex characters>",
    "filename": "safe_display_name.eml",
    "download_url": "/api/mail-artifacts/<handle>/download"
  }
}
```

Every case parsed from the same EML receives the same handle. The download
route verifies the content hash and returns `Cache-Control: private, no-store`.
Retention remains disabled by default, and the private directory is not even
created while disabled.

`POST /api/tran/drafts` accepts `assets`, `mail_artifact_handle`, a mandatory
operator-approved `body_intro`, and the optional workbook date/sheet fields.
It requires `ASSET_HUB_DRAFT_FROM_ADDRESS`. The response returns separate
workbook and `.eml` draft download URLs. The draft has `X-Unsent: 1`; the
product exposes no send endpoint and never starts Outlook.

## Individual and batch PDF

```json
POST /api/mail-pdfs/individual
{"mail_artifact_handle":"eml-sha256-..."}
```

The `201` response contains an opaque `output_id` and attachment download URL.

```json
POST /api/mail-pdfs/batches
{
  "mail_artifact_handles": ["eml-sha256-..."],
  "pages_per_mail": 2,
  "overflow_policy": "fail",
  "batch_name": "GN2140826"
}
```

A batch accepts 1–20 unique handles, 1–10 pages per message, and an explicit
overflow policy of `fail` or `warn`. The response contains a merged download
URL, individual item URLs, page counts and warnings. `warn` is the only mode
that can deliberately truncate overflow pages; the default `fail` mode never
drops a page. The optional batch must match `GN2ddmmyy`; when present the
attachment keeps the original `chungtu_<batch>.pdf` naming contract. Every
server destination remains new and opaque, so a repeated label cannot overwrite
an existing output.

## Accounting policy configuration

`POST /api/batches` also accepts optional `invoice_start` from 0 through
999999. Before the workbook is written, semantic parser policy keys are
resolved through deployment-owned settings. An unknown policy or unverified
damaged repair status fails with HTTP `400` before any output is published.

Safe demo fallbacks remain in source. Real mappings must be injected at
runtime; never commit them:

- `ASSET_HUB_PREPAYMENT_GL`;
- `ASSET_HUB_DAMAGED_REPAIR_CREDIT_GL`;
- `ASSET_HUB_DAMAGED_NO_REPAIR_CREDIT_GL`;
- `ASSET_HUB_LOST_DEPRECIATION_ASSET_GL_TEMPLATE`;
- `ASSET_HUB_LOST_DEPRECIATION_OTHER_GL_TEMPLATE`;
- `ASSET_HUB_LOST_RESPONSIBILITY_GL_TEMPLATE`.

Lost-account templates may use only `{cost_center}`, `{product_code}`, and
`{location}`. If a specialized setting is absent, the existing demo-safe
damaged/lost credit GL is used. If `ASSET_HUB_PREPAYMENT_GL` is absent, DAMAGED
and LOST debit GL settings must be identical.

## Disposable staging cleanup

The guarded `POST /api/test-data/clear` operation additionally clears only:

- hash-named retained EML artifacts;
- app-managed Tran reference versions;
- server-named Tran workbooks/drafts and mail-PDF outputs.

Unknown files, external configured references/templates, inbox files and
unreferenced legacy outputs are preserved. This endpoint remains disabled
unless `ASSET_HUB_ALLOW_TEST_RESET=true`.
