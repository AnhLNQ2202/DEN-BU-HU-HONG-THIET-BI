# Supplier and email upload API

Both upload routes require the application's normal authentication and a
route-specific request header. The custom header prevents a cross-site HTML
form from submitting files with a browser-cached Basic Auth session.

## Upload the Supplier pair

```http
POST /api/suppliers/upload
Content-Type: multipart/form-data
X-Asset-Hub-Upload: supplier-v1
```

The multipart body must contain exactly one `active_file` and one
`inactive_file`, with no extra file or text fields.

- Accepted extensions: `.csv`, `.xlsx`, and Oracle BI Publisher HTML exports
  named `.xls`.
- Each file is limited to 20 MiB; combined file bytes are limited to 48 MiB.
- CSV must be UTF-8. XLSX archives cannot be encrypted or contain VBA,
  embedded objects, external links, unsafe paths, excessive expansion, more
  than 32 columns, 20,000 data rows, or 640,000 cells.
- A legacy `.xls` must contain the Oracle BI Publisher signature and valid
  UTF-8 HTML. Binary/OLE `.xls` files are rejected.
- A real `Domain` column is used when present. For the legacy Oracle report,
  domain is derived only from the final `-domain` suffix of `Supplier Name`.
  `Employee Number` is an identity check, never a replacement for domain.

The response reports record counts, changes, warnings, and every ambiguous
normalized domain. Ambiguous domains are excluded from the active lookup; they
are not resolved by row order or Active/Inactive priority.

```json
{
  "ok": true,
  "message": "Supplier directory uploaded and activated",
  "active_count": 120,
  "inactive_count": 12,
  "total_count": 130,
  "collision_count": 1,
  "review_required": true,
  "collisions": [
    {
      "domain": "synthetic.user",
      "supplier_numbers": ["000101", "000202"],
      "supplier_sites": ["01", "02"],
      "message": "Domain maps to conflicting Supplier Number/Employee Number/Site/name/status identities"
    }
  ],
  "warnings": []
}
```

`GET /api/suppliers/status` returns the active version's status. Raw uploads
are private temporary files and are deleted before activation. Only a
data-minimized normalized directory and collision metadata remain below
`ASSET_HUB_DATA_DIR/reference`. Activation uses an atomic version pointer, so
a failed replacement leaves the previous directory available. The next email
ingestion request sees the new version without restarting the process.

## Upload and ingest EML files

```http
POST /api/emails/upload
Content-Type: multipart/form-data
X-Asset-Hub-Upload: email-v1
```

Use the repeated multipart field `files`. The request accepts 1–20 `.eml`
files, at most 2 MiB each and 25 MiB in aggregate. Files must have bounded RFC
822 headers and a bounded MIME tree. Text and inline images are accepted;
archives and other attachments are rejected. Raw EML bytes are not copied into
application data or intentionally retained; the HTTP request layer may use
temporary spooling which is closed after the request.

Uploaded source names are replaced with server-generated names such as
`upload-01.eml`. Raw Subject, Sender, and Message-ID headers are not persisted
for this upload path. The service keeps hashes of Message-ID and content so a
reused Message-ID with different content is sent to manual review instead of
silently overwriting a case.

```json
{
  "ok": true,
  "message": "Ingested 2 of 3 uploaded emails",
  "received_count": 3,
  "ingested": 2,
  "case_ids": ["DMG-...", "LOST-..."],
  "case_types": {"DAMAGED": 1, "LOST": 1},
  "cases": [
    {"id": "DMG-...", "case_type": "DAMAGED"},
    {"id": "LOST-...", "case_type": "LOST"}
  ],
  "warnings": [],
  "unknown_files": ["upload-03.eml"]
}
```

Valid emails are persisted in one repository transaction. LOST-case business
metadata parsed from the message—including usage start, loss date, and
original value—is preserved. Missing values remain missing and must be
reviewed; the upload flow does not invent accounting inputs.

## Clear disposable staging data

When `ASSET_HUB_ALLOW_TEST_RESET=true`, the dashboard exposes a guarded test
cleanup action. It sends `POST /api/test-data/clear` with header
`X-Asset-Hub-Action: clear-test-data-v1` and the exact JSON body
`{"confirm":"CLEAR_TEST_DATA"}`. The action removes application cases, status
events, batches, their named output files, and the uploaded normalized Supplier
reference. It is disabled by default and must not be enabled as a general
production deletion API.

## Clear disposable test data

This capability is disabled by default. Set
`ASSET_HUB_ALLOW_TEST_RESET=true` only on a disposable staging deployment.
When enabled, `GET /api/dashboard` reports `capabilities.test_reset: true`.

```http
POST /api/test-data/clear
Content-Type: application/json
X-Asset-Hub-Action: clear-test-data-v1

{"confirm":"CLEAR_TEST_DATA"}
```

The header and JSON object must match exactly. The operation clears repository
cases, status events and batches; generated output files named by those batch
records; and app-managed normalized Supplier upload versions. It does not
delete the configured inbox, external Supplier source, accounting template,
unreferenced output files, or other files in the data directories. The
response includes the number of records and owned files removed.
