import React, { useEffect, useRef } from "react";

import { STATUS_META, TYPE_META } from "../constants.js";
import { formatCurrency, formatDate, numberFormatter, statusClass } from "../utils.js";
import { Icon } from "./Icon.jsx";

function SortButton({ field, sort, alignRight = false, children, onSort }) {
  const active = sort.field === field;
  return (
    <button
      className={`sort-button ${alignRight ? "sort-button--right" : ""} ${active ? "is-active" : ""}`}
      type="button"
      onClick={() => onSort(field)}
    >
      {children}<span aria-hidden="true">{active ? (sort.direction === "asc" ? "↑" : "↓") : ""}</span>
    </button>
  );
}

function CaseRow({ caseItem, selected, onSelect, onOpen }) {
  const type = TYPE_META[caseItem.case_type] || TYPE_META.UNKNOWN;
  const status = STATUS_META[caseItem.status] || STATUS_META.NEW;
  const person = caseItem.employee_name || caseItem.domain || "Chưa xác định";
  const secondaryPerson = caseItem.employee_name && caseItem.domain ? caseItem.domain : "";
  const asset = [caseItem.asset_code, caseItem.asset_name].filter(Boolean).join(" · ") || "Chưa có thông tin tài sản";

  return (
    <tr className={selected ? "is-selected" : ""}>
      <td className="select-cell">
        <input className="checkbox case-select" type="checkbox" checked={selected} onChange={(event) => onSelect(caseItem.id, event.target.checked)} aria-label={`Chọn hồ sơ ${caseItem.id}`} />
      </td>
      <td data-label="Hồ sơ">
        <button className="case-id-button" type="button" onClick={() => onOpen(caseItem.id)}>{caseItem.id}</button>
        <span className="case-date">{formatDate(caseItem.received_at)}</span>
      </td>
      <td data-label="Loại"><span className={`type-badge type-badge--${type.className}`}>{type.label}</span></td>
      <td data-label="Nhân viên">
        <div className="person-asset">
          <strong title={person}>{person}</strong>
          {secondaryPerson && <span>{secondaryPerson}</span>}
          <span title={asset}>{asset}</span>
        </div>
      </td>
      <td className="align-right" data-label="Giá trị"><span className="amount-cell">{formatCurrency(caseItem.amount)}</span></td>
      <td data-label="Trạng thái"><span className={`status-badge status-badge--${statusClass(caseItem.status)}`}>{status.label}</span></td>
      <td data-label="Cảnh báo">
        {caseItem.warnings.length
          ? <span className="warning-chip" title={caseItem.warnings.join(" · ")}><Icon name="alert" />{caseItem.warnings.length}</span>
          : <span className="no-warning">Không</span>}
      </td>
      <td className="action-cell">
        <button className="icon-button row-action" type="button" onClick={() => onOpen(caseItem.id)} aria-label={`Xem chi tiết hồ sơ ${caseItem.id}`}><Icon name="chevron" /></button>
      </td>
    </tr>
  );
}

export function CaseTable({
  cases,
  totalCases,
  filters,
  sort,
  selected,
  refreshing,
  onFiltersChange,
  onSort,
  onSelect,
  onSelectVisible,
  onClearSelection,
  onOpen,
  onOpenBatch,
  onRefresh,
  onIngest,
}) {
  const selectAllRef = useRef(null);
  const selectedVisible = cases.filter((item) => selected.has(item.id)).length;
  const allVisibleSelected = cases.length > 0 && selectedVisible === cases.length;
  const hasFilters = Boolean(filters.query) || filters.type !== "ALL" || filters.status !== "ALL";

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedVisible > 0 && !allVisibleSelected;
  }, [allVisibleSelected, selectedVisible]);

  return (
    <article className="panel cases-panel" aria-labelledby="cases-heading">
      <div className="panel__header panel__header--stack-mobile">
        <div><span className="section-kicker">Hàng đợi xử lý</span><h2 id="cases-heading">Hồ sơ đền bù</h2></div>
        <button className="button button--primary button--compact" type="button" disabled={!selected.size} onClick={onOpenBatch}>
          <Icon name="batch" /> Tạo batch
          {!!selected.size && <span className="button__count">{numberFormatter.format(selected.size)}</span>}
        </button>
      </div>

      <div className="filter-bar" aria-label="Bộ lọc hồ sơ">
        <div className="search-field">
          <Icon name="search" />
          <label className="sr-only" htmlFor="case-search">Tìm hồ sơ</label>
          <input id="case-search" type="search" autoComplete="off" value={filters.query} onChange={(event) => onFiltersChange({ query: event.target.value })} placeholder="Tìm domain, nhân viên, mã tài sản…" />
          {!!filters.query && <button className="icon-button search-field__clear" type="button" aria-label="Xóa nội dung tìm kiếm" onClick={() => onFiltersChange({ query: "" })}><Icon name="close" /></button>}
        </div>
        <div className="select-field">
          <label htmlFor="type-filter">Loại hồ sơ</label>
          <select id="type-filter" value={filters.type} onChange={(event) => onFiltersChange({ type: event.target.value })}>
            <option value="ALL">Tất cả loại</option><option value="DAMAGED">Hư hỏng</option><option value="LOST">Thất lạc</option>
          </select>
        </div>
        <div className="select-field">
          <label htmlFor="status-filter">Trạng thái</label>
          <select id="status-filter" value={filters.status} onChange={(event) => onFiltersChange({ status: event.target.value })}>
            <option value="ALL">Tất cả trạng thái</option>
            {Object.entries(STATUS_META).map(([value, meta]) => <option value={value} key={value}>{meta.label}</option>)}
          </select>
        </div>
        <button className={`icon-button filter-bar__refresh ${refreshing ? "is-busy" : ""}`} type="button" disabled={refreshing} aria-busy={refreshing} onClick={onRefresh} aria-label="Làm mới dữ liệu" title="Làm mới dữ liệu"><Icon name="refresh" /></button>
      </div>

      {!!selected.size && <div className="selection-bar" role="status">
        <span><strong>{numberFormatter.format(selected.size)}</strong> hồ sơ đã chọn</span>
        <button className="text-button" type="button" onClick={onClearSelection}>Bỏ chọn tất cả</button>
      </div>}

      <div className="table-meta"><p aria-live="polite">{numberFormatter.format(cases.length)} / {numberFormatter.format(totalCases)} hồ sơ</p><p className="table-meta__hint">Chọn hồ sơ sẵn sàng để tạo batch</p></div>

      {!!cases.length && <div className="table-wrap">
        <table className="cases-table">
          <caption className="sr-only">Danh sách hồ sơ đền bù tài sản</caption>
          <thead><tr>
            <th className="select-column" scope="col"><input ref={selectAllRef} className="checkbox" type="checkbox" checked={allVisibleSelected} onChange={(event) => onSelectVisible(cases, event.target.checked)} aria-label="Chọn tất cả hồ sơ đang hiển thị" /></th>
            <th scope="col" aria-sort={sort.field === "id" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}><SortButton field="id" sort={sort} onSort={onSort}>Mã hồ sơ</SortButton></th>
            <th scope="col" aria-sort={sort.field === "case_type" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}><SortButton field="case_type" sort={sort} onSort={onSort}>Loại</SortButton></th>
            <th scope="col">Nhân viên &amp; tài sản</th>
            <th className="align-right" scope="col" aria-sort={sort.field === "amount" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}><SortButton field="amount" sort={sort} alignRight onSort={onSort}>Giá trị</SortButton></th>
            <th scope="col" aria-sort={sort.field === "status" ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}><SortButton field="status" sort={sort} onSort={onSort}>Trạng thái</SortButton></th>
            <th scope="col">Cảnh báo</th><th className="action-column" scope="col"><span className="sr-only">Thao tác</span></th>
          </tr></thead>
          <tbody>{cases.map((caseItem) => <CaseRow key={caseItem.id} caseItem={caseItem} selected={selected.has(caseItem.id)} onSelect={onSelect} onOpen={onOpen} />)}</tbody>
        </table>
      </div>}

      {!cases.length && <div className="empty-state">
        <div className="empty-state__icon" aria-hidden="true"><Icon name="empty" /></div>
        <h3>{totalCases && hasFilters ? "Không tìm thấy hồ sơ phù hợp" : "Chưa có hồ sơ"}</h3>
        <p>{totalCases && hasFilters ? "Thử thay đổi từ khóa hoặc bộ lọc để xem thêm kết quả." : "Nạp email mới để bắt đầu tạo hàng đợi xử lý."}</p>
        {!totalCases && <button className="button button--primary button--compact" type="button" onClick={onIngest}>Nạp email</button>}
      </div>}
    </article>
  );
}
