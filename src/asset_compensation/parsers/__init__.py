"""Pure input parsers."""

from .contracts import CaseKind, ParsedCase, SupplierRecord
from .eml import EmlParseError, EmlParser, decode_mime_header, parse_eml, parse_money
from .suppliers import (
    DuplicateSupplierDomainError,
    SupplierLoadError,
    load_supplier_directory,
    load_supplier_records,
    normalize_domain,
)

__all__ = [
    "CaseKind",
    "DuplicateSupplierDomainError",
    "EmlParseError",
    "EmlParser",
    "ParsedCase",
    "SupplierLoadError",
    "SupplierRecord",
    "decode_mime_header",
    "load_supplier_directory",
    "load_supplier_records",
    "normalize_domain",
    "parse_eml",
    "parse_money",
]
