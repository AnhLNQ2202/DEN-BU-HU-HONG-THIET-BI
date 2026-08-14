"""External format and automation adapters."""

from .accounting_template import (
    ACCOUNTING_TEMPLATE_HEADERS,
    AccountingTemplateAdapter,
    AccountingTemplateExportResult,
)
from .accounting_xlsx import (
    AccountingExportError,
    AccountingExportResult,
    AccountingValidationError,
    AccountingXlsxAdapter,
    GlAccountPair,
    OutputExistsError,
)
from .pdf import (
    EmlPdfConverter,
    PdfAdapterError,
    PdfDependencyError,
    PdfMerger,
    PypdfMerger,
    WordPdfConverter,
)

__all__ = [
    "AccountingExportError",
    "AccountingExportResult",
    "AccountingTemplateAdapter",
    "AccountingTemplateExportResult",
    "AccountingValidationError",
    "AccountingXlsxAdapter",
    "ACCOUNTING_TEMPLATE_HEADERS",
    "EmlPdfConverter",
    "GlAccountPair",
    "OutputExistsError",
    "PdfAdapterError",
    "PdfDependencyError",
    "PdfMerger",
    "PypdfMerger",
    "WordPdfConverter",
]
