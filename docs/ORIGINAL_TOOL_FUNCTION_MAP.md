# Original tool function map

This document is the migration contract between the six Python scripts in the
parent/original workspace and the maintainable product under this repository.
The original folder is treated as read-only. Operational EML, PDF and Office
files are deliberately not copied to Git.

The AST inventory contains **73 function definitions** (including nested
helpers), **11 Flask routes**, no classes, and six script entry flows. A function
is considered migrated only when its observable business behaviour has a
service/adapter equivalent and regression coverage; copying the old source is
not sufficient.

## Status legend

- `DONE`: equivalent behaviour exists in a tested module.
- `PARTIAL`: a safer replacement exists, but one or more original behaviours
  are not yet represented.
- `IN PROGRESS`: implementation is part of the current parity work.
- `LOCAL`: intentionally isolated behind a Windows/Office adapter.
- `RETIRED`: behaviour is intentionally superseded and documented.

## `app.py` — 16 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `load_module` | Dynamically import rule scripts | `RETIRED`: static package imports and dependency injection avoid arbitrary code execution. |
| `refresh_cases` | Scan inbox, merge cases, rewrite Excel log | `DONE/RETIRED`: explicit ingestion + SQLite preserves cases; GET-side mutation and log rewrites are intentionally removed. |
| `run_subprocess` | Run accounting/PDF scripts and inspect console markers | `RETIRED`: typed service/adapter calls with exceptions and structured results. |
| `resolve_output_file` | Resolve generated basename from logs/fallback | `DONE` for accounting through batch metadata and safe-root validation. |
| `api_cases` | Refresh and return dashboard cases | `DONE`: `/api/dashboard` and `/api/cases`; ingestion is an explicit POST instead of a mutating GET. |
| `api_status` | Update a two-value status in `case_log.xlsx` | `DONE`: validated, audited SQLite workflow transitions replace two-value Excel state. |
| `api_run_hachtoan` | Validate batch and invoke accounting script | `DONE`: `/api/batches` applies semantic, configuration-driven policy to an approved external or clean built-in template. |
| `api_run_pdfmerge` | Invoke Word mail-to-PDF batch | `DONE`: retained mail artifacts can be rendered individually or merged through Word on Windows or a sandboxed cloud renderer. |
| `api_download` | Download generated XLSM/PDF by basename | `DONE`: typed accounting, Tran, EML-draft and PDF download routes enforce managed output roots. |
| `api_output_file` | Check whether an expected output exists | `RETIRED`: successful typed responses return exact download URLs; arbitrary filename probing is not exposed. |
| `api_upload_supplier` | Replace one raw Supplier file | `DONE`: paired Active/Inactive upload, validation, normalization, atomic pointer and private cleanup. |
| `api_mail` | Download source EML | `DONE`: optional content-addressed private retention and no-store download; disabled by default. |
| `api_mail_pdf` | Convert/cache one EML as inline PDF | `DONE`: capability-gated individual PDF creation and managed download. |
| `assets_logo` | Locate and serve a logo | `DONE`: bundled React asset. |
| `index` | Return dashboard HTML | `DONE`: Flask shell + React bundle. |
| `get_lan_ip` | Discover a LAN address with an external socket | `RETIRED`: explicit host/port/deployment configuration is deterministic and safer. |

## `case_dashboard.py` — 9 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `load_skill_module` | Execute the old accounting script as a parser library | `RETIRED`: pure parser modules with explicit contracts. |
| `get_mail_date` | Parse email date with mtime fallback | `DONE`: UTC parsing plus controlled fallback. |
| `load_existing_case_log` | Read prior IDs/status from Excel | `RETIRED`: SQLite is the workflow source of truth. |
| `next_case_id` | Allocate monthly sequential IDs | `RETIRED`: deterministic content/source identity prevents duplicate ingestion. |
| `collect_cases` | Parse all mail, enrich Supplier data, emit warnings | `DONE`: multi-record damaged/lost parsing, Supplier enrichment, explicit skips and semantic credit components. |
| `merge_with_existing` | Preserve old case IDs/status | `DONE`: transactional upsert preserves workflow state. |
| `write_case_log` | Rewrite and style `case_log.xlsx` | `RETIRED/PARTIAL`: SQLite replaces the mutable workflow log and the original file is no longer mutated. A separate monthly Excel/CSV case-report export is not currently implemented and remains a backlog item if the business still needs it. |
| `build_dashboard_html` | Generate standalone dashboard HTML | `DONE`: componentized React dashboard retaining the original visual baseline. |
| `main` | CLI orchestration for scan/log/dashboard | `DONE/RETIRED`: `asset-hub ingest/serve` covers runtime orchestration; the old mutable log-generation entry flow is retired. This does not imply a monthly case-report export exists. |

## `build_mail_table_from_sentout.py` — 5 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `fmt_money` | Render whole-VND values | `DONE`: shared Tran mail-table formatter. |
| `fmt_date` | Render `dd/mm/yyyy` | `DONE`: shared Tran mail-table formatter. |
| `clean_text` | Normalize blank/quoted identifiers | `DONE`: shared Tran mail-table formatter. |
| `build_table` | Build the 15-column Sent-out HTML table and totals | `DONE`: escaped 15-column HTML table with totals. |
| `main` | Read `Sent out` and write mail-body HTML | `DONE`: the Tran flow produces a downloadable unsent RFC822 draft from the current export. |

## `draft_mail_outlook.py` — 5 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `_is_reply_or_forward` | Detect thread replies/forwards | `RETIRED`: the cloud flow does not scan a mailbox; it replies against the explicitly retained source EML. |
| `_iter_folders_2_levels` | Traverse Outlook folders | `RETIRED/EXTERNAL`: no Outlook/MAPI adapter exists in this repository; the product requires explicit source EML selection. |
| `find_best_mail_by_subject` | Prefer oldest original, else newest reply | `RETIRED`: explicit source-artifact selection removes global mailbox guessing. |
| nested `_received` | Normalize Outlook received time | `RETIRED`: no mailbox scan occurs in the cloud flow. |
| `main` | `ReplyAll`, prepend HTML and display a draft | `DONE/EXTERNAL`: product creates a downloadable Reply-All RFC822 draft and never sends automatically. Outlook `Display()` is not implemented; an operator may open the downloaded draft in an approved local client. |

## `ghep_mail_pdf_word.py` — 12 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `decode_subject` | Decode RFC 2047 subject | `DONE` in the EML parser. |
| `build_inline_images` | Collect CID images as data URLs | `DONE`: bounded raster-only MIME/CID renderer. |
| `resolve_cid_images` | Replace `cid:` references | `DONE`: allowlisted sanitizer resolves embedded images and blocks remote resources. |
| nested `repl` | CID replacement callback | `DONE`: renderer helper. |
| `get_full_eml_content` | Select HTML/plain body and inline resources | `DONE`: shared printable mail-document parser. |
| `safe_filename` | Produce safe output names | `DONE`: upload display names and individual/merged PDF output names are bounded and sanitized. |
| `is_wide_table_mail` | Detect tables that require landscape | `DONE`: renderer selects portrait/landscape from sanitized table content. |
| `strip_fixed_table_widths` | Remove mail widths before Word autofit | `DONE`: styling is allowlisted and fixed widths are not carried into printable HTML. |
| `build_html_document` | Wrap printable HTML/CSS | `DONE`: printable document builder for Word and cloud renderers. |
| `eml_to_pdf_via_word` | Export one mail through Word | `DONE/LOCAL`: lazy isolated Word instance on Windows; a sandboxed WeasyPrint adapter handles Render/Linux. |
| `normalize_pdf_pages_from_file` | Force a fixed page count | `DONE`: pads short output; overflow fails by default or returns an explicit truncation warning when requested. |
| `main` | Create individual PDFs and one merged batch | `DONE`: atomic individual and merged outputs with managed downloads. |

## `skill_hachtoandenbu.py` — 26 functions

| Original function | Responsibility | Product mapping |
|---|---|---|
| `parse_batch_date` | Validate `GN2ddmmyy` and derive invoice date | `DONE`: batch validation. |
| `find_file` | Resolve one input by glob | `RETIRED`: explicit upload/config paths; no first-match guessing. |
| `find_supplier_files` | Locate Active/Inactive Supplier exports | `RETIRED`: explicit paired upload. |
| `_read_supplier_dataframe` | Read CSV/XLSX/Oracle-BIP HTML | `DONE`: strict streaming/bounded Supplier parsers. |
| `load_supplier_map` | Build domain lookup | `DONE`: normalized directory with collision exclusion. |
| `decode_subject` | Decode email subject | `DONE`. |
| `get_full_eml_content` | Extract plain/HTML text | `DONE` for parsing and sanitized printable content. |
| `strip_tags` | Normalize HTML to text | `DONE`: bounded HTML text extraction. |
| `should_skip` | Skip MOU/technical/no-compensation mail | `DONE`: explicit skip reasons, while MOU-prefixed asset tags remain valid. |
| `_parse_amount` | Parse VND tokens | `DONE`: exact whole-VND parser with overflow checks. |
| `_extract_assets_from_block_hh` | Extract several damaged assets | `DONE`: multi-record parser. |
| `extract_records_hu_hong` | Build damaged records | `DONE`: multi-record parser preserves repair state and source metadata. |
| `build_description_hu_hong` | Build legacy damaged description | `DONE`: accounting metadata builder. |
| `determine_code_hu_hong` | Select repair/no-repair GL | `DONE`: semantic policy keys resolve only through approved runtime configuration. |
| `_to_amount` | Normalize cell amount | `DONE`: exact financial validation. |
| `parse_asset_table` | Parse LOST multi-row tables | `DONE`: multi-row table parser. |
| nested `find_header_key` | Match flexible table headers | `DONE`: table schema helper. |
| nested `cell_text` | Normalize table cell values | `DONE`: table schema helper. |
| `group_by_domain` | Group LOST assets by employee domain | `DONE`: grouped cases preserve source relationships and exact totals. |
| `build_credit_lines` | Split lost credit lines by dimensions | `DONE`: config-neutral credit components are reconciled before GL resolution. |
| `build_description_that_lac` | Build legacy lost description | `DONE`: accounting metadata builder. |
| `capture_row_style` | Capture template row archetype | `DONE`: template adapter copies formatting only. |
| `apply_row_style` | Apply row archetype | `DONE`: no business value is copied from template rows. |
| `set_cell_font_red` | Highlight reconciliation errors | `RETIRED`: mismatched totals are rejected before export instead of publishing a known-invalid workbook. |
| `write_row` | Write one accounting import row | `DONE`: typed template adapter preserves approved layout and resolves semantic GL policy from configuration. |
| `main` | Parse all mail and produce ordered accounting workbook | `DONE`: batch orchestration parses all records, orders damaged before lost and supports an explicit invoice start number. |

## Rules that are already numerically equivalent

The TranNNB calculation core was checked against the historical workbook without
publishing operational rows:

- 25/25 formula rows match the remaining-rate calculation.
- 7/7 exempt rows match the `< 500,000 VND` and `>= 12 months` rule.
- 32/32 rows match the 5%/30% responsibility-fee mapping.
- European `DAYS360`, half-up rounding, four-year schedule, six-year schedule and
  the additional flat year-seven 10% rule match.

Reference lookup, approved exception inputs, workbook/`Sent out` generation and
the unsent draft lifecycle are implemented behind fail-closed API capabilities.
They are covered with synthetic workbooks; production UAT is still required for
the organization's actual CCDC export because no real CCDC workbook was present
in the audited source folder.

## Non-equivalent behaviours that must not be copied blindly

- Dynamic Python imports and subprocess marker parsing.
- GET requests that mutate `case_log.xlsx`.
- Deleting raw Supplier files by filename pattern.
- Scanning every Outlook mailbox without account/sender scope.
- Silent PDF page truncation.
- Raw HTML insertion into an email body.
- Automatic send or overwrite. Draft creation and source/template writes require
  explicit user action.
