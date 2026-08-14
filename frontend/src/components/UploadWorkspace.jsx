import React, { useEffect, useMemo, useRef, useState } from "react";

import { translate } from "../i18n.js";
import { numberFormatter } from "../utils.js";

const MIB = 1024 * 1024;
const SUPPLIER_EXTENSIONS = new Set([".xls", ".xlsx", ".csv"]);
const SUPPLIER_MAX_FILE_SIZE = 20 * MIB;
const SUPPLIER_MAX_TOTAL_SIZE = 48 * MIB;
const EMAIL_MAX_FILES = 20;
const EMAIL_MAX_FILE_SIZE = 2 * MIB;
const EMAIL_MAX_TOTAL_SIZE = 25 * MIB;

function extensionOf(file) {
  const name = String(file?.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot);
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < MIB) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1 }).format(bytes / MIB)} MB`;
}

function readableMessage(value) {
  if (typeof value === "string") return value.trim();
  if (!value || typeof value !== "object") return String(value || "").trim();
  const fileName = value.file || value.filename || value.source_file;
  const message = value.message || value.detail || value.error || value.reason || value.code;
  if (fileName && message) return `${fileName}: ${message}`;
  return String(message || fileName || JSON.stringify(value)).trim();
}

function messagesFrom(value) {
  if (value == null || value === "") return [];
  return (Array.isArray(value) ? value : [value]).map(readableMessage).filter(Boolean);
}

function countFrom(result, key, fallback = 0) {
  const value = result?.[key] ?? result?.status?.[key];
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function sameFile(first, second) {
  return Boolean(
    first
      && second
      && first.name === second.name
      && first.size === second.size
      && first.lastModified === second.lastModified,
  );
}

function supplierFileError(file, role, language) {
  if (!file) return "";
  if (!SUPPLIER_EXTENSIONS.has(extensionOf(file))) {
    return `${role}: ${translate(language, "supplierInvalidType")}`;
  }
  if (file.size === 0) return `${role}: ${translate(language, "uploadEmptyFile")}`;
  if (file.size > SUPPLIER_MAX_FILE_SIZE) {
    return `${role}: ${translate(language, "supplierFileTooLarge")}`;
  }
  return "";
}

function emailFilesError(files, language) {
  if (!files.length) return "";
  if (files.length > EMAIL_MAX_FILES) return translate(language, "emailTooManyFiles");

  const signatures = new Set();
  for (const file of files) {
    if (extensionOf(file) !== ".eml") {
      return `${file.name}: ${translate(language, "emailInvalidType")}`;
    }
    if (file.size === 0) return `${file.name}: ${translate(language, "uploadEmptyFile")}`;
    if (file.size > EMAIL_MAX_FILE_SIZE) {
      return `${file.name}: ${translate(language, "emailFileTooLarge")}`;
    }
    const signature = `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
    if (signatures.has(signature)) return translate(language, "emailDuplicateFile");
    signatures.add(signature);
  }

  const totalSize = files.reduce((sum, file) => sum + file.size, 0);
  if (totalSize > EMAIL_MAX_TOTAL_SIZE) return translate(language, "emailTotalTooLarge");
  return "";
}

function UploadProgress({ language, percent, kind }) {
  const label = translate(
    language,
    kind === "supplier" ? "supplierUploading" : "emailUploading",
  );
  return (
    <div className="upload-progress" role="status" aria-live="polite">
      <span>{label}{percent == null ? "…" : ` ${percent}%`}</span>
      <progress max="100" value={percent == null ? undefined : percent} aria-label={label} />
    </div>
  );
}

function WarningList({ className = "", heading, items }) {
  if (!items.length) return null;
  return (
    <div className={`upload-feedback-list ${className}`.trim()}>
      <strong>{heading}</strong>
      <ul>{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
    </div>
  );
}

function SupplierResult({ language, result }) {
  if (!result) return null;
  const collisions = Array.isArray(result.collisions) ? result.collisions : [];
  const collisionMessages = collisions.map((collision) => {
    const domain = String(collision?.domain || "").trim();
    const message = readableMessage(collision);
    const supplierNumbers = Array.isArray(collision?.supplier_numbers)
      ? collision.supplier_numbers.filter(Boolean).join(", ")
      : "";
    const supplierSites = Array.isArray(collision?.supplier_sites)
      ? collision.supplier_sites.filter(Boolean).join(", ")
      : "";
    const identifiers = [supplierNumbers, supplierSites].filter(Boolean).join(" · ");
    return [domain, message && message !== domain ? message : "", identifiers]
      .filter(Boolean)
      .join(": ");
  }).filter(Boolean);
  const warnings = messagesFrom(result.warnings);
  const collisionCount = countFrom(result, "collision_count", collisions.length);

  return (
    <div className="upload-result" role="status" aria-live="polite">
      <strong>{result.message || translate(language, "supplierUploadSuccess")}</strong>
      {result.uploadedFileNames && (
        <span className="upload-result__files">
          {result.uploadedFileNames.active} · {result.uploadedFileNames.inactive}
        </span>
      )}
      <dl className="upload-summary">
        <div><dt>{translate(language, "supplierActiveRows")}</dt><dd>{numberFormatter.format(countFrom(result, "active_count"))}</dd></div>
        <div><dt>{translate(language, "supplierInactiveRows")}</dt><dd>{numberFormatter.format(countFrom(result, "inactive_count"))}</dd></div>
        <div><dt>{translate(language, "supplierTotalRows")}</dt><dd>{numberFormatter.format(countFrom(result, "total_count"))}</dd></div>
        <div className={collisionCount ? "has-warning" : ""}><dt>{translate(language, "supplierCollisions")}</dt><dd>{numberFormatter.format(collisionCount)}</dd></div>
      </dl>
      <WarningList
        className="is-warning"
        heading={translate(language, "supplierCollisionDetails")}
        items={collisionMessages}
      />
      <WarningList
        className="is-warning"
        heading={translate(language, "uploadWarnings")}
        items={warnings}
      />
    </div>
  );
}

function EmailResult({ language, result }) {
  if (!result) return null;
  const warnings = messagesFrom(result.warnings);
  const unknownFiles = messagesFrom(result.unknown_files);
  const caseIds = Array.isArray(result.case_ids) ? result.case_ids.filter(Boolean) : [];
  return (
    <div className="upload-result" role="status" aria-live="polite">
      <strong>{result.message || translate(language, "emailUploadSuccess")}</strong>
      <dl className="upload-summary upload-summary--email">
        <div><dt>{translate(language, "emailReceived")}</dt><dd>{numberFormatter.format(countFrom(result, "received_count"))}</dd></div>
        <div><dt>{translate(language, "emailIngested")}</dt><dd>{numberFormatter.format(countFrom(result, "ingested"))}</dd></div>
        <div className={unknownFiles.length ? "has-warning" : ""}><dt>{translate(language, "emailRejected")}</dt><dd>{numberFormatter.format(unknownFiles.length)}</dd></div>
      </dl>
      {!!caseIds.length && (
        <div className="upload-case-ids">
          <strong>{translate(language, "emailCreatedCases")}</strong> {caseIds.join(", ")}
        </div>
      )}
      <WarningList
        className="is-warning"
        heading={translate(language, "uploadWarnings")}
        items={warnings}
      />
      <WarningList
        className="is-error"
        heading={translate(language, "emailRejectedFiles")}
        items={unknownFiles}
      />
    </div>
  );
}

export function UploadWorkspace({
  capabilities,
  clearingTestData,
  language,
  onClearTestData,
  onEmailUpload,
  onReset,
  onSupplierUpload,
  resetting,
  testDataClearVersion,
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [activeFile, setActiveFile] = useState(null);
  const [inactiveFile, setInactiveFile] = useState(null);
  const [supplierBusy, setSupplierBusy] = useState(false);
  const [supplierProgress, setSupplierProgress] = useState(null);
  const [supplierError, setSupplierError] = useState("");
  const [supplierResult, setSupplierResult] = useState(null);
  const [emailFiles, setEmailFiles] = useState([]);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailProgress, setEmailProgress] = useState(null);
  const [emailError, setEmailError] = useState("");
  const [emailResult, setEmailResult] = useState(null);
  const activeInputRef = useRef(null);
  const inactiveInputRef = useRef(null);
  const emailInputRef = useRef(null);
  const supplierAbortRef = useRef(null);
  const emailAbortRef = useRef(null);

  useEffect(() => () => {
    supplierAbortRef.current?.abort();
    emailAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (testDataClearVersion < 1) return;
    setConfirmed(false);
    setActiveFile(null);
    setInactiveFile(null);
    setSupplierProgress(null);
    setSupplierError("");
    setSupplierResult(null);
    setEmailFiles([]);
    setEmailProgress(null);
    setEmailError("");
    setEmailResult(null);
    if (activeInputRef.current) activeInputRef.current.value = "";
    if (inactiveInputRef.current) inactiveInputRef.current.value = "";
    if (emailInputRef.current) emailInputRef.current.value = "";
  }, [testDataClearVersion]);

  const activeError = supplierFileError(
    activeFile,
    translate(language, "supplierActiveFile"),
    language,
  );
  const inactiveError = supplierFileError(
    inactiveFile,
    translate(language, "supplierInactiveFile"),
    language,
  );
  const supplierPairError = useMemo(() => {
    if (sameFile(activeFile, inactiveFile)) return translate(language, "supplierSameFile");
    const totalSize = (activeFile?.size || 0) + (inactiveFile?.size || 0);
    if (totalSize > SUPPLIER_MAX_TOTAL_SIZE) return translate(language, "supplierTotalTooLarge");
    return "";
  }, [activeFile, inactiveFile, language]);
  const selectedEmailError = useMemo(
    () => emailFilesError(emailFiles, language),
    [emailFiles, language],
  );
  const dataActionBusy = Boolean(clearingTestData || resetting);

  const supplierReady = Boolean(
    confirmed
      && activeFile
      && inactiveFile
      && !activeError
      && !inactiveError
      && !supplierPairError
      && !supplierBusy
      && !emailBusy
      && !dataActionBusy,
  );
  const emailReady = Boolean(
    confirmed
      && emailFiles.length
      && !selectedEmailError
      && !emailBusy
      && !supplierBusy
      && !dataActionBusy,
  );

  function selectSupplierFile(role, event) {
    const file = event.target.files?.[0] || null;
    if (role === "active") setActiveFile(file);
    else setInactiveFile(file);
    setSupplierError("");
    setSupplierResult(null);
  }

  function selectEmailFiles(event) {
    setEmailFiles(Array.from(event.target.files || []));
    setEmailError("");
    setEmailResult(null);
  }

  async function uploadSuppliers(event) {
    event.preventDefault();
    const validationError = activeError
      || inactiveError
      || supplierPairError
      || (!activeFile ? translate(language, "supplierMissingActive") : "")
      || (!inactiveFile ? translate(language, "supplierMissingInactive") : "")
      || (!confirmed ? translate(language, "uploadConfirmationRequired") : "");
    if (validationError) {
      setSupplierError(validationError);
      return;
    }

    const controller = new AbortController();
    supplierAbortRef.current = controller;
    setSupplierBusy(true);
    setSupplierProgress(0);
    setSupplierError("");
    setSupplierResult(null);
    try {
      const result = await onSupplierUpload({
        activeFile,
        inactiveFile,
        signal: controller.signal,
        onProgress: ({ percent }) => setSupplierProgress(percent),
      });
      setSupplierResult({
        ...result,
        uploadedFileNames: { active: activeFile.name, inactive: inactiveFile.name },
      });
      setActiveFile(null);
      setInactiveFile(null);
      if (activeInputRef.current) activeInputRef.current.value = "";
      if (inactiveInputRef.current) inactiveInputRef.current.value = "";
    } catch (error) {
      setSupplierError(
        error.name === "AbortError" ? translate(language, "uploadCancelled") : error.message,
      );
    } finally {
      supplierAbortRef.current = null;
      setSupplierBusy(false);
      setSupplierProgress(null);
    }
  }

  async function uploadEmails(event) {
    event.preventDefault();
    const validationError = selectedEmailError
      || (!emailFiles.length ? translate(language, "emailMissingFiles") : "")
      || (!confirmed ? translate(language, "uploadConfirmationRequired") : "");
    if (validationError) {
      setEmailError(validationError);
      return;
    }

    const controller = new AbortController();
    emailAbortRef.current = controller;
    setEmailBusy(true);
    setEmailProgress(0);
    setEmailError("");
    setEmailResult(null);
    try {
      const result = await onEmailUpload({
        files: emailFiles,
        signal: controller.signal,
        onProgress: ({ percent }) => setEmailProgress(percent),
      });
      setEmailResult(result);
      setEmailFiles([]);
      if (emailInputRef.current) emailInputRef.current.value = "";
    } catch (error) {
      setEmailError(
        error.name === "AbortError" ? translate(language, "uploadCancelled") : error.message,
      );
    } finally {
      emailAbortRef.current = null;
      setEmailBusy(false);
      setEmailProgress(null);
    }
  }

  return (
    <section
      className="panel operation-panel upload-workspace"
      aria-busy={supplierBusy || emailBusy || dataActionBusy}
    >
      <h3>{translate(language, "supplierPanel")}</h3>
      <div className="upload-staging-warning">
        <strong>{translate(language, "uploadStagingTitle")}</strong>
        <span>{translate(language, "supplierHint")}</span>
      </div>
      <label className="upload-confirmation" htmlFor="synthetic-data-confirmation">
        <input
          id="synthetic-data-confirmation"
          type="checkbox"
          checked={confirmed}
          disabled={supplierBusy || emailBusy || dataActionBusy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>{translate(language, "uploadConfirmation")}</span>
      </label>

      <form className="upload-form" onSubmit={uploadSuppliers} noValidate>
        <div className="supplier-file-grid">
          <label className="upload-file-field" htmlFor="supplier-active-file">
            <span>{translate(language, "supplierActiveFile")}</span>
            <input
              ref={activeInputRef}
              id="supplier-active-file"
              type="file"
              accept=".xls,.xlsx,.csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
              required
              disabled={supplierBusy || emailBusy || dataActionBusy}
              aria-describedby="supplier-file-hint supplier-active-error"
              aria-invalid={Boolean(activeError)}
              onChange={(event) => selectSupplierFile("active", event)}
            />
            {activeFile && !activeError && <small>{activeFile.name} · {formatSize(activeFile.size)}</small>}
            {activeError && <small id="supplier-active-error" className="field-error">{activeError}</small>}
          </label>
          <label className="upload-file-field" htmlFor="supplier-inactive-file">
            <span>{translate(language, "supplierInactiveFile")}</span>
            <input
              ref={inactiveInputRef}
              id="supplier-inactive-file"
              type="file"
              accept=".xls,.xlsx,.csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
              required
              disabled={supplierBusy || emailBusy || dataActionBusy}
              aria-describedby="supplier-file-hint supplier-inactive-error"
              aria-invalid={Boolean(inactiveError)}
              onChange={(event) => selectSupplierFile("inactive", event)}
            />
            {inactiveFile && !inactiveError && <small>{inactiveFile.name} · {formatSize(inactiveFile.size)}</small>}
            {inactiveError && <small id="supplier-inactive-error" className="field-error">{inactiveError}</small>}
          </label>
        </div>
        <p id="supplier-file-hint" className="upload-hint">{translate(language, "supplierFileTypes")}</p>
        {supplierPairError && <div className="inline-error" role="alert">{supplierPairError}</div>}
        {supplierError && <div className="inline-error" role="alert">{supplierError}</div>}
        {supplierBusy && <UploadProgress language={language} percent={supplierProgress} kind="supplier" />}
        <div className="upload-actions">
          <button className="btn secondary" type="submit" disabled={!supplierReady}>
            {supplierBusy ? translate(language, "supplierUploading") : translate(language, "supplierAction")}
          </button>
          {supplierBusy && (
            <button className="btn secondary" type="button" onClick={() => supplierAbortRef.current?.abort()}>
              {translate(language, "uploadCancel")}
            </button>
          )}
        </div>
        <SupplierResult language={language} result={supplierResult} />
      </form>

      <div className="upload-divider" />
      <form className="upload-form" onSubmit={uploadEmails} noValidate>
        <h4>{translate(language, "emailUploadPanel")}</h4>
        <div className="panel-row email-upload-row">
          <label className="small" htmlFor="email-files">{translate(language, "emailFileLabel")}</label>
          <input
            ref={emailInputRef}
            id="email-files"
            type="file"
            multiple
            accept=".eml,message/rfc822"
            required
            disabled={emailBusy || supplierBusy || dataActionBusy}
            aria-describedby="email-file-hint email-file-error"
            aria-invalid={Boolean(selectedEmailError)}
            onChange={selectEmailFiles}
          />
          <button className="btn secondary" type="submit" disabled={!emailReady}>
            {emailBusy ? translate(language, "emailUploading") : translate(language, "ingestEmail")}
          </button>
          {emailBusy && (
            <button className="btn secondary" type="button" onClick={() => emailAbortRef.current?.abort()}>
              {translate(language, "uploadCancel")}
            </button>
          )}
          {capabilities?.test_reset === true && (
            <button
              className="btn danger-secondary"
              type="button"
              disabled={clearingTestData || resetting || supplierBusy || emailBusy}
              onClick={onClearTestData}
            >
              {clearingTestData
                ? translate(language, "clearingTestData")
                : translate(language, "clearTestData")}
            </button>
          )}
          {capabilities?.demo_reset === true && (
            <button
              className="btn secondary"
              type="button"
              disabled={resetting || clearingTestData || supplierBusy || emailBusy}
              onClick={onReset}
            >
              {resetting ? translate(language, "resettingDemo") : translate(language, "resetDemo")}
            </button>
          )}
        </div>
        <p id="email-file-hint" className="upload-hint">{translate(language, "emailFileTypes")}</p>
        {!!emailFiles.length && !selectedEmailError && (
          <div className="selected-upload-files">
            <strong>{numberFormatter.format(emailFiles.length)} {translate(language, "emailFilesSelected")}</strong>
            <span>{emailFiles.slice(0, 4).map((file) => file.name).join(", ")}{emailFiles.length > 4 ? ` +${emailFiles.length - 4}` : ""}</span>
          </div>
        )}
        {selectedEmailError && <div id="email-file-error" className="inline-error" role="alert">{selectedEmailError}</div>}
        {emailError && <div className="inline-error" role="alert">{emailError}</div>}
        {emailBusy && <UploadProgress language={language} percent={emailProgress} kind="email" />}
        <EmailResult language={language} result={emailResult} />
      </form>
    </section>
  );
}
