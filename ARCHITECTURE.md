# Architecture

Asset Compensation Hub is a local-first workflow application. Its design keeps
business decisions independent from file formats and Windows automation.

```text
React dashboard / CLI
   |
Application services  ---- status policy, compensation preview, batch orchestration
   |
Domain model          ---- Case, warning, accounting entry, audit event
   |
Ports                 ---- repository, parser, exporter, document generator
   |
Adapters              ---- SQLite, EML, Excel, Word/cloud PDF, RFC822 draft
```

## Design decisions

1. **SQLite is the source of workflow state.** Files are inputs and outputs,
   never the database. Every status change is recorded as an audit event.
2. **Parsing is pure.** An EML parser returns structured candidate cases and
   warnings; it does not write workbooks or update case state.
3. **Exports are explicit and idempotent.** A batch is created from selected
   case IDs. The service rejects duplicate or ineligible cases before writing.
4. **External automation is optional.** Word PDF conversion is loaded only on
   Windows; Linux/Render uses the sandboxed cloud renderer. The product creates
   an RFC822 `.eml` draft but has no Outlook mailbox/display/send adapter. The
   core application and demo remain cross-platform.
5. **Operational data is private by default.** EML, Excel and PDF files are
   ignored by Git. The repository contains synthetic demo records only.
6. **The server is local by default.** Binding to a LAN interface requires an
   explicit environment setting and should be paired with authentication.
7. **The accounting template is a versioned contract.** Export code validates
   all 30 headers before writing, clones the template's row formats and keeps
   VBA when an approved `.xlsm` is configured. The repository copy is
   sanitised and contains no operational records.
8. **TranNNB calculation is a side-effect-free preview.** The compensation
   service applies the documented depreciation, responsibility-fee and
   exemption rules without editing source workbooks, changing case state or
   sending mail. Unknown or conflicting inputs are returned as
   `NEEDS_REVIEW`; they are never guessed.

## Case lifecycle

```text
NEW -> NEEDS_REVIEW -> READY_FOR_ACCOUNTING -> ACCOUNTED -> CLOSED
NEW -----------------> READY_FOR_ACCOUNTING
NEEDS_REVIEW <-------- READY_FOR_ACCOUNTING  (return before accounting)
```

Invalid transitions are rejected by the service, not silently accepted by the
UI. Re-ingesting the same source updates the same stable case instead of
creating another accounting candidate.

## API contract

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/capabilities`
- `GET /api/cases`
- `GET /api/cases/<case_id>`
- `PATCH /api/cases/<case_id>/status`
- `POST /api/ingest`
- `GET /api/suppliers/status`
- `POST /api/suppliers/upload`
- `POST /api/emails/upload`
- `POST /api/test-data/clear` (explicitly enabled disposable staging only)
- `POST /api/compensation/preview`
- `GET /api/tran/references/status`
- `POST /api/tran/references/upload`
- `POST /api/tran/resolve`
- `POST /api/tran/workbooks`
- `GET /api/tran/workbooks/<output_id>/download`
- `POST /api/tran/drafts`
- `GET /api/tran/drafts/<output_id>/download`
- `GET /api/mail-artifacts/<handle>/download`
- `POST /api/mail-pdfs/individual`
- `POST /api/mail-pdfs/batches`
- `POST /api/batches`
- `GET /api/batches/<batch_id>/download`
- `POST /api/demo/reset`

The web layer only validates HTTP input and delegates to services. It does not
parse mail, calculate compensation or accounting lines, or edit SQLite
directly. See
[docs/COMPENSATION_PREVIEW_API.md](docs/COMPENSATION_PREVIEW_API.md) for the
preview contract and review states.
See [docs/UPLOAD_API.md](docs/UPLOAD_API.md) for upload limits, collision
handling, privacy guarantees and multipart contracts.
See [docs/TRAN_API.md](docs/TRAN_API.md) for reference upload, Tran workbook,
unsent draft, retained EML and PDF contracts.

## Growth path

For a small local team, SQLite and synchronous jobs are sufficient. Revisit
authentication, background queues, object storage and PostgreSQL only when the
product moves from a hackathon/local workflow to a shared production service.
