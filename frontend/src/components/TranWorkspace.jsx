import React, { useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi } from "../api.js";
import { translate } from "../i18n.js";
import { formatCurrency } from "../utils.js";
import { EmailUploadPanel } from "./UploadWorkspace.jsx";

const MAX_REFERENCE_BYTES = 50 * 1024 * 1024;
const MAX_TRAN_ASSETS = 100;
const EMPTY_REFERENCE_STATUS = Object.freeze({
  fa_gl: { configured: false, available: false, source: null },
  ccdc: { configured: false, available: false, source: null },
  managed_updated_at: null,
});
const EMPTY_FORM = Object.freeze({
  tag_number: "",
  asset_name: "",
  domain: "",
  lost_date: "",
  physical: "",
  confirmed_cost: "",
  confirmed_start_date: "",
  confirmed_group: "",
  confirmed_fee_rate: "",
  classification_confirmed: false,
});
const DEMO_FORM = Object.freeze({
  ...EMPTY_FORM,
  tag_number: "MOU10001",
  asset_name: "Synthetic mouse",
  domain: "demo.user",
  physical: true,
  confirmed_cost: "1000000",
  confirmed_start_date: "2025-01-01",
  confirmed_group: "FOUR_YEAR",
  confirmed_fee_rate: "0.05",
  classification_confirmed: true,
});

function localIsoDate(value = new Date()) {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 10);
}

function firstSourceValue(caseItem, keys) {
  const metadata = caseItem?.metadata && typeof caseItem.metadata === "object"
    ? caseItem.metadata
    : {};
  for (const key of keys) {
    const value = caseItem?.[key] ?? metadata[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") return value;
  }
  return "";
}

function inputDate(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}`;
  const local = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/.exec(text);
  if (!local) return "";
  return `${local[3]}-${local[2].padStart(2, "0")}-${local[1].padStart(2, "0")}`;
}

function inputMoney(value) {
  if (value === "" || value == null) return "";
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value >= 0 ? String(value) : "";
  }
  const text = String(value).trim();
  return /^\d+$/.test(text) ? text : "";
}

function inputBoolean(value) {
  if (typeof value === "boolean") return value;
  const normalized = String(value || "").trim().toLowerCase();
  if (["true", "yes", "1", "physical", "hardware"].includes(normalized)) return true;
  if (["false", "no", "0", "non-physical", "software"].includes(normalized)) return false;
  return "";
}

function formFromCase(caseItem) {
  const group = String(firstSourceValue(caseItem, ["group", "depreciation_group"]) || "")
    .trim()
    .toUpperCase();
  const fee = firstSourceValue(caseItem, ["fee_rate", "responsibility_fee_rate"]);
  return {
    ...EMPTY_FORM,
    tag_number: String(caseItem?.asset_code || "").trim().toUpperCase(),
    asset_name: String(caseItem?.asset_name || "").trim(),
    domain: String(caseItem?.domain || "").trim(),
    lost_date: inputDate(firstSourceValue(caseItem, ["loss_date", "lost_date"])),
    physical: inputBoolean(firstSourceValue(caseItem, ["physical", "is_physical"])),
    confirmed_cost: inputMoney(
      firstSourceValue(caseItem, ["original_value", "original_cost", "cost"]),
    ),
    confirmed_start_date: inputDate(
      firstSourceValue(caseItem, ["usage_start", "start_date", "in_service_date"]),
    ),
    confirmed_group: ["FOUR_YEAR", "SIX_YEAR"].includes(group) ? group : "",
    confirmed_fee_rate: fee === "" ? "" : String(fee),
    classification_confirmed: firstSourceValue(
      caseItem,
      ["classification_confirmed"],
    ) === true,
  };
}

export function buildTranAssetPayload(form, language = "vi") {
  const asset = {
    tag_number: form.tag_number.trim().toUpperCase(),
    asset_name: form.asset_name.trim(),
    domain: form.domain.trim(),
    lost_date: form.lost_date,
    physical: form.physical,
    classification_confirmed: form.classification_confirmed === true,
  };
  const confirmedCost = String(form.confirmed_cost || "").trim();
  if (confirmedCost) {
    if (!/^\d+$/.test(confirmedCost)) {
      throw new Error(translate(language, "tranCostWholeNumber"));
    }
    asset.confirmed_cost = confirmedCost;
  }
  if (form.confirmed_start_date) asset.confirmed_start_date = form.confirmed_start_date;
  if (form.confirmed_group) asset.confirmed_group = form.confirmed_group;
  if (form.confirmed_fee_rate) asset.confirmed_fee_rate = form.confirmed_fee_rate;
  return asset;
}

export function buildTranAssetsPayload(forms, language = "vi") {
  if (!Array.isArray(forms) || forms.length < 1 || forms.length > MAX_TRAN_ASSETS) {
    throw new Error(translate(language, "tranTooManyAssets"));
  }
  return forms.map((form) => buildTranAssetPayload(form, language));
}

export function groupTranCasesBySource(cases) {
  const groups = new Map();
  cases.forEach((caseItem) => {
    const handle = caseItem.source_eml?.handle || "";
    const key = handle ? `eml:${handle}` : `case:${caseItem.id}`;
    const current = groups.get(key) || { key, handle, cases: [] };
    current.cases.push(caseItem);
    groups.set(key, current);
  });
  return [...groups.values()];
}

export function sharedTranSourceHandle(cases) {
  if (!Array.isArray(cases) || !cases.length) return "";
  const handles = new Set(cases.map((item) => item.source_eml?.handle).filter(Boolean));
  return handles.size === 1 && cases.every((item) => item.source_eml?.handle)
    ? [...handles][0]
    : "";
}

function validReferenceFile(file, language) {
  if (!file) return translate(language, "tranReferenceFaRequired");
  if (!file.name.toLowerCase().endsWith(".xlsx")) {
    return `${file.name}: ${translate(language, "tranReferenceTypeError")}`;
  }
  if (!file.size) return `${file.name}: ${translate(language, "uploadEmptyFile")}`;
  if (file.size > MAX_REFERENCE_BYTES) {
    return `${file.name}: ${translate(language, "tranReferenceSizeError")}`;
  }
  return "";
}

function resultStatusLabel(language, status) {
  const keys = {
    CALCULATED: "resultCalculated",
    EXEMPT: "resultExempt",
    NEEDS_REVIEW: "resultReview",
    NOT_APPLICABLE: "resultNotApplicable",
  };
  return translate(language, keys[status] || "resultStatus");
}

function referenceStatusLabel(language, status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "MATCHED") return translate(language, "referenceMatched");
  if (normalized === "NOT_FOUND") return translate(language, "referenceMissing");
  if (normalized === "AMBIGUOUS") return translate(language, "referenceAmbiguous");
  return "—";
}

function percentage(value) {
  if (value == null) return "—";
  return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(value * 100)}%`;
}

function sourceLabel(language, item) {
  if (item?.configured && !item?.available) {
    return translate(language, "tranReferenceUnavailable");
  }
  if (item?.source === "uploaded") return translate(language, "tranReferenceUploaded");
  if (item?.source === "configured") return translate(language, "tranReferenceConfigured");
  return translate(language, "tranReferenceMissing");
}

export function TranResolutionCard({ item, index, language }) {
  const preview = item.preview;
  const asset = item.asset;
  return (
    <article className="tran-resolution-item">
      <header>
        <strong>{translate(language, "tranAssetResult")} {index + 1}</strong>
        <span>{asset?.tag_number || "—"}</span>
      </header>
      <div className={`compensation-result-summary ${item.ready ? "result-exempt" : "result-needs_review"}`}>
        <span>{translate(language, "resultStatus")}</span>
        <strong>{translate(language, item.ready ? "tranReady" : "resultReview")}</strong>
      </div>
      <div className="tran-reference-grid">
        <div className="tran-reference-card"><span>FA&amp;GL</span><strong>{referenceStatusLabel(language, item.fa_status)}</strong></div>
        <div className="tran-reference-card"><span>CCDC</span><strong>{referenceStatusLabel(language, item.classification_status)}</strong></div>
      </div>
      {preview && (
        <dl className="compensation-metrics">
          <div><dt>{translate(language, "resultStatus")}</dt><dd>{resultStatusLabel(language, preview.status)}</dd></div>
          <div><dt>{translate(language, "usageMonths")}</dt><dd>{preview.usage_months == null ? "—" : `${preview.usage_months} ${translate(language, "months")}`}</dd></div>
          <div><dt>{translate(language, "remainingRate")}</dt><dd>{percentage(preview.remaining_rate)}</dd></div>
          <div><dt>{translate(language, "remainingValue")}</dt><dd>{preview.remaining_value == null ? "—" : formatCurrency(preview.remaining_value)}</dd></div>
          <div><dt>{translate(language, "responsibilityFee")}</dt><dd>{preview.fee_value == null ? "—" : `${formatCurrency(preview.fee_value)} · ${percentage(preview.fee_rate)}`}</dd></div>
          <div className="compensation-total"><dt>{translate(language, "totalCompensation")}</dt><dd>{preview.total_amount == null ? "—" : formatCurrency(preview.total_amount)}</dd></div>
        </dl>
      )}
      {asset && (
        <details className="policy-trace tran-provenance">
          <summary>{translate(language, "tranResolvedEvidence")}</summary>
          <dl>
            <div><dt>{translate(language, "tagNumber")}</dt><dd>{asset.tag_number || "—"}</dd></div>
            <div><dt>{translate(language, "assetNumber")}</dt><dd>{asset.asset_number || "—"}</dd></div>
            <div><dt>{translate(language, "book")}</dt><dd>{asset.book || "—"}</dd></div>
            <div><dt>{translate(language, "entity")}</dt><dd>{asset.entity || "—"}</dd></div>
            <div><dt>{translate(language, "costCenter")}</dt><dd>{asset.cost_center || "—"}</dd></div>
            <div><dt>{translate(language, "productCode")}</dt><dd>{asset.product_code || "—"}</dd></div>
            <div><dt>{translate(language, "location")}</dt><dd>{asset.location || "—"}</dd></div>
            <div><dt>{translate(language, "originalCost")}</dt><dd>{asset.cost == null ? "—" : formatCurrency(asset.cost)}</dd></div>
          </dl>
        </details>
      )}
      {!!item.notes.length && (
        <div className="tran-notes"><strong>{translate(language, "tranProvenanceNotes")}</strong><ul>{item.notes.map((note) => <li key={note}>{note}</li>)}</ul></div>
      )}
      {!!item.issues.length && (
        <div className="compensation-reasons"><strong>{translate(language, "tranBlockingIssues")}</strong><ul>{item.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul></div>
      )}
    </article>
  );
}

export function TranWorkspace({
  capabilities,
  cases,
  language,
  onEmailUpload,
  onReferencesChanged,
  testDataClearVersion,
}) {
  const [forms, setForms] = useState([{ ...EMPTY_FORM }]);
  const [selectedGroupKey, setSelectedGroupKey] = useState("");
  const [selectedCaseIds, setSelectedCaseIds] = useState([]);
  const [pendingUploadedCaseId, setPendingUploadedCaseId] = useState("");
  const [emailNotice, setEmailNotice] = useState("");
  const [referenceStatus, setReferenceStatus] = useState(EMPTY_REFERENCE_STATUS);
  const [referenceLoading, setReferenceLoading] = useState(true);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [referenceProgress, setReferenceProgress] = useState(null);
  const [referenceError, setReferenceError] = useState("");
  const [referenceNotice, setReferenceNotice] = useState("");
  const [faFile, setFaFile] = useState(null);
  const [ccdcFile, setCcdcFile] = useState(null);
  const [clearCcdc, setClearCcdc] = useState(false);
  const [resolution, setResolution] = useState(null);
  const [busyAction, setBusyAction] = useState("");
  const [actionError, setActionError] = useState("");
  const [workbookResult, setWorkbookResult] = useState(null);
  const [draftResult, setDraftResult] = useState(null);
  const [processingDate, setProcessingDate] = useState(localIsoDate());
  const [yearSheet, setYearSheet] = useState("");
  const [bodyIntro, setBodyIntro] = useState("");
  const faInputRef = useRef(null);
  const ccdcInputRef = useRef(null);
  const referenceControllerRef = useRef(null);
  const actionControllerRef = useRef(null);
  const lostCases = useMemo(
    () => cases.filter((item) => item.case_type === "LOST"),
    [cases],
  );
  const caseGroups = useMemo(() => groupTranCasesBySource(lostCases), [lostCases]);
  const selectedCases = useMemo(
    () => selectedCaseIds
      .map((caseId) => lostCases.find((item) => item.id === caseId))
      .filter(Boolean),
    [lostCases, selectedCaseIds],
  );
  const selectedHandles = new Set(selectedCases.map((item) => item.source_eml?.handle).filter(Boolean));
  const sourceHandle = sharedTranSourceHandle(selectedCases);
  const resolvedItems = resolution?.results || [];
  const canResolve = capabilities?.tran_lookup === true || referenceStatus.fa_gl.available;
  const canExport = capabilities?.tran_workbook_export === true;
  const canDraft = capabilities?.tran_draft === true;

  useEffect(() => {
    referenceControllerRef.current?.abort();
    actionControllerRef.current?.abort();
    setForms([{ ...EMPTY_FORM }]);
    setSelectedGroupKey("");
    setSelectedCaseIds([]);
    setPendingUploadedCaseId("");
    setEmailNotice("");
    setFaFile(null);
    setCcdcFile(null);
    setClearCcdc(false);
    setReferenceError("");
    setReferenceNotice("");
    setReferenceProgress(null);
    setReferenceStatus(EMPTY_REFERENCE_STATUS);
    setReferenceBusy(false);
    setResolution(null);
    setBusyAction("");
    setActionError("");
    setWorkbookResult(null);
    setDraftResult(null);
    setProcessingDate(localIsoDate());
    setYearSheet("");
    setBodyIntro("");
    if (faInputRef.current) faInputRef.current.value = "";
    if (ccdcInputRef.current) ccdcInputRef.current.value = "";

    const controller = new AbortController();
    referenceControllerRef.current = controller;
    setReferenceLoading(true);
    dashboardApi.tranReferenceStatus(controller.signal)
      .then(setReferenceStatus)
      .catch((error) => {
        if (error.name !== "AbortError") setReferenceError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setReferenceLoading(false);
      });
    return () => controller.abort();
  }, [testDataClearVersion]);

  useEffect(() => () => {
    referenceControllerRef.current?.abort();
    actionControllerRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!pendingUploadedCaseId) return;
    const uploaded = lostCases.find((item) => item.id === pendingUploadedCaseId);
    if (!uploaded) return;
    const group = caseGroups.find((item) => item.cases.some((caseItem) => caseItem.id === uploaded.id));
    if (!group) return;
    if (group.cases.length > MAX_TRAN_ASSETS) {
      setPendingUploadedCaseId("");
      setEmailNotice(translate(language, "tranTooManyAssets"));
      return;
    }
    setSelectedGroupKey(group.key);
    setSelectedCaseIds(group.cases.map((caseItem) => caseItem.id));
    setForms(group.cases.map(formFromCase));
    setResolution(null);
    setWorkbookResult(null);
    setDraftResult(null);
    setPendingUploadedCaseId("");
    setEmailNotice(translate(language, "tranEmailPrefillReady"));
  }, [caseGroups, language, lostCases, pendingUploadedCaseId]);

  useEffect(() => {
    if (!selectedGroupKey || caseGroups.some((item) => item.key === selectedGroupKey)) return;
    setSelectedGroupKey("");
    setSelectedCaseIds([]);
    setForms([{ ...EMPTY_FORM }]);
    setResolution(null);
    setWorkbookResult(null);
    setDraftResult(null);
  }, [caseGroups, selectedGroupKey]);

  function invalidateOutputs() {
    setResolution(null);
    setWorkbookResult(null);
    setDraftResult(null);
    setActionError("");
  }

  function updateField(index, field, value) {
    setForms((current) => current.map((item, itemIndex) => (
      itemIndex === index ? { ...item, [field]: value } : item
    )));
    invalidateOutputs();
  }

  function chooseCaseGroup(groupKey) {
    const group = caseGroups.find((item) => item.key === groupKey);
    if (group && group.cases.length > MAX_TRAN_ASSETS) {
      setActionError(translate(language, "tranTooManyAssets"));
      return;
    }
    setSelectedGroupKey(groupKey);
    setSelectedCaseIds(group ? group.cases.map((item) => item.id) : []);
    setForms(group ? group.cases.map(formFromCase) : [{ ...EMPTY_FORM }]);
    setEmailNotice("");
    invalidateOutputs();
  }

  function removeAssetRow(index) {
    if (forms.length <= 1) return;
    setForms((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setSelectedCaseIds((current) => current.filter((_, itemIndex) => itemIndex !== index));
    invalidateOutputs();
  }

  function emailUploadCompleted(uploadResult) {
    const uploadedLostCase = Array.isArray(uploadResult?.cases)
      ? uploadResult.cases.find((item) => item?.case_type === "LOST" && item?.id)
      : null;
    if (!uploadedLostCase) {
      setPendingUploadedCaseId("");
      setEmailNotice(translate(language, "tranEmailNoLostCase"));
      return;
    }
    setPendingUploadedCaseId(String(uploadedLostCase.id));
    setEmailNotice(translate(language, "tranEmailWaitingForCase"));
  }

  async function uploadReferences(event) {
    event.preventDefault();
    const faError = validReferenceFile(faFile, language);
    const ccdcError = ccdcFile ? validReferenceFile(ccdcFile, language) : "";
    if (faError || ccdcError) {
      setReferenceError(faError || ccdcError);
      return;
    }
    if (ccdcFile && clearCcdc) {
      setReferenceError(translate(language, "tranReferenceClearConflict"));
      return;
    }
    const controller = new AbortController();
    referenceControllerRef.current?.abort();
    referenceControllerRef.current = controller;
    setReferenceBusy(true);
    setReferenceProgress(0);
    setReferenceError("");
    setReferenceNotice("");
    try {
      const response = await dashboardApi.uploadTranReferences(
        faFile,
        ccdcFile,
        clearCcdc,
        {
          signal: controller.signal,
          onProgress: ({ percent }) => setReferenceProgress(percent),
        },
      );
      setReferenceStatus(response.status);
      setReferenceNotice(translate(language, "tranReferenceUploadSuccess"));
      setFaFile(null);
      setCcdcFile(null);
      setClearCcdc(false);
      if (faInputRef.current) faInputRef.current.value = "";
      if (ccdcInputRef.current) ccdcInputRef.current.value = "";
      invalidateOutputs();
      await onReferencesChanged?.();
    } catch (error) {
      if (error.name !== "AbortError") setReferenceError(error.message);
    } finally {
      if (!controller.signal.aborted) setReferenceBusy(false);
    }
  }

  async function resolveAsset(event) {
    event.preventDefault();
    if (!forms.length || forms.length > MAX_TRAN_ASSETS) {
      setActionError(translate(language, "tranTooManyAssets"));
      return;
    }
    if (!canResolve) {
      setActionError(translate(language, "tranLookupUnavailable"));
      return;
    }
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setBusyAction("resolve");
    setActionError("");
    setWorkbookResult(null);
    setDraftResult(null);
    try {
      setResolution(await dashboardApi.resolveTranAssets(
        buildTranAssetsPayload(forms, language),
        controller.signal,
      ));
    } catch (error) {
      if (error.name !== "AbortError") {
        setResolution(null);
        setActionError(error.message);
      }
    } finally {
      if (!controller.signal.aborted) setBusyAction("");
    }
  }

  async function exportWorkbook() {
    if (!resolution?.ready || !canExport) return;
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setBusyAction("workbook");
    setActionError("");
    setWorkbookResult(null);
    try {
      setWorkbookResult(await dashboardApi.exportTranWorkbook(
        buildTranAssetsPayload(forms, language),
        processingDate,
        yearSheet.trim(),
        controller.signal,
      ));
    } catch (error) {
      if (error.name !== "AbortError") setActionError(error.message);
    } finally {
      if (!controller.signal.aborted) setBusyAction("");
    }
  }

  async function createDraft() {
    const intro = bodyIntro.trim();
    if (!resolution?.ready || !canDraft || !sourceHandle) return;
    if (!intro) {
      setActionError(translate(language, "tranDraftIntroRequired"));
      return;
    }
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setBusyAction("draft");
    setActionError("");
    setDraftResult(null);
    try {
      setDraftResult(await dashboardApi.createTranDraft(
        buildTranAssetsPayload(forms, language),
        sourceHandle,
        intro,
        processingDate,
        yearSheet.trim(),
        controller.signal,
      ));
    } catch (error) {
      if (error.name !== "AbortError") setActionError(error.message);
    } finally {
      if (!controller.signal.aborted) setBusyAction("");
    }
  }

  return (
    <div className="tran-workspace">
      <div className="task-heading">
        <div>
          <h2>{translate(language, "tranTitle")}</h2>
          <p>{translate(language, "tranDescription")}</p>
        </div>
        <span className={`pdf-capability-badge ${canResolve ? "" : "is-disabled"}`}>
          {translate(language, canResolve ? "tranLookupReady" : "tranLookupUnavailable")}
        </span>
      </div>

      <EmailUploadPanel
        idPrefix="tran-email"
        language={language}
        onCompleted={emailUploadCompleted}
        onEmailUpload={onEmailUpload}
        resetVersion={testDataClearVersion}
        titleKey="tranEmailUploadPanel"
      />
      {emailNotice && <div className="dialog-note" role="status"><p>{emailNotice}</p></div>}

      <section className="panel operation-panel">
        <div className="pdf-panel-heading">
          <h3>{translate(language, "tranReferencePanel")}</h3>
          <span className="disabled-note">
            {referenceLoading ? translate(language, "tranReferenceLoading") : "FA&GL + CCDC"}
          </span>
        </div>
        <div className="tran-reference-grid" aria-live="polite">
          <div className={`tran-reference-card ${referenceStatus.fa_gl.available ? "is-ready" : "is-missing"}`}>
            <span>FA&amp;GL · {translate(language, "tranReferenceRequired")}</span>
            <strong>{sourceLabel(language, referenceStatus.fa_gl)}</strong>
          </div>
          <div className={`tran-reference-card ${referenceStatus.ccdc.available ? "is-ready" : ""}`}>
            <span>CCDC · {translate(language, "tranReferenceOptional")}</span>
            <strong>{sourceLabel(language, referenceStatus.ccdc)}</strong>
          </div>
        </div>
        {capabilities?.tran_reference_upload ? (
          <form className="tran-reference-form" onSubmit={uploadReferences}>
            <label>
              <span>FA&amp;GL (.xlsx) *</span>
              <input
                ref={faInputRef}
                type="file"
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                disabled={referenceBusy || Boolean(busyAction)}
                onChange={(event) => {
                  setFaFile(event.target.files?.[0] || null);
                  setReferenceError("");
                }}
              />
            </label>
            <label>
              <span>CCDC (.xlsx) · {translate(language, "tranReferenceOptional")}</span>
              <input
                ref={ccdcInputRef}
                type="file"
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                disabled={referenceBusy || Boolean(busyAction) || clearCcdc}
                onChange={(event) => {
                  setCcdcFile(event.target.files?.[0] || null);
                  setReferenceError("");
                }}
              />
            </label>
            <label className="checkbox-field tran-clear-reference">
              <input
                type="checkbox"
                checked={clearCcdc}
                disabled={referenceBusy || Boolean(busyAction)}
                onChange={(event) => {
                  const checked = event.target.checked;
                  setClearCcdc(checked);
                  if (checked) {
                    setCcdcFile(null);
                    if (ccdcInputRef.current) ccdcInputRef.current.value = "";
                  }
                  setReferenceError("");
                }}
              />
              <span>{translate(language, "tranReferenceClearCcdc")}</span>
            </label>
            <div className="upload-actions">
              <button className="btn" type="submit" disabled={referenceBusy || Boolean(busyAction) || !faFile}>
                {translate(language, referenceBusy ? "tranReferenceUploading" : "tranReferenceUpload")}
              </button>
            </div>
            {referenceBusy && (
              <div className="upload-progress" role="status">
                <span>{translate(language, "tranReferenceUploading")}</span>
                <progress max="100" value={referenceProgress ?? undefined} />
              </div>
            )}
          </form>
        ) : <div className="disabled-note">{translate(language, "tranReferenceUploadUnavailable")}</div>}
        {referenceError && <div className="inline-error" role="alert">{referenceError}</div>}
        {referenceNotice && <div className="dialog-note" role="status"><p>{referenceNotice}</p></div>}
      </section>

      <section className="panel operation-panel compensation-panel">
        <h3>{translate(language, "compensationInput")}</h3>
        <form className="compensation-form" onSubmit={resolveAsset}>
          <div className="compensation-prefill">
            <label>
              <span>{translate(language, "chooseLostCase")}</span>
              <select value={selectedGroupKey} onChange={(event) => chooseCaseGroup(event.target.value)}>
                <option value="">{translate(language, "manualEntry")}</option>
                {caseGroups.map((group) => (
                  <option value={group.key} key={group.key}>
                    {group.handle
                      ? `${group.cases[0]?.source_eml?.filename || group.cases[0]?.asset_code} · ${group.cases.length} ${translate(language, "tranAssets")}`
                      : `${group.cases[0]?.asset_code} · ${group.cases[0]?.domain}`}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn secondary"
              type="button"
              onClick={() => {
                setSelectedGroupKey("");
                setSelectedCaseIds([]);
                setForms([{ ...DEMO_FORM, lost_date: localIsoDate() }]);
                setEmailNotice("");
                invalidateOutputs();
              }}
            >
              {translate(language, "loadDemo")}
            </button>
          </div>

          {selectedGroupKey && (
            <div className="dialog-note is-warning" role="status">
              <p>{translate(language, "casePrefillNotice")} {translate(language, "tranSameEmailGroupHint")}</p>
            </div>
          )}

          <div className="tran-asset-editor-list">
            {forms.map((form, index) => (
              <article className="tran-asset-editor" key={selectedCaseIds[index] || `manual-${index}`}>
                <header>
                  <strong>{translate(language, "tranAssetRow")} {index + 1}</strong>
                  <span>{form.tag_number || translate(language, "manualEntry")}</span>
                  {forms.length > 1 && (
                    <button className="btn secondary button--compact" type="button" onClick={() => removeAssetRow(index)}>
                      {translate(language, "tranRemoveAsset")}
                    </button>
                  )}
                </header>
                <div className="compensation-grid">
                  <label><span>{translate(language, "tagNumber")} *</span><input type="text" value={form.tag_number} onChange={(event) => updateField(index, "tag_number", event.target.value.toUpperCase())} required /></label>
                  <label><span>{translate(language, "assetName")} *</span><input type="text" value={form.asset_name} onChange={(event) => updateField(index, "asset_name", event.target.value)} required /></label>
                  <label><span>{translate(language, "domain")} *</span><input type="text" value={form.domain} onChange={(event) => updateField(index, "domain", event.target.value)} required /></label>
                  <label><span>{translate(language, "lostDate")} *</span><input type="date" value={form.lost_date} onChange={(event) => updateField(index, "lost_date", event.target.value)} required /></label>
                  <label>
                    <span>{translate(language, "physicalAsset")} *</span>
                    <select
                      value={form.physical === "" ? "" : String(form.physical)}
                      onChange={(event) => updateField(index, "physical", event.target.value === "" ? "" : event.target.value === "true")}
                      required
                    >
                      <option value="">{translate(language, "choosePhysical")}</option>
                      <option value="true">{translate(language, "physicalYes")}</option>
                      <option value="false">{translate(language, "physicalNo")}</option>
                    </select>
                  </label>
                </div>
                <details className="compensation-advanced">
                  <summary>{translate(language, "tranOperatorDecisions")}</summary>
                  <p className="field-hint">{translate(language, "tranOperatorDecisionHint")}</p>
                  <div className="compensation-grid">
                    <label><span>{translate(language, "originalCost")}</span><input type="text" inputMode="numeric" pattern="[0-9]*" value={form.confirmed_cost} onChange={(event) => { if (/^\d*$/.test(event.target.value)) updateField(index, "confirmed_cost", event.target.value); }} /></label>
                    <label><span>{translate(language, "startDate")}</span><input type="date" value={form.confirmed_start_date} onChange={(event) => updateField(index, "confirmed_start_date", event.target.value)} /></label>
                    <label>
                      <span>{translate(language, "depreciationOverride")}</span>
                      <select value={form.confirmed_group} onChange={(event) => updateField(index, "confirmed_group", event.target.value)}>
                        <option value="">{translate(language, "automaticMapping")}</option>
                        <option value="FOUR_YEAR">{translate(language, "fourYear")}</option>
                        <option value="SIX_YEAR">{translate(language, "sixYear")}</option>
                      </select>
                    </label>
                    <label>
                      <span>{translate(language, "feeOverride")}</span>
                      <select value={form.confirmed_fee_rate} onChange={(event) => updateField(index, "confirmed_fee_rate", event.target.value)}>
                        <option value="">{translate(language, "automaticMapping")}</option>
                        <option value="0.05">5%</option>
                        <option value="0.30">30%</option>
                      </select>
                    </label>
                    <label className="checkbox-field">
                      <input type="checkbox" checked={form.classification_confirmed} onChange={(event) => updateField(index, "classification_confirmed", event.target.checked)} />
                      <span>{translate(language, "tranClassificationConfirmed")}</span>
                    </label>
                  </div>
                </details>
              </article>
            ))}
          </div>

          {!canResolve && <div className="dialog-note is-warning"><p>{translate(language, "tranLookupUnavailable")}</p></div>}
          <div className="compensation-actions">
            <button className="btn" type="submit" disabled={Boolean(busyAction) || referenceBusy || !canResolve || !forms.length}>
              {translate(language, busyAction === "resolve" ? "tranResolving" : "tranResolve")}
            </button>
            <span className="disabled-note">{translate(language, "tranResolveHint")}</span>
          </div>
        </form>
      </section>

      <section className="panel compensation-result-panel" aria-live="polite">
        <h3>{translate(language, "tranResolutionResult")}</h3>
        {actionError && <div className="inline-error" role="alert">{actionError}</div>}
        {!resolvedItems.length && busyAction !== "resolve" && (
          <div className="compensation-empty">{translate(language, "tranNoResolution")}</div>
        )}
        {busyAction === "resolve" && (
          <div className="upload-progress" role="status">
            <span>{translate(language, "tranResolving")}</span><progress />
          </div>
        )}
        {!!resolvedItems.length && (
          <>
            <div className={`compensation-result-summary ${resolution.ready ? "result-exempt" : "result-needs_review"}`}>
              <span>{translate(language, "tranResolutionSummary")}</span>
              <strong>
                {resolvedItems.filter((item) => item.ready).length}/{resolvedItems.length} {translate(language, "tranAssetsReady")}
              </strong>
            </div>
            <div className="tran-resolution-list">
              {resolvedItems.map((item, index) => (
                <TranResolutionCard
                  item={item}
                  index={index}
                  language={language}
                  key={`${item.asset?.tag_number || "asset"}-${index}`}
                />
              ))}
            </div>

            <div className="tran-output-section">
              <h4>{translate(language, "tranOutputs")}</h4>
              <div className="panel-row tran-output-controls">
                <label className="small" htmlFor="tran-processing-date">{translate(language, "tranProcessingDate")}</label>
                <input id="tran-processing-date" type="date" value={processingDate} onChange={(event) => { setProcessingDate(event.target.value); setWorkbookResult(null); setDraftResult(null); }} />
                <label className="small" htmlFor="tran-year-sheet">{translate(language, "tranYearSheet")}</label>
                <input id="tran-year-sheet" className="short-input" type="text" maxLength="31" value={yearSheet} placeholder={processingDate.slice(0, 4)} onChange={(event) => { setYearSheet(event.target.value); setWorkbookResult(null); setDraftResult(null); }} />
                <button className="btn" type="button" disabled={Boolean(busyAction) || referenceBusy || !resolution.ready || !canExport} onClick={exportWorkbook}>
                  {translate(language, busyAction === "workbook" ? "tranExporting" : "tranExportWorkbook")}
                </button>
              </div>
              {!canExport && <div className="disabled-note">{translate(language, "tranExportUnavailable")}</div>}
              {workbookResult?.download_url && (
                <div className="result-file"><span>{translate(language, "tranWorkbookReady")}</span><a href={workbookResult.download_url} download>↓ Excel</a></div>
              )}

              <div className="tran-draft-box">
                <label htmlFor="tran-body-intro"><strong>{translate(language, "tranDraftIntro")}</strong></label>
                <textarea
                  id="tran-body-intro"
                  rows="4"
                  maxLength="10000"
                  value={bodyIntro}
                  placeholder={translate(language, "tranDraftIntroPlaceholder")}
                  onChange={(event) => { setBodyIntro(event.target.value); setDraftResult(null); setActionError(""); }}
                />
                <div className="upload-actions">
                  <button className="btn secondary" type="button" disabled={Boolean(busyAction) || referenceBusy || !resolution.ready || !canDraft || !sourceHandle} onClick={createDraft}>
                    {translate(language, busyAction === "draft" ? "tranDrafting" : "tranCreateDraft")}
                  </button>
                  <span className="disabled-note">{translate(language, "tranNeverSend")}</span>
                </div>
                {!sourceHandle && <div className="disabled-note">{translate(language, selectedHandles.size > 1 ? "tranDraftMixedSources" : "tranDraftNeedsSource")}</div>}
                {!canDraft && <div className="disabled-note">{translate(language, "tranDraftUnavailable")}</div>}
              </div>
              {draftResult && (
                <div className="upload-result">
                  <strong>{translate(language, "tranDraftReady")}</strong>
                  <div className="result-list">
                    {draftResult.workbook_download_url && <div className="result-file"><span>{translate(language, "tranDraftWorkbook")}</span><a href={draftResult.workbook_download_url} download>↓ Excel</a></div>}
                    {draftResult.draft_download_url && <div className="result-file"><span>{translate(language, "tranUnsentEml")}</span><a href={draftResult.draft_download_url} download>↓ EML</a></div>}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
