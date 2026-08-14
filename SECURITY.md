# Security and data handling

This repository is designed for source code and synthetic demo data only.

## Never commit

- Employee emails or Outlook exports (`.eml`, `.msg`)
- Supplier master files
- FA/GL workbooks
- ERP import workbooks
- Evidence PDFs
- Runtime SQLite databases or application secrets

The `.gitignore` blocks these formats by default. Keep operational data below
`var/`, which is also ignored.

## Browser uploads

Supplier upload accepts an explicit Active/Inactive pair and keeps only the
minimum normalized lookup fields. Raw Supplier inputs are removed before the
new reference version is activated. EML upload validates bounded RFC 822 and
MIME structures, rejects attachments, replaces client filenames, and does not
copy raw messages into application storage. Multipart handling may use
short-lived operating-system temp files during the request.

Both routes require the normal application authentication plus a
route-specific request header. These controls are suitable for a hackathon
test environment, not a replacement for SSO/RBAC, a data-processing agreement,
retention policy, or an approved internal hosting review. Public Render Free
staging must contain synthetic or approved anonymised fixtures only.

## Macro-enabled templates

The built-in accounting template is a data-free `.xlsx`. Legacy `.xlsm` files
can contain operational records, identifying document metadata and executable
VBA, so they must remain outside Git. The exporter can preserve VBA from an
explicitly configured approved template, but it does not certify or sign that
macro code. Review macros separately before using them on a shared service.

## Local server

The application binds to `127.0.0.1` by default. Do not change the host to
`0.0.0.0` on a shared network until authentication, TLS and access logging are
configured.

## Reporting a vulnerability

Do not open a public issue containing personal or financial data. Contact the
repository owner privately with a minimal, sanitised reproduction.
