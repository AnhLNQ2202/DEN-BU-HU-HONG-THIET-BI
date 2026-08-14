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

## Local server

The application binds to `127.0.0.1` by default. Do not change the host to
`0.0.0.0` on a shared network until authentication, TLS and access logging are
configured.

## Reporting a vulnerability

Do not open a public issue containing personal or financial data. Contact the
repository owner privately with a minimal, sanitised reproduction.
