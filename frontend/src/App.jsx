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
import { statusLabel, translate } from "./i18n.js";
import { filterAndSortCases, numberFormatter, toNumber } from "./utils.js";

const EMPTY_DASHBOARD = Object.freeze({ cases: [], issues: [], batches: [], summary: null });
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
  const [batchCases, setBatchCases] = useState([]);
  const [initialBatchName, setInitialBatchName] = useState("");
  const [ingesting, setIngesting] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
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

  function openBatch(batchName, cases) {
    setInitialBatchName(batchName);
    setBatchCases(cases);
    setBatchOpen(true);
  }

  async function ingestEmails() {
    setIngesting(true);
    try {
      const result = await dashboardApi.ingest();
      const ingested = toNumber(result?.ingested ?? result?.created ?? result?.count);
      const warningCount = Array.isArray(result?.warnings) ? result.warnings.length : 0;
      const unknownCount = Array.isArray(result?.unknown_files) ? result.unknown_files.length : 0;
      if (warningCount || unknownCount) {
        pushToast(
          ingested === 0 ? "Không nạp được email" : "Nạp email có cảnh báo",
          `${numberFormatter.format(ingested)} hồ sơ · ${numberFormatter.format(unknownCount)} file không nạp được · ${numberFormatter.format(warningCount)} cảnh báo.`,
          "warning",
        );
      } else {
        pushToast("Nạp email hoàn tất", `${numberFormatter.format(ingested)} hồ sơ đã được cập nhật.`, "success");
      }
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không nạp được email", error.message, "error");
    } finally {
      setIngesting(false);
    }
  }

  async function resetDemo() {
    if (!window.confirm("Đặt lại toàn bộ dữ liệu demo về trạng thái ban đầu?")) return;
    setResetting(true);
    try {
      await dashboardApi.reset();
      setActiveCaseId(null);
      pushToast("Đã đặt lại demo", "Dữ liệu mẫu đã được khôi phục.", "success");
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không đặt lại được demo", error.message, "error");
    } finally {
      setResetting(false);
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

  async function createBatch(batchName, cases) {
    setBatchBusy(true);
    try {
      const result = await dashboardApi.createBatch(
        batchName,
        cases.map((item) => item.api_id),
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
      <Topbar language={language} onLanguageChange={setLanguage} />
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
                onFiltersChange={updateFilters}
                onOpen={setActiveCaseId}
                onRefresh={() => loadDashboard()}
                onUpdateStatus={updateCaseStatus}
              />
            </section>
            <section className={`tab-content ${activeTab === "ngan" ? "active" : ""}`}>
              <NganWorkspace
                batches={dashboard.batches}
                cases={dashboard.cases}
                ingesting={ingesting}
                language={language}
                resetting={resetting}
                statusBusy={statusBusy}
                onIngest={ingestEmails}
                onOpenBatch={openBatch}
                onOpenCase={setActiveCaseId}
                onReset={resetDemo}
                onUpdateStatus={updateCaseStatus}
              />
            </section>
            <section className={`tab-content ${activeTab === "tran" ? "active" : ""}`}>
              <TranWorkspace cases={dashboard.cases} language={language} />
            </section>
          </>
        )}
      </main>

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
        onClose={() => setBatchOpen(false)}
        onCreate={createBatch}
      />
      <ToastRegion toasts={toasts} onDismiss={dismissToast} />
      <IconSprite />
    </>
  );
}
