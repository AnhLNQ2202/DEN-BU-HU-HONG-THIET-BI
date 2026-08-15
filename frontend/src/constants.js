export const API = Object.freeze({
  dashboard: "/api/dashboard",
  ingest: "/api/ingest",
  supplierUpload: "/api/suppliers/upload",
  supplierStatus: "/api/suppliers/status",
  emailUpload: "/api/emails/upload",
  tranReferenceStatus: "/api/tran/references/status",
  tranReferenceUpload: "/api/tran/references/upload",
  tranResolve: "/api/tran/resolve",
  tranWorkbooks: "/api/tran/workbooks",
  tranDrafts: "/api/tran/drafts",
  tranOutlookDrafts: "/api/tran/outlook-drafts",
  m365: "/api/m365",
  mailPdfIndividual: "/api/mail-pdfs/individual",
  mailPdfBatches: "/api/mail-pdfs/batches",
  clearTestData: "/api/test-data/clear",
  cases: "/api/cases",
  batches: "/api/batches",
  compensationPreview: "/api/compensation/preview",
  reset: "/api/demo/reset",
});

export const STATUS_ORDER = Object.freeze([
  "NEW",
  "NEEDS_REVIEW",
  "READY_FOR_ACCOUNTING",
  "ACCOUNTED",
  "CLOSED",
]);

export const STATUS_META = Object.freeze({
  NEW: { label: "Mới", shortLabel: "Mới" },
  NEEDS_REVIEW: { label: "Cần kiểm tra", shortLabel: "Kiểm tra" },
  READY_FOR_ACCOUNTING: { label: "Sẵn sàng hạch toán", shortLabel: "Sẵn sàng" },
  ACCOUNTED: { label: "Đã hạch toán", shortLabel: "Hạch toán" },
  CLOSED: { label: "Đã đóng", shortLabel: "Đóng" },
});

export const ALLOWED_TRANSITIONS = Object.freeze({
  NEW: ["NEEDS_REVIEW", "READY_FOR_ACCOUNTING"],
  NEEDS_REVIEW: ["READY_FOR_ACCOUNTING"],
  READY_FOR_ACCOUNTING: ["NEEDS_REVIEW", "ACCOUNTED"],
  ACCOUNTED: ["CLOSED"],
  CLOSED: [],
});

export const TYPE_META = Object.freeze({
  DAMAGED: { label: "Hư hỏng", className: "damaged" },
  LOST: { label: "Thất lạc", className: "lost" },
  UNKNOWN: { label: "Khác", className: "unknown" },
});
