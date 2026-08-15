# Original workspace file safety

The product repository is intentionally nested below the original working
folder. Source business files stay in the parent folder and are not copied to
GitHub or a public staging container.

## Verified baseline (2026-08-14)

A read-only comparison against the original OneDrive ZIP found:

- 51 of 51 archived paths still present at their original locations;
- 5 of 5 EML files unchanged;
- 11 of 11 PDFs present;
- 12 workbooks plus one Office lock file present;
- 6 of 6 original Python files unchanged;
- both Supplier exports and all four XLSM files unchanged.

No original path was found missing or moved. The repository omits operational
`.eml`, `.pdf`, `.xls`, `.xlsx`, `.xlsm` and `.csv` files through ignore rules;
an omitted Git file is not a deleted source file.

Four runtime-generated artifacts differ from the ZIP baseline: two Python byte
code caches, one cached mail PDF, and `case_log.xlsx`. The original ZIP remains
the immutable recovery source. Restoring one of those files would overwrite a
newer runtime artifact and therefore must be an explicit operator action.

## Deletion boundaries

Application cleanup may remove only paths created and tracked below its own
configured data directory:

- SQLite test records;
- batch output files referenced by those records;
- normalized Supplier versions managed by the application;
- private test mail/document artifacts managed by the application.

It must never recurse into the parent/original workspace, an arbitrary inbox,
an external template location, or an operator-provided reference workbook.
