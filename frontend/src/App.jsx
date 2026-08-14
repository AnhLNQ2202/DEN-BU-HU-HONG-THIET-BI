import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi } from "./api.js";
import { BatchDialog } from "./components/BatchDialog.jsx";
import { CaseDrawer } from "./components/CaseDrawer.jsx";
import { CaseTable } from "./components/CaseTable.jsx";
import { FatalError, LoadingState, ToastRegion } from "./components/Feedback.jsx";
import { Hero, Topbar } from "./components/Header.jsx";
import { IconSprite } from "./components/Icon.jsx";
import { KpiGrid } from "./components/KpiGrid.jsx";
import { Sidebar } from "./components/Sidebar.jsx";
import { STATUS_META } from "./constants.js";
import { filterAndSortCases, numberFormatter, toNumber } from "./utils.js";

const EMPTY_DASHBOARD = Object.freeze({ cases: [], issues: [], batches: [], summary: null });

export default function App() {
  const [dashboard, setDashboard] = useState(EMPTY_DASHBOARD);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [fatalError, setFatalError] = useState("");
  const [connection, setConnection] = useState("loading");
  const [updatedAt, setUpdatedAt] = useState(null);
  const [filters, setFilters] = useState({ query: "", type: "ALL", status: "ALL" });
  const [sort, setSort] = useState({ field: "received_at", direction: "desc" });
  const [selected, setSelected] = useState(() => new Set());
  const [activeCaseId, setActiveCaseId] = useState(null);
  const [batchOpen, setBatchOpen] = useState(false);
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
  const dismissToast = useCallback((id) => setToasts((current) => current.filter((toast) => toast.id !== id)), []);

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
      setSelected((current) => {
        const validIds = new Set(data.cases.map((item) => item.id));
        return new Set([...current].filter((id) => validIds.has(id)));
      });
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
  }, []); // The initial fetch intentionally runs once; later refreshes are explicit.

  const filteredCases = useMemo(
    () => filterAndSortCases(dashboard.cases, filters, sort),
    [dashboard.cases, filters, sort],
  );
  const activeCase = useMemo(
    () => dashboard.cases.find((item) => item.id === activeCaseId) || null,
    [activeCaseId, dashboard.cases],
  );
  const selectedCases = useMemo(
    () => dashboard.cases.filter((item) => selected.has(item.id)),
    [dashboard.cases, selected],
  );

  function updateFilters(patch) {
    setFilters((current) => ({ ...current, ...patch }));
  }

  function updateSort(field) {
    setSort((current) => current.field === field
      ? { field, direction: current.direction === "asc" ? "desc" : "asc" }
      : { field, direction: field === "amount" ? "desc" : "asc" });
  }

  function selectCase(caseId, checked) {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(caseId);
      else next.delete(caseId);
      return next;
    });
  }

  function selectVisible(cases, checked) {
    setSelected((current) => {
      const next = new Set(current);
      cases.forEach((item) => checked ? next.add(item.id) : next.delete(item.id));
      return next;
    });
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
          `${numberFormatter.format(ingested)} hồ sơ đã nạp · ${numberFormatter.format(unknownCount)} file không nạp được · ${numberFormatter.format(warningCount)} cảnh báo.`,
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
    if (!window.confirm("Đặt lại toàn bộ dữ liệu demo về trạng thái ban đầu? Thao tác này không thể hoàn tác.")) return;
    setResetting(true);
    try {
      await dashboardApi.reset();
      setSelected(new Set());
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
      pushToast("Đã cập nhật trạng thái", `${caseItem.id} → ${STATUS_META[nextStatus].label}`, "success");
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
      const result = await dashboardApi.createBatch(batchName, cases.map((item) => item.api_id));
      setBatchOpen(false);
      setSelected(new Set());
      pushToast("Đã tạo batch", result?.message || `${batchName} đã sẵn sàng để tải xuống.`, "success");
      await loadDashboard({ quiet: true });
    } catch (error) {
      pushToast("Không tạo được batch", error.message, "error");
    } finally {
      setBatchBusy(false);
    }
  }

  return (
    <>
      <Topbar connection={connection} updatedAt={updatedAt} />
      <main id="main-content" className="page-shell" tabIndex="-1">
        <Hero ingesting={ingesting} resetting={resetting} onIngest={ingestEmails} onReset={resetDemo} />
        {fatalError && <FatalError message={fatalError} onRetry={() => loadDashboard({ initial: true })} />}
        {loading && <LoadingState />}
        {!loading && dashboard.summary && <>
          <KpiGrid summary={dashboard.summary} />
          <section className="workspace-grid">
            <CaseTable
              cases={filteredCases}
              totalCases={dashboard.cases.length}
              filters={filters}
              sort={sort}
              selected={selected}
              refreshing={refreshing}
              onFiltersChange={updateFilters}
              onSort={updateSort}
              onSelect={selectCase}
              onSelectVisible={selectVisible}
              onClearSelection={() => setSelected(new Set())}
              onOpen={setActiveCaseId}
              onOpenBatch={() => setBatchOpen(true)}
              onRefresh={() => loadDashboard()}
              onIngest={ingestEmails}
            />
            <Sidebar issues={dashboard.issues} batches={dashboard.batches} cases={dashboard.cases} summary={dashboard.summary} onOpenCase={setActiveCaseId} />
          </section>
        </>}
      </main>

      <CaseDrawer caseItem={activeCase} statusBusy={statusBusy} onClose={() => setActiveCaseId(null)} onUpdateStatus={updateCaseStatus} />
      <BatchDialog open={batchOpen} cases={selectedCases} busy={batchBusy} onClose={() => setBatchOpen(false)} onCreate={createBatch} />
      <ToastRegion toasts={toasts} onDismiss={dismissToast} />
      <IconSprite />
    </>
  );
}
