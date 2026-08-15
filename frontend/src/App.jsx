import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi } from "./api.js";
import { BatchDialog } from "./components/BatchDialog.jsx";
import { CaseDrawer } from "./components/CaseDrawer.jsx";
import { CaseTable } from "./components/CaseTable.jsx";
import { FatalError, LoadingState, ToastRegion } from "./components/Feedback.jsx";
import { Topbar } from "./components/Header.jsx";
import { IconSprite } from "./components/Icon.jsx";
import { KpiGrid } from "./components/KpiGrid.jsx";
import { NganWorkspace, TranWorkspace } from "./components/TaskWorkspace.jsx";
import { OutlookConnectionsDialog } from "./components/OutlookConnectionsDialog.jsx";
import { statusLabel, translate } from "./i18n.js";
import { filterAndSortCases, numberFormatter, toNumber } from "./utils.js";

const EMPTY_DASHBOARD = Object.freeze({
  cases: [],
  issues: [],
  batches: [],
  summary: null,
  retained_source_count: 0,
  capabilities: {
    test_reset: false,
    demo_reset: false,
    raw_eml_retention: false,
    source_eml_download: false,
    tran_reference_upload: false,
    tran_lookup: false,
    tran_workbook_export: false,
    tran_draft: false,
    m365_configured: false,
    m365_ngan: false,
    m365_tran: false,
    tran_outlook_draft: false,
    companion_pairing: false,
    outlook_addin: false,
    local_bridge: false,
    tran_companion_draft: false,
    mail_pdf_individual: false,
    mail_pdf_batch: false,
    mail_pdf_backend: null,
  },
});
const DEFAULT_SORT = Object.freeze({ field: "received_at", direction: "desc" });

function initialLanguage() {
  try {
    return window.localStorage.getItem("denbu_lang") === "en" ? "en" : "vi";
  } catch {
    return "vi";
  }
}

export default function App() {
  const [dashboard, setDashboard] = useState(EMPTY_DASHBOARD);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [fatalError, setFatalError] = useState("");
  const [connection, setConnection] = useState("loading");
  const [updatedAt, setUpdatedAt] = useState(null);
  const [language, setLanguage] = useState(initialLanguage);
  const [activeTab, setActiveTab] = useState("overview");
  const [lastTaskTab, setLastTaskTab] = useState("ngan");
  const [filters, setFilters] = useState({
    query: "",
    type: "ALL",
    status: "ALL",
    warning: "ALL",
  });
  const [activeCaseId, setActiveCaseId] = useState(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [outlookConnectionsOpen, setOutlookConnectionsOpen] = useState(false);
  const [tranOutlookConnected, setTranOutlookConnected] = useState(false);
  const [tranOutlookRefreshVersion, setTranOutlookRefreshVersion] = useState(0);
  const [batchCases, setBatchCases] = useState([]);
  const [initialBatchName, setInitialBatchName] = useState("");
  const [initialInvoiceStart, setInitialInvoiceStart] = useState(1);
  const [clearingTestData, setClearingTestData] = useState(false);
  const [testDataClearVersion, setTestDataClearVersion] = useState(0);
  const [resetting, setResetting] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const [mailPdfBusyHandle, setMailPdfBusyHandle] = useState("");
  const [mailPdfDownloads, setMailPdfDownloads] = useState({});
  const [toasts, setToasts] = useState([]);
  const requestId = useRef(0);
  const abortRef = useRef(null);
  const toastId = useRef(0);

  const pushToast = useCallback((title, message, type = "info") => {
    const id = ++toastId.current;
    setToasts((current) => [...current, { id, title, message, type }].slice(-4));
  }, []);
  const dismissToast = useCallback(
    (id) => setToasts((current) => current.filter((toast) => toast.id !== id)),
    [],
  );

  const loadDashboard = useCallback(async ({ initial = false, quiet = false } = {}) => {
    const currentRequest = ++requestId.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    if (initial) setLoading(true);
    else setRefreshing(true);
    setConnection("loading");

    try {
      const data = await dashboardApi.load(controller.signal);
      if (currentRequest !== requestId.current) return;
      setDashboard(data);
      setFatalError("");
      setConnection("online");
      setUpdatedAt(new Date());
    } catch (error) {
      if (error.name === "AbortError" || currentRequest !== requestId.current) return;
      setConnection("offline");
      if (initial || !dashboard.summary) setFatalError(error.message);
      else if (!quiet) pushToast("Không thể làm mới", error.message, "error");
    } finally {
      if (currentRequest === requestId.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [dashboard.summary, pushToast]);

  useEffect(() => {
    loadDashboard({ initial: true });
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("denbu_lang", language);
    } catch {
      // Language persistence is optional in restricted browser contexts.
    }
  }, [language]);

  const filteredCases = useMemo(
    () => filterAndSortCases(dashboard.cases, filters, DEFAULT_SORT),
    [dashboard.cases, filters],
  );
  const activeCase = useMemo(
    () => dashboard.cases.find((item) => item.id === activeCaseId) || null,
    [activeCaseId, dashboard.cases],
  );

  function switchTab(tab) {
    if (tab === "ngan" || tab === "tran") setLastTaskTab(tab);
    setActiveTab(tab);
  }

  function updateFilters(patch) {
    setFilters((current) => ({ ...current, ...patch }));
  }

  function openBatch(batchName, cases, invoiceStart = 1) {
    setInitialBatchName(batchName);
    setInitialInvoiceStart(invoiceStart);
    setBatchCases(cases);
    setBatchOpen(true);
  }

  async function uploadSuppliers({ activeFile, inactiveFile, signal, onProgress }) {
    const result = await dashboardApi.uploadSuppliers(activeFile, inactiveFile, {
      signal,
      onProgress,
    });
    const collisionCount = toNumber(
      result?.collision_count ?? result?.status?.collision_count,
    );
    const totalCount = toNumber(result?.total_count ?? result?.status?.total_count);
    pushToast(
      collisionCount ? "Supplier có domain cần kiểm tra" : "Đã cập nhật Supplier",
      `${numberFormatter.format(totalCount)} bản ghi · ${numberFormatter.format(collisionCount)} domain trùng.`,
      collisionCount ? "warning" : "success",
    );
    await loadDashboard({ quiet: true });
    return result;
  }

  async function uploadEmails({ files, signal, onProgress }) {
    const result = await dashboardApi.uploadEmails(files, { signal, onProgress });
    const ingested = toNumber(result?.ingested ?? result?.created ?? result?.count);
    const warningCount = Array.isArray(result?.warnings) ? result.warnings.length : 0;
    const skippedCount = Array.isArray(result?.skipped_files) ? result.skipped_files.length : 0;
    const unknownCount = Array.isArray(result?.unknown_files) ? result.unknown_files.length : 0;
    pushToast(
      warningCount || skippedCount || unknownCount ? "Nạp email có cảnh báo" : "Nạp email hoàn tất",
      `${numberFormatter.format(ingested)} hồ sơ · ${numberFormatter.format(skippedCount)} file bỏ qua theo rule · ${numberFormatter.format(unknownCount)} file không nạp được · ${numberFormatter.format(warningCount)} cảnh báo.`,
      warningCount || skippedCount || unknownCount ? "warning" : "success",
    );
    await loadDashboard({ quiet: true });
    return result;
  }

  async function resetDemo() {
    if (!window.confirm("Đặt lại toàn bộ dữ liệu demo về trạng thái ban đầu?")) return;
    setResetting(true);
    try {
      await dashboardApi.reset();
      setActiveCaseId(null);
      setMailPdfDownloads({});
      setTestDataClearVersion((current) => current + 1);
      pushToast("Đã đặt lại demo", "Dữ liệu mẫu đã được khôi phục.", "success");
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không đặt lại được demo", error.message, "error");
    } finally {
      setResetting(false);
    }
  }

  async function clearTestData() {
    const confirmed = window.confirm(
      "Xóa toàn bộ dữ liệu test? Tất cả case, batch, file output và dữ liệu Supplier đã upload sẽ bị xóa. Hành động này không thể hoàn tác.",
    );
    if (!confirmed) return;
    setClearingTestData(true);
    try {
      const result = await dashboardApi.clearTestData();
      setActiveCaseId(null);
      setBatchOpen(false);
      setBatchCases([]);
      setMailPdfDownloads({});
      setTestDataClearVersion((current) => current + 1);
      pushToast(
        "Đã xóa dữ liệu test",
        result?.message || "Case, batch, file output và Supplier reference đã được xóa.",
        "success",
      );
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không xóa được dữ liệu test", error.message, "error");
    } finally {
      setClearingTestData(false);
    }
  }

  async function updateCaseStatus(caseItem, nextStatus) {
    setStatusBusy(true);
    try {
      await dashboardApi.updateStatus(caseItem.api_id, nextStatus);
      pushToast(
        "Đã cập nhật trạng thái",
        `${caseItem.id} → ${statusLabel(language, nextStatus)}`,
        "success",
      );
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không cập nhật được", error.message, "error");
    } finally {
      setStatusBusy(false);
    }
  }

  async function createBatch(batchName, cases, invoiceStart) {
    setBatchBusy(true);
    try {
      const result = await dashboardApi.createBatch(
        batchName,
        cases.map((item) => item.api_id),
        invoiceStart,
      );
      setBatchOpen(false);
      pushToast(
        "Đã tạo batch",
        result?.message || `${batchName} đã sẵn sàng để tải xuống.`,
        "success",
      );
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không tạo được batch", error.message, "error");
    } finally {
      setBatchBusy(false);
    }
  }

  async function createCaseMailPdf(caseItem) {
    const handle = caseItem?.source_eml?.handle;
    if (!handle || mailPdfBusyHandle) return { result: null, error: null };
    setMailPdfBusyHandle(handle);
    try {
      const result = await dashboardApi.createIndividualMailPdf(handle);
      if (result.download_url) {
        setMailPdfDownloads((current) => ({
          ...current,
          [handle]: result.download_url,
        }));
      }
      pushToast(
        translate(language, "pdfToastReadyTitle"),
        translate(language, "pdfIndividualReady"),
        "success",
      );
      return { result, error: null };
    } catch (error) {
      pushToast(
        translate(language, "pdfToastErrorTitle"),
        error.message,
        "error",
      );
      return { result: null, error };
    } finally {
      setMailPdfBusyHandle("");
    }
  }

  const connectionText = translate(
    language,
    connection === "online"
      ? "connectionOnline"
      : connection === "offline"
        ? "connectionOffline"
        : "connectionLoading",
  );
  const updatedText = updatedAt
    ? updatedAt.toLocaleString(language === "en" ? "en-US" : "vi-VN")
    : "—";

  return (
    <>
      <a className="skip-link" href="#main-content">{translate(language, "overview")}</a>
      <Topbar
        language={language}
        onLanguageChange={setLanguage}
        onOpenOutlook={() => setOutlookConnectionsOpen(true)}
        outlookOpen={outlookConnectionsOpen}
      />
      <div className="meta meta-line" role="status">
        {translate(language, "updatedPrefix")} {updatedText} {translate(language, "updatedSuffix")}
        <span className={`connection-text connection-${connection}`}>● {connectionText}</span>
      </div>

      <nav className="tabs" aria-label="Dashboard">
        <button
          className={`tab-btn ${activeTab === "overview" ? "active" : ""}`}
          type="button"
          onClick={() => switchTab("overview")}
        >
          {translate(language, "overview")}
        </button>
        <div className="tab-dropdown">
          <button
            className={`tab-btn ${activeTab !== "overview" ? "active" : ""}`}
            type="button"
            onClick={() => switchTab(lastTaskTab)}
          >
            {translate(language, "task")} <span className="dropdown-arrow">▾</span>
          </button>
          <div className="dropdown-menu">
            <button
              className={activeTab === "ngan" ? "active-sub" : ""}
              type="button"
              onClick={() => switchTab("ngan")}
            >
              {translate(language, "nganTask")}
            </button>
            <button
              className={activeTab === "tran" ? "active-sub" : ""}
              type="button"
              onClick={() => switchTab("tran")}
            >
              {translate(language, "tranTask")}
            </button>
          </div>
        </div>
      </nav>

      <main id="main-content" className="dashboard-shell" tabIndex="-1">
        {fatalError && <FatalError message={fatalError} onRetry={() => loadDashboard({ initial: true })} />}
        {loading && <LoadingState />}
        {!loading && dashboard.summary && (
          <>
            <section className={`tab-content ${activeTab === "overview" ? "active" : ""}`}>
              <KpiGrid summary={dashboard.summary} language={language} />
              <CaseTable
                cases={filteredCases}
                filters={filters}
                language={language}
                refreshing={refreshing}
                statusBusy={statusBusy}
                mailPdfBusyHandle={mailPdfBusyHandle}
                mailPdfCapabilities={dashboard.capabilities}
                mailPdfDownloads={mailPdfDownloads}
                onFiltersChange={updateFilters}
                onCreateMailPdf={createCaseMailPdf}
                onOpen={setActiveCaseId}
                onRefresh={() => loadDashboard()}
                onUpdateStatus={updateCaseStatus}
              />
            </section>
            <section className={`tab-content ${activeTab === "ngan" ? "active" : ""}`}>
              <NganWorkspace
                batches={dashboard.batches}
                capabilities={dashboard.capabilities}
                cases={dashboard.cases}
                clearingTestData={clearingTestData}
                language={language}
                mailPdfBusyHandle={mailPdfBusyHandle}
                mailPdfDownloads={mailPdfDownloads}
                resetting={resetting}
                statusBusy={statusBusy}
                testDataClearVersion={testDataClearVersion}
                onEmailUpload={uploadEmails}
                onCreateMailPdf={createCaseMailPdf}
                onClearTestData={clearTestData}
                onOpenBatch={openBatch}
                onOpenCase={setActiveCaseId}
                onReset={resetDemo}
                onSupplierUpload={uploadSuppliers}
                onUpdateStatus={updateCaseStatus}
              />
            </section>
            <section className={`tab-content ${activeTab === "tran" ? "active" : ""}`}>
              <TranWorkspace
                capabilities={dashboard.capabilities}
                cases={dashboard.cases}
                language={language}
                onEmailUpload={uploadEmails}
                onMailboxSynced={() => loadDashboard({ quiet: true })}
                onOutlookMailboxInvalid={() => {
                  setTranOutlookConnected(false);
                  setTranOutlookRefreshVersion((current) => current + 1);
                }}
                onReferencesChanged={() => loadDashboard({ quiet: true })}
                outlookMailboxConnected={tranOutlookConnected}
                testDataClearVersion={testDataClearVersion}
              />
            </section>
          </>
        )}
      </main>

      <OutlookConnectionsDialog
        capabilities={dashboard.capabilities}
        language={language}
        onClose={() => setOutlookConnectionsOpen(false)}
        onMailboxSynced={() => loadDashboard({ quiet: true })}
        onTranStatusChange={(nextStatus) => setTranOutlookConnected(nextStatus?.connected === true)}
        open={outlookConnectionsOpen}
        preferredRole={activeTab === "overview" ? lastTaskTab : activeTab}
        refreshVersion={testDataClearVersion}
        tranRefreshVersion={tranOutlookRefreshVersion}
      />

      <CaseDrawer
        caseItem={activeCase}
        statusBusy={statusBusy}
        onClose={() => setActiveCaseId(null)}
        onUpdateStatus={updateCaseStatus}
      />
      <BatchDialog
        open={batchOpen}
        cases={batchCases}
        busy={batchBusy}
        initialBatchName={initialBatchName}
        initialInvoiceStart={initialInvoiceStart}
        onClose={() => setBatchOpen(false)}
        onCreate={createBatch}
      />
      <ToastRegion toasts={toasts} onDismiss={dismissToast} />
      <IconSprite />
    </>
  );
}
