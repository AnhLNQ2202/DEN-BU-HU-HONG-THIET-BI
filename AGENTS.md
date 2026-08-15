# Repository guidance for coding agents

This file applies to the whole repository. Start with `PROJECT_CONTEXT.md`; it
is the canonical AI handoff with the current release state, product decisions,
business rules, API/deploy map and known limitations. Then read `README.md`,
`ARCHITECTURE.md` and `docs/TEAM_DEVELOPMENT.md` before making broad changes.

When a change alters behavior, rules, API, configuration, deployment or release
state, update `PROJECT_CONTEXT.md` in the same pull request.

## Product contracts

- Keep the React dashboard visually compatible with the original VNG dashboard
  unless the task explicitly changes that contract.
- Keep the approved accounting workbook's sheet/header/layout contract. Do not
  replace it with a newly designed workbook.
- Keep domain logic out of Flask routes and React presentation components; use
  the existing domain/service/adapter boundaries.
- Keep `Dockerfile` and `render.yaml` production-safe. Local experiments belong
  in `compose.dev.yaml`, `var/`, or a feature branch.

## Data and secrets

- Use synthetic demo data in code, tests, screenshots and pull requests.
- Never commit real EML/MSG/PDF/Office files, supplier exports, employee data,
  SQLite databases, generated outputs, `.env` files, access tokens or passwords.
- Do not weaken `.gitignore`, `.dockerignore`, upload validation or spreadsheet
  template validation to make a test pass.
- Keep Codex/Claude credentials on each developer's machine; never add them to
  the dev container, Compose environment or repository.

## Validation

Run the checks relevant to the change before handing it off:

```text
ruff check .
pytest --cov=asset_compensation --cov-report=term-missing
pnpm --dir frontend build
docker build -t asset-compensation-hub:local .
```

For UI changes, also open the dashboard, exercise the changed flow and compare
it with the original visual contract. Do not hand-edit generated files under
`src/asset_compensation/web/static/dist`; generate them with the frontend build.

## Collaboration

- Create a focused feature branch and keep commits reviewable.
- Inspect `git status` before editing. Preserve unrelated local changes.
- One coding agent should own one worktree at a time. Use separate clones or Git
  worktrees when Codex and Claude work in parallel.
- Open a pull request; do not push feature work straight to protected `main`.
