# Architecture

Asset Compensation Hub is a local-first workflow application. Its design keeps
business decisions independent from file formats and Windows automation.

```text
React dashboard / CLI
   |
Flask API             ---- shared auth, bounded HTTP validation, OAuth callback
   |
Application services  ---- status policy, compensation, sync, batch orchestration
   |
Domain model          ---- Case, warning, accounting entry, audit event
   |
Ports / adapters      ---- SQLite, EML, Excel, PDF, RFC822 draft, Microsoft Graph
```

## Design decisions

1. **SQLite is the source of workflow state.** Files are inputs and outputs,
   never the database. Every status change is recorded as an audit event.
2. **Parsing is pure.** An EML parser returns structured candidate cases and
   warnings; it does not write workbooks or update case state.
3. **Exports are explicit and idempotent.** A batch is created from selected
   case IDs. The service rejects duplicate or ineligible cases before writing.
4. **External automation is optional and least-privileged.** Word PDF conversion
   is loaded only on Windows; Linux/Render uses the sandboxed cloud renderer.
   The local RFC822 `.eml` draft remains available without a mailbox connection.
   Optional Microsoft Graph integration keeps two role-scoped OAuth token caches:
   Ngan uses delegated `Mail.Read`; Tran uses `Mail.ReadWrite` to create an
   unsent Reply-All draft. The product never requests `Mail.Send`, has no send
   route and does not automate Outlook `Display()`.
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
9. **Mailbox scope is explicit and ephemeral in the MVP.** Each role connects
   independently, selects exactly one folder and syncs only `created` changes
   from that folder. The first pass looks back 30 days. Tokens, folder choices
   and delta cursors remain in a bounded 8-hour in-memory browser session, so a
   restart requires reconnect/reselect/resync. The five-minute UI auto-sync is
   active only while the tab is open and visible; it is not a server worker or
   webhook.

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
- `GET /api/m365/<role>/status`
- `POST /api/m365/<role>/connect`
- `GET /api/m365/callback`
- `POST /api/m365/<role>/disconnect`
- `GET /api/m365/<role>/folders`
- `POST /api/m365/<role>/folder`
- `POST /api/m365/<role>/sync`
- `POST /api/test-data/clear` (explicitly enabled disposable staging only)
- `POST /api/compensation/preview`
- `GET /api/tran/references/status`
- `POST /api/tran/references/upload`
- `POST /api/tran/resolve`
- `POST /api/tran/workbooks`
- `GET /api/tran/workbooks/<output_id>/download`
- `POST /api/tran/drafts`
- `POST /api/tran/outlook-drafts`
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
See [docs/MICROSOFT_365.md](docs/MICROSOFT_365.md) for OAuth, exact-folder sync,
Graph Reply-All draft, permissions, limits and deployment configuration.

## Growth path

For a small local team, SQLite and synchronous jobs are sufficient. Revisit
authentication, background queues, object storage and PostgreSQL only when the
product moves from a hackathon/local workflow to a shared production service.
Durable mailbox sync also requires encrypted token/cursor persistence, a worker
or Graph webhook/subscription lifecycle, distributed coordination and per-user
SSO/RBAC; the current browser timer is deliberately not that architecture.
