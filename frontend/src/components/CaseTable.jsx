import React from "react";

import { ALLOWED_TRANSITIONS, STATUS_ORDER } from "../constants.js";
import { statusLabel, translate } from "../i18n.js";

function formatAmount(value) {
  if (value == null || value === "") return "";
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function formatCaseDate(value, language) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(language === "en" ? "en-US" : "vi-VN", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(date);
}

function StatusSelect({ caseItem, language, busy, onUpdateStatus }) {
  const allowed = ALLOWED_TRANSITIONS[caseItem.status] || [];
  return (
    <select
      className="status-select"
      value={caseItem.status}
      disabled={busy || !allowed.length}
      aria-label={`${translate(language, "status")} ${caseItem.id}`}
      onChange={(event) => onUpdateStatus(caseItem, event.target.value)}
    >
      {STATUS_ORDER.map((status) => (
        <option
          value={status}
          disabled={status !== caseItem.status && !allowed.includes(status)}
          key={status}
        >
          {statusLabel(language, status)}
        </option>
      ))}
    </select>
  );
}

function CaseRows({ cases, language, statusBusy, onOpen, onUpdateStatus }) {
  return cases.map((caseItem) => {
    const isDone = ["ACCOUNTED", "CLOSED"].includes(caseItem.status);
    const hasWarnings = caseItem.warnings.length > 0;
    const rowClass = [isDone ? "done" : "", hasWarnings ? "warning" : ""]
      .filter(Boolean)
      .join(" ");
    const typeClass = caseItem.case_type === "LOST" ? "tag-lost" : "tag-damaged";
    const typeLabel = translate(
      language,
      caseItem.case_type === "LOST" ? "lostLabel" : "damagedLabel",
    );

    return (
      <tr className={rowClass} key={caseItem.id}>
        <td>
          <button className="case-link" type="button" onClick={() => onOpen(caseItem.id)}>
            {caseItem.id}
          </button>
        </td>
        <td><span className={`tag ${typeClass}`}>{typeLabel}</span></td>
        <td>{caseItem.domain || "—"}</td>
        <td>
          <strong>{caseItem.asset_code || "—"}</strong>
          {caseItem.asset_name && <span className="asset-name">{caseItem.asset_name}</span>}
        </td>
        <td>{formatCaseDate(caseItem.received_at, language)}</td>
        <td className="col-amount">{formatAmount(caseItem.amount)}</td>
        <td className="warn-text">{caseItem.warnings.join("; ")}</td>
        <td>
          <StatusSelect
            caseItem={caseItem}
            language={language}
            busy={statusBusy}
            onUpdateStatus={onUpdateStatus}
          />
        </td>
        <td className="doc-cell">
          <button
            className="mail-link document-button"
            type="button"
            title={caseItem.source_file || translate(language, "openDetails")}
            onClick={() => onOpen(caseItem.id)}
          >
            ✉ {translate(language, "mail")}
          </button>
          <button
            className="mail-link-pdf document-button is-disabled"
            type="button"
            disabled
            title={translate(language, "localOnly")}
          >
            ▧ {translate(language, "pdf")}
          </button>
        </td>
      </tr>
    );
  });
}

export function CaseTable({
  cases,
  filters,
  language,
  refreshing,
  statusBusy,
  showFilters = true,
  onFiltersChange,
  onOpen,
  onRefresh,
  onUpdateStatus,
}) {
  return (
    <>
      {showFilters && (
        <div className="filter-row">
          <div className="search-box">
            <span className="search-icon" aria-hidden="true">⌕</span>
            <label className="sr-only" htmlFor="case-search">{translate(language, "searchPlaceholder")}</label>
            <input
              id="case-search"
              type="text"
              autoComplete="off"
              value={filters.query}
              placeholder={translate(language, "searchPlaceholder")}
              onChange={(event) => onFiltersChange({ query: event.target.value })}
            />
          </div>
          <label className="filter-chip">
            <span className="chip-label">{translate(language, "type")}</span>
            <select
              value={filters.type}
              onChange={(event) => onFiltersChange({ type: event.target.value })}
            >
              <option value="ALL">{translate(language, "allTypes")}</option>
              <option value="LOST">{translate(language, "lostLabel")}</option>
              <option value="DAMAGED">{translate(language, "damagedLabel")}</option>
            </select>
          </label>
          <label className="filter-chip">
            <span className="chip-label">{translate(language, "status")}</span>
            <select
              value={filters.status}
              onChange={(event) => onFiltersChange({ status: event.target.value })}
            >
              <option value="ALL">{translate(language, "allStatuses")}</option>
              {STATUS_ORDER.map((status) => (
                <option value={status} key={status}>{statusLabel(language, status)}</option>
              ))}
            </select>
          </label>
          <label className="filter-chip">
            <span className="chip-label">{translate(language, "warningFilter")}</span>
            <select
              value={filters.warning}
              onChange={(event) => onFiltersChange({ warning: event.target.value })}
            >
              <option value="ALL">{translate(language, "all")}</option>
              <option value="WARN">{translate(language, "onlyWarnings")}</option>
            </select>
          </label>
          <button
            className="btn secondary btn-refresh"
            type="button"
            disabled={refreshing}
            onClick={onRefresh}
          >
            <span className={refreshing ? "refresh-icon is-spinning" : "refresh-icon"}>↻</span>
            {translate(language, "refresh")}
          </button>
        </div>
      )}

      <div className="table-scroll">
        <table className="case-table">
          <thead>
            <tr>
              <th>{translate(language, "caseId")}</th>
              <th>{translate(language, "type")}</th>
              <th>{translate(language, "domain")}</th>
              <th>{translate(language, "asset")}</th>
              <th>{translate(language, "receivedDate")}</th>
              <th className="col-amount">{translate(language, "amount")}</th>
              <th>{translate(language, "warningFilter")}</th>
              <th>{translate(language, "status")}</th>
              <th>{translate(language, "documents")}</th>
            </tr>
          </thead>
          <tbody>
            <CaseRows
              cases={cases}
              language={language}
              statusBusy={statusBusy}
              onOpen={onOpen}
              onUpdateStatus={onUpdateStatus}
            />
          </tbody>
        </table>
      </div>
      {!cases.length && <div className="table-empty">{translate(language, "noCases")}</div>}
    </>
  );
}
