import React, { useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi } from "../api.js";
import { translate } from "../i18n.js";
import { useToast } from "./Feedback.jsx";

export const MAIL_PDF_BATCH_LIMIT = 20;

function retainedArtifacts(cases) {
  const byHandle = new Map();
  cases.forEach((caseItem) => {
    const source = caseItem.source_eml;
    if (!source?.handle) return;
    const current = byHandle.get(source.handle);
    if (current) {
      current.caseIds.push(caseItem.id);
      if (caseItem.asset_code && !current.assetCodes.includes(caseItem.asset_code)) {
        current.assetCodes.push(caseItem.asset_code);
      }
      if (!current.downloadUrl && source.download_url) current.downloadUrl = source.download_url;
      return;
    }
    byHandle.set(source.handle, {
      handle: source.handle,
      filename: source.filename,
      downloadUrl: source.download_url,
      caseIds: [caseItem.id],
      assetCodes: caseItem.asset_code ? [caseItem.asset_code] : [],
    });
  });
  return [...byHandle.values()];
}

function backendLabel(language, backend) {
  if (backend === "word-windows") return translate(language, "pdfBackendWord");
  if (backend === "weasyprint-cloud") return translate(language, "pdfBackendCloud");
  return translate(language, "pdfBackendUnavailable");
}

function itemWarnings(batch, language) {
  if (!batch) return [];
  const warnings = [...(batch.warnings || [])];
  (batch.items || []).forEach((item) => {
    (item.warnings || []).forEach((warning) => {
      warnings.push(`${translate(language, "pdfEmailNumber")} ${item.index}: ${warning}`);
    });
  });
  return [...new Set(warnings)];
}

export function MailPdfPanel({
  batchName,
  capabilities,
  cases,
  language,
  resetVersion,
}) {
  const artifacts = useMemo(() => retainedArtifacts(cases), [cases]);
  const validHandles = useMemo(
    () => new Set(artifacts.map((artifact) => artifact.handle)),
    [artifacts],
  );
  const [selectedHandles, setSelectedHandles] = useState([]);
  const [pagesPerMail, setPagesPerMail] = useState("2");
  const [overflowPolicy, setOverflowPolicy] = useState("fail");
  const [busyAction, setBusyAction] = useState("");
  const [individualResult, setIndividualResult] = useState(null);
  const [batchResult, setBatchResult] = useState(null);
  const controllerRef = useRef(null);
  const pushToast = useToast();

  const canCreateIndividual = capabilities?.mail_pdf_individual === true;
  const canCreateBatch = capabilities?.mail_pdf_batch === true;
  const canDownloadSource = capabilities?.source_eml_download === true;
  const selectedArtifact = selectedHandles.length === 1
    ? artifacts.find((artifact) => artifact.handle === selectedHandles[0])
    : null;
  const warnings = useMemo(
    () => itemWarnings(batchResult, language),
    [batchResult, language],
  );
  const selectableHandles = useMemo(
    () => artifacts.slice(0, MAIL_PDF_BATCH_LIMIT).map((artifact) => artifact.handle),
    [artifacts],
  );
  const allSelectableSelected = Boolean(
    selectableHandles.length
      && selectableHandles.every((handle) => selectedHandles.includes(handle)),
  );

  useEffect(() => {
    setSelectedHandles((current) => current.filter((handle) => validHandles.has(handle)));
  }, [validHandles]);

  useEffect(() => {
    controllerRef.current?.abort();
    setSelectedHandles([]);
    setPagesPerMail("2");
    setOverflowPolicy("fail");
    setBusyAction("");
    setIndividualResult(null);
    setBatchResult(null);
  }, [resetVersion]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  function toggleArtifact(handle) {
    if (selectedHandles.includes(handle)) {
      setSelectedHandles((current) => current.filter((item) => item !== handle));
    } else if (selectedHandles.length >= MAIL_PDF_BATCH_LIMIT) {
      pushToast(
        translate(language, "toastWarningTitle"),
        translate(language, "pdfSelectionLimitReached"),
        "warning",
      );
      return;
    } else {
      setSelectedHandles((current) => [...current, handle]);
    }
  }

  function toggleAll() {
    setSelectedHandles(allSelectableSelected ? [] : selectableHandles);
  }

  async function createIndividual() {
    if (!selectedArtifact || !canCreateIndividual) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusyAction("individual");
    setIndividualResult(null);
    try {
      const result = await dashboardApi.createIndividualMailPdf(
        selectedArtifact.handle,
        controller.signal,
      );
      setIndividualResult(result);
      pushToast(
        translate(language, "pdfToastReadyTitle"),
        translate(language, "pdfIndividualReady"),
        "success",
      );
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        pushToast(
          translate(language, "pdfToastErrorTitle"),
          requestError.message,
          "error",
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusyAction("");
    }
  }

  async function createBatch() {
    if (!selectedHandles.length || !canCreateBatch) return;
    if (selectedHandles.length > MAIL_PDF_BATCH_LIMIT) {
      pushToast(
        translate(language, "toastWarningTitle"),
        translate(language, "pdfSelectionLimitReached"),
        "warning",
      );
      return;
    }
    const pages = Number(pagesPerMail);
    if (!Number.isInteger(pages) || pages < 1 || pages > 10) {
      pushToast(
        translate(language, "toastWarningTitle"),
        translate(language, "pdfPagesInvalid"),
        "warning",
      );
      return;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusyAction("batch");
    setBatchResult(null);
    try {
      const result = await dashboardApi.createMailPdfBatch(
        selectedHandles,
        pages,
        overflowPolicy,
        batchName,
        controller.signal,
      );
      setBatchResult(result);
      pushToast(
        translate(language, "pdfToastReadyTitle"),
        translate(language, "pdfBatchReady"),
        (result.warnings || []).length ? "warning" : "success",
      );
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        pushToast(
          translate(language, "pdfToastErrorTitle"),
          requestError.message,
          "error",
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusyAction("");
    }
  }

  return (
    <section className="panel operation-panel pdf-workspace" aria-labelledby="mail-pdf-heading">
      <div className="pdf-panel-heading">
        <h3 id="mail-pdf-heading">{translate(language, "pdfPanel")}</h3>
        <span className="pdf-capability-badge">
          {backendLabel(language, capabilities?.mail_pdf_backend)}
        </span>
      </div>

      {!capabilities?.raw_eml_retention && (
        <div className="dialog-note is-warning" role="status">
          <p>{translate(language, "pdfRetentionDisabled")}</p>
        </div>
      )}
      {!canCreateIndividual && !canCreateBatch && (
        <div className="disabled-note">{translate(language, "pdfCapabilityUnavailable")}</div>
      )}

      {artifacts.length ? (
        <>
          <div className="pdf-selection-heading">
            <span>
              {translate(language, "pdfChooseEvidence")} · {selectedHandles.length}/{artifacts.length}
            </span>
            <button
              className="btn secondary button--compact"
              type="button"
              disabled={Boolean(busyAction)}
              onClick={toggleAll}
            >
              {translate(
                language,
                allSelectableSelected ? "pdfClearSelection" : "pdfSelectAll",
              )}
            </button>
          </div>
          {(artifacts.length > MAIL_PDF_BATCH_LIMIT
            || selectedHandles.length >= MAIL_PDF_BATCH_LIMIT) && (
            <div className="disabled-note" role="status">
              {translate(
                language,
                selectedHandles.length >= MAIL_PDF_BATCH_LIMIT
                  ? "pdfSelectionLimitReached"
                  : "pdfSelectionLimit",
              )}
            </div>
          )}
          <div className="pdf-artifact-list" role="group" aria-label={translate(language, "pdfChooseEvidence")}>
            {artifacts.map((artifact) => (
              <div className="pdf-artifact-row" key={artifact.handle}>
                <label className="pdf-artifact-select">
                  <input
                    type="checkbox"
                    checked={selectedHandles.includes(artifact.handle)}
                    disabled={Boolean(busyAction)
                      || (!selectedHandles.includes(artifact.handle)
                        && selectedHandles.length >= MAIL_PDF_BATCH_LIMIT)}
                    onChange={() => toggleArtifact(artifact.handle)}
                  />
                  <span className="pdf-artifact-copy">
                    <strong>{artifact.assetCodes.join(", ") || artifact.caseIds.join(", ")}</strong>
                    <small>
                      {artifact.caseIds.join(", ")} · {artifact.filename || translate(language, "mail")}
                    </small>
                  </span>
                </label>
                {canDownloadSource && artifact.downloadUrl && (
                  <a
                    className="mail-link"
                    href={artifact.downloadUrl}
                    download
                  >
                    ↓ EML
                  </a>
                )}
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="compensation-empty">{translate(language, "pdfNoEvidence")}</div>
      )}

      <div className="panel-row pdf-controls">
        <label className="small" htmlFor="pdf-batch">{translate(language, "batchName")}</label>
        <input id="pdf-batch" type="text" value={batchName} readOnly />
        <label className="small" htmlFor="pdf-pages">{translate(language, "pagesPerMail")}</label>
        <input
          id="pdf-pages"
          className="short-input"
          type="number"
          min="1"
          max="10"
          step="1"
          value={pagesPerMail}
          disabled={Boolean(busyAction)}
          onChange={(event) => setPagesPerMail(event.target.value)}
        />
        <label className="small" htmlFor="pdf-overflow">
          {translate(language, "pdfOverflowPolicy")}
        </label>
        <select
          id="pdf-overflow"
          value={overflowPolicy}
          disabled={Boolean(busyAction)}
          onChange={(event) => setOverflowPolicy(event.target.value)}
        >
          <option value="fail">{translate(language, "pdfOverflowFail")}</option>
          <option value="warn">{translate(language, "pdfOverflowWarn")}</option>
        </select>
      </div>

      <div className="upload-actions">
        <button
          className="btn secondary"
          type="button"
          disabled={Boolean(busyAction) || !canCreateIndividual || !selectedArtifact}
          onClick={createIndividual}
        >
          {translate(language, busyAction === "individual" ? "pdfCreating" : "pdfCreateIndividual")}
        </button>
        <button
          className="btn"
          type="button"
          disabled={Boolean(busyAction)
            || !canCreateBatch
            || !selectedHandles.length
            || selectedHandles.length > MAIL_PDF_BATCH_LIMIT}
          onClick={createBatch}
        >
          {translate(language, busyAction === "batch" ? "pdfMerging" : "mergePdf")}
        </button>
      </div>

      {busyAction && (
        <div className="upload-progress" role="status" aria-live="polite">
          <span>{translate(language, busyAction === "batch" ? "pdfMergingProgress" : "pdfCreatingProgress")}</span>
          <progress aria-label={translate(language, "pdfProgressLabel")} />
        </div>
      )}
      {individualResult && (
        <div className="upload-result" aria-live="polite">
          <strong>{translate(language, "pdfIndividualReady")}</strong>
          {individualResult.download_url ? (
            <a className="mail-link-pdf" href={individualResult.download_url} download>
              ↓ {translate(language, "pdfDownloadIndividual")}
            </a>
          ) : <span className="disabled-note">{translate(language, "pdfDownloadMissing")}</span>}
        </div>
      )}

      {batchResult && (
        <div className="upload-result pdf-result" aria-live="polite">
          <strong>{translate(language, "pdfBatchReady")}</strong>
          <div className="result-list">
            <div className="result-file">
              <span>{batchResult.output_name || batchResult.batch_id || translate(language, "pdfMergedFile")}</span>
              {batchResult.merged_download_url ? (
                <a href={batchResult.merged_download_url} download>
                  ↓ {translate(language, "pdfDownloadMerged")}
                </a>
              ) : <span className="none">{translate(language, "pdfDownloadMissing")}</span>}
            </div>
            {(batchResult.items || []).map((item) => (
              <div className="result-file" key={`${batchResult.batch_id}-${item.index}`}>
                <span>
                  {translate(language, "pdfEmailNumber")} {item.index} · {item.output_pages} {translate(language, "pdfPages")}
                  {item.padded_pages ? ` · +${item.padded_pages} ${translate(language, "pdfBlankPages")}` : ""}
                  {item.truncated_pages ? ` · -${item.truncated_pages} ${translate(language, "pdfOverflowPages")}` : ""}
                </span>
                {item.download_url ? (
                  <a href={item.download_url} download>↓ PDF</a>
                ) : <span className="none">{translate(language, "pdfDownloadMissing")}</span>}
              </div>
            ))}
          </div>
          {!!warnings.length && (
            <div className="upload-feedback-list">
              <strong>{translate(language, "uploadWarnings")}</strong>
              <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
