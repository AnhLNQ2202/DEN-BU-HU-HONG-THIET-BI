import React from "react";

import { Icon } from "./Icon.jsx";
import { formatCurrency, numberFormatter } from "../utils.js";

const cards = [
  { id: "total", label: "Tổng hồ sơ", icon: "folder", tone: "orange" },
  { id: "ready", label: "Sẵn sàng hạch toán", icon: "check", tone: "navy" },
  { id: "amount", label: "Tổng giá trị đền bù", icon: "wallet", tone: "teal" },
  { id: "warnings", label: "Cần kiểm tra", icon: "alert", tone: "red" },
];

export function KpiGrid({ summary }) {
  const total = summary?.total || 0;
  const damaged = summary?.by_type?.DAMAGED || 0;
  const lost = summary?.by_type?.LOST || 0;
  const ready = summary?.by_status?.READY_FOR_ACCOUNTING || 0;
  const warnings = summary?.warnings || 0;
  const amount = summary?.total_amount || 0;
  const values = {
    total: numberFormatter.format(total),
    ready: numberFormatter.format(ready),
    amount: formatCurrency(amount),
    warnings: numberFormatter.format(warnings),
  };
  const notes = {
    total: total ? `${numberFormatter.format(damaged)} hư hỏng · ${numberFormatter.format(lost)} thất lạc` : "Chưa có dữ liệu",
    ready: `${total ? Math.round((ready / total) * 100) : 0}% tổng hồ sơ`,
    amount: total ? `Bình quân ${formatCurrency(amount / total)} / hồ sơ` : "Giá trị của mọi hồ sơ",
    warnings: warnings ? "Ưu tiên xử lý trước khi tạo batch" : "Không có cảnh báo",
  };

  return (
    <section className="kpi-grid" aria-label="Chỉ số tổng quan">
      {cards.map((card) => (
        <article className={`kpi-card kpi-card--${card.tone}`} key={card.id}>
          <div className="kpi-card__topline">
            <span className="kpi-card__label">{card.label}</span>
            <span className="kpi-card__icon" aria-hidden="true"><Icon name={card.icon} /></span>
          </div>
          <strong className={`kpi-card__value ${card.id === "amount" ? "kpi-card__value--currency" : ""}`}>{values[card.id]}</strong>
          <p className="kpi-card__note">{notes[card.id]}</p>
        </article>
      ))}
    </section>
  );
}
