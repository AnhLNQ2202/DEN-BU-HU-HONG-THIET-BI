import { API, STATUS_META } from "./constants.js";
import { normalizeCaseType, normalizeStatus, toNumber } from "./utils.js";

function normalizeWarnings(value) {
  if (value == null || value === "") return [];
  return (Array.isArray(value) ? value : [value])
    .map((item) => {
      if (typeof item === "string") return item.trim();
      if (item && typeof item === "object") {
        return String(item.message || item.title || item.code || JSON.stringify(item));
      }
      return String(item || "").trim();
    })
    .filter(Boolean);
}

export function normalizeCase(raw = {}, index = 0) {
  const apiId = raw.id ?? raw.case_id ?? `CASE-${index + 1}`;
  return {
    ...raw,
    api_id: apiId,
    id: String(apiId),
    case_type: normalizeCaseType(raw.case_type ?? raw.type),
    status: normalizeStatus(raw.status),
    domain: String(raw.domain ?? raw.employee_domain ?? "").trim(),
    employee_name: String(raw.employee_name ?? raw.employee ?? "").trim(),
    asset_code: String(raw.asset_code ?? raw.asset ?? "").trim(),
    asset_name: String(raw.asset_name ?? "").trim(),
    received_at: raw.received_at ?? raw.received_date ?? raw.created_at ?? null,
    amount: toNumber(raw.amount ?? raw.compensation_amount ?? raw.total_amount),
    residual_value: toNumber(raw.residual_value ?? raw.depreciation_amount),
    responsibility_fee: toNumber(raw.responsibility_fee ?? raw.repair_cost),
    repair_status: String(raw.repair_status ?? "").trim(),
    supplier_number: raw.supplier_number == null ? "" : String(raw.supplier_number).trim(),
    supplier_site: String(raw.supplier_site ?? "").trim(),
    supplier_name: String(raw.supplier_name ?? "").trim(),
    warnings: normalizeWarnings(raw.warnings),
    source_file: String(raw.source_file ?? raw.email_file ?? "").trim(),
    metadata: raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {},
    created_at: raw.created_at ?? null,
    updated_at: raw.updated_at ?? null,
  };
}

function normalizeIssue(raw, index) {
  if (typeof raw === "string") {
    return {
      id: `issue-${index}`,
      severity: "warning",
      title: "Cảnh báo dữ liệu",
      message: raw,
      case_id: null,
    };
  }

  const level = String(raw?.severity ?? raw?.level ?? raw?.type ?? "warning").toLowerCase();
  const severity = /error|critical|high/.test(level)
    ? "error"
    : /info|low/.test(level)
      ? "info"
      : "warning";
  return {
    ...raw,
    id: String(raw?.id ?? raw?.issue_id ?? `issue-${index}`),
    severity,
    title: String(raw?.title ?? raw?.code ?? "Cảnh báo dữ liệu"),
    message: String(raw?.message ?? raw?.detail ?? raw?.description ?? "Cần kiểm tra hồ sơ này."),
    case_id: raw?.case_id == null ? null : String(raw.case_id),
  };
}

function issuesFromCases(cases) {
  return cases.flatMap((caseItem) => caseItem.warnings.map((warning, index) => ({
    id: `${caseItem.id}-warning-${index}`,
    severity: "warning",
    title: `Hồ sơ ${caseItem.id}`,
    message: warning,
    case_id: caseItem.id,
  })));
}

function normalizeBatch(raw = {}, index = 0) {
  const apiId = raw.id ?? raw.batch_id ?? raw.batch_name ?? `batch-${index + 1}`;
  const caseIds = Array.isArray(raw.case_ids) ? raw.case_ids : [];
  return {
    ...raw,
    api_id: apiId,
    id: String(apiId),
    batch_name: String(raw.batch_name ?? raw.name ?? apiId),
    case_count: toNumber(raw.case_count ?? raw.total_cases ?? caseIds.length),
    total_amount: toNumber(raw.total_amount ?? raw.amount),
    created_at: raw.created_at ?? raw.created_date ?? null,
    status: String(raw.status ?? "READY").trim().toUpperCase(),
    download_url: raw.download_url || null,
  };
}

function countBy(items, field) {
  return items.reduce((counts, item) => {
    counts[item[field]] = (counts[item[field]] || 0) + 1;
    return counts;
  }, {});
}

function normalizeCountMap(value, normalizer) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.entries(value).reduce((result, [key, count]) => {
    const normalizedKey = normalizer(key);
    result[normalizedKey] = (result[normalizedKey] || 0) + toNumber(count);
    return result;
  }, {});
}

function normalizeSummary(raw, cases, issues) {
  const suppliedWarnings = Array.isArray(raw?.warnings)
    ? raw.warnings.length
    : toNumber(raw?.warnings ?? raw?.warning_count, Number.NaN);
  return {
    total: toNumber(raw?.total ?? raw?.total_cases, cases.length),
    total_amount: toNumber(
      raw?.total_amount ?? raw?.compensation_amount,
      cases.reduce((sum, item) => sum + item.amount, 0),
    ),
    by_type: {
      ...countBy(cases, "case_type"),
      ...normalizeCountMap(raw?.by_type, normalizeCaseType),
    },
    by_status: {
      ...countBy(cases, "status"),
      ...normalizeCountMap(raw?.by_status, normalizeStatus),
    },
    warnings: Number.isFinite(suppliedWarnings) ? suppliedWarnings : issues.length,
  };
}

export function normalizeDashboard(payload) {
  const root = payload?.data && typeof payload.data === "object" ? payload.data : payload || {};
  const cases = Array.isArray(root.cases) ? root.cases.map(normalizeCase) : [];
  const explicitIssues = Array.isArray(root.issues) ? root.issues.map(normalizeIssue) : [];
  const issues = explicitIssues.length ? explicitIssues : issuesFromCases(cases);
  return {
    cases,
    issues,
    batches: Array.isArray(root.batches) ? root.batches.map(normalizeBatch) : [],
    summary: normalizeSummary(root.summary || {}, cases, issues),
  };
}

export async function request(path, options = {}) {
  const requestOptions = {
    method: options.method || "GET",
    headers: { Accept: "application/json", ...(options.headers || {}) },
    signal: options.signal,
  };
  if (options.body !== undefined) {
    requestOptions.headers["Content-Type"] = "application/json";
    requestOptions.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(path, requestOptions);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new Error("Không thể kết nối tới máy chủ. Vui lòng kiểm tra dịch vụ và thử lại.", { cause: error });
  }

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }
  if (!response.ok) {
    const detail = data?.detail;
    const message = data?.message || data?.error || (typeof detail === "string" ? detail : null);
    throw new Error(message || `Yêu cầu thất bại (${response.status}).`);
  }
  return data || {};
}

export const dashboardApi = {
  load: (signal) => request(API.dashboard, { signal }).then(normalizeDashboard),
  ingest: () => request(API.ingest, { method: "POST" }),
  reset: () => request(API.reset, { method: "POST" }),
  updateStatus: (caseId, status) => request(`${API.cases}/${encodeURIComponent(caseId)}/status`, {
    method: "PATCH",
    body: { status },
  }),
  caseDetail: (caseId, signal) => request(`${API.cases}/${encodeURIComponent(caseId)}`, { signal }),
  createBatch: (batchName, caseIds) => request(API.batches, {
    method: "POST",
    body: { batch_name: batchName, case_ids: caseIds },
  }),
};

export function statusLabel(status) {
  return STATUS_META[status]?.label || status;
}
