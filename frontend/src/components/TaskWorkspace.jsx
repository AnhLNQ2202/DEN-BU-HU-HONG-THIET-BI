import React, { useMemo, useState } from "react";

import { dashboardApi } from "../api.js";
import { API } from "../constants.js";
import { translate } from "../i18n.js";
import { formatCurrency, suggestedBatchName, toNumber } from "../utils.js";
import { CaseTable } from "./CaseTable.jsx";
import { UploadWorkspace } from "./UploadWorkspace.jsx";

function localIsoDate(value = new Date()) {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 10);
}

const EMPTY_COMPENSATION_FORM = Object.freeze({
  tag_number: "",
  asset_name: "",
  domain: "",
  lost_date: "",
  start_date: "",
  cost: "",
  asset_number: "",
  book: "",
  entity: "",
  cost_center: "",
  product_code: "",
  location: "",
  group: "",
  fee_rate: "",
  physical: "",
  lookup_status: "",
});

const DEMO_COMPENSATION_FORM = Object.freeze({
  ...EMPTY_COMPENSATION_FORM,
  tag_number: "LAP-DEMO-001",
  asset_name: "Laptop demo",
  domain: "demo.user",
  start_date: "2025-01-15",
  cost: "20000000",
  physical: true,
  lookup_status: "MATCHED",
});

function resultStatusLabel(language, status) {
  const keys = {
    CALCULATED: "resultCalculated",
    EXEMPT: "resultExempt",
    NEEDS_REVIEW: "resultReview",
    NOT_APPLICABLE: "resultNotApplicable",
  };
  return translate(language, keys[status] || "resultStatus");
}

function percentage(value) {
  if (value == null) return "—";
  return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(value * 100)}%`;
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
  const local = /^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$/.exec(text);
  if (!local) return "";
  return `${local[3]}-${local[2].padStart(2, "0")}-${local[1].padStart(2, "0")}`;
}

function inputMoney(value) {
  if (value === "" || value == null) return "";
  const parsed = toNumber(value, Number.NaN);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? String(parsed) : "";
}

function inputBoolean(value) {
  if (typeof value === "boolean") return value;
  const normalized = String(value || "").trim().toLowerCase();
  if (["true", "yes", "1", "physical", "hardware"].includes(normalized)) return true;
  if (["false", "no", "0", "non-physical", "software"].includes(normalized)) return false;
  return "";
}

function lookupStatusFromCase(caseItem) {
  const explicit = String(firstSourceValue(caseItem, ["lookup_status", "reference_status"]) || "")
    .trim()
    .toUpperCase();
  if (["MATCHED", "NOT_FOUND", "AMBIGUOUS"].includes(explicit)) return explicit;
  const warnings = (caseItem?.warnings || []).join(" ").toLowerCase();
  if (/multiple|ambiguous|duplicate/.test(warnings)) return "AMBIGUOUS";
  if (/supplier.*not found|missing supplier/.test(warnings)) return "NOT_FOUND";
  if (caseItem?.supplier_number && caseItem?.supplier_site) return "MATCHED";
  return "";
}

function compensationFormFromCase(caseItem) {
  const group = String(firstSourceValue(caseItem, ["group", "depreciation_group"]) || "")
    .trim()
    .toUpperCase();
  const feeRate = firstSourceValue(caseItem, ["fee_rate", "responsibility_fee_rate"]);
  return {
    ...EMPTY_COMPENSATION_FORM,
    tag_number: String(caseItem?.asset_code || "").trim().toUpperCase(),
    asset_name: String(caseItem?.asset_name || "").trim(),
    domain: String(caseItem?.domain || "").trim(),
    lost_date: inputDate(firstSourceValue(caseItem, ["loss_date", "lost_date"])),
    start_date: inputDate(firstSourceValue(caseItem, ["usage_start", "start_date", "in_service_date"])),
    cost: inputMoney(firstSourceValue(caseItem, ["original_value", "original_cost", "cost"])),
    asset_number: String(firstSourceValue(caseItem, ["asset_number"]) || "").trim(),
    book: String(firstSourceValue(caseItem, ["book"]) || "").trim(),
    entity: String(firstSourceValue(caseItem, ["entity"]) || "").trim(),
    cost_center: String(firstSourceValue(caseItem, ["cost_center"]) || "").trim(),
    product_code: String(firstSourceValue(caseItem, ["product_code"]) || "").trim(),
    location: String(firstSourceValue(caseItem, ["location"]) || "").trim(),
    group: ["FOUR_YEAR", "SIX_YEAR"].includes(group) ? group : "",
    fee_rate: feeRate === "" ? "" : String(feeRate),
    physical: inputBoolean(firstSourceValue(caseItem, ["physical", "is_physical"])),
    lookup_status: lookupStatusFromCase(caseItem),
  };
}

export function NganWorkspace({
  batches,
  capabilities,
  cases,
  clearingTestData,
  language,
  resetting,
  statusBusy,
  testDataClearVersion,
  onClearTestData,
  onEmailUpload,
  onOpenBatch,
  onOpenCase,
  onReset,
  onSupplierUpload,
  onUpdateStatus,
}) {
  const [batchName, setBatchName] = useState(suggestedBatchName());
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
          <input id="accounting-start" className="short-input" type="number" value="1" readOnly />
          <button
            className="btn"
            type="button"
            disabled={!readyCases.length}
            onClick={() => onOpenBatch(batchName, readyCases)}
          >
            {translate(language, "runAccounting")}
          </button>
          {!readyCases.length && <span className="result-file none">{translate(language, "noReadyCases")}</span>}
        </div>
      </section>

      <section className="panel operation-panel">
        <h3>{translate(language, "pdfPanel")}</h3>
        <div className="panel-row">
          <label className="small" htmlFor="pdf-batch">{translate(language, "batchName")}</label>
          <input id="pdf-batch" type="text" value={batchName} readOnly />
          <label className="small" htmlFor="pdf-pages">{translate(language, "pagesPerMail")}</label>
          <input id="pdf-pages" className="short-input" type="number" value="2" readOnly />
          <button className="btn" type="button" disabled>{translate(language, "mergePdf")}</button>
        </div>
        <div className="disabled-note">{translate(language, "localOnly")}</div>
      </section>

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
        statusBusy={statusBusy}
        showFilters={false}
        onOpen={onOpenCase}
        onUpdateStatus={onUpdateStatus}
      />
    </>
  );
}

export function TranWorkspace({ cases, language }) {
  const [form, setForm] = useState({ ...EMPTY_COMPENSATION_FORM });
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lostCases = useMemo(
    () => cases.filter((item) => item.case_type === "LOST"),
    [cases],
  );

  function updateField(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
    setError("");
  }

  function chooseCase(caseId) {
    setSelectedCaseId(caseId);
    const selected = lostCases.find((item) => item.id === caseId);
    if (!selected) {
      setForm({ ...EMPTY_COMPENSATION_FORM });
      setResult(null);
      setError("");
      return;
    }
    setForm(compensationFormFromCase(selected));
    setResult(null);
    setError("");
  }

  async function calculate(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const asset = Object.fromEntries(
        Object.entries(form).filter(([, value]) => value !== ""),
      );
      const response = await dashboardApi.previewCompensation([asset]);
      setResult(response?.results?.[0] || null);
    } catch (requestError) {
      setResult(null);
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tran-workspace">
      <div className="task-heading">
        <div>
          <h2>{translate(language, "tranTitle")}</h2>
          <p>{translate(language, "tranDescription")}</p>
        </div>
        <button className="btn secondary" type="button" disabled>
          {translate(language, "draftMail")}
        </button>
      </div>

      <section className="panel operation-panel compensation-panel">
        <h3>{translate(language, "compensationInput")}</h3>
        <form className="compensation-form" onSubmit={calculate}>
          <div className="compensation-prefill">
            <label>
              <span>{translate(language, "chooseLostCase")}</span>
              <select value={selectedCaseId} onChange={(event) => chooseCase(event.target.value)}>
                <option value="">{translate(language, "manualEntry")}</option>
                {lostCases.map((caseItem) => (
                  <option value={caseItem.id} key={caseItem.id}>
                    {caseItem.asset_code} · {caseItem.domain}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn secondary"
              type="button"
              onClick={() => {
                setSelectedCaseId("");
                setForm({ ...DEMO_COMPENSATION_FORM, lost_date: localIsoDate() });
                setResult(null);
                setError("");
              }}
            >
              {translate(language, "loadDemo")}
            </button>
          </div>

          {selectedCaseId && (
            <div className="dialog-note is-warning" role="status">
              <p>{translate(language, "casePrefillNotice")}</p>
            </div>
          )}

          <div className="compensation-grid">
            <label><span>{translate(language, "tagNumber")} *</span><input type="text" value={form.tag_number} onChange={(event) => updateField("tag_number", event.target.value.toUpperCase())} required /></label>
            <label><span>{translate(language, "assetName")} *</span><input type="text" value={form.asset_name} onChange={(event) => updateField("asset_name", event.target.value)} required /></label>
            <label><span>{translate(language, "domain")} *</span><input type="text" value={form.domain} onChange={(event) => updateField("domain", event.target.value)} required /></label>
            <label><span>{translate(language, "lostDate")} *</span><input type="date" value={form.lost_date} onChange={(event) => updateField("lost_date", event.target.value)} required /></label>
            <label><span>{translate(language, "startDate")} *</span><input type="date" value={form.start_date} onChange={(event) => updateField("start_date", event.target.value)} required /></label>
            <label><span>{translate(language, "originalCost")} *</span><input type="number" min="0" step="1" value={form.cost} onChange={(event) => updateField("cost", event.target.value)} required /></label>
            <label>
              <span>{translate(language, "referenceStatus")}</span>
              <select value={form.lookup_status} onChange={(event) => updateField("lookup_status", event.target.value)} required>
                <option value="">{translate(language, "chooseReferenceStatus")}</option>
                <option value="MATCHED">{translate(language, "referenceMatched")}</option>
                <option value="NOT_FOUND">{translate(language, "referenceMissing")}</option>
                <option value="AMBIGUOUS">{translate(language, "referenceAmbiguous")}</option>
              </select>
            </label>
            <label>
              <span>{translate(language, "physicalAsset")}</span>
              <select
                value={form.physical === "" ? "" : String(form.physical)}
                onChange={(event) => updateField(
                  "physical",
                  event.target.value === "" ? "" : event.target.value === "true",
                )}
                required
              >
                <option value="">{translate(language, "choosePhysical")}</option>
                <option value="true">{translate(language, "physicalYes")}</option>
                <option value="false">{translate(language, "physicalNo")}</option>
              </select>
            </label>
          </div>

          <details className="compensation-advanced">
            <summary>{translate(language, "advancedSource")}</summary>
            <div className="compensation-grid">
              <label><span>{translate(language, "assetNumber")}</span><input type="text" value={form.asset_number} onChange={(event) => updateField("asset_number", event.target.value)} /></label>
              <label><span>{translate(language, "book")}</span><input type="text" value={form.book} onChange={(event) => updateField("book", event.target.value)} /></label>
              <label><span>{translate(language, "entity")}</span><input type="text" value={form.entity} onChange={(event) => updateField("entity", event.target.value)} /></label>
              <label><span>{translate(language, "costCenter")}</span><input type="text" value={form.cost_center} onChange={(event) => updateField("cost_center", event.target.value)} /></label>
              <label><span>{translate(language, "productCode")}</span><input type="text" value={form.product_code} onChange={(event) => updateField("product_code", event.target.value)} /></label>
              <label><span>{translate(language, "location")}</span><input type="text" value={form.location} onChange={(event) => updateField("location", event.target.value)} /></label>
              <label>
                <span>{translate(language, "depreciationOverride")}</span>
                <select value={form.group} onChange={(event) => updateField("group", event.target.value)}>
                  <option value="">{translate(language, "automaticMapping")}</option>
                  <option value="FOUR_YEAR">{translate(language, "fourYear")}</option>
                  <option value="SIX_YEAR">{translate(language, "sixYear")}</option>
                </select>
              </label>
              <label>
                <span>{translate(language, "feeOverride")}</span>
                <select value={form.fee_rate} onChange={(event) => updateField("fee_rate", event.target.value)}>
                  <option value="">{translate(language, "automaticMapping")}</option>
                  <option value="0.05">5%</option>
                  <option value="0.30">30%</option>
                </select>
              </label>
            </div>
          </details>

          {error && <div className="inline-error" role="alert">{error}</div>}
          <div className="compensation-actions">
            <button className="btn" type="submit" disabled={busy}>
              {busy ? translate(language, "calculatingPreview") : translate(language, "calculatePreview")}
            </button>
            <span className="disabled-note">{translate(language, "tranDisabled")}</span>
          </div>
        </form>
      </section>

      <section className="panel compensation-result-panel" aria-live="polite">
        <h3>{translate(language, "calculationResult")}</h3>
        {!result && !busy && <div className="compensation-empty">{translate(language, "noCalculation")}</div>}
        {result && (
          <>
            <div className={`compensation-result-summary result-${result.status?.toLowerCase()}`}>
              <span>{translate(language, "resultStatus")}</span>
              <strong>{resultStatusLabel(language, result.status)}</strong>
            </div>
            <dl className="compensation-metrics">
              <div><dt>{translate(language, "usageMonths")}</dt><dd>{result.usage_months == null ? "—" : `${result.usage_months} ${translate(language, "months")}`}</dd></div>
              <div><dt>{translate(language, "depreciationOverride")}</dt><dd>{result.depreciation_group === "FOUR_YEAR" ? translate(language, "fourYear") : result.depreciation_group === "SIX_YEAR" ? translate(language, "sixYear") : "—"}</dd></div>
              <div><dt>{translate(language, "remainingRate")}</dt><dd>{percentage(result.remaining_rate)}</dd></div>
              <div><dt>{translate(language, "remainingValue")}</dt><dd>{result.remaining_value == null ? "—" : formatCurrency(result.remaining_value)}</dd></div>
              <div><dt>{translate(language, "responsibilityFee")}</dt><dd>{result.fee_value == null ? "—" : `${formatCurrency(result.fee_value)} · ${percentage(result.fee_rate)}`}</dd></div>
              <div className="compensation-total"><dt>{translate(language, "totalCompensation")}</dt><dd>{result.total_amount == null ? "—" : formatCurrency(result.total_amount)}</dd></div>
            </dl>
            {!!result.reasons?.length && <ul className="compensation-reasons">{result.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
            {result.formula_explanation && <details className="policy-trace"><summary>{translate(language, "policyTrace")}</summary><code>{result.formula_explanation}</code></details>}
          </>
        )}
      </section>
    </div>
  );
}
