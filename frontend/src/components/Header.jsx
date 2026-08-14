import React, { useEffect, useRef, useState } from "react";

import logoUrl from "../assets/vng-orange-compact.png";
import { LANGUAGES, translate } from "../i18n.js";

export function Topbar({ language, onLanguageChange }) {
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef(null);

  useEffect(() => {
    function closeOnOutsideClick(event) {
      if (!dropdownRef.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("click", closeOnOutsideClick);
    return () => document.removeEventListener("click", closeOnOutsideClick);
  }, []);

  return (
    <header className="topbar">
      <div className="brand">
        <img src={logoUrl} alt="VNG" />
        <span className="divider" aria-hidden="true" />
        <h1>{translate(language, "title")}</h1>
      </div>
      <div className="lang-dropdown" ref={dropdownRef}>
        <button
          className="lang-toggle-btn"
          type="button"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={(event) => {
            event.stopPropagation();
            setOpen((current) => !current);
          }}
        >
          <span>{LANGUAGES[language]}</span><span className="dropdown-arrow">▾</span>
        </button>
        <div className={`lang-menu ${open ? "open" : ""}`} role="menu">
          {Object.entries(LANGUAGES).map(([value, label]) => (
            <button
              className={language === value ? "active-lang" : ""}
              type="button"
              role="menuitem"
              key={value}
              onClick={() => {
                onLanguageChange(value);
                setOpen(false);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}
