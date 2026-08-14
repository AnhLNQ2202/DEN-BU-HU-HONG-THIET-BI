import { STATUS_META, STATUS_ORDER, TYPE_META } from "./constants.js";

const currencyFormatter = new Intl.NumberFormat("vi-VN", {
  style: "currency",
  currency: "VND",
  maximumFractionDigits: 0,
});
export const numberFormatter = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 0 });
const dateFormatter = new Intl.DateTimeFormat("vi-VN", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});
const dateTimeFormatter = new Intl.DateTimeFormat("vi-VN", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function toNumber(value, fallback = 0) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    let normalized = value.replace(/[^0-9,.-]/g, "");
    if (/^-?\d{1,3}([.,]\d{3})+$/.test(normalized)) {
      normalized = normalized.replace(/[.,]/g, "");
    } else if (normalized.includes(",") && normalized.includes(".")) {
      const decimal = normalized.lastIndexOf(",") > normalized.lastIndexOf(".") ? "," : ".";
      normalized = normalized.replaceAll(decimal === "," ? "." : ",", "");
      if (decimal === ",") normalized = normalized.replace(",", ".");
    } else if (normalized.includes(",")) {
      normalized = normalized.replace(",", ".");
    }
    const parsed = Number(normalized);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

export function normalizeStatus(value) {
  const status = String(value || "NEW").trim().toUpperCase().replace(/[\s-]+/g, "_");
  const aliases = {
    PENDING: "NEW",
    REVIEW: "NEEDS_REVIEW",
    REVIEWING: "NEEDS_REVIEW",
    READY: "READY_FOR_ACCOUNTING",
    COMPLETED: "ACCOUNTED",
    DONE: "CLOSED",
  };
  const canonical = aliases[status] || status;
  return STATUS_META[canonical] ? canonical : "NEW";
}

export function normalizeCaseType(value) {
  const type = String(value || "UNKNOWN").trim().toUpperCase().replace(/[\s-]+/g, "_");
  if (["DAMAGED", "HU_HONG", "HƯ_HỎNG", "HUHONG"].includes(type)) return "DAMAGED";
  if (["LOST", "THAT_LAC", "THẤT_LẠC", "THATLAC"].includes(type)) return "LOST";
  return "UNKNOWN";
}

export function formatCurrency(value) {
  return currencyFormatter.format(toNumber(value));
}

export function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return (includeTime ? dateTimeFormatter : dateFormatter).format(date);
}

export function toTimestamp(value) {
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

export function displayValue(value) {
  return value == null || value === "" ? "—" : String(value);
}

export function statusClass(status) {
  return String(status || "NEW").toLowerCase().replaceAll("_", "-");
}

export function normalizeSearch(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replaceAll("đ", "d")
    .replaceAll("Đ", "D")
    .toLowerCase();
}

export function filterAndSortCases(cases, filters, sort) {
  const query = normalizeSearch(filters.query);
  return cases
    .filter((caseItem) => {
      if (filters.type !== "ALL" && caseItem.case_type !== filters.type) return false;
      if (filters.status !== "ALL" && caseItem.status !== filters.status) return false;
      if (!query) return true;
      return normalizeSearch([
        caseItem.id,
        caseItem.domain,
        caseItem.employee_name,
        caseItem.asset_code,
        caseItem.asset_name,
        caseItem.source_file,
      ].join(" ")).includes(query);
    })
    .sort((a, b) => compareCases(a, b, sort.field, sort.direction));
}

function compareCases(a, b, field, direction) {
  let first = a[field];
  let second = b[field];
  if (field === "status") {
    first = STATUS_ORDER.indexOf(a.status);
    second = STATUS_ORDER.indexOf(b.status);
  } else if (field === "received_at") {
    first = toTimestamp(a.received_at);
    second = toTimestamp(b.received_at);
  } else if (field === "amount") {
    first = toNumber(first);
    second = toNumber(second);
  } else {
    first = String(first || "").toLocaleLowerCase("vi");
    second = String(second || "").toLocaleLowerCase("vi");
  }
  const result = first < second ? -1 : first > second ? 1 : 0;
  return direction === "asc" ? result : -result;
}

export function suggestedBatchName(now = new Date()) {
  const day = String(now.getDate()).padStart(2, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const year = String(now.getFullYear()).slice(-2);
  return `GN2${day}${month}${year}`;
}

export function isValidBatchName(value) {
  const match = /^GN2(\d{2})(\d{2})(\d{2})$/.exec(value);
  if (!match) return false;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = 2000 + Number(match[3]);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day;
}

export function batchStatusLabel(status) {
  return ({ READY: "Sẵn sàng", CREATED: "Đã tạo", GENERATED: "Đã tạo", COMPLETED: "Hoàn tất", FAILED: "Lỗi" })[status]
    || String(status || "Sẵn sàng").replaceAll("_", " ");
}

export function formatMetadataValue(value) {
  if (Array.isArray(value)) return value.map(formatMetadataValue).join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Có" : "Không";
  return displayValue(value);
}

export function humanizeKey(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function caseTypeMeta(caseType) {
  return TYPE_META[caseType] || TYPE_META.UNKNOWN;
}
