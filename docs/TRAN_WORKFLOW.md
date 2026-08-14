# TranNNB lost-asset workflow

The TranNNB core implements the seven steps in `SKILLtrannnb.md` as reusable,
transport-neutral Python contracts. The Flask adapter now exposes those
services through the fail-closed endpoints documented in
[TRAN_API.md](TRAN_API.md); business rules remain outside HTTP routes and React.

Operational workbooks, emails, and personal data stay outside Git. All workbook adapters open
their references read-only or write to a new destination. They never overwrite the source
template and the mail adapter creates an `.eml` draft only; it has no send method.

## Public contracts

### Reference workbooks

```python
from asset_compensation.adapters import CcdcWorkbookIndex, FaGlWorkbookIndex

fa_gl = FaGlWorkbookIndex.from_path("/external/VNG-FA&GL_01_07.2026.xlsx")
lookup = fa_gl.lookup("LAP10001")

# Optional. Omit when the CCDC workbook is not available.
ccdc = CcdcWorkbookIndex.from_path("/external/CCDC.xlsx")
classification = ccdc.classification("LAP")
oldest_start = ccdc.earliest_start_date("ADA10001")
```

`FaGlWorkbookIndex` validates and searches the exact four-sheet contract:

| Sheet | Header | Data | Book | Entity |
|---|---:|---:|---|---|
| `VNG-Asset` | 3 | 4 | Asset | VNG |
| `VNG-Tool` | 3 | 4 | Tool | VNG |
| `VNGS-Asset` | 2 | 3 | Asset | VNGS |
| `VNGS-Tool` | 2 | 3 | Tool | VNGS |

It reads B/G/H/J/L/P/V/Y/AA and returns `MATCHED`, `NOT_FOUND`, or `AMBIGUOUS`. An
ambiguous Tag Number is never selected automatically. The accounting life in column Y is
validated as part of the source schema but is never used for the compensation schedule.

`CcdcWorkbookIndex` supports:

- `Define`: `Product Type`, `Barcode`, `Group Type`;
- `CMDB`: `Asset Name`, `Product Type`, used only when Define has no barcode;
- `BC Xuatkho`: `Asset Name`, `Start time`; multiple matches resolve to the oldest date.

### Resolution and calculation

```python
from datetime import date
from asset_compensation.services import TranAssetRequest, TranWorkflowService

request = TranAssetRequest(
    tag_number="LAP10001",
    asset_name="Synthetic laptop",
    domain="demo.user",
    lost_date=date(2026, 8, 14),
)
resolution = TranWorkflowService().resolve(request, fa_gl, ccdc=ccdc)
```

`TranAssetRequest` contains the four email fields and optional, explicit operator decisions:

- `physical`;
- `confirmed_cost` and `confirmed_start_date`;
- `confirmed_group`, `confirmed_fee_rate`, and `classification_confirmed=True`.

The service returns `TranResolution` with `asset`, `preview`, provenance `notes`, blocking
`issues`, FA status, and classification status. `ready` is true only when all required evidence
is resolved and the calculation does not require review.

Fail-closed rules:

- `physical` and `lookup_status` have no implicit `True`/`MATCHED` defaults in the calculation
  model;
- a zero FA cost stops for an approved replacement cost;
- an unknown barcode requires both group and fee plus an explicit user confirmation, unless a
  valid Define/CMDB row resolves it;
- a duplicate FA or conflicting CCDC match stops for review;
- an unknown value is never recovered from a historical calculation/template.

A verified nonphysical `Service`/`Software`/`Virtual asset` is still returned as
`NOT_APPLICABLE`, even when FA cost/start date are absent; those fields remain null instead of
being invented.

Approved FA-not-found fallbacks are applied field-by-field: Asset Number `ko có trên ORC`,
Book `Tool`, Entity `VNG`, Cost center `0603`, Product code `000`, and Location `01`. Start date
comes from the oldest matching BC Xuatkho row or explicit confirmation. ADA prices are applied
automatically only when the tag is absent from all four FA sheets: Lenovo 1,060,000 VND and
Macbook/Apple 2,044,545 VND. Every fallback is recorded in `notes`.

The pure `CompensationService` preserves the rule order and formulas: exemption first for each
asset, European `DAYS360`, half-up month/money rounding, four-/six-year schedules, the approved
extra year-seven 10% rule, and 5%/30% responsibility fees.

### Workbook output

```python
from asset_compensation.adapters import TranWorkbookAdapter

result = TranWorkbookAdapter("/external/Template - Tính toán đền bù Trannnb.xlsx").export(
    resolutions,
    "/app-managed/outputs/tran-request.xlsx",
    processing_date=date(2026, 8, 14),
)
```

The destination must be new and retain the source `.xlsx`/`.xlsm` suffix. The exporter:

1. appends only the current request to the selected year sheet, preserving the original workbook;
2. writes static lookup fields and provenance notes;
3. writes real J/K/L/M Excel formulas, including every elapsed/current/future schedule term;
4. deletes all old `Sent out` rows and rebuilds it from current-request D:R values only;
5. rebuilds the adjacent Total row and the documented Arial 10, light-blue header/total, border,
   alignment, date, and money formats;
6. saves atomically and verifies the output before publishing it.

The exporter rejects unresolved or `NEEDS_REVIEW` items. Formula values copied into `Sent out`
come from the verified Python calculation, so no hidden Excel recalculation is required to build
the mail table.

### HTML table and `.eml` draft

```python
from asset_compensation.adapters import TranMailDraftBuilder, build_tran_mail_table

html_table = build_tran_mail_table(resolutions)
draft = TranMailDraftBuilder().build(
    original_eml_bytes,
    resolutions,
    result.path,
    "/app-managed/outputs/reply-draft.eml",
    from_address="operator@example.invalid",
    body_intro="Approved response text supplied by the operator",
)
```

The HTML table escapes all source values. The draft preserves reply-thread headers, builds
reply-all recipients while excluding the operator address, attaches the workbook, sets
`X-Unsent: 1`, and writes a new RFC822 file atomically. `body_intro` is mandatory because the
original rule says to ask for a mail template rather than invent one. Sending remains a separate,
explicitly authorized action and is not implemented here.

## Current external-data gap

No operational CCDC workbook containing `Define`, `CMDB`, and `BC Xuatkho` is present in the
workspace. The adapter and synthetic tests implement the documented contract, but parity against
the team's real CCDC column values cannot be certified until that workbook is supplied. This does
not block FA-matched, known-barcode cases; it does block automatic warehouse-date recovery and
real Define/CMDB verification for FA-not-found or legacy barcodes.

## Test strategy

- Unit: 30/360 boundaries, half-up rounding, schedules, exemption order, zero-cost and unknown
  classification decisions, formula text, HTML escaping, and recipient de-duplication.
- Integration: synthetic four-sheet FA lookup, synthetic Define/CMDB/BC Xuatkho lookup, complete
  resolution, template-preserving output, request-only Sent out, and parseable RFC822 attachment.
- Safety: ambiguous references, unresolved data, formula injection, source/output no-clobber, and
  missing/invalid workbook contracts.

All fixtures are synthetic. Operational `.eml`, `.pdf`, `.xls`, `.xlsx`, `.xlsm`, and `.csv`
files remain ignored by Git.
