import React, { useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi } from "../api.js";
import { translate } from "../i18n.js";
import { formatCurrency } from "../utils.js";
import {
  buildTranSourceBatches,
  isAmbiguousDraftError,
  resolveLostDate,
  runTranBatch,
  selectTranGroupEntries,
  tranDraftRequestFingerprint,
} from "../workflowContracts.js";
import { useToast } from "./Feedback.jsx";
import { TranCasePickerDialog } from "./TranCasePickerDialog.jsx";
import { EmailUploadPanel } from "./UploadWorkspace.jsx";

const MAX_REFERENCE_BYTES = 50 * 1024 * 1024;
const MAX_TRAN_ASSETS = 100;
const MAX_TRAN_DRAFT_SOURCES = 20;
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

function elapsedClock(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
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
    lost_date: resolveLostDate(
      inputDate(firstSourceValue(caseItem, ["loss_date", "lost_date"])),
      localIsoDate(),
    ),
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

function formFromSourceRow(caseItem, row) {
  const base = formFromCase(caseItem);
  return {
    ...base,
    tag_number: String(row?.asset_code || base.tag_number).trim().toUpperCase(),
    asset_name: String(row?.asset_name || base.asset_name).trim(),
    domain: String(row?.domain || base.domain).trim(),
    lost_date: resolveLostDate(inputDate(row?.loss_date), base.lost_date),
    confirmed_cost: inputMoney(row?.original_value) || base.confirmed_cost,
    confirmed_start_date: inputDate(row?.usage_start) || base.confirmed_start_date,
  };
}

export function expandTranCaseRows(caseItem) {
  const rows = Array.isArray(caseItem?.metadata?.asset_rows)
    ? caseItem.metadata.asset_rows
    : [];
  if (!rows.length) {
    return [{
      form: formFromCase(caseItem),
      binding: { case_id: caseItem.id, source_row_index: null },
    }];
  }
  return rows.map((row, sourceRowIndex) => ({
    form: formFromSourceRow(caseItem, row),
    binding: { case_id: caseItem.id, source_row_index: sourceRowIndex },
  }));
}

function expandTranGroup(group) {
  return group?.cases?.flatMap(expandTranCaseRows) || [];
}

function tranBindingKey(binding) {
  if (!binding?.case_id) return "";
  return `${binding.case_id}:${binding.source_row_index ?? "case"}`;
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
    <details className="tran-resolution-item" open={!item.ready}>
      <summary className="tran-resolution-summary">
        <span className="tran-resolution-summary__asset">
          <strong>{translate(language, "tranAssetResult")} {index + 1}</strong>
          <span>{asset?.tag_number || "—"}</span>
        </span>
        <strong className={item.ready ? "is-ready" : "is-review"}>
          {translate(language, item.ready ? "tranReady" : "resultReview")}
        </strong>
        <span className="tran-resolution-summary__amount">
          {preview?.total_amount == null ? "—" : formatCurrency(preview.total_amount)}
        </span>
        <span className="tran-resolution-summary__details">{translate(language, "tranResultDetails")}</span>
      </summary>
      <div className="tran-resolution-body">
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
      </div>
    </details>
  );
}

function tranSourceLabel(batch, language) {
  const firstCase = batch?.cases?.[0];
  return firstCase?.source_eml?.filename
    || firstCase?.source_file
    || firstCase?.asset_code
    || translate(language, "tranDraftUnknownSource");
}

function TranDraftBatchResult({ busy, language, mode, onRetry, onRetryUncertain, outcome }) {
  if (!outcome) return null;
  const remaining = outcome.remaining || [];
  const uncertainFailures = outcome.failures.filter(({ error }) => isAmbiguousDraftError(error));
  const retryableFailures = outcome.failures.filter(({ error }) => !isAmbiguousDraftError(error));
  const retryableCount = retryableFailures.length + remaining.length;
  const incompleteCount = outcome.failures.length + remaining.length;
  const titleKey = {
    companion: "tranCompanionDraftReady",
    eml: "tranDraftReady",
    outlook: "tranOutlookDraftReady",
  }[mode];
  return (
    <div className="upload-result tran-batch-result">
      <strong>{translate(language, titleKey)}</strong>
      <p>
        {translate(language, "tranDraftBatchSummary")
          .replace("{success}", String(outcome.successes.length))
          .replace("{failed}", String(incompleteCount))
          .replace("{total}", String(outcome.total))}
      </p>
      <div className="tran-batch-result__list">
        {outcome.successes.map(({ batch, result }) => (
          <article className="tran-batch-result__item is-success" key={batch.key}>
            <div>
              <strong>{tranSourceLabel(batch, language)}</strong>
              <span>{translate(language, "tranDraftBatchSuccess")}</span>
            </div>
            <div className="tran-batch-result__links">
              {result.workbook_download_url && <a href={result.workbook_download_url} download>↓ Excel</a>}
              {mode === "eml" && result.draft_download_url && <a href={result.draft_download_url} download>↓ EML</a>}
              {mode === "outlook" && result.outlook_draft?.web_url && (
                <a href={result.outlook_draft.web_url} target="_blank" rel="noreferrer">
                  {translate(language, "tranOpenOutlookDraft")}
                </a>
              )}
              {mode === "companion" && <span>{translate(language, "tranCompanionDraftQueuedShort")}</span>}
            </div>
          </article>
        ))}
        {outcome.failures.map(({ batch, error }) => (
          <article className={`tran-batch-result__item ${isAmbiguousDraftError(error) ? "is-pending" : "is-failed"}`} key={batch.key}>
            <div>
              <strong>{tranSourceLabel(batch, language)}</strong>
              <span>
                {isAmbiguousDraftError(error)
                  ? translate(language, "tranDraftBatchUncertain")
                  : error?.message || translate(language, "tranDraftBatchFailed")}
              </span>
            </div>
          </article>
        ))}
        {remaining.map((batch) => (
          <article className="tran-batch-result__item is-pending" key={batch.key}>
            <div>
              <strong>{tranSourceLabel(batch, language)}</strong>
              <span>{translate(language, "tranDraftBatchNotRun")}</span>
            </div>
          </article>
        ))}
      </div>
      {outcome.stopped && <div className="dialog-note is-warning"><p>{translate(language, "tranDraftBatchStopped")}</p></div>}
      {retryableCount > 0 && (
        <button className="btn secondary" type="button" disabled={busy} onClick={onRetry}>
          {translate(language, "tranDraftBatchRetry")}
        </button>
      )}
      {uncertainFailures.length > 0 && (
        <button className="btn secondary" type="button" disabled={busy} onClick={onRetryUncertain}>
          {translate(language, "tranDraftBatchRetryUncertain")}
        </button>
      )}
    </div>
  );
}

export function TranWorkspace({
  capabilities,
  cases,
  language,
  onEmailUpload,
  onMailboxSynced,
  onOutlookMailboxInvalid,
  onReferencesChanged,
  outlookMailboxConnected = false,
  testDataClearVersion,
}) {
  const [forms, setForms] = useState([{ ...EMPTY_FORM }]);
  const [selectedGroupKeys, setSelectedGroupKeys] = useState([]);
  const [casePickerOpen, setCasePickerOpen] = useState(false);
  const [sourceBindings, setSourceBindings] = useState([]);
  const [pendingUploadedCaseId, setPendingUploadedCaseId] = useState("");
  const [emailNotice, setEmailNotice] = useState("");
  const [referenceStatus, setReferenceStatus] = useState(EMPTY_REFERENCE_STATUS);
  const [referenceLoading, setReferenceLoading] = useState(true);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [referenceProgress, setReferenceProgress] = useState(null);
  const [referencePhase, setReferencePhase] = useState("");
  const [referenceScanSeconds, setReferenceScanSeconds] = useState(0);
  const [referenceError, setReferenceError] = useState("");
  const [faFile, setFaFile] = useState(null);
  const [ccdcFile, setCcdcFile] = useState(null);
  const [clearCcdc, setClearCcdc] = useState(false);
  const [resolution, setResolution] = useState(null);
  const [busyAction, setBusyAction] = useState("");
  const [actionError, setActionError] = useState("");
  const pushToast = useToast();
  const [workbookResult, setWorkbookResult] = useState(null);
  const [draftResult, setDraftResult] = useState(null);
  const [outlookDraftResult, setOutlookDraftResult] = useState(null);
  const [companionDraftResult, setCompanionDraftResult] = useState(null);
  const [processingDate, setProcessingDate] = useState(localIsoDate());
  const [yearSheet, setYearSheet] = useState("");
  const [bodyIntro, setBodyIntro] = useState("");
  const [workspaceStep, setWorkspaceStep] = useState("input");
  const [draftBatchProgress, setDraftBatchProgress] = useState(null);
  const faInputRef = useRef(null);
  const ccdcInputRef = useRef(null);
  const referenceControllerRef = useRef(null);
  const actionControllerRef = useRef(null);
  const actionGenerationRef = useRef(0);
  const successfulDraftFingerprintsRef = useRef({
    companion: new Map(),
    eml: new Map(),
    outlook: new Map(),
  });
  const lostCases = useMemo(
    () => cases.filter((item) => (
      item.case_type === "LOST"
      && !["ACCOUNTED", "CLOSED"].includes(item.status)
    )),
    [cases],
  );
  const caseGroups = useMemo(() => groupTranCasesBySource(lostCases), [lostCases]);
  const casePickerGroups = useMemo(
    () => caseGroups.map((group) => {
      const firstCase = group.cases[0];
      const expanded = expandTranGroup(group);
      const assetCount = expanded.length;
      const label = group.handle
        ? firstCase?.source_eml?.filename || firstCase?.source_file || firstCase?.asset_code
        : firstCase?.asset_code || firstCase?.id;
      const tags = expanded.map((item) => item.form?.tag_number).filter(Boolean);
      const domains = expanded.map((item) => item.form?.domain).filter(Boolean);
      const assetNames = expanded.map((item) => item.form?.asset_name).filter(Boolean);
      return {
        key: group.key,
        label: label || translate(language, "tranDraftUnknownSource"),
        meta: [...new Set([...tags, ...domains])].join(" · ") || translate(language, "tranBatchNoMetadata"),
        searchText: [label, ...tags, ...assetNames, ...domains].filter(Boolean).join(" "),
        assetCount,
        caseCount: group.cases.length,
      };
    }),
    [caseGroups, language],
  );
  const selection = useMemo(
    () => selectTranGroupEntries(caseGroups, selectedGroupKeys, expandTranGroup, MAX_TRAN_ASSETS),
    [caseGroups, selectedGroupKeys],
  );
  const selectedCaseCount = useMemo(
    () => new Set(selection.groups.flatMap((group) => group.cases.map((item) => item.id))).size,
    [selection.groups],
  );
  const sourceBatchState = useMemo(
    () => {
      try {
        return {
          batches: buildTranSourceBatches(caseGroups, selectedGroupKeys, forms, sourceBindings),
          error: "",
        };
      } catch (error) {
        return { batches: [], error: error.message };
      }
    },
    [caseGroups, forms, selectedGroupKeys, sourceBindings],
  );
  const sourceBatches = sourceBatchState.batches;
  const draftSourcesReady = selectedGroupKeys.length > 0
    && !sourceBatchState.error
    && sourceBatches.length === selectedGroupKeys.length
    && sourceBatches.every((batch) => batch.handle && batch.forms.length === batch.bindings.length);
  const draftSourceLimitReady = sourceBatches.length <= MAX_TRAN_DRAFT_SOURCES;
  const pendingEmlDraftCount = draftBatchesNeedingCreation("eml").length;
  const pendingOutlookDraftCount = draftBatchesNeedingCreation("outlook").length;
  const pendingCompanionDraftCount = draftBatchesNeedingCreation("companion").length;
  const resolvedItems = resolution?.results || [];
  const canResolve = capabilities?.tran_lookup === true || referenceStatus.fa_gl.available;
  const canExport = capabilities?.tran_workbook_export === true;
  const canDraft = capabilities?.tran_draft === true;
  const canOutlookDraft = capabilities?.tran_outlook_draft === true;
  const canCompanionDraft = capabilities?.tran_companion_draft === true;

  useEffect(() => {
    referenceControllerRef.current?.abort();
    actionControllerRef.current?.abort();
    actionGenerationRef.current += 1;
    successfulDraftFingerprintsRef.current = {
      companion: new Map(),
      eml: new Map(),
      outlook: new Map(),
    };
    setForms([{ ...EMPTY_FORM }]);
    setSelectedGroupKeys([]);
    setCasePickerOpen(false);
    setSourceBindings([]);
    setPendingUploadedCaseId("");
    setEmailNotice("");
    setFaFile(null);
    setCcdcFile(null);
    setClearCcdc(false);
    setReferenceError("");
    setReferenceProgress(null);
    setReferencePhase("");
    setReferenceScanSeconds(0);
    setReferenceStatus(EMPTY_REFERENCE_STATUS);
    setReferenceBusy(false);
    setResolution(null);
    setBusyAction("");
    setActionError("");
    setWorkbookResult(null);
    setDraftResult(null);
    setOutlookDraftResult(null);
    setCompanionDraftResult(null);
    setProcessingDate(localIsoDate());
    setYearSheet("");
    setBodyIntro("");
    setWorkspaceStep("input");
    setDraftBatchProgress(null);
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
    if (!referenceBusy || referencePhase !== "processing") return undefined;
    const startedAt = Date.now();
    setReferenceScanSeconds(0);
    const timer = window.setInterval(() => {
      setReferenceScanSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [referenceBusy, referencePhase]);

  useEffect(() => {
    if (!pendingUploadedCaseId) return;
    const uploadedCase = cases.find((item) => item.id === pendingUploadedCaseId);
    if (!uploadedCase) return;
    const uploaded = lostCases.find((item) => item.id === pendingUploadedCaseId);
    if (!uploaded) {
      setPendingUploadedCaseId("");
      setEmailNotice(translate(language, "tranEmailFinalizedCase"));
      return;
    }
    const group = caseGroups.find((item) => item.cases.some((caseItem) => caseItem.id === uploaded.id));
    if (!group) return;
    const expanded = expandTranGroup(group);
    if (expanded.length > MAX_TRAN_ASSETS) {
      setPendingUploadedCaseId("");
      setEmailNotice(translate(language, "tranTooManyAssets"));
      return;
    }
    const applied = applyCaseSelection([...selectedGroupKeys, group.key], { announceChange: false });
    if (!applied) {
      setPendingUploadedCaseId("");
      setEmailNotice(translate(language, "tranTooManyAssets"));
      return;
    }
    setPendingUploadedCaseId("");
    setEmailNotice(translate(language, "tranEmailPrefillReady"));
  }, [caseGroups, cases, language, lostCases, pendingUploadedCaseId, selectedGroupKeys]);

  useEffect(() => {
    const validKeys = selectedGroupKeys.filter((key) => caseGroups.some((item) => item.key === key));
    if (validKeys.length === selectedGroupKeys.length) return;
    applyCaseSelection(validKeys, { announceChange: false });
  }, [caseGroups, selectedGroupKeys]);

  function draftFingerprint(mode, batch) {
    return tranDraftRequestFingerprint(mode, batch, {
      bodyIntro,
      processingDate,
      yearSheet,
    });
  }

  function draftBatchesNeedingCreation(mode) {
    const fingerprints = successfulDraftFingerprintsRef.current[mode];
    return sourceBatches.filter((batch) => fingerprints.get(batch.key) !== draftFingerprint(mode, batch));
  }

  function invalidateOutputs() {
    actionGenerationRef.current += 1;
    actionControllerRef.current?.abort();
    actionControllerRef.current = null;
    setBusyAction("");
    setResolution(null);
    setWorkbookResult(null);
    setDraftResult(null);
    setOutlookDraftResult(null);
    setCompanionDraftResult(null);
    setActionError("");
  }

  function updateField(index, field, value) {
    setForms((current) => current.map((item, itemIndex) => (
      itemIndex === index ? { ...item, [field]: value } : item
    )));
    invalidateOutputs();
    setWorkspaceStep("input");
  }

  function applyCaseSelection(nextKeys, { announceChange = true } = {}) {
    const nextSelection = selectTranGroupEntries(
      caseGroups,
      [...new Set(nextKeys || [])],
      expandTranGroup,
      MAX_TRAN_ASSETS,
    );
    if (nextSelection.overLimit) {
      const message = translate(language, "tranBatchSelectAllExceeded")
        .replace("{count}", String(nextSelection.assetCount));
      setActionError(message);
      pushToast(translate(language, "toastErrorTitle"), message, "error");
      return false;
    }
    const previousForms = new Map(
      sourceBindings.map((binding, index) => [tranBindingKey(binding), forms[index]]),
    );
    const nextEntries = nextSelection.entries;
    const hadSnapshot = Boolean(
      resolution || workbookResult || draftResult || outlookDraftResult || companionDraftResult,
    );
    setSelectedGroupKeys(nextSelection.groups.map((group) => group.key));
    setSourceBindings(nextEntries.map((entry) => entry.binding));
    setForms(nextEntries.length
      ? nextEntries.map((entry) => previousForms.get(tranBindingKey(entry.binding)) || entry.form)
      : [{ ...EMPTY_FORM }]);
    setEmailNotice("");
    invalidateOutputs();
    setDraftBatchProgress(null);
    setWorkspaceStep("input");
    setCasePickerOpen(false);
    if (announceChange && hadSnapshot) {
      pushToast(
        translate(language, "toastInfoTitle"),
        translate(language, "tranBatchChanged"),
        "info",
      );
    }
    return true;
  }

  function loadDemoData() {
    setSelectedGroupKeys([]);
    setSourceBindings([]);
    setForms([{ ...DEMO_FORM, lost_date: localIsoDate() }]);
    setEmailNotice("");
    invalidateOutputs();
    setDraftBatchProgress(null);
    setWorkspaceStep("input");
    setCasePickerOpen(false);
  }

  function removeAssetRow(index) {
    const binding = sourceBindings[index];
    if (binding) {
      const sourceGroup = caseGroups.find((group) => (
        group.cases.some((caseItem) => caseItem.id === binding.case_id)
      ));
      if (sourceGroup) {
        applyCaseSelection(selectedGroupKeys.filter((key) => key !== sourceGroup.key));
        return;
      }
    }
    if (forms.length <= 1) return;
    setForms((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setSourceBindings((current) => current.filter((_, itemIndex) => itemIndex !== index));
    invalidateOutputs();
    setWorkspaceStep("input");
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
    setReferencePhase("uploading");
    setReferenceScanSeconds(0);
    setReferenceError("");
    try {
      const response = await dashboardApi.uploadTranReferences(
        faFile,
        ccdcFile,
        clearCcdc,
        {
          signal: controller.signal,
          onProgress: ({ percent }) => setReferenceProgress(percent),
          onUploadComplete: () => {
            setReferenceProgress(null);
            setReferencePhase("processing");
          },
        },
      );
      setReferenceStatus(response.status);
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "tranReferenceUploadSuccess"),
        "success",
      );
      setFaFile(null);
      setCcdcFile(null);
      setClearCcdc(false);
      if (faInputRef.current) faInputRef.current.value = "";
      if (ccdcInputRef.current) ccdcInputRef.current.value = "";
      invalidateOutputs();
      setWorkspaceStep("input");
      await onReferencesChanged?.();
    } catch (error) {
      if (error.name !== "AbortError") {
        pushToast(translate(language, "toastErrorTitle"), error.message, "error");
      }
    } finally {
      if (!controller.signal.aborted) {
        setReferenceBusy(false);
        setReferencePhase("");
        setReferenceProgress(null);
      }
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
    const generation = actionGenerationRef.current + 1;
    actionGenerationRef.current = generation;
    setBusyAction("resolve");
    setResolution(null);
    setWorkspaceStep("result");
    setActionError("");
    setWorkbookResult(null);
    setDraftResult(null);
    setOutlookDraftResult(null);
    setCompanionDraftResult(null);
    try {
      const result = await dashboardApi.resolveTranAssets(
        buildTranAssetsPayload(forms, language),
        controller.signal,
      );
      if (controller.signal.aborted || generation !== actionGenerationRef.current) return;
      setResolution(result);
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "tranLookupReady"),
        result.ready ? "success" : "warning",
      );
    } catch (error) {
      if (error.name !== "AbortError" && generation === actionGenerationRef.current) {
        setResolution(null);
        setWorkspaceStep("input");
        pushToast(translate(language, "toastErrorTitle"), error.message, "error");
      }
    } finally {
      if (!controller.signal.aborted && generation === actionGenerationRef.current) setBusyAction("");
    }
  }

  async function exportWorkbook() {
    if (!resolution?.ready || !canExport) return;
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    const generation = actionGenerationRef.current + 1;
    actionGenerationRef.current = generation;
    setBusyAction("workbook");
    setActionError("");
    setWorkbookResult(null);
    try {
      const result = await dashboardApi.exportTranWorkbook(
        buildTranAssetsPayload(forms, language),
        processingDate,
        yearSheet.trim(),
        controller.signal,
      );
      if (controller.signal.aborted || generation !== actionGenerationRef.current) return;
      setWorkbookResult(result);
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "tranWorkbookReady"),
        "success",
      );
    } catch (error) {
      if (error.name !== "AbortError" && generation === actionGenerationRef.current) {
        pushToast(translate(language, "toastErrorTitle"), error.message, "error");
      }
    } finally {
      if (!controller.signal.aborted && generation === actionGenerationRef.current) setBusyAction("");
    }
  }

  async function createDraftBatch(mode, requestedBatches = sourceBatches, previousOutcome = null) {
    const intro = bodyIntro.trim();
    const available = mode === "eml"
      ? canDraft
      : mode === "outlook"
        ? canOutlookDraft && outlookMailboxConnected
        : canCompanionDraft;
    if (!resolution?.ready || !available || !draftSourcesReady) return;
    if (!intro) {
      setActionError(translate(language, "tranDraftIntroRequired"));
      return;
    }
    if (!draftSourceLimitReady) {
      setActionError(translate(language, "tranDraftSourceLimit"));
      return;
    }
    if (!requestedBatches.length) return;
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    const generation = actionGenerationRef.current + 1;
    actionGenerationRef.current = generation;
    const requestFingerprints = new Map(
      requestedBatches.map((batch) => [batch.key, draftFingerprint(mode, batch)]),
    );
    const busyKey = mode === "eml" ? "draft" : `${mode}-draft`;
    setBusyAction(busyKey);
    setActionError("");
    setDraftBatchProgress({ completed: 0, total: requestedBatches.length });
    if (!previousOutcome && mode === "eml") setDraftResult(null);
    if (!previousOutcome && mode === "outlook") setOutlookDraftResult(null);
    if (!previousOutcome && mode === "companion") setCompanionDraftResult(null);
    try {
      const outcome = await runTranBatch(
        requestedBatches,
        (batch) => {
          const args = [
            buildTranAssetsPayload(batch.forms, language),
            batch.bindings,
            batch.handle,
            intro,
            processingDate,
            yearSheet.trim(),
            controller.signal,
          ];
          if (mode === "eml") return dashboardApi.createTranDraft(...args);
          if (mode === "outlook") return dashboardApi.createTranOutlookDraft(...args);
          return dashboardApi.createTranCompanionDraft(...args);
        },
        {
          onSuccess: ({ batch }) => {
            successfulDraftFingerprintsRef.current[mode].set(
              batch.key,
              requestFingerprints.get(batch.key),
            );
          },
          onProgress: ({ completed, total }) => setDraftBatchProgress({ completed, total }),
          shouldStop: (error) => {
            const status = Number(error?.status || 0);
            return !status || status === 401 || status === 403 || status >= 500;
          },
        },
      );
      if (controller.signal.aborted || generation !== actionGenerationRef.current) return;
      const replacedKeys = new Set(requestedBatches.map((batch) => batch.key));
      const previousSuccesses = previousOutcome
        ? previousOutcome.successes.filter(({ batch }) => !replacedKeys.has(batch.key))
        : [];
      const previousFailures = previousOutcome
        ? previousOutcome.failures.filter(({ batch }) => !replacedKeys.has(batch.key))
        : [];
      const previousRemaining = previousOutcome
        ? (previousOutcome.remaining || []).filter((batch) => !replacedKeys.has(batch.key))
        : [];
      const order = new Map(sourceBatches.map((batch, index) => [batch.key, index]));
      const combinedOutcome = {
        ...outcome,
        successes: [...previousSuccesses, ...outcome.successes]
          .sort((left, right) => order.get(left.batch.key) - order.get(right.batch.key)),
        failures: [...previousFailures, ...outcome.failures]
          .sort((left, right) => order.get(left.batch.key) - order.get(right.batch.key)),
        remaining: [...previousRemaining, ...outcome.remaining]
          .sort((left, right) => order.get(left.key) - order.get(right.key)),
        attempted: previousSuccesses.length + outcome.attempted,
        total: sourceBatches.length,
      };
      if (mode === "eml") setDraftResult(combinedOutcome);
      if (mode === "outlook") setOutlookDraftResult(combinedOutcome);
      if (mode === "companion") setCompanionDraftResult(combinedOutcome);
      const incompleteCount = combinedOutcome.failures.length + combinedOutcome.remaining.length;
      const allSucceeded = incompleteCount === 0 && !combinedOutcome.stopped;
      const message = allSucceeded
        ? translate(language, "tranDraftBatchAllReady").replace("{count}", String(combinedOutcome.successes.length))
        : translate(language, "tranDraftBatchPartial")
          .replace("{success}", String(combinedOutcome.successes.length))
          .replace("{failed}", String(incompleteCount));
      pushToast(
        translate(language, allSucceeded ? "toastSuccessTitle" : "toastWarningTitle"),
        message,
        allSucceeded ? "success" : "warning",
      );
      if (!allSucceeded) setActionError(message);
      if (mode === "outlook") {
        const authFailure = combinedOutcome.failures.some(({ error }) => [401, 503].includes(Number(error?.status)));
        if (authFailure) {
          onOutlookMailboxInvalid?.();
          await Promise.resolve(onMailboxSynced?.()).catch(() => {});
        }
      }
    } catch (error) {
      if (error.name !== "AbortError" && generation === actionGenerationRef.current) {
        pushToast(translate(language, "toastErrorTitle"), error.message, "error");
      }
    } finally {
      if (!controller.signal.aborted && generation === actionGenerationRef.current) {
        setBusyAction("");
        setDraftBatchProgress(null);
      }
    }
  }

  function createDraft() {
    return createDraftBatch("eml", draftBatchesNeedingCreation("eml"), draftResult);
  }

  function createOutlookDraft() {
    return createDraftBatch("outlook", draftBatchesNeedingCreation("outlook"), outlookDraftResult);
  }

  function createCompanionDraft() {
    return createDraftBatch("companion", draftBatchesNeedingCreation("companion"), companionDraftResult);
  }

  function retryDraftBatch(mode, outcome) {
    const retryKeys = new Set([
      ...outcome.failures
        .filter(({ error }) => !isAmbiguousDraftError(error))
        .map(({ batch }) => batch.key),
      ...(outcome.remaining || []).map((batch) => batch.key),
    ]);
    return createDraftBatch(
      mode,
      sourceBatches.filter((batch) => retryKeys.has(batch.key)),
      outcome,
    );
  }

  function retryUncertainDraftBatch(mode, outcome) {
    if (!window.confirm(translate(language, "tranDraftBatchUncertainConfirm"))) return undefined;
    const retryKeys = new Set(
      outcome.failures
        .filter(({ error }) => isAmbiguousDraftError(error))
        .map(({ batch }) => batch.key),
    );
    return createDraftBatch(
      mode,
      sourceBatches.filter((batch) => retryKeys.has(batch.key)),
      outcome,
    );
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
                {translate(
                  language,
                  referenceBusy
                    ? (referencePhase === "processing" ? "tranReferenceProcessingButton" : "tranReferenceUploadingButton")
                    : "tranReferenceUpload",
                )}
              </button>
              <span className="disabled-note">{translate(language, "tranReferenceTrustedErp")}</span>
            </div>
            {referenceBusy && (
              <div className="upload-progress" role="status">
                {referencePhase === "processing" ? (
                  <>
                    <span>
                      {translate(language, "tranReferenceProcessing")} {" "}
                      {translate(language, "tranReferenceElapsed")} {elapsedClock(referenceScanSeconds)}
                    </span>
                    <progress />
                  </>
                ) : (
                  <>
                    <span>{translate(language, "tranReferenceUploading")}</span>
                    <progress max="100" value={referenceProgress ?? undefined} />
                  </>
                )}
              </div>
            )}
          </form>
        ) : <div className="disabled-note">{translate(language, "tranReferenceUploadUnavailable")}</div>}
        {referenceError && <div className="inline-error" role="alert">{referenceError}</div>}
      </section>

      <nav className="panel tran-workbench-nav" aria-label={translate(language, "tranWorkflowSteps")}>
        <div className="tran-workbench-tabs" role="tablist">
          <button
            aria-controls="tran-step-input"
            aria-selected={workspaceStep === "input"}
            className={`tran-workbench-tab ${workspaceStep === "input" ? "is-active" : ""}`}
            disabled={Boolean(busyAction)}
            id="tran-tab-input"
            onClick={() => setWorkspaceStep("input")}
            role="tab"
            type="button"
          >
            {translate(language, "tranStepInput")}
            <span className="tran-workbench-tab__count">{forms.length}</span>
          </button>
          <button
            aria-controls="tran-step-result-output"
            aria-selected={workspaceStep === "result"}
            className={`tran-workbench-tab ${workspaceStep === "result" ? "is-active" : ""}`}
            disabled={Boolean(busyAction) || !resolution}
            id="tran-tab-result"
            onClick={() => setWorkspaceStep("result")}
            role="tab"
            type="button"
          >
            {translate(language, "tranStepResult")}
            {resolution && <span className="tran-workbench-tab__count">{resolvedItems.length}</span>}
          </button>
          <button
            aria-controls="tran-step-result-output"
            aria-selected={workspaceStep === "output"}
            className={`tran-workbench-tab ${workspaceStep === "output" ? "is-active" : ""}`}
            disabled={Boolean(busyAction) || !resolution?.ready}
            id="tran-tab-output"
            onClick={() => setWorkspaceStep("output")}
            role="tab"
            type="button"
          >
            {translate(language, "tranStepOutput")}
            {resolution?.ready && <span className="tran-workbench-tab__count">{sourceBatches.length || 1}</span>}
          </button>
        </div>
        <div className="tran-batch-bar">
          <div className="tran-batch-bar__summary" aria-live="polite">
            <span>{translate(language, "tranBatchSelectionTitle")}</span>
            <strong>
              {selectedGroupKeys.length
                ? translate(language, "tranBatchSelectedSummary")
                  .replace("{cases}", String(selectedCaseCount))
                  .replace("{assets}", String(sourceBindings.length))
                : translate(language, "tranBatchManualMode")}
            </strong>
          </div>
          <div className="tran-batch-bar__actions">
            <button className="btn secondary" type="button" disabled={Boolean(busyAction)} onClick={() => setCasePickerOpen(true)}>
              {translate(language, "tranBatchChoose")}
            </button>
            <button className="btn secondary" type="button" disabled={Boolean(busyAction) || !selectedGroupKeys.length} onClick={() => applyCaseSelection([])}>
              {translate(language, "tranBatchClear")}
            </button>
            <button className="btn secondary" type="button" disabled={Boolean(busyAction)} onClick={loadDemoData}>
              {translate(language, "loadDemo")}
            </button>
          </div>
        </div>
      </nav>

      <TranCasePickerDialog
        groups={casePickerGroups}
        language={language}
        maxAssets={MAX_TRAN_ASSETS}
        onApply={(keys) => applyCaseSelection(keys)}
        onClose={() => setCasePickerOpen(false)}
        open={casePickerOpen}
        selectedKeys={selectedGroupKeys}
      />

      <section
        aria-labelledby="tran-tab-input"
        className="panel operation-panel compensation-panel tran-workbench-panel"
        hidden={workspaceStep !== "input"}
        id="tran-step-input"
        role="tabpanel"
      >
        <h3>{translate(language, "compensationInput")}</h3>
        <form className="compensation-form" onSubmit={resolveAsset}>
          <fieldset className="tran-form-lock" disabled={Boolean(busyAction)}>
          {selectedGroupKeys.length > 0 && (
            <div className="dialog-note is-warning" role="status">
              <p>{translate(language, "casePrefillNotice")} {translate(language, "tranBatchPerSourceHint")}</p>
            </div>
          )}

          <div className="tran-asset-editor-list">
            {forms.map((form, index) => (
              <article className="tran-asset-editor" key={sourceBindings[index] ? `${sourceBindings[index].case_id}:${sourceBindings[index].source_row_index ?? "case"}` : `manual-${index}`}>
                <header>
                  <strong>{translate(language, "tranAssetRow")} {index + 1}</strong>
                  <span>{form.tag_number || translate(language, "manualEntry")}</span>
                  {forms.length > 1 && (
                    <button className="btn secondary button--compact" type="button" onClick={() => removeAssetRow(index)}>
                      {translate(language, sourceBindings[index] ? "tranRemoveSourceGroup" : "tranRemoveAsset")}
                    </button>
                  )}
                </header>
                {sourceBindings[index] && (
                  <p className="field-hint">{translate(language, "tranSourceIdentityLocked")}</p>
                )}
                <div className="compensation-grid">
                  <label><span>{translate(language, "tagNumber")} *</span><input type="text" maxLength={255} value={form.tag_number} readOnly={Boolean(sourceBindings[index])} aria-readonly={sourceBindings[index] ? "true" : undefined} onChange={(event) => updateField(index, "tag_number", event.target.value.toUpperCase())} required /></label>
                  <label><span>{translate(language, "assetName")} *</span><input type="text" maxLength={1024} value={form.asset_name} onChange={(event) => updateField(index, "asset_name", event.target.value)} required /></label>
                  <label><span>{translate(language, "domain")} *</span><input type="text" maxLength={253} value={form.domain} readOnly={Boolean(sourceBindings[index])} aria-readonly={sourceBindings[index] ? "true" : undefined} onChange={(event) => updateField(index, "domain", event.target.value)} required /></label>
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
                    <label><span>{translate(language, "originalCost")}</span><input type="text" inputMode="numeric" pattern="[0-9]*" maxLength={32} value={form.confirmed_cost} onChange={(event) => { if (/^\d*$/.test(event.target.value)) updateField(index, "confirmed_cost", event.target.value); }} /></label>
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
          </fieldset>
        </form>
      </section>

      <section
        aria-labelledby={workspaceStep === "output" ? "tran-tab-output" : "tran-tab-result"}
        aria-live="polite"
        className="panel compensation-result-panel tran-workbench-panel"
        hidden={workspaceStep === "input"}
        id="tran-step-result-output"
        role="tabpanel"
      >
        <h3>{translate(language, workspaceStep === "output" ? "tranOutputs" : "tranResolutionResult")}</h3>
        {actionError && <div className="inline-error" role="alert">{actionError}</div>}
        {workspaceStep === "result" && !resolvedItems.length && busyAction !== "resolve" && (
          <div className="compensation-empty">{translate(language, "tranNoResolution")}</div>
        )}
        {workspaceStep === "result" && busyAction === "resolve" && (
          <div className="upload-progress" role="status">
            <span>{translate(language, "tranResolving")}</span><progress />
          </div>
        )}
        {!!resolvedItems.length && (
          <>
            <div className="tran-result-step" hidden={workspaceStep !== "result"}>
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
              <div className="tran-step-actions">
                <button className="btn secondary" type="button" disabled={Boolean(busyAction)} onClick={() => setWorkspaceStep("input")}>
                  {translate(language, "tranEditAsset")}
                </button>
                <button className="btn" type="button" disabled={Boolean(busyAction) || !resolution.ready} onClick={() => setWorkspaceStep("output")}>
                  {translate(language, "tranContinueOutput")}
                </button>
              </div>
            </div>

            <div className="tran-output-section" hidden={workspaceStep !== "output"}>
              <div className="tran-step-actions tran-step-actions--top">
                <button className="btn secondary" type="button" disabled={Boolean(busyAction)} onClick={() => setWorkspaceStep("result")}>
                  {translate(language, "tranBackToResult")}
                </button>
                <button className="btn secondary" type="button" disabled={Boolean(busyAction)} onClick={() => setWorkspaceStep("input")}>
                  {translate(language, "tranEditAsset")}
                </button>
              </div>
              <div className="panel-row tran-output-controls">
                <label className="small" htmlFor="tran-processing-date">{translate(language, "tranProcessingDate")}</label>
                <input id="tran-processing-date" type="date" disabled={Boolean(busyAction)} value={processingDate} onChange={(event) => { setProcessingDate(event.target.value); setWorkbookResult(null); setDraftResult(null); setOutlookDraftResult(null); setCompanionDraftResult(null); }} />
                <label className="small" htmlFor="tran-year-sheet">{translate(language, "tranYearSheet")}</label>
                <input id="tran-year-sheet" className="short-input" type="text" disabled={Boolean(busyAction)} maxLength="31" value={yearSheet} placeholder={processingDate.slice(0, 4)} onChange={(event) => { setYearSheet(event.target.value); setWorkbookResult(null); setDraftResult(null); setOutlookDraftResult(null); setCompanionDraftResult(null); }} />
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
                  disabled={Boolean(busyAction)}
                  maxLength="10000"
                  value={bodyIntro}
                  placeholder={translate(language, "tranDraftIntroPlaceholder")}
                  onChange={(event) => { setBodyIntro(event.target.value); setDraftResult(null); setOutlookDraftResult(null); setCompanionDraftResult(null); setActionError(""); }}
                />
                <div className="upload-actions">
                  <button className="btn secondary" type="button" disabled={Boolean(busyAction) || referenceBusy || !resolution.ready || !canDraft || !draftSourcesReady || !draftSourceLimitReady || pendingEmlDraftCount === 0} onClick={createDraft}>
                    {translate(language, busyAction === "draft" ? "tranDrafting" : "tranCreateDraft")}{pendingEmlDraftCount > 1 ? ` (${pendingEmlDraftCount})` : ""}
                  </button>
                  <button className="btn" type="button" disabled={Boolean(busyAction) || referenceBusy || !resolution.ready || !canOutlookDraft || !outlookMailboxConnected || !draftSourcesReady || !draftSourceLimitReady || pendingOutlookDraftCount === 0} onClick={createOutlookDraft}>
                    {translate(language, busyAction === "outlook-draft" ? "tranOutlookDrafting" : "tranCreateOutlookDraft")}{pendingOutlookDraftCount > 1 ? ` (${pendingOutlookDraftCount})` : ""}
                  </button>
                  <button className="btn" type="button" disabled={Boolean(busyAction) || referenceBusy || !resolution.ready || !canCompanionDraft || !draftSourcesReady || !draftSourceLimitReady || pendingCompanionDraftCount === 0} onClick={createCompanionDraft}>
                    {translate(language, busyAction === "companion-draft" ? "tranCompanionDrafting" : "tranCreateCompanionDraft")}{pendingCompanionDraftCount > 1 ? ` (${pendingCompanionDraftCount})` : ""}
                  </button>
                </div>
                <span className="disabled-note">{translate(language, "tranDraftSafety")}</span>
                {selectedGroupKeys.length > 1 && <div className="disabled-note">{translate(language, "tranDraftMixedSources")}</div>}
                {!draftSourcesReady && <div className="disabled-note">{translate(language, "tranDraftNeedsSource")}</div>}
                {!draftSourceLimitReady && <div className="disabled-note">{translate(language, "tranDraftSourceLimit")}</div>}
                {!canDraft && <div className="disabled-note">{translate(language, "tranDraftUnavailable")}</div>}
                {canOutlookDraft && !outlookMailboxConnected && <div className="disabled-note">{translate(language, "tranOutlookDraftNeedsConnection")}</div>}
                {!canOutlookDraft && <div className="disabled-note">{translate(language, "tranOutlookDraftUnavailable")}</div>}
                {!canCompanionDraft && <div className="disabled-note">{translate(language, "tranCompanionDraftUnavailable")}</div>}
                {draftBatchProgress && (
                  <div className="upload-progress" role="status">
                    <span>
                      {translate(language, "tranDraftBatchProgress")
                        .replace("{done}", String(draftBatchProgress.completed))
                        .replace("{total}", String(draftBatchProgress.total))}
                    </span>
                    <progress max={draftBatchProgress.total} value={draftBatchProgress.completed} />
                  </div>
                )}
              </div>
              <TranDraftBatchResult busy={Boolean(busyAction)} language={language} mode="eml" outcome={draftResult} onRetry={() => retryDraftBatch("eml", draftResult)} onRetryUncertain={() => retryUncertainDraftBatch("eml", draftResult)} />
              <TranDraftBatchResult busy={Boolean(busyAction)} language={language} mode="outlook" outcome={outlookDraftResult} onRetry={() => retryDraftBatch("outlook", outlookDraftResult)} onRetryUncertain={() => retryUncertainDraftBatch("outlook", outlookDraftResult)} />
              <TranDraftBatchResult busy={Boolean(busyAction)} language={language} mode="companion" outcome={companionDraftResult} onRetry={() => retryDraftBatch("companion", companionDraftResult)} onRetryUncertain={() => retryUncertainDraftBatch("companion", companionDraftResult)} />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
