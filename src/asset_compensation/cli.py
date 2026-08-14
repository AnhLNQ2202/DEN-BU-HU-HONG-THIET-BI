"""Command-line entry points for local operation and demos."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from asset_compensation.config import Settings
from asset_compensation.demo import seed_demo
from asset_compensation.parsers import SupplierLoadError, load_supplier_directory
from asset_compensation.repositories import SQLiteCaseRepository
from asset_compensation.services import CaseService
from asset_compensation.web.app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asset-hub", description="Asset Compensation Hub")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Initialise local storage")
    init.add_argument("--demo", action="store_true", help="Load synthetic demo records")

    serve = subcommands.add_parser("serve", help="Run the local dashboard")
    serve.add_argument("--host", default=None, help="Override ASSET_HUB_HOST")
    serve.add_argument("--port", type=int, default=None, help="Override ASSET_HUB_PORT")

    ingest = subcommands.add_parser("ingest", help="Parse EML files from var/inbox")
    ingest.add_argument("--json", action="store_true", help="Print machine-readable output")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = build_parser()
    args = argument_parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "serve":
        app = create_app(settings)
        app.run(host=args.host or settings.host, port=args.port or settings.port, debug=False)
        return 0

    with SQLiteCaseRepository(settings.database_path) as repository:
        service = CaseService(repository)
        if args.command == "init":
            cases = seed_demo(service) if args.demo else service.list_cases()
            print(f"Storage ready with {len(cases)} cases")
            return 0

        if args.command == "ingest":
            from asset_compensation.services.ingestion_service import ingest_eml_directory

            try:
                supplier_directory = (
                    load_supplier_directory(settings.supplier_file)
                    if settings.supplier_file is not None
                    else None
                )
            except (OSError, SupplierLoadError) as exc:
                argument_parser.error(f"Could not load supplier reference: {exc}")
            result = ingest_eml_directory(
                service,
                settings.inbox_dir,
                supplier_directory=supplier_directory,
            )
            payload = {
                "ingested": len(result.cases),
                "case_ids": [case.id for case in result.cases],
                "warnings": list(result.warnings),
                "unknown_files": list(result.unknown_files),
            }
            print(
                json.dumps(payload, ensure_ascii=False)
                if args.json
                else f"Ingested {len(result.cases)} cases"
            )
            return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
