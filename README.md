# Asset Compensation Hub

> A local-first, auditable workflow for damaged and lost IT asset compensation.

Asset Compensation Hub replaces a fragile chain of email parsing, manual case
tracking and spreadsheet generation with one modular product. The dashboard is
implemented in React while the original accounting-import template remains the
output contract.

## Why it matters

- Detects `DAMAGED` and `LOST` cases from saved EML evidence.
- Keeps case state and status history in SQLite instead of rebuilding a log.
- Surfaces blocking data-quality issues before accounting export.
- Creates batches from explicitly selected, eligible cases.
- Fills the original 30-column accounting template instead of inventing a new
  workbook layout.
- Runs with synthetic demo data and never requires real employee data in Git.

## Architecture

```text
React dashboard -> Flask JSON API
    -> application services
       -> domain model and transition policy
          -> SQLite repository
          -> EML/Supplier parsers
          -> Excel/PDF/Outlook adapters
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for decisions, API contracts and the
growth path.

## Quick start

Requirements: Python 3.11+, Node.js 20+ and pnpm 11 (Corepack is fine).

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,email]"
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

1. Copy EML files into `var/inbox/`.
2. Keep Supplier/FA&GL workbooks outside the repository or under `var/`.
3. Set `ASSET_HUB_DEMO_MODE=false` before the first real-data startup so demo
   and operational cases are never mixed.
4. Run ingestion from the UI or CLI.
5. Review warnings and move valid cases through the workflow.
6. Export only cases marked `READY_FOR_ACCOUNTING`.

To preserve the exact approved `.xlsx` or `.xlsm` output—including VBA when
present—set `ASSET_HUB_ACCOUNTING_TEMPLATE` to that template's absolute path.
The repository ships a data-free `.xlsx` copy with the same sheet and 30-column
layout for demo and CI. Keep the operational template outside Git.

VBA preservation is compatibility, not a security endorsement. Have IT/Finance
review and sign any operational macro project before configuring it on a shared
or cloud environment.

```powershell
$env:ASSET_HUB_ACCOUNTING_TEMPLATE = "C:\approved\Template_DENBU2.xlsm"
$env:ASSET_HUB_ACCOUNTING_ORG_ID = "your-approved-org-id"
```

The application does not load `.env` automatically; set runtime variables in
the shell, Docker/Render dashboard, or your process manager.

Do not commit real email, supplier, accounting or evidence files. See
[SECURITY.md](SECURITY.md).

## Quality checks

```powershell
ruff check .
pytest --cov=asset_compensation
```

The core test suite does not require Microsoft Word or Outlook. Windows COM
integrations are optional adapters and should be tested separately on a machine
with Office installed.

## Demo

Use the scripted five-minute flow in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

## Deploy

The repository includes a single-service Docker/Render Blueprint with a
persistent SQLite disk. Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for
local image checks, first deployment, secrets, smoke tests and rollback limits.

## Project status

This is a hackathon MVP. The hosted demo can be protected with shared Basic
authentication, and Render terminates TLS. Before multi-user production use,
replace that shared gate with SSO/RBAC, add background jobs and move to a
managed database. Those concerns are deliberately kept outside the domain logic
so they can be introduced without another rewrite.
