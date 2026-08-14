import React, { useEffect, useMemo, useRef, useState } from "react";

import { dashboardApi, normalizeCase } from "../api.js";
import { ALLOWED_TRANSITIONS, STATUS_META, STATUS_ORDER } from "../constants.js";
import {
  caseTypeMeta,
  displayValue,
  formatCurrency,
  formatDate,
  formatMetadataValue,
  humanizeKey,
  statusClass,
} from "../utils.js";
import { Icon } from "./Icon.jsx";

function DescriptionList({ fields, className = "detail-grid" }) {
  return <dl className={className}>{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{displayValue(value)}</dd></div>)}</dl>;
}

function HistoryTimeline({ loading, error, history }) {
  return (
    <section className="detail-section audit-section" aria-labelledby="audit-history-heading">
      <div className="detail-section__heading"><h3 id="audit-history-heading">Lịch sử xử lý</h3>{loading && <span className="inline-loading" role="status">Đang tải…</span>}</div>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {!loading && !error && !history.length && <div className="audit-empty"><Icon name="clock" /><span>Chưa có thay đổi trạng thái.</span></div>}
      {!!history.length && <ol className="audit-timeline">{history.map((event, index) => {
        const target = STATUS_META[event.to_status]?.label || event.to_status;
        const source = event.from_status ? (STATUS_META[event.from_status]?.label || event.from_status) : "Khởi tạo";
        return (
          <li key={event.id ?? `${event.changed_at}-${index}`}>
            <span className="audit-timeline__marker" aria-hidden="true" />
            <div className="audit-timeline__content">
              <div><strong>{target}</strong><time dateTime={event.changed_at}>{formatDate(event.changed_at, true)}</time></div>
              <p>{source} → {target}</p>
              <span>{event.actor || "system"}{event.note ? ` · ${event.note}` : ""}</span>
            </div>
          </li>
        );
      })}</ol>}
    </section>
  );
}

export function CaseDrawer({ caseItem, statusBusy, onClose, onUpdateStatus }) {
  const dialogRef = useRef(null);
  const [detailCase, setDetailCase] = useState(null);
  const [history, setHistory] = useState([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [nextStatus, setNextStatus] = useState(caseItem?.status || "NEW");

  const displayCase = detailCase || caseItem;
  const allowed = useMemo(() => ALLOWED_TRANSITIONS[displayCase?.status] || [], [displayCase?.status]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (caseItem && dialog && !dialog.open) dialog.showModal();
    if (!caseItem && dialog?.open) dialog.close();
  }, [caseItem]);

  useEffect(() => {
    if (!caseItem) return undefined;
    setDetailCase(null);
    setHistory([]);
    setNextStatus(caseItem.status);
    setDetailLoading(true);
    setDetailError("");
    const controller = new AbortController();
    dashboardApi.caseDetail(caseItem.api_id, controller.signal)
      .then((payload) => {
        setDetailCase(payload?.case ? normalizeCase(payload.case) : caseItem);
        setHistory(Array.isArray(payload?.history) ? payload.history : []);
      })
      .catch((error) => {
        if (error.name !== "AbortError") setDetailError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [caseItem]);

  useEffect(() => {
    if (displayCase) setNextStatus(displayCase.status);
  }, [displayCase?.id, displayCase?.status]);

  if (!displayCase) return null;
  const type = caseTypeMeta(displayCase.case_type);
  const status = STATUS_META[displayCase.status] || STATUS_META.NEW;
  const detailFields = [
    ["Mã tài sản", displayCase.asset_code],
    ["Tên tài sản", displayCase.asset_name],
    ["Ngày tiếp nhận", formatDate(displayCase.received_at, true)],
    ["Giá trị còn lại", formatCurrency(displayCase.residual_value)],
    ["Phí trách nhiệm / sửa chữa", formatCurrency(displayCase.responsibility_fee)],
    ["Tình trạng sửa chữa", displayCase.repair_status],
    ["Supplier", [displayCase.supplier_number, displayCase.supplier_name].filter(Boolean).join(" · ")],
    ["Supplier site", displayCase.supplier_site],
    ["File nguồn", displayCase.source_file],
    ["Cập nhật lần cuối", formatDate(displayCase.updated_at || displayCase.created_at, true)],
  ];
  const metadataFields = Object.entries(displayCase.metadata || {})
    .filter(([, value]) => value != null && value !== "")
    .map(([key, value]) => [humanizeKey(key), formatMetadataValue(value)]);

  function submitStatus(event) {
    event.preventDefault();
    if (nextStatus !== displayCase.status && allowed.includes(nextStatus)) onUpdateStatus(displayCase, nextStatus);
  }

  return (
    <dialog ref={dialogRef} className="dialog drawer" aria-labelledby="case-dialog-title" onCancel={onClose} onClose={onClose} onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className="drawer__shell">
        <header className="dialog__header drawer__header">
          <div><span className="section-kicker">Chi tiết hồ sơ · {type.label}</span><h2 id="case-dialog-title">{displayCase.id}</h2></div>
          <button className="icon-button" type="button" aria-label="Đóng chi tiết hồ sơ" onClick={onClose}><Icon name="close" /></button>
        </header>
        <div className="drawer__body">
          <div className="detail-hero">
            <div><span className={`type-badge type-badge--${type.className}`}>{type.label}</span><h3>{displayCase.employee_name || displayCase.domain || "Chưa xác định nhân viên"}</h3><p>{displayCase.domain ? `@${displayCase.domain}` : "Chưa có domain"}</p></div>
            <div className="detail-amount"><span>Giá trị đền bù</span><strong>{formatCurrency(displayCase.amount)}</strong></div>
          </div>

          <section className="detail-section" aria-labelledby="detail-status-heading">
            <div className="detail-section__heading"><h3 id="detail-status-heading">Trạng thái xử lý</h3><span className={`status-badge status-badge--${statusClass(displayCase.status)}`}>{status.label}</span></div>
            <form className="status-form" onSubmit={submitStatus}>
              <label htmlFor="detail-status-select">Chuyển trạng thái</label>
              <div className="status-form__controls">
                <select id="detail-status-select" value={nextStatus} disabled={!allowed.length || statusBusy} onChange={(event) => setNextStatus(event.target.value)}>
                  {STATUS_ORDER.map((value) => <option value={value} disabled={value !== displayCase.status && !allowed.includes(value)} key={value}>{STATUS_META[value].label}</option>)}
                </select>
                <button className={`button button--primary button--compact ${statusBusy ? "is-busy" : ""}`} type="submit" disabled={statusBusy || !allowed.length || nextStatus === displayCase.status} aria-busy={statusBusy}>Lưu trạng thái</button>
              </div>
            </form>
          </section>

          <section className="detail-section" aria-labelledby="detail-info-heading"><h3 id="detail-info-heading">Thông tin hồ sơ</h3><DescriptionList fields={detailFields} /></section>

          {!!displayCase.warnings.length && <section className="detail-section" aria-labelledby="detail-warning-heading"><h3 id="detail-warning-heading">Điểm cần kiểm tra</h3><ul className="detail-warnings">{displayCase.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul></section>}

          <HistoryTimeline loading={detailLoading} error={detailError} history={history} />

          {!!metadataFields.length && <section className="detail-section"><details className="metadata-disclosure"><summary>Dữ liệu bổ sung</summary><DescriptionList fields={metadataFields} className="detail-grid detail-grid--compact" /></details></section>}
        </div>
      </div>
    </dialog>
  );
}
