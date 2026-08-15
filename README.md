# Asset Compensation Hub

> A local-first, auditable workflow for damaged and lost IT asset compensation.

Asset Compensation Hub replaces a fragile chain of email parsing, manual case
tracking and spreadsheet generation with one modular product. The dashboard is
implemented in React while the original accounting-import and TranNNB workbook/
mail templates remain the output contracts.

For a complete handoff to a new developer or AI—including current GitHub/Render
state, non-negotiable product decisions, every workflow, business rules, API,
configuration, security boundaries, deployment runbooks and remaining work—
start with [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Why it matters

- Detects `DAMAGED` and `LOST` cases from saved EML evidence.
- Optionally connects two independent Microsoft 365 accounts: NganTLT reads one
  explicitly selected folder with delegated `Mail.Read`, while TranNNB uses
  `Mail.ReadWrite` so the product can create an unsent Reply-All draft. The app
  never requests `Mail.Send`.
- Previews `LOST` compensation under the TranNNB/IT.POL.01 rules, with an
  explicit `NEEDS_REVIEW` result whenever source data is incomplete or
  ambiguous.
- Keeps case state and status history in SQLite instead of rebuilding a log.
- Surfaces blocking data-quality issues before accounting export.
- Creates batches from explicitly selected, eligible cases.
- Creates a PDF for one retained email or a fixed-page merged evidence PDF;
  overflow is reported instead of silently dropping pages.
- Fills the original 30-column accounting template instead of inventing a new
  workbook layout.
- Runs with synthetic demo data and never requires real employee data in Git.

## Architecture

```text
React dashboard -> Flask JSON API
    -> application services
       -> domain model, transition policy and compensation rules
          -> SQLite repository
          -> EML/Supplier parsers
          -> Excel/PDF/RFC822-draft adapters
          -> optional Microsoft Graph folder-sync/draft adapter
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for decisions, API contracts and the
growth path.

## Quick start

Requirements: Python 3.11+, Node.js 20+ and pnpm 11 (Corepack is fine).

For a cross-platform team setup with hot reload, Dev Containers, Codex/Claude
workflows and isolated cloud previews, see
[docs/TEAM_DEVELOPMENT.md](docs/TEAM_DEVELOPMENT.md).

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,email,windows]"
cd frontend
corepack enable
corepack prepare pnpm@11.19.0 --activate
pnpm install --frozen-lockfile
pnpm build
cd ..
python -m asset_compensation.cli init --demo
python -m asset_compensation.cli serve
```

Open <http://127.0.0.1:5000>.

Runtime state is created below `var/` and is ignored by Git.

## Working with real files

1. From **Task > NganTLT**, upload exactly one Supplier Active and one Supplier
   Inactive file, then upload one or more `.eml` files. The legacy server-inbox
   flow under `var/inbox/` remains available for local batch ingestion.
2. Keep Supplier/FA&GL workbooks outside the repository or under `var/`.
3. Set `ASSET_HUB_DEMO_MODE=false` before the first real-data startup so demo
   and operational cases are never mixed.
4. Run ingestion from the UI or CLI.
5. Review warnings and move valid cases through the workflow.
6. Export only cases marked `READY_FOR_ACCOUNTING`.

Instead of uploading EML manually, an optional Microsoft 365 integration can
connect one mailbox for each role and ingest only newly created messages from
one selected folder. The first sync is limited to the latest 30 days. The UI can
repeat sync every five minutes while its browser tab is open and visible; this
is not a durable background worker. OAuth sessions, selected folders and delta
cursors currently live in process memory and are lost on restart. Follow
[docs/MICROSOFT_365.md](docs/MICROSOFT_365.md) for Entra registration, Render
environment values, permissions and limits.

To download source EML, create individual PDFs, or merge mail evidence on a
trusted local machine, explicitly enable private retention before starting the
server:

```powershell
$env:ASSET_HUB_RETAIN_RAW_EML = "true"
```

The Windows extra uses Microsoft Word for the closest legacy layout. Docker and
Render use the sandboxed cloud renderer included in the image. Retention stays
off by default outside the disposable staging Blueprint.

To preserve the exact approved `.xlsx` or `.xlsm` output—including VBA when
present—set `ASSET_HUB_ACCOUNTING_TEMPLATE` to that template's absolute path.
The repository ships a data-free `.xlsx` copy with the same sheet and 30-column
layout for demo and CI. Keep the operational template outside Git.

The bundled Tran workbook is a data-free, layout/style-preserving derivative of
the private reference template. It keeps the approved sheet order, active/hidden
state, views, filter, year-sheet archetype and `Sent out` layout while removing
all operational rows, helper values, metadata, drawings and active/external
content. The source workbook remains outside Git and is never overwritten.

VBA preservation is compatibility, not a security endorsement. Have IT/Finance
review and sign any operational macro project before configuring it on a shared
or cloud environment.

The complete FA&GL/CCDC lookup, compensation, Tran template export, `Sent out`,
HTML table, and unsent `.eml` draft contracts are documented in
[docs/TRAN_WORKFLOW.md](docs/TRAN_WORKFLOW.md). The HTTP upload, download,
retention and PDF contracts are in [docs/TRAN_API.md](docs/TRAN_API.md).

```powershell
$env:ASSET_HUB_ACCOUNTING_TEMPLATE = "C:\approved\Template_DENBU2.xlsm"
$env:ASSET_HUB_ACCOUNTING_ORG_ID = "your-approved-org-id"
```

The application does not load `.env` automatically; set runtime variables in
the shell, Docker/Render dashboard, or your process manager.

Do not commit real email, supplier, accounting or evidence files. See
[SECURITY.md](SECURITY.md). Do not upload operational employee data to the
public Render Free staging service; use only synthetic or approved anonymised
fixtures there.

## Quality checks

```powershell
ruff check .
pytest --cov=asset_compensation
```

The core test suite does not require Microsoft Word. Word PDF conversion is an
optional Windows COM adapter and should be tested separately on a machine with
Office installed. The product keeps its downloadable RFC822 `.eml` draft flow.
When Microsoft 365 is configured, TranNNB can additionally create an
unsent Reply-All draft in the connected Outlook mailbox and attach the generated
workbook. There is no Outlook `Display()` automation and no send endpoint.

## Demo

Use the scripted five-minute flow in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

## Deploy

The repository includes a single-service Docker/Render Blueprint with a
persistent SQLite disk. Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for
local image checks, first deployment, secrets, smoke tests and rollback limits.

For a disposable $0 team preview, use the separate
[`render.staging.yaml`](render.staging.yaml) Blueprint and follow
[docs/RENDER_FREE_STAGING.md](docs/RENDER_FREE_STAGING.md). It starts empty,
accepts only explicitly uploaded test data, and keeps SQLite plus generated
outputs intentionally ephemeral.

For GreenNode, use the one-vServer Docker Compose deployment in
[docs/GREENNODE_DEPLOYMENT.md](docs/GREENNODE_DEPLOYMENT.md). It keeps the app
port private behind Caddy HTTPS and persists SQLite/output under `/var/data`.

## Project status

This is a hackathon MVP. The hosted demo can be protected with shared Basic
authentication, and Render terminates TLS. Before multi-user production use,
replace that shared gate with SSO/RBAC, add background jobs and move to a
managed database. Those concerns are deliberately kept outside the domain logic
so they can be introduced without another rewrite.
