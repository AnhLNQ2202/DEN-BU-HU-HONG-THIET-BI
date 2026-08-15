import React, { useEffect, useRef, useState } from "react";

import { translate } from "../i18n.js";
import { Icon } from "./Icon.jsx";
import { M365MailboxPanel } from "./M365MailboxPanel.jsx";
import { OutlookCompanionPanel } from "./OutlookCompanionPanel.jsx";

export function OutlookConnectionsDialog({
  capabilities,
  language,
  onClose,
  onMailboxSynced,
  onTranStatusChange,
  open,
  preferredRole = "ngan",
  refreshVersion = 0,
  tranRefreshVersion = 0,
}) {
  const dialogRef = useRef(null);
  const [activeRole, setActiveRole] = useState(preferredRole);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) {
      setActiveRole(preferredRole === "tran" ? "tran" : "ngan");
      dialog.showModal();
      window.requestAnimationFrame(() => dialog.querySelector(".dialog__header button")?.focus());
    }
    if (!open && dialog?.open) {
      dialog.close();
      window.requestAnimationFrame(() => {
        document.getElementById("outlook-connections-trigger")?.focus();
      });
    }
  }, [open, preferredRole]);

  useEffect(() => {
    if (!open) return undefined;
    function closeOnEscape(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose, open]);

  function closeDialog() {
    onClose();
  }

  return (
    <dialog
      id="outlook-connections-dialog"
      ref={dialogRef}
      className="dialog outlook-connections-dialog"
      aria-labelledby="outlook-connections-title"
      aria-describedby="outlook-connections-description"
      onCancel={(event) => {
        event.preventDefault();
        closeDialog();
      }}
      onClose={() => {
        if (open) onClose();
      }}
      onClick={(event) => event.target === event.currentTarget && closeDialog()}
    >
      <div className="dialog__surface outlook-connections-surface">
        <header className="dialog__header">
          <div>
            <span className="section-kicker">Microsoft 365 · Add-in · Local Bridge</span>
            <h2 id="outlook-connections-title">{translate(language, "outlookConnectionsTitle")}</h2>
            <p id="outlook-connections-description">{translate(language, "outlookConnectionsDescription")}</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label={translate(language, "outlookConnectionsClose")}
            onClick={closeDialog}
          >
            <Icon name="close" />
          </button>
        </header>

        <div className="dialog__body outlook-connections-body">
          <div className="outlook-role-switcher" aria-label={translate(language, "outlookConnectionsRoleLabel")}>
            <button
              className={activeRole === "ngan" ? "is-active" : ""}
              type="button"
              aria-pressed={activeRole === "ngan"}
              onClick={() => setActiveRole("ngan")}
            >
              NganTLT
            </button>
            <button
              className={activeRole === "tran" ? "is-active" : ""}
              type="button"
              aria-pressed={activeRole === "tran"}
              onClick={() => setActiveRole("tran")}
            >
              TranNNB
            </button>
          </div>

          <section className="outlook-role-pane" hidden={activeRole !== "ngan"} aria-label="NganTLT">
            <p className="outlook-role-intro">{translate(language, "outlookConnectionsNganIntro")}</p>
            <M365MailboxPanel
              capabilities={capabilities}
              language={language}
              onSynced={onMailboxSynced}
              refreshVersion={refreshVersion}
              role="ngan"
            />
            <OutlookCompanionPanel
              capabilities={capabilities}
              language={language}
              refreshVersion={refreshVersion}
              role="ngan"
            />
          </section>

          <section className="outlook-role-pane" hidden={activeRole !== "tran"} aria-label="TranNNB">
            <p className="outlook-role-intro">{translate(language, "outlookConnectionsTranIntro")}</p>
            <M365MailboxPanel
              capabilities={capabilities}
              language={language}
              onStatusChange={onTranStatusChange}
              onSynced={onMailboxSynced}
              refreshVersion={`${refreshVersion}:${tranRefreshVersion}`}
              role="tran"
            />
            <OutlookCompanionPanel
              capabilities={capabilities}
              language={language}
              refreshVersion={refreshVersion}
              role="tran"
            />
          </section>
        </div>
      </div>
    </dialog>
  );
}
