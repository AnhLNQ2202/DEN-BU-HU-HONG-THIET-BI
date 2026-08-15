import React, { useEffect, useMemo, useRef, useState } from "react";

import { translate } from "../i18n.js";
import { Icon } from "./Icon.jsx";

export function TranCasePickerDialog({
  groups,
  language,
  maxAssets = 100,
  onApply,
  onClose,
  open,
  selectedKeys,
}) {
  const dialogRef = useRef(null);
  const searchRef = useRef(null);
  const selectAllRef = useRef(null);
  const [query, setQuery] = useState("");
  const [draftKeys, setDraftKeys] = useState([]);
  const [validation, setValidation] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) {
      setQuery("");
      setDraftKeys(Array.isArray(selectedKeys) ? selectedKeys : []);
      setValidation("");
      dialog.showModal();
      window.requestAnimationFrame(() => searchRef.current?.focus());
    }
    if (!open && dialog?.open) dialog.close();
  }, [open, selectedKeys]);

  const visibleGroups = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase(language === "vi" ? "vi" : "en");
    if (!needle) return groups;
    return groups.filter((group) => group.searchText.toLocaleLowerCase(language === "vi" ? "vi" : "en").includes(needle));
  }, [groups, language, query]);

  const draftSet = useMemo(() => new Set(draftKeys), [draftKeys]);
  const selectedGroups = useMemo(
    () => groups.filter((group) => draftSet.has(group.key)),
    [draftSet, groups],
  );
  const selectedCaseCount = selectedGroups.reduce(
    (total, group) => total + Number(group.caseCount || 0),
    0,
  );
  const selectedAssetCount = selectedGroups.reduce((total, group) => total + group.assetCount, 0);
  const visibleSelectedCount = visibleGroups.filter((group) => draftSet.has(group.key)).length;
  const allVisibleSelected = visibleGroups.length > 0 && visibleSelectedCount === visibleGroups.length;
  const someVisibleSelected = visibleSelectedCount > 0 && !allVisibleSelected;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someVisibleSelected;
  }, [someVisibleSelected]);

  function toggleGroup(group) {
    const next = draftSet.has(group.key)
      ? draftKeys.filter((key) => key !== group.key)
      : [...draftKeys, group.key];
    const nextSet = new Set(next);
    const nextAssetCount = groups
      .filter((item) => nextSet.has(item.key))
      .reduce((total, item) => total + item.assetCount, 0);
    if (nextAssetCount > maxAssets) {
      setValidation(translate(language, "tranBatchLimitExceeded"));
      return;
    }
    setValidation("");
    setDraftKeys(next);
  }

  function toggleAllVisible() {
    if (allVisibleSelected) {
      const visibleKeys = new Set(visibleGroups.map((group) => group.key));
      setDraftKeys((current) => current.filter((key) => !visibleKeys.has(key)));
      setValidation("");
      return;
    }
    const next = [...new Set([...draftKeys, ...visibleGroups.map((group) => group.key)])];
    const nextSet = new Set(next);
    const nextAssetCount = groups
      .filter((group) => nextSet.has(group.key))
      .reduce((total, group) => total + group.assetCount, 0);
    if (nextAssetCount > maxAssets) {
      setValidation(translate(language, "tranBatchSelectAllExceeded").replace("{count}", String(nextAssetCount)));
      return;
    }
    setValidation("");
    setDraftKeys(next);
  }

  function applySelection() {
    onApply(draftKeys);
  }

  return (
    <dialog
      ref={dialogRef}
      className="dialog tran-case-picker"
      aria-labelledby="tran-case-picker-title"
      aria-describedby="tran-case-picker-description"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="dialog__surface tran-case-picker__surface">
        <header className="dialog__header">
          <div>
            <span className="section-kicker">TranNNB · {selectedAssetCount}/{maxAssets} {translate(language, "tranAssets")}</span>
            <h2 id="tran-case-picker-title">{translate(language, "tranBatchPickerTitle")}</h2>
            <p id="tran-case-picker-description">{translate(language, "tranBatchPickerHint")}</p>
          </div>
          <button className="icon-button" type="button" aria-label={translate(language, "tranBatchClose")} onClick={onClose}>
            <Icon name="close" />
          </button>
        </header>

        <div className="dialog__body tran-case-picker__body">
          <label className="tran-case-picker__search">
            <span>{translate(language, "tranBatchSearch")}</span>
            <input
              ref={searchRef}
              type="search"
              value={query}
              maxLength="255"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={translate(language, "tranBatchSearchPlaceholder")}
            />
          </label>

          <div className="tran-case-picker__tools">
            <label className="tran-case-picker__select-all">
              <input
                ref={selectAllRef}
                type="checkbox"
                checked={allVisibleSelected}
                disabled={!visibleGroups.length}
                onChange={toggleAllVisible}
              />
              <span>{translate(language, "tranBatchSelectAllVisible")}</span>
            </label>
            <span aria-live="polite">
              {translate(language, "tranBatchSelectedSummary")
                .replace("{cases}", String(selectedCaseCount))
                .replace("{assets}", String(selectedAssetCount))}
            </span>
          </div>

          {validation && <div className="inline-error" role="alert">{validation}</div>}

          <div className="tran-case-picker__list" aria-label={translate(language, "tranBatchPickerTitle")}>
            {visibleGroups.map((group) => (
              <label className="tran-case-option" key={group.key}>
                <input
                  type="checkbox"
                  checked={draftSet.has(group.key)}
                  onChange={() => toggleGroup(group)}
                />
                <span className="tran-case-option__copy">
                  <strong title={group.label}>{group.label}</strong>
                  <span>{group.meta}</span>
                </span>
                <span className="tran-case-option__count">{group.assetCount} {translate(language, "tranAssets")}</span>
              </label>
            ))}
            {!visibleGroups.length && <div className="compensation-empty">{translate(language, "tranBatchNoMatches")}</div>}
          </div>
        </div>

        <footer className="dialog__footer">
          <button className="button button--secondary" type="button" onClick={onClose}>{translate(language, "tranBatchCancel")}</button>
          <button className="button button--primary" type="button" onClick={applySelection}>
            {translate(language, "tranBatchApply").replace("{cases}", String(selectedCaseCount))}
          </button>
        </footer>
      </div>
    </dialog>
  );
}
