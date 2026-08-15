"""Application configuration with safe local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    retain_raw_eml: bool = False
    fa_gl_reference: Path | None = None
    ccdc_reference: Path | None = None
    tran_template: Path | None = None
    draft_from_address: str | None = None
    m365_tenant_id: str | None = None
    m365_client_id: str | None = None
    m365_client_secret: str | None = field(default=None, repr=False)
    m365_redirect_uri: str | None = None
    damaged_debit_gl: str = "DEMO.COMP.RECEIVABLE"
    damaged_credit_gl: str = "DEMO.DAMAGED.OFFSET"
    lost_debit_gl: str = "DEMO.COMP.RECEIVABLE"
    lost_credit_gl: str = "DEMO.LOST.OFFSET"
    prepayment_gl: str | None = None
    damaged_repair_credit_gl: str | None = None
    damaged_no_repair_credit_gl: str | None = None
    lost_depreciation_asset_gl_template: str | None = None
    lost_depreciation_other_gl_template: str | None = None
    lost_responsibility_gl_template: str | None = None
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

    @property
    def mail_artifact_dir(self) -> Path:
        """Private content-addressed EML storage; created only when opted in."""

        return self.data_dir / "private-mail-artifacts"

    @property
    def tran_output_dir(self) -> Path:
        return self.output_dir / "tran"

    @property
    def mail_pdf_output_dir(self) -> Path:
        return self.output_dir / "mail-pdfs"

    @property
    def effective_tran_template(self) -> Path | None:
        """Use an external approved template or the bundled data-free fallback."""

        if self.tran_template is not None:
            return self.tran_template
        bundled = Path(__file__).resolve().parent / "templates" / "tran_compensation_template.xlsx"
        return bundled if bundled.is_file() else None

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
        raw_fa_gl_reference = os.getenv("ASSET_HUB_FA_GL_REFERENCE", "").strip()
        raw_ccdc_reference = os.getenv("ASSET_HUB_CCDC_REFERENCE", "").strip()
        raw_tran_template = os.getenv("ASSET_HUB_TRAN_TEMPLATE", "").strip()
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
            retain_raw_eml=_as_bool(
                os.getenv("ASSET_HUB_RETAIN_RAW_EML"), False
            ),
            fa_gl_reference=(
                Path(raw_fa_gl_reference).expanduser().resolve()
                if raw_fa_gl_reference
                else None
            ),
            ccdc_reference=(
                Path(raw_ccdc_reference).expanduser().resolve()
                if raw_ccdc_reference
                else None
            ),
            tran_template=(
                Path(raw_tran_template).expanduser().resolve()
                if raw_tran_template
                else None
            ),
            draft_from_address=os.getenv("ASSET_HUB_DRAFT_FROM_ADDRESS") or None,
            m365_tenant_id=os.getenv("ASSET_HUB_M365_TENANT_ID") or None,
            m365_client_id=os.getenv("ASSET_HUB_M365_CLIENT_ID") or None,
            m365_client_secret=os.getenv("ASSET_HUB_M365_CLIENT_SECRET") or None,
            m365_redirect_uri=os.getenv("ASSET_HUB_M365_REDIRECT_URI") or None,
            damaged_debit_gl=os.getenv(
                "ASSET_HUB_DAMAGED_DEBIT_GL", "DEMO.COMP.RECEIVABLE"
            ),
            damaged_credit_gl=os.getenv(
                "ASSET_HUB_DAMAGED_CREDIT_GL", "DEMO.DAMAGED.OFFSET"
            ),
            lost_debit_gl=os.getenv("ASSET_HUB_LOST_DEBIT_GL", "DEMO.COMP.RECEIVABLE"),
            lost_credit_gl=os.getenv("ASSET_HUB_LOST_CREDIT_GL", "DEMO.LOST.OFFSET"),
            prepayment_gl=os.getenv("ASSET_HUB_PREPAYMENT_GL") or None,
            damaged_repair_credit_gl=(
                os.getenv("ASSET_HUB_DAMAGED_REPAIR_CREDIT_GL") or None
            ),
            damaged_no_repair_credit_gl=(
                os.getenv("ASSET_HUB_DAMAGED_NO_REPAIR_CREDIT_GL") or None
            ),
            lost_depreciation_asset_gl_template=(
                os.getenv("ASSET_HUB_LOST_DEPRECIATION_ASSET_GL_TEMPLATE") or None
            ),
            lost_depreciation_other_gl_template=(
                os.getenv("ASSET_HUB_LOST_DEPRECIATION_OTHER_GL_TEMPLATE") or None
            ),
            lost_responsibility_gl_template=(
                os.getenv("ASSET_HUB_LOST_RESPONSIBILITY_GL_TEMPLATE") or None
            ),
            access_user=os.getenv("ASSET_HUB_ACCESS_USER") or None,
            access_password=os.getenv("ASSET_HUB_ACCESS_PASSWORD") or None,
        )
        settings.ensure_directories()
        return settings
