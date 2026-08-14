import React from "react";

import { Icon } from "./Icon.jsx";
import { formatDate } from "../utils.js";

export function Topbar({ connection, updatedAt }) {
  const connectionLabel = connection === "online"
    ? "Đã kết nối"
    : connection === "offline"
      ? "Mất kết nối"
      : "Đang kết nối";

  return (
    <>
      <a className="skip-link" href="#main-content">Bỏ qua đến nội dung chính</a>
      <header className="topbar">
        <div className="topbar__inner">
          <a className="brand" href="/" aria-label="Asset Compensation Hub - Trang chủ">
            <span className="brand__mark" aria-hidden="true">
              <svg viewBox="0 0 32 32" focusable="false">
                <path d="M7 23.5 16 5l9 18.5h-5.1L16 15l-3.9 8.5H7Z"/>
                <circle cx="16" cy="25.5" r="2.5"/>
              </svg>
            </span>
            <span><strong>Asset Compensation Hub</strong><small>IT Finance Operations</small></span>
          </a>
          <div className="topbar__meta">
            <span className={`connection-pill ${connection === "online" ? "is-online" : connection === "offline" ? "is-offline" : ""}`} role="status">
              <span className="connection-pill__dot" aria-hidden="true" />
              <span>{connectionLabel}</span>
            </span>
            <span className="last-updated">{updatedAt ? `Đồng bộ ${formatDate(updatedAt, true)}` : "Chưa đồng bộ"}</span>
          </div>
        </div>
      </header>

    </>
  );
}

export function Hero({ ingesting, resetting, onIngest, onReset }) {
  return (
    <section className="hero" aria-labelledby="page-title">
      <div className="hero__copy">
        <span className="eyebrow">Bàn điều phối nghiệp vụ</span>
        <h1 id="page-title">Đền bù tài sản, rõ từng bước.</h1>
        <p>Theo dõi hồ sơ hư hỏng và thất lạc, xử lý cảnh báo, rồi đóng batch hạch toán trong một luồng duy nhất.</p>
      </div>
      <div className="hero__actions" aria-label="Thao tác nhanh">
        <button className={`button button--secondary ${resetting ? "is-busy" : ""}`} type="button" disabled={resetting} aria-busy={resetting} onClick={onReset}>
          <Icon name="reset" /> Đặt lại demo
        </button>
        <button className={`button button--primary ${ingesting ? "is-busy" : ""}`} type="button" disabled={ingesting} aria-busy={ingesting} onClick={onIngest}>
          <Icon name="inbox" /> Nạp email mới
        </button>
      </div>
    </section>
  );
}
