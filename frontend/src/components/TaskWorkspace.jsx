import React, { useEffect, useMemo, useState } from "react";

import { API } from "../constants.js";
import { translate } from "../i18n.js";
import { suggestedBatchName } from "../utils.js";
import { CaseTable } from "./CaseTable.jsx";
import { MailPdfPanel } from "./MailPdfPanel.jsx";
import { M365MailboxPanel } from "./M365MailboxPanel.jsx";
import { UploadWorkspace } from "./UploadWorkspace.jsx";

export { TranWorkspace } from "./TranWorkspace.jsx";

export function NganWorkspace({
  batches,
  capabilities,
  cases,
  clearingTestData,
  language,
  mailPdfBusyHandle,
  mailPdfDownloads,
  resetting,
  statusBusy,
  testDataClearVersion,
  onClearTestData,
  onCreateMailPdf,
  onEmailUpload,
  onMailboxSynced,
  onOpenBatch,
  onOpenCase,
  onReset,
  onSupplierUpload,
  onUpdateStatus,
}) {
  const [batchName, setBatchName] = useState(suggestedBatchName());
  const [invoiceStart, setInvoiceStart] = useState("1");
  const [accountingError, setAccountingError] = useState("");
  const readyCases = useMemo(
    () => cases.filter((item) => item.status === "READY_FOR_ACCOUNTING"),
    [cases],
  );
  const latestBatches = useMemo(
    () => [...batches]
      .sort((first, second) => new Date(second.created_at || 0) - new Date(first.created_at || 0))
      .slice(0, 5),
    [batches],
  );

  useEffect(() => {
    setBatchName(suggestedBatchName());
    setInvoiceStart("1");
    setAccountingError("");
  }, [testDataClearVersion]);

  function openAccountingBatch() {
    const parsed = Number(invoiceStart);
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 999999) {
      setAccountingError(translate(language, "invoiceStartInvalid"));
      return;
    }
    setAccountingError("");
    onOpenBatch(batchName, readyCases, parsed);
  }

  return (
    <>
      <div className="meta ngan-queue-line">
        {translate(language, "pendingQueue")} <strong>{readyCases.length}</strong>
      </div>

      <UploadWorkspace
        capabilities={capabilities}
        clearingTestData={clearingTestData}
        language={language}
        resetting={resetting}
        testDataClearVersion={testDataClearVersion}
        onClearTestData={onClearTestData}
        onEmailUpload={onEmailUpload}
        onReset={onReset}
        onSupplierUpload={onSupplierUpload}
      />

      <M365MailboxPanel
        capabilities={capabilities}
        language={language}
        onSynced={onMailboxSynced}
        refreshVersion={testDataClearVersion}
        role="ngan"
      />

      <section className="panel operation-panel">
        <h3>{translate(language, "accountingPanel")}</h3>
        <div className="panel-row">
          <label className="small" htmlFor="accounting-batch">{translate(language, "batchName")}</label>
          <input
            id="accounting-batch"
            type="text"
            value={batchName}
            maxLength="9"
            placeholder="GN2140826"
            onChange={(event) => setBatchName(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""))}
          />
          <label className="small" htmlFor="accounting-start">{translate(language, "startNumber")}</label>
          <input
            id="accounting-start"
            className="short-input"
            type="number"
            min="0"
            max="999999"
            step="1"
            value={invoiceStart}
            aria-invalid={accountingError ? "true" : undefined}
            onChange={(event) => {
              setInvoiceStart(event.target.value);
              setAccountingError("");
            }}
          />
          <button
            className="btn"
            type="button"
            disabled={!readyCases.length}
            onClick={openAccountingBatch}
          >
            {translate(language, "runAccounting")}
          </button>
          {!readyCases.length && <span className="result-file none">{translate(language, "noReadyCases")}</span>}
        </div>
        {accountingError && <div className="inline-error" role="alert">{accountingError}</div>}
      </section>

      <MailPdfPanel
        batchName={batchName}
        capabilities={capabilities}
        cases={cases}
        language={language}
        resetVersion={testDataClearVersion}
      />

      <section className="panel operation-panel batch-results">
        <h3>{translate(language, "recentBatches")}</h3>
        {latestBatches.length ? (
          <div className="result-list">
            {latestBatches.map((batch) => (
              <div className="result-file" key={batch.id}>
                <span>{batch.batch_name} · {batch.case_count} case</span>
                {batch.download_url ? (
                  <a href={`${API.batches}/${encodeURIComponent(batch.api_id)}/download`} download>⬇ {batch.output_name || "Excel"}</a>
                ) : <span className="none">{translate(language, "noBatches")}</span>}
              </div>
            ))}
          </div>
        ) : <div className="disabled-note">{translate(language, "noBatches")}</div>}
      </section>

      <CaseTable
        cases={readyCases}
        language={language}
        mailPdfBusyHandle={mailPdfBusyHandle}
        mailPdfCapabilities={capabilities}
        mailPdfDownloads={mailPdfDownloads}
        statusBusy={statusBusy}
        showFilters={false}
        onCreateMailPdf={onCreateMailPdf}
        onOpen={onOpenCase}
        onUpdateStatus={onUpdateStatus}
      />
    </>
  );
}
