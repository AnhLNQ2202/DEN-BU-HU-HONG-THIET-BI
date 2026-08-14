import React, { useEffect } from "react";

import { Icon } from "./Icon.jsx";

export function LoadingState() {
  return (
    <div className="loading-layout" aria-label="Đang tải dữ liệu" aria-busy="true">
      {[0, 1, 2, 3].map((item) => <div className="skeleton skeleton--kpi" key={item} />)}
      <div className="skeleton skeleton--main" />
      <div className="skeleton skeleton--side" />
    </div>
  );
}

export function FatalError({ message, onRetry }) {
  return (
    <section className="fatal-error" role="alert">
      <div className="fatal-error__icon" aria-hidden="true"><Icon name="alert" /></div>
      <div><h2>Không tải được dữ liệu</h2><p>{message}</p></div>
      <button className="button button--secondary" type="button" onClick={onRetry}>Thử lại</button>
    </section>
  );
}

function Toast({ toast, onDismiss }) {
  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(toast.id), toast.type === "error" ? 8000 : 5000);
    return () => window.clearTimeout(timer);
  }, [onDismiss, toast.id, toast.type]);

  const iconName = toast.type === "success" ? "check" : ["error", "warning"].includes(toast.type) ? "alert" : "info";
  return (
    <div className={`toast toast--${toast.type}`} role={toast.type === "error" ? "alert" : "status"}>
      <span className="toast__icon" aria-hidden="true"><Icon name={iconName} /></span>
      <div className="toast__copy"><strong>{toast.title}</strong><span>{toast.message}</span></div>
      <button className="icon-button toast__close" type="button" aria-label="Đóng thông báo" onClick={() => onDismiss(toast.id)}><Icon name="close" /></button>
    </div>
  );
}

export function ToastRegion({ toasts, onDismiss }) {
  return <div className="toast-region" aria-live="polite" aria-atomic="false">{toasts.map((toast) => <Toast key={toast.id} toast={toast} onDismiss={onDismiss} />)}</div>;
}
