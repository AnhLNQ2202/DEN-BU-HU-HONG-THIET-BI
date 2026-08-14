# Architecture

Asset Compensation Hub is a local-first workflow application. Its design keeps
business decisions independent from file formats and Windows automation.

```text
React dashboard / CLI
   |
Application services  ---- status policy, idempotency, batch orchestration
   |
Domain model          ---- Case, warning, accounting entry, audit event
   |
Ports                 ---- repository, parser, exporter, document generator
   |
Adapters              ---- SQLite, EML, Excel, Word PDF, Outlook
```

## Design decisions

1. **SQLite is the source of workflow state.** Files are inputs and outputs,
   never the database. Every status change is recorded as an audit event.
2. **Parsing is pure.** An EML parser returns structured candidate cases and
   warnings; it does not write workbooks or update case state.
3. **Exports are explicit and idempotent.** A batch is created from selected
   case IDs. The service rejects duplicate or ineligible cases before writing.
4. **External automation is optional.** Word and Outlook adapters are loaded
   only on Windows. The core application and demo remain cross-platform.
5. **Operational data is private by default.** EML, Excel and PDF files are
   ignored by Git. The repository contains synthetic demo records only.
6. **The server is local by default.** Binding to a LAN interface requires an
   explicit environment setting and should be paired with authentication.
7. **The accounting template is a versioned contract.** Export code validates
   all 30 headers before writing, clones the template's row formats and keeps
   VBA when an approved `.xlsm` is configured. The repository copy is
   sanitised and contains no operational records.

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
- `GET /api/cases`
- `GET /api/cases/<case_id>`
- `PATCH /api/cases/<case_id>/status`
- `POST /api/ingest`
- `POST /api/batches`
- `GET /api/batches/<batch_id>/download`
- `POST /api/demo/reset`

The web layer only validates HTTP input and delegates to services. It does not
parse mail, calculate accounting lines or edit SQLite directly.

## Growth path

For a small local team, SQLite and synchronous jobs are sufficient. Revisit
authentication, background queues, object storage and PostgreSQL only when the
product moves from a hackathon/local workflow to a shared production service.
