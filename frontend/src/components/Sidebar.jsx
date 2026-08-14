import React from "react";

import { API, STATUS_META, STATUS_ORDER } from "../constants.js";
import { batchStatusLabel, formatCurrency, formatDate, numberFormatter, statusClass } from "../utils.js";
import { Icon } from "./Icon.jsx";

function IssuesPanel({ issues, cases, onOpenCase }) {
  return (
    <article className="panel issues-panel" aria-labelledby="issues-heading">
      <div className="panel__header">
        <div><span className="section-kicker">Kiểm soát dữ liệu</span><h2 id="issues-heading">Cảnh báo</h2></div>
        <span className="count-badge">{numberFormatter.format(issues.length)}</span>
      </div>
      {!!issues.length && <div className="issues-list">{issues.map((issue) => {
        const canOpen = issue.case_id && cases.some((item) => item.id === issue.case_id);
        return (
          <div className={`issue-item issue-item--${issue.severity}`} key={issue.id}>
            <span className="issue-item__icon" aria-hidden="true"><Icon name="alert" /></span>
            <div className="issue-item__copy"><strong title={issue.title}>{issue.title}</strong><span title={issue.message}>{issue.message}</span></div>
            {canOpen && <button className="issue-item__link" type="button" onClick={() => onOpenCase(issue.case_id)}>Mở case</button>}
          </div>
        );
      })}</div>}
      {!issues.length && <div className="mini-empty"><span className="mini-empty__check" aria-hidden="true"><Icon name="check" /></span><div><strong>Dữ liệu đang sạch</strong><span>Không có cảnh báo cần xử lý.</span></div></div>}
    </article>
  );
}

function BatchesPanel({ batches }) {
  const latest = [...batches].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)).slice(0, 6);
  return (
    <article className="panel batches-panel" aria-labelledby="batches-heading">
      <div className="panel__header">
        <div><span className="section-kicker">Đầu ra ERP</span><h2 id="batches-heading">Batch gần đây</h2></div>
        <Icon name="batch" className="panel__header-icon" />
      </div>
      {!!latest.length && <div className="batch-list">{latest.map((batch) => (
        <div className="batch-item" key={batch.id}>
          <div className="batch-item__main">
            <div className="batch-item__name"><strong title={batch.batch_name}>{batch.batch_name}</strong><span className="batch-status">{batchStatusLabel(batch.status)}</span></div>
            <div className="batch-item__meta">
              <span>{numberFormatter.format(batch.case_count)} hồ sơ</span>
              {!!batch.total_amount && <span>{formatCurrency(batch.total_amount)}</span>}
              <span>{formatDate(batch.created_at)}</span>
            </div>
          </div>
          {batch.download_url
            ? <a className="batch-download" href={`${API.batches}/${encodeURIComponent(batch.api_id)}/download`} download aria-label={`Tải batch ${batch.batch_name}`} title="Tải file ERP"><Icon name="download" /></a>
            : <span className="batch-download batch-download--disabled" aria-label={`File batch ${batch.batch_name} chưa sẵn sàng`} title="File chưa sẵn sàng"><Icon name="download" /></span>}
        </div>
      ))}</div>}
      {!latest.length && <div className="mini-empty"><span className="mini-empty__check mini-empty__check--muted" aria-hidden="true"><Icon name="batch" /></span><div><strong>Chưa có batch</strong><span>Chọn hồ sơ để tạo batch đầu tiên.</span></div></div>}
    </article>
  );
}

function StatusPanel({ summary }) {
  const counts = summary?.by_status || {};
  const total = STATUS_ORDER.reduce((sum, status) => sum + (counts[status] || 0), 0);
  const label = total
    ? STATUS_ORDER.map((status) => `${STATUS_META[status].label}: ${counts[status] || 0}`).join(", ")
    : "Chưa có hồ sơ";
  return (
    <article className="panel status-panel" aria-labelledby="status-overview-heading">
      <div className="panel__header"><div><span className="section-kicker">Tiến độ</span><h2 id="status-overview-heading">Phân bổ trạng thái</h2></div></div>
      <div className="status-meter" role="img" aria-label={label}>
        {STATUS_ORDER.map((status) => <progress className={`status-meter__progress status-meter__progress--${statusClass(status)}`} max={Math.max(total, 1)} value={counts[status] || 0} aria-label={`${STATUS_META[status].label}: ${counts[status] || 0} / ${total}`} key={status} />)}
      </div>
      <ul className="status-legend">{STATUS_ORDER.map((status) => (
        <li key={status}><span className={`status-legend__dot status-meter__segment--${statusClass(status)}`} aria-hidden="true"/><span className="status-legend__label">{STATUS_META[status].shortLabel}</span><strong>{numberFormatter.format(counts[status] || 0)}</strong></li>
      ))}</ul>
    </article>
  );
}

export function Sidebar({ issues, batches, cases, summary, onOpenCase }) {
  return <aside className="side-column" aria-label="Cảnh báo và batch gần đây"><IssuesPanel issues={issues} cases={cases} onOpenCase={onOpenCase} /><BatchesPanel batches={batches} /><StatusPanel summary={summary} /></aside>;
}
