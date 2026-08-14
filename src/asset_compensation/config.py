"""Application configuration with safe local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime paths and server settings.

    Operational files live below ``data_dir`` and are excluded from Git. The
    server binds to localhost by default; sharing on a LAN must be deliberate.
    """

    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 5000
    demo_mode: bool = True
    allow_test_reset: bool = False
    secret_key: str = "local-development-only"
    supplier_file: Path | None = None
    accounting_template: Path | None = None
    accounting_org_id: str | None = None
    damaged_debit_gl: str = "DEMO.COMP.RECEIVABLE"
    damaged_credit_gl: str = "DEMO.DAMAGED.OFFSET"
    lost_debit_gl: str = "DEMO.COMP.RECEIVABLE"
    lost_credit_gl: str = "DEMO.LOST.OFFSET"
    access_user: str | None = None
    access_password: str | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "asset_hub.sqlite3"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.reference_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Settings:
        repository_root = Path(__file__).resolve().parents[2]
        raw_data_dir = os.getenv("ASSET_HUB_DATA_DIR", str(repository_root / "var"))
        raw_supplier_file = os.getenv("ASSET_HUB_SUPPLIER_FILE", "").strip()
        raw_accounting_template = os.getenv(
            "ASSET_HUB_ACCOUNTING_TEMPLATE", ""
        ).strip()
        settings = cls(
            data_dir=Path(raw_data_dir).expanduser().resolve(),
            host=os.getenv("ASSET_HUB_HOST", "127.0.0.1"),
            port=int(os.getenv("ASSET_HUB_PORT", "5000")),
            demo_mode=_as_bool(os.getenv("ASSET_HUB_DEMO_MODE"), True),
            allow_test_reset=_as_bool(
                os.getenv("ASSET_HUB_ALLOW_TEST_RESET"), False
            ),
            secret_key=os.getenv("ASSET_HUB_SECRET_KEY", "local-development-only"),
            supplier_file=(
                Path(raw_supplier_file).expanduser().resolve() if raw_supplier_file else None
            ),
            accounting_template=(
                Path(raw_accounting_template).expanduser().resolve()
                if raw_accounting_template
                else None
            ),
            accounting_org_id=os.getenv("ASSET_HUB_ACCOUNTING_ORG_ID") or None,
            damaged_debit_gl=os.getenv(
                "ASSET_HUB_DAMAGED_DEBIT_GL", "DEMO.COMP.RECEIVABLE"
            ),
            damaged_credit_gl=os.getenv(
                "ASSET_HUB_DAMAGED_CREDIT_GL", "DEMO.DAMAGED.OFFSET"
            ),
            lost_debit_gl=os.getenv("ASSET_HUB_LOST_DEBIT_GL", "DEMO.COMP.RECEIVABLE"),
            lost_credit_gl=os.getenv("ASSET_HUB_LOST_CREDIT_GL", "DEMO.LOST.OFFSET"),
            access_user=os.getenv("ASSET_HUB_ACCESS_USER") or None,
            access_password=os.getenv("ASSET_HUB_ACCESS_PASSWORD") or None,
        )
        settings.ensure_directories()
        return settings
