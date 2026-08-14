import React from "react";

export function Icon({ name, className = "icon", ...props }) {
  return (
    <svg className={className} aria-hidden="true" focusable="false" {...props}>
      <use href={`#icon-${name}`} />
    </svg>
  );
}

export function IconSprite() {
  return (
    <svg className="svg-sprite" aria-hidden="true">
      <symbol id="icon-alert" viewBox="0 0 24 24"><path d="M12 3.75 2.7 20h18.6L12 3.75Z"/><path d="M12 9v4.75M12 17.2v.05"/></symbol>
      <symbol id="icon-batch" viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M7 9h10M7 13h7M7 17h4"/></symbol>
      <symbol id="icon-chevron" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></symbol>
      <symbol id="icon-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/></symbol>
      <symbol id="icon-close" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></symbol>
      <symbol id="icon-download" viewBox="0 0 24 24"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 19h14"/></symbol>
      <symbol id="icon-empty" viewBox="0 0 24 24"><path d="M4 7.5h5l1.6 2H20v9.25A1.25 1.25 0 0 1 18.75 20H5.25A1.25 1.25 0 0 1 4 18.75V7.5Z"/><path d="M4 7.5V5.25C4 4.56 4.56 4 5.25 4h5l1.5 2H18"/></symbol>
      <symbol id="icon-folder" viewBox="0 0 24 24"><path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h5l2 2h7A1.5 1.5 0 0 1 20.5 8.5v9A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5v-11Z"/></symbol>
      <symbol id="icon-inbox" viewBox="0 0 24 24"><path d="M4 5.5h16v13H4z"/><path d="m4 7 8 6 8-6M17 3v5M14.5 5.5 17 8l2.5-2.5"/></symbol>
      <symbol id="icon-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 10.5V17M12 7.25v.1"/></symbol>
      <symbol id="icon-refresh" viewBox="0 0 24 24"><path d="M19 8a8 8 0 1 0 .6 7.1M19 4v4h-4"/></symbol>
      <symbol id="icon-reset" viewBox="0 0 24 24"><path d="M5 8a8 8 0 1 1-.6 7.1M5 4v4h4"/></symbol>
      <symbol id="icon-search" viewBox="0 0 24 24"><circle cx="10.75" cy="10.75" r="6.25"/><path d="m15.5 15.5 4 4"/></symbol>
      <symbol id="icon-wallet" viewBox="0 0 24 24"><path d="M4 6.5h14.5A1.5 1.5 0 0 1 20 8v10H5.5A1.5 1.5 0 0 1 4 16.5v-10Z"/><path d="M4 7V5.5A1.5 1.5 0 0 1 5.5 4H17M15 11h5v4h-5a2 2 0 1 1 0-4Z"/></symbol>
      <symbol id="icon-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></symbol>
    </svg>
  );
}
