import React, { useEffect, useMemo, useRef, useState } from "react";

import { translate } from "../i18n.js";
import { numberFormatter } from "../utils.js";
import { classifySupplierFiles, hasExactSupplierRoles } from "../workflowContracts.js";

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

function supplierSelectionError(files, classification, language) {
  if (!files.length) return "";
  if (files.length !== 2) {
    return `${translate(language, "supplierExactlyTwo")} (${numberFormatter.format(files.length)})`;
  }
  if (classification.unknown.length) {
    return translate(language, "supplierUnknownFilename");
  }
  if (classification.active.length !== 1 || classification.inactive.length !== 1) {
    return translate(language, "supplierRoleMismatch");
  }
  const signatures = new Set(files.map(
    (file) => `${file.name}\u0000${file.size}\u0000${file.lastModified}`,
  ));
  if (signatures.size !== files.length) return translate(language, "supplierSameFile");
  const totalSize = files.reduce((sum, file) => sum + file.size, 0);
  if (totalSize > SUPPLIER_MAX_TOTAL_SIZE) return translate(language, "supplierTotalTooLarge");
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

function DisclosureList({ className = "", heading, items, language }) {
  if (!items.length) return null;
  return (
    <details className={`upload-feedback-list upload-details ${className}`.trim()}>
      <summary>
        <span>{heading}</span>
        <strong>{numberFormatter.format(items.length)} · {translate(language, "uploadViewDetails")}</strong>
      </summary>
      <ul>{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>
    </details>
  );
}

function SelectedFilesDisclosure({ files, language }) {
  if (!files.length) return null;
  return (
    <div className="selected-upload-files" role="status">
      <strong>{numberFormatter.format(files.length)} {translate(language, "emailFilesSelected")}</strong>
      <details className="upload-details">
        <summary>{translate(language, "uploadViewDetails")}</summary>
        <ul>
          {files.map((file) => (
            <li key={`${file.name}-${file.size}-${file.lastModified}`}>
              <span>{file.name}</span><small>{formatSize(file.size)}</small>
            </li>
          ))}
        </ul>
      </details>
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
        <details className="upload-details upload-result__files">
          <summary>{translate(language, "uploadViewFileNames")}</summary>
          <ul>
            <li>{translate(language, "supplierActiveFile")}: {result.uploadedFileNames.active}</li>
            <li>{translate(language, "supplierInactiveFile")}: {result.uploadedFileNames.inactive}</li>
          </ul>
        </details>
      )}
      <dl className="upload-summary">
        <div><dt>{translate(language, "supplierActiveRows")}</dt><dd>{numberFormatter.format(countFrom(result, "active_count"))}</dd></div>
        <div><dt>{translate(language, "supplierInactiveRows")}</dt><dd>{numberFormatter.format(countFrom(result, "inactive_count"))}</dd></div>
        <div><dt>{translate(language, "supplierTotalRows")}</dt><dd>{numberFormatter.format(countFrom(result, "total_count"))}</dd></div>
        <div className={collisionCount ? "has-warning" : ""}><dt>{translate(language, "supplierCollisions")}</dt><dd>{numberFormatter.format(collisionCount)}</dd></div>
      </dl>
      <DisclosureList
        className="is-warning"
        heading={translate(language, "supplierCollisionDetails")}
        items={collisionMessages}
        language={language}
      />
      <DisclosureList
        className="is-warning"
        heading={translate(language, "uploadWarnings")}
        items={warnings}
        language={language}
      />
    </div>
  );
}

export function EmailResult({ language, result }) {
  if (!result) return null;
  const warnings = messagesFrom(result.warnings);
  const skippedFiles = messagesFrom(result.skipped_files);
  const unknownFiles = messagesFrom(result.unknown_files);
  const caseIds = Array.isArray(result.case_ids) ? result.case_ids.filter(Boolean) : [];
  return (
    <div className="upload-result" role="status" aria-live="polite">
      <strong>{result.message || translate(language, "emailUploadSuccess")}</strong>
      <dl className="upload-summary upload-summary--email">
        <div><dt>{translate(language, "emailReceived")}</dt><dd>{numberFormatter.format(countFrom(result, "received_count"))}</dd></div>
        <div><dt>{translate(language, "emailIngested")}</dt><dd>{numberFormatter.format(countFrom(result, "ingested"))}</dd></div>
        <div className={skippedFiles.length ? "has-warning" : ""}><dt>{translate(language, "emailSkipped")}</dt><dd>{numberFormatter.format(skippedFiles.length)}</dd></div>
        <div className={unknownFiles.length ? "has-warning" : ""}><dt>{translate(language, "emailRejected")}</dt><dd>{numberFormatter.format(unknownFiles.length)}</dd></div>
      </dl>
      {!!caseIds.length && (
        <details className="upload-details upload-case-ids">
          <summary>
            <span>{translate(language, "emailCreatedCases")} {numberFormatter.format(caseIds.length)}</span>
            <strong>{translate(language, "uploadViewDetails")}</strong>
          </summary>
          <ul>{caseIds.map((caseId) => <li key={caseId}>{caseId}</li>)}</ul>
        </details>
      )}
      <DisclosureList
        className="is-warning"
        heading={translate(language, "uploadWarnings")}
        items={warnings}
        language={language}
      />
      <DisclosureList
        className="is-warning"
        heading={translate(language, "emailSkippedFiles")}
        items={skippedFiles}
        language={language}
      />
      <DisclosureList
        className="is-error"
        heading={translate(language, "emailRejectedFiles")}
        items={unknownFiles}
        language={language}
      />
    </div>
  );
}

export function EmailUploadPanel({
  idPrefix = "email",
  language,
  onCompleted,
  onEmailUpload,
  resetVersion = 0,
  titleKey = "emailUploadPanel",
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [emailFiles, setEmailFiles] = useState([]);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailProgress, setEmailProgress] = useState(null);
  const [emailError, setEmailError] = useState("");
  const [emailResult, setEmailResult] = useState(null);
  const emailInputRef = useRef(null);
  const emailAbortRef = useRef(null);
  const selectedEmailError = useMemo(
    () => emailFilesError(emailFiles, language),
    [emailFiles, language],
  );
  const confirmationId = `${idPrefix}-synthetic-confirmation`;
  const inputId = `${idPrefix}-files`;
  const hintId = `${idPrefix}-file-hint`;
  const errorId = `${idPrefix}-file-error`;

  useEffect(() => () => emailAbortRef.current?.abort(), []);

  useEffect(() => {
    if (resetVersion < 1) return;
    emailAbortRef.current?.abort();
    setConfirmed(false);
    setEmailFiles([]);
    setEmailProgress(null);
    setEmailError("");
    setEmailResult(null);
    if (emailInputRef.current) emailInputRef.current.value = "";
  }, [resetVersion]);

  function selectEmailFiles(event) {
    setEmailFiles(Array.from(event.target.files || []));
    setEmailError("");
    setEmailResult(null);
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
      onCompleted?.(result);
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
    <section className="panel operation-panel upload-workspace" aria-busy={emailBusy}>
      <h3>{translate(language, titleKey)}</h3>
      <div className="upload-staging-warning">
        <strong>{translate(language, "uploadStagingTitle")}</strong>
        <span>{translate(language, "emailStagingHint")}</span>
      </div>
      <label className="upload-confirmation" htmlFor={confirmationId}>
        <input
          id={confirmationId}
          type="checkbox"
          checked={confirmed}
          disabled={emailBusy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>{translate(language, "uploadConfirmation")}</span>
      </label>
      <form className="upload-form" onSubmit={uploadEmails} noValidate>
        <div className="panel-row email-upload-row">
          <label className="small" htmlFor={inputId}>{translate(language, "emailFileLabel")}</label>
          <input
            ref={emailInputRef}
            id={inputId}
            type="file"
            multiple
            accept=".eml,message/rfc822"
            required
            disabled={emailBusy}
            aria-describedby={`${hintId} ${errorId}`}
            aria-invalid={Boolean(selectedEmailError)}
            onChange={selectEmailFiles}
          />
          <button
            className="btn secondary"
            type="submit"
            disabled={!confirmed || !emailFiles.length || Boolean(selectedEmailError) || emailBusy}
          >
            {emailBusy ? translate(language, "emailUploading") : translate(language, "ingestEmail")}
          </button>
          {emailBusy && (
            <button className="btn secondary" type="button" onClick={() => emailAbortRef.current?.abort()}>
              {translate(language, "uploadCancel")}
            </button>
          )}
        </div>
        <p id={hintId} className="upload-hint">{translate(language, "emailFileTypes")}</p>
        {!!emailFiles.length && !selectedEmailError && (
          <SelectedFilesDisclosure files={emailFiles} language={language} />
        )}
        {selectedEmailError && <div id={errorId} className="inline-error" role="alert">{selectedEmailError}</div>}
        {emailError && <div className="inline-error" role="alert">{emailError}</div>}
        {emailBusy && <UploadProgress language={language} percent={emailProgress} kind="email" />}
        <EmailResult language={language} result={emailResult} />
      </form>
    </section>
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
  const [supplierFiles, setSupplierFiles] = useState([]);
  const [supplierBusy, setSupplierBusy] = useState(false);
  const [supplierProgress, setSupplierProgress] = useState(null);
  const [supplierError, setSupplierError] = useState("");
  const [supplierResult, setSupplierResult] = useState(null);
  const [emailFiles, setEmailFiles] = useState([]);
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailProgress, setEmailProgress] = useState(null);
  const [emailError, setEmailError] = useState("");
  const [emailResult, setEmailResult] = useState(null);
  const supplierInputRef = useRef(null);
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
    setSupplierFiles([]);
    setSupplierProgress(null);
    setSupplierError("");
    setSupplierResult(null);
    setEmailFiles([]);
    setEmailProgress(null);
    setEmailError("");
    setEmailResult(null);
    if (supplierInputRef.current) supplierInputRef.current.value = "";
    if (emailInputRef.current) emailInputRef.current.value = "";
  }, [testDataClearVersion]);

  const supplierClassification = useMemo(
    () => classifySupplierFiles(supplierFiles),
    [supplierFiles],
  );
  const activeFile = supplierClassification.active.length === 1
    ? supplierClassification.active[0]
    : null;
  const inactiveFile = supplierClassification.inactive.length === 1
    ? supplierClassification.inactive[0]
    : null;
  const supplierRolesReady = hasExactSupplierRoles(supplierFiles, supplierClassification);
  const supplierFileValidation = useMemo(() => {
    for (const file of supplierFiles) {
      const error = supplierFileError(file, file.name, language);
      if (error) return error;
    }
    return "";
  }, [language, supplierFiles]);
  const supplierPairError = useMemo(
    () => supplierSelectionError(supplierFiles, supplierClassification, language),
    [language, supplierClassification, supplierFiles],
  );
  const selectedEmailError = useMemo(
    () => emailFilesError(emailFiles, language),
    [emailFiles, language],
  );
  const dataActionBusy = Boolean(clearingTestData || resetting);

  const supplierReady = Boolean(
    confirmed
      && activeFile
      && inactiveFile
      && supplierRolesReady
      && !supplierPairError
      && !supplierFileValidation
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

  function selectSupplierFiles(event) {
    setSupplierFiles(Array.from(event.target.files || []));
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
    const validationError = supplierPairError
      || supplierFileValidation
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
      setSupplierFiles([]);
      if (supplierInputRef.current) supplierInputRef.current.value = "";
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
        <label className="upload-file-field supplier-file-picker" htmlFor="supplier-files">
          <span>{translate(language, "supplierLabel")}</span>
          <input
            ref={supplierInputRef}
            id="supplier-files"
            type="file"
            multiple
            accept=".xls,.xlsx,.csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
            required
            disabled={supplierBusy || emailBusy || dataActionBusy}
            aria-describedby="supplier-file-hint supplier-file-error"
            aria-invalid={Boolean(supplierPairError || supplierFileValidation)}
            onChange={selectSupplierFiles}
          />
        </label>
        <p id="supplier-file-hint" className="upload-hint">{translate(language, "supplierFileTypes")} {translate(language, "supplierFilenameRule")}</p>
        {!!supplierFiles.length && (
          <div className="supplier-detection" role="status" aria-live="polite">
            <div className={activeFile ? "is-ready" : "is-missing"}>
              <span>{translate(language, "supplierActiveFile")}</span>
              <strong>{activeFile
                ? translate(language, "supplierDetected")
                : supplierClassification.active.length > 1
                  ? `${numberFormatter.format(supplierClassification.active.length)} · ${translate(language, "supplierAmbiguous")}`
                  : translate(language, "supplierNotDetected")}</strong>
            </div>
            <div className={inactiveFile ? "is-ready" : "is-missing"}>
              <span>{translate(language, "supplierInactiveFile")}</span>
              <strong>{inactiveFile
                ? translate(language, "supplierDetected")
                : supplierClassification.inactive.length > 1
                  ? `${numberFormatter.format(supplierClassification.inactive.length)} · ${translate(language, "supplierAmbiguous")}`
                  : translate(language, "supplierNotDetected")}</strong>
            </div>
            <details className="upload-details supplier-selection-details">
              <summary>{numberFormatter.format(supplierFiles.length)} · {translate(language, "uploadViewDetails")}</summary>
              <ul>
                {supplierFiles.map((file) => (
                  <li key={`${file.name}-${file.size}-${file.lastModified}`}>
                    <span>{file.name}</span><small>{formatSize(file.size)}</small>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
        {(supplierPairError || supplierFileValidation) && (
          <div id="supplier-file-error" className="inline-error" role="alert">
            {supplierPairError || supplierFileValidation}
          </div>
        )}
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
          <SelectedFilesDisclosure files={emailFiles} language={language} />
        )}
        {selectedEmailError && <div id="email-file-error" className="inline-error" role="alert">{selectedEmailError}</div>}
        {emailError && <div className="inline-error" role="alert">{emailError}</div>}
        {emailBusy && <UploadProgress language={language} percent={emailProgress} kind="email" />}
        <EmailResult language={language} result={emailResult} />
      </form>
    </section>
  );
}
