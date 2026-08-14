import React, { useEffect, useMemo, useRef, useState } from "react";

import { TYPE_META } from "../constants.js";
import { formatCurrency, isValidBatchName, numberFormatter, suggestedBatchName } from "../utils.js";
import { Icon } from "./Icon.jsx";

export function BatchDialog({ open, cases, busy, initialBatchName, onClose, onCreate }) {
  const dialogRef = useRef(null);
  const [batchName, setBatchName] = useState(suggestedBatchName());
  const [validation, setValidation] = useState("");
  const notReady = useMemo(() => cases.filter((item) => item.status !== "READY_FOR_ACCOUNTING"), [cases]);
  const totalAmount = useMemo(() => cases.reduce((sum, item) => sum + item.amount, 0), [cases]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) {
      setBatchName(initialBatchName || suggestedBatchName());
      setValidation("");
      dialog.showModal();
      window.requestAnimationFrame(() => dialog.querySelector("input")?.focus());
    }
    if (!open && dialog?.open) dialog.close();
  }, [initialBatchName, open]);

  function submit(event) {
    event.preventDefault();
    const normalized = batchName.trim().toUpperCase();
    if (!isValidBatchName(normalized)) {
      setValidation("Batch name phải đúng định dạng GN2 + ngày DDMMYY hợp lệ.");
      return;
    }
    if (notReady.length) {
      setValidation("Mọi hồ sơ phải ở trạng thái Sẵn sàng hạch toán.");
      return;
    }
    onCreate(normalized, cases);
  }

  return (
    <dialog ref={dialogRef} className="dialog batch-dialog" aria-labelledby="batch-dialog-title" onCancel={onClose} onClose={onClose} onClick={(event) => event.target === event.currentTarget && onClose()}>
      <form className="dialog__surface" onSubmit={submit}>
        <header className="dialog__header">
          <div><span className="section-kicker">Xuất dữ liệu ERP</span><h2 id="batch-dialog-title">Tạo batch hạch toán</h2></div>
          <button className="icon-button" type="button" aria-label="Đóng hộp thoại tạo batch" onClick={onClose}><Icon name="close" /></button>
        </header>
        <div className="dialog__body">
          <div className="form-field">
            <label htmlFor="batch-name">Batch name</label>
            <input id="batch-name" name="batch_name" type="text" inputMode="text" autoComplete="off" maxLength="9" pattern="GN2[0-9]{6}" placeholder="GN2250526" value={batchName} aria-describedby="batch-name-hint batch-name-error" aria-invalid={validation ? "true" : undefined} onChange={(event) => { setBatchName(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "")); setValidation(""); }} required />
            <span className="field-hint" id="batch-name-hint">Định dạng GN2 + DDMMYY, ví dụ GN2250526.</span>
            {validation && <span className="field-error" id="batch-name-error" role="alert">{validation}</span>}
          </div>

          <div className="batch-summary">
            <div><span>Hồ sơ đã chọn</span><strong>{numberFormatter.format(cases.length)}</strong></div>
            <div><span>Tổng giá trị</span><strong>{formatCurrency(totalAmount)}</strong></div>
          </div>

          <div className="selected-case-list" aria-label="Hồ sơ trong batch">
            {cases.map((caseItem) => (
              <div className="selected-case-item" key={caseItem.id}>
                <div><strong>{caseItem.id} · {caseItem.domain || caseItem.employee_name || "Chưa xác định"}</strong><span>{[caseItem.asset_code, caseItem.asset_name].filter(Boolean).join(" · ") || TYPE_META[caseItem.case_type]?.label}</span></div>
                <span className="selected-case-item__amount">{formatCurrency(caseItem.amount)}</span>
              </div>
            ))}
          </div>

          <div className={`dialog-note ${notReady.length ? "is-warning" : ""}`}>
            <Icon name={notReady.length ? "alert" : "info"} />
            <p>{notReady.length
              ? <><strong>{numberFormatter.format(notReady.length)} hồ sơ chưa sẵn sàng.</strong> Chuyển trạng thái trước khi tạo batch.</>
              : <>Tất cả hồ sơ đã <strong>sẵn sàng hạch toán</strong>. Bạn có thể tạo file ERP.</>}</p>
          </div>
        </div>
        <footer className="dialog__footer">
          <button className="button button--secondary" type="button" onClick={onClose}>Hủy</button>
          <button className={`button button--primary ${busy ? "is-busy" : ""}`} type="submit" disabled={busy || !!notReady.length || !cases.length} aria-busy={busy}><Icon name="batch" /> Tạo batch &amp; file ERP</button>
        </footer>
      </form>
    </dialog>
  );
}
