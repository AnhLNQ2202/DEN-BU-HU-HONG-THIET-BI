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

function safeApiDownloadUrl(value) {
  const url = String(value || "").trim();
  return url.startsWith("/api/") && !url.startsWith("//") ? url : null;
}

function safeExternalUrl(value, allowedHosts) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:"
      && allowedHosts.has(url.hostname.toLowerCase())
      && !url.username
      && !url.password
      && !url.port
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function m365Path(role, suffix = "") {
  if (!["ngan", "tran"].includes(role)) throw new Error("Microsoft 365 role is invalid.");
  return `${API.m365}/${role}${suffix}`;
}

function normalizeM365Folder(raw = {}) {
  const id = String(raw?.id || "").trim();
  if (!id) return null;
  return {
    id,
    display_name: String(raw?.display_name || "").trim() || "—",
    path: String(raw?.path || "").trim(),
    child_folder_count: Math.max(0, toNumber(raw?.child_folder_count)),
  };
}

function normalizeM365Status(raw = {}, role = "") {
  const account = raw?.account && typeof raw.account === "object"
    ? {
      display_name: String(raw.account.display_name || "").trim(),
      email: String(raw.account.email || "").trim(),
    }
    : null;
  return {
    configured: raw?.configured === true,
    role: ["ngan", "tran"].includes(raw?.role) ? raw.role : role,
    connected: raw?.connected === true,
    account,
    selected_folder: normalizeM365Folder(raw?.selected_folder),
    cursor_ready: raw?.cursor_ready === true,
    storage: raw?.storage === "memory" ? "memory" : null,
    background_sync: raw?.background_sync === true,
  };
}

function normalizeM365Sync(raw = {}) {
  return {
    ...raw,
    folder: normalizeM365Folder(raw?.folder),
    fetched_count: Math.max(0, toNumber(raw?.fetched_count)),
    ingested: Math.max(0, toNumber(raw?.ingested)),
    case_ids: Array.isArray(raw?.case_ids)
      ? raw.case_ids.map((item) => String(item || "").trim()).filter(Boolean)
      : [],
    warnings: normalizeWarnings(raw?.warnings),
    unknown_files: normalizeWarnings(raw?.unknown_files),
    skipped_files: normalizeWarnings(raw?.skipped_files),
    has_more: raw?.has_more === true,
    cursor_ready: raw?.cursor_ready === true,
  };
}

function normalizeSourceEml(raw = {}) {
  const metadata = raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
  const source = raw.source_eml && typeof raw.source_eml === "object" ? raw.source_eml : {};
  const handle = String(
    source.handle ?? raw.mail_artifact_handle ?? metadata.mail_artifact_handle ?? "",
  ).trim();
  if (!/^eml-sha256-[0-9a-f]{64}$/.test(handle)) return null;
  return {
    handle,
    filename: String(
      source.filename
        ?? raw.mail_artifact_filename
        ?? metadata.mail_artifact_filename
        ?? "source.eml",
    ).trim() || "source.eml",
    download_url: safeApiDownloadUrl(source.download_url),
  };
}

function normalizeIndividualMailPdf(raw = {}) {
  return {
    ...raw,
    output_id: String(raw.output_id || "").trim(),
    download_url: safeApiDownloadUrl(raw.download_url),
  };
}

function normalizeMailPdfBatch(raw = {}) {
  return {
    ...raw,
    batch_id: String(raw.batch_id || "").trim(),
    merged_download_url: safeApiDownloadUrl(raw.merged_download_url),
    warnings: normalizeWarnings(raw.warnings),
    items: Array.isArray(raw.items)
      ? raw.items.map((item, index) => ({
        ...item,
        index: toNumber(item?.index, index + 1),
        source_pages: toNumber(item?.source_pages),
        output_pages: toNumber(item?.output_pages),
        padded_pages: toNumber(item?.padded_pages),
        truncated_pages: toNumber(item?.truncated_pages),
        warnings: normalizeWarnings(item?.warnings),
        download_url: safeApiDownloadUrl(item?.download_url),
      }))
      : [],
  };
}

function normalizeReferenceItem(raw = {}) {
  const source = ["uploaded", "configured"].includes(raw?.source) ? raw.source : null;
  return {
    configured: raw?.configured === true,
    available: raw?.available === true,
    source,
  };
}

function normalizeTranReferenceStatus(raw = {}) {
  const status = raw?.status && typeof raw.status === "object" ? raw.status : raw;
  return {
    fa_gl: normalizeReferenceItem(status?.fa_gl),
    ccdc: normalizeReferenceItem(status?.ccdc),
    managed_updated_at: status?.managed_updated_at || null,
  };
}

function normalizeTranResolution(raw = {}) {
  return {
    asset: raw?.asset && typeof raw.asset === "object" ? raw.asset : null,
    preview: raw?.preview && typeof raw.preview === "object" ? raw.preview : null,
    notes: normalizeWarnings(raw?.notes),
    issues: normalizeWarnings(raw?.issues),
    fa_status: String(raw?.fa_status || "").trim().toUpperCase(),
    classification_status: String(raw?.classification_status || "").trim().toUpperCase(),
    ready: raw?.ready === true,
  };
}

function normalizeTranResolve(raw = {}) {
  return {
    ...raw,
    ready: raw?.ready === true,
    review_required: raw?.review_required === true,
    results: Array.isArray(raw?.results) ? raw.results.map(normalizeTranResolution) : [],
    mail_table_html: typeof raw?.mail_table_html === "string" ? raw.mail_table_html : null,
  };
}

function normalizeTranOutput(raw = {}) {
  return {
    ...raw,
    output_id: /^[0-9a-f]{32}$/.test(String(raw?.output_id || ""))
      ? String(raw.output_id)
      : "",
    download_url: safeApiDownloadUrl(raw?.download_url),
    workbook_download_url: safeApiDownloadUrl(raw?.workbook_download_url),
    draft_download_url: safeApiDownloadUrl(raw?.draft_download_url),
    sent: raw?.sent === true,
  };
}

function normalizeTranOutlookOutput(raw = {}) {
  const outlookDraft = raw?.outlook_draft && typeof raw.outlook_draft === "object"
    ? {
      subject: String(raw.outlook_draft.subject || "").trim(),
      web_url: safeExternalUrl(
        raw.outlook_draft.web_url,
        new Set(["outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"]),
      ),
    }
    : null;
  return {
    ...normalizeTranOutput(raw),
    outlook_draft: outlookDraft,
  };
}

function normalizeCompanionPairing(raw = {}) {
  const code = String(raw?.code || "").trim().toUpperCase();
  const role = ["ngan", "tran"].includes(raw?.role) ? raw.role : "";
  const clientType = ["outlook_addin", "local_bridge"].includes(raw?.client_type)
    ? raw.client_type
    : "";
  if (!code || !role || !clientType) {
    throw new Error("Server did not return a valid Outlook pairing code.");
  }
  return {
    code,
    role,
    client_type: clientType,
    expires_in_seconds: Math.max(0, toNumber(raw?.expires_in_seconds)),
    expires_at: raw?.expires_at || null,
  };
}

function normalizeCompanionDraft(raw = {}) {
  return {
    ...normalizeTranOutput(raw),
    package_id: /^[0-9a-f]{32}$/.test(String(raw?.package_id || ""))
      ? String(raw.package_id)
      : "",
    expires_in_seconds: Math.max(0, toNumber(raw?.expires_in_seconds)),
  };
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
    source_eml: normalizeSourceEml(raw),
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
    retained_source_count: toNumber(root.retained_source_count),
    capabilities: {
      ...(root.capabilities || {}),
      test_reset: root.capabilities?.test_reset === true,
      demo_reset: root.capabilities?.demo_reset === true,
      raw_eml_retention: root.capabilities?.raw_eml_retention === true,
      source_eml_download: root.capabilities?.source_eml_download === true,
      tran_reference_upload: root.capabilities?.tran_reference_upload === true,
      tran_lookup: root.capabilities?.tran_lookup === true,
      tran_workbook_export: root.capabilities?.tran_workbook_export === true,
      tran_draft: root.capabilities?.tran_draft === true,
      m365_configured: root.capabilities?.m365_configured === true,
      m365_ngan: root.capabilities?.m365_ngan === true,
      m365_tran: root.capabilities?.m365_tran === true,
      tran_outlook_draft: root.capabilities?.tran_outlook_draft === true,
      companion_pairing: root.capabilities?.companion_pairing === true,
      outlook_addin: root.capabilities?.outlook_addin === true,
      local_bridge: root.capabilities?.local_bridge === true,
      tran_companion_draft: root.capabilities?.tran_companion_draft === true,
      mail_pdf_individual: root.capabilities?.mail_pdf_individual === true,
      mail_pdf_batch: root.capabilities?.mail_pdf_batch === true,
      mail_pdf_backend: ["word-windows", "weasyprint-cloud"].includes(
        root.capabilities?.mail_pdf_backend,
      ) ? root.capabilities.mail_pdf_backend : null,
    },
  };
}

export async function request(path, options = {}) {
  const requestOptions = {
    method: options.method || "GET",
    headers: { Accept: "application/json", ...(options.headers || {}) },
    credentials: "same-origin",
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
    const error = new Error(message || `Yêu cầu thất bại (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data || {};
}

function responseMessage(data, status) {
  const detail = data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && typeof detail.message === "string") {
    return detail.message;
  }
  if (typeof data?.message === "string" && data.message.trim()) return data.message;
  if (typeof data?.error === "string" && data.error.trim()) return data.error;
  return `Yêu cầu tải file thất bại (${status}).`;
}

/** Upload multipart data with real browser upload progress and abort support. */
export function uploadMultipart(path, formData, { signal, onProgress, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;

    const abortError = () => {
      const error = new Error("Đã huỷ tải file.");
      error.name = "AbortError";
      return error;
    };
    const onSignalAbort = () => xhr.abort();
    const cleanup = () => signal?.removeEventListener("abort", onSignalAbort);
    const settle = (callback, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback(value);
    };

    if (signal?.aborted) {
      reject(abortError());
      return;
    }

    xhr.open("POST", path);
    xhr.setRequestHeader("Accept", "application/json");
    Object.entries(headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));
    xhr.withCredentials = true;
    xhr.upload.addEventListener("progress", (event) => {
      const percent = event.lengthComputable && event.total > 0
        ? Math.min(100, Math.round((event.loaded / event.total) * 100))
        : null;
      onProgress?.({ loaded: event.loaded, total: event.total, percent });
    });
    xhr.addEventListener("load", () => {
      let data = {};
      if (xhr.responseText) {
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          data = { message: xhr.responseText };
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.({ loaded: 1, total: 1, percent: 100 });
        settle(resolve, data);
      } else {
        settle(reject, new Error(responseMessage(data, xhr.status)));
      }
    });
    xhr.addEventListener("error", () => {
      settle(reject, new Error("Không thể kết nối tới máy chủ để tải file."));
    });
    xhr.addEventListener("abort", () => settle(reject, abortError()));
    signal?.addEventListener("abort", onSignalAbort, { once: true });
    xhr.send(formData);
  });
}

export const dashboardApi = {
  load: (signal) => request(API.dashboard, { signal }).then(normalizeDashboard),
  ingest: () => request(API.ingest, { method: "POST" }),
  uploadSuppliers: (activeFile, inactiveFile, options = {}) => {
    const formData = new FormData();
    formData.append("active_file", activeFile, activeFile.name);
    formData.append("inactive_file", inactiveFile, inactiveFile.name);
    return uploadMultipart(API.supplierUpload, formData, {
      ...options,
      headers: { ...options.headers, "X-Asset-Hub-Upload": "supplier-v1" },
    });
  },
  supplierStatus: (signal) => request(API.supplierStatus, { signal }),
  uploadEmails: (files, options = {}) => {
    const formData = new FormData();
    Array.from(files).forEach((file) => formData.append("files", file, file.name));
    return uploadMultipart(API.emailUpload, formData, {
      ...options,
      headers: { ...options.headers, "X-Asset-Hub-Upload": "email-v1" },
    });
  },
  tranReferenceStatus: (signal) => request(API.tranReferenceStatus, { signal })
    .then(normalizeTranReferenceStatus),
  uploadTranReferences: (faGlFile, ccdcFile, clearCcdc = false, options = {}) => {
    const formData = new FormData();
    formData.append("fa_gl_file", faGlFile, faGlFile.name);
    if (ccdcFile) formData.append("ccdc_file", ccdcFile, ccdcFile.name);
    if (clearCcdc) formData.append("clear_ccdc", "true");
    return uploadMultipart(API.tranReferenceUpload, formData, {
      ...options,
      headers: { ...options.headers, "X-Asset-Hub-Upload": "tran-reference-v1" },
    }).then((raw) => ({
      ...raw,
      status: normalizeTranReferenceStatus(raw?.status),
    }));
  },
  resolveTranAssets: (assets, signal) => request(API.tranResolve, {
    method: "POST",
    signal,
    body: { assets },
  }).then(normalizeTranResolve),
  exportTranWorkbook: (assets, processingDate, yearSheet, signal) => request(
    API.tranWorkbooks,
    {
      method: "POST",
      signal,
      body: {
        assets,
        ...(processingDate ? { processing_date: processingDate } : {}),
        ...(yearSheet ? { year_sheet: yearSheet } : {}),
      },
    },
  ).then(normalizeTranOutput),
  createTranDraft: (
    assets,
    sourceBindings,
    mailArtifactHandle,
    bodyIntro,
    processingDate,
    yearSheet,
    signal,
  ) => request(API.tranDrafts, {
    method: "POST",
    signal,
    body: {
      assets,
      source_bindings: sourceBindings,
      mail_artifact_handle: mailArtifactHandle,
      body_intro: bodyIntro,
      ...(processingDate ? { processing_date: processingDate } : {}),
      ...(yearSheet ? { year_sheet: yearSheet } : {}),
    },
  }).then(normalizeTranOutput),
  createTranOutlookDraft: (
    assets,
    sourceBindings,
    mailArtifactHandle,
    bodyIntro,
    processingDate,
    yearSheet,
    signal,
  ) => request(API.tranOutlookDrafts, {
    method: "POST",
    signal,
    body: {
      assets,
      source_bindings: sourceBindings,
      mail_artifact_handle: mailArtifactHandle,
      body_intro: bodyIntro,
      ...(processingDate ? { processing_date: processingDate } : {}),
      ...(yearSheet ? { year_sheet: yearSheet } : {}),
    },
  }).then(normalizeTranOutlookOutput),
  createTranCompanionDraft: (
    assets,
    sourceBindings,
    mailArtifactHandle,
    bodyIntro,
    processingDate,
    yearSheet,
    signal,
  ) => request(API.tranCompanionDrafts, {
    method: "POST",
    signal,
    body: {
      assets,
      source_bindings: sourceBindings,
      mail_artifact_handle: mailArtifactHandle,
      body_intro: bodyIntro,
      ...(processingDate ? { processing_date: processingDate } : {}),
      ...(yearSheet ? { year_sheet: yearSheet } : {}),
    },
  }).then(normalizeCompanionDraft),
  createCompanionPairing: (role, clientType, signal) => {
    if (!["ngan", "tran"].includes(role)) throw new Error("Outlook role is invalid.");
    if (!["outlook_addin", "local_bridge"].includes(clientType)) {
      throw new Error("Outlook connection type is invalid.");
    }
    return request(API.companionPairings, {
      method: "POST",
      signal,
      headers: { "X-Asset-Hub-Action": "companion-pair-v1" },
      body: { role, client_type: clientType },
    }).then((raw) => normalizeCompanionPairing(raw?.pairing || raw));
  },
  m365Status: (role, signal) => request(m365Path(role, "/status"), { signal })
    .then((raw) => normalizeM365Status(raw, role)),
  connectM365: (role, signal) => request(m365Path(role, "/connect"), {
    method: "POST",
    signal,
    body: { return_to: "/" },
  }).then((raw) => ({
    ...raw,
    authorization_url: safeExternalUrl(
      raw?.authorization_url,
      new Set(["login.microsoftonline.com"]),
    ),
  })),
  disconnectM365: (role, signal) => request(m365Path(role, "/disconnect"), {
    method: "POST",
    signal,
    headers: { "X-Asset-Hub-Action": "m365-disconnect-v1" },
    body: {},
  }),
  m365Folders: (role, signal) => request(m365Path(role, "/folders"), { signal })
    .then((raw) => ({
      ...raw,
      folders: Array.isArray(raw?.folders)
        ? raw.folders.map(normalizeM365Folder).filter(Boolean)
        : [],
    })),
  selectM365Folder: (role, folderId, signal) => request(m365Path(role, "/folder"), {
    method: "POST",
    signal,
    body: { folder_id: folderId },
  }).then((raw) => ({
    ...raw,
    selected_folder: normalizeM365Folder(raw?.selected_folder),
    cursor_ready: raw?.cursor_ready === true,
  })),
  syncM365: (role, signal) => request(m365Path(role, "/sync"), {
    method: "POST",
    signal,
    headers: { "X-Asset-Hub-Action": "m365-sync-v1" },
    body: {},
  }).then(normalizeM365Sync),
  createIndividualMailPdf: (mailArtifactHandle, signal) => request(API.mailPdfIndividual, {
    method: "POST",
    signal,
    body: { mail_artifact_handle: mailArtifactHandle },
  }).then(normalizeIndividualMailPdf),
  createMailPdfBatch: (
    mailArtifactHandles,
    pagesPerMail,
    overflowPolicy = "fail",
    batchName = "",
    signal,
  ) => request(API.mailPdfBatches, {
    method: "POST",
    signal,
    body: {
      mail_artifact_handles: mailArtifactHandles,
      pages_per_mail: pagesPerMail,
      overflow_policy: overflowPolicy,
      ...(String(batchName || "").trim()
        ? { batch_name: String(batchName).trim().toUpperCase() }
        : {}),
    },
  }).then(normalizeMailPdfBatch),
  clearTestData: () => request(API.clearTestData, {
    method: "POST",
    headers: { "X-Asset-Hub-Action": "clear-test-data-v1" },
    body: { confirm: "CLEAR_TEST_DATA" },
  }),
  reset: () => request(API.reset, { method: "POST" }),
  updateStatus: (caseId, status) => request(`${API.cases}/${encodeURIComponent(caseId)}/status`, {
    method: "PATCH",
    body: { status },
  }),
  caseDetail: (caseId, signal) => request(`${API.cases}/${encodeURIComponent(caseId)}`, { signal }),
  createBatch: (batchName, caseIds, invoiceStart = 1) => request(API.batches, {
    method: "POST",
    body: { batch_name: batchName, case_ids: caseIds, invoice_start: invoiceStart },
  }),
  previewCompensation: (assets) => request(API.compensationPreview, {
    method: "POST",
    body: { assets },
  }),
};

export function statusLabel(status) {
  return STATUS_META[status]?.label || status;
}
