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
