import React, { useCallback, useEffect, useRef, useState } from "react";

import { dashboardApi } from "../api.js";
import { translate } from "../i18n.js";
import { useToast } from "./Feedback.jsx";

const AUTO_SYNC_INTERVAL_MS = 5 * 60 * 1000;

function autoSyncStorageKey(role) {
  return `asset_hub_m365_auto_sync_${role}`;
}

function initialAutoSync(role) {
  try {
    return window.localStorage.getItem(autoSyncStorageKey(role)) === "true";
  } catch {
    return false;
  }
}

function persistAutoSync(role, enabled) {
  try {
    window.localStorage.setItem(autoSyncStorageKey(role), enabled ? "true" : "false");
  } catch {
    // The opt-in is allowed to be session-only in restricted browser contexts.
  }
}

function folderLabel(folder) {
  return folder.path || folder.display_name || "—";
}

export function M365MailboxPanel({
  capabilities,
  language,
  onStatusChange,
  onSynced,
  refreshVersion = 0,
  role,
}) {
  const roleCapability = role === "tran" ? "m365_tran" : "m365_ngan";
  const capabilityAvailable = capabilities?.m365_configured === true
    && capabilities?.[roleCapability] === true;
  const [status, setStatus] = useState(null);
  const [folders, setFolders] = useState([]);
  const [folderId, setFolderId] = useState("");
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState("");
  const [autoSync, setAutoSync] = useState(() => initialAutoSync(role));
  const pushToast = useToast();
  const statusRef = useRef(null);
  const statusControllerRef = useRef(null);
  const statusBusyRef = useRef(false);
  const actionControllerRef = useRef(null);
  const syncBusyRef = useRef(false);
  const onStatusChangeRef = useRef(onStatusChange);
  const onSyncedRef = useRef(onSynced);

  useEffect(() => {
    onStatusChangeRef.current = onStatusChange;
  }, [onStatusChange]);

  useEffect(() => {
    onSyncedRef.current = onSynced;
  }, [onSynced]);

  const publishStatus = useCallback((nextStatus) => {
    statusRef.current = nextStatus;
    setStatus(nextStatus);
    setFolderId(nextStatus?.selected_folder?.id || "");
    onStatusChangeRef.current?.(nextStatus);
  }, []);

  const loadStatus = useCallback(async ({ quiet = false } = {}) => {
    statusControllerRef.current?.abort();
    if (!capabilityAvailable) {
      const unavailable = {
        configured: false,
        connected: false,
        selected_folder: null,
        cursor_ready: false,
        storage: "memory",
        background_sync: false,
      };
      setFolders([]);
      setFolderId("");
      setLoading(false);
      setAutoSync(false);
      persistAutoSync(role, false);
      statusBusyRef.current = false;
      statusControllerRef.current = null;
      publishStatus(unavailable);
      return unavailable;
    }

    const controller = new AbortController();
    statusControllerRef.current = controller;
    statusBusyRef.current = true;
    if (!quiet) setLoading(true);
    try {
      const nextStatus = await dashboardApi.m365Status(role, controller.signal);
      if (controller.signal.aborted) return null;
      publishStatus(nextStatus);
      if (!nextStatus.connected) {
        setAutoSync(false);
        persistAutoSync(role, false);
        setFolders([]);
        if (!quiet) {
          pushToast(
            translate(language, "toastSuccessTitle"),
            translate(language, "m365StatusRefreshed"),
            "success",
          );
        }
        return nextStatus;
      }
      const folderResponse = await dashboardApi.m365Folders(role, controller.signal);
      if (!controller.signal.aborted) setFolders(folderResponse.folders);
      if (!quiet) {
        pushToast(
          translate(language, "toastSuccessTitle"),
          translate(language, "m365StatusRefreshed"),
          "success",
        );
      }
      return nextStatus;
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        if (!quiet) {
          pushToast(
            translate(language, "toastErrorTitle"),
            requestError.message,
            "error",
          );
        }
        if (requestError.status === 401 || requestError.status === 503) {
          setAutoSync(false);
          persistAutoSync(role, false);
          publishStatus({
            ...(statusRef.current || {}),
            configured: requestError.status === 503 ? false : statusRef.current?.configured,
            connected: false,
            account: null,
            selected_folder: null,
            cursor_ready: false,
          });
          setFolders([]);
        }
      }
      return null;
    } finally {
      if (statusControllerRef.current === controller) {
        statusBusyRef.current = false;
        statusControllerRef.current = null;
        if (!controller.signal.aborted) setLoading(false);
      }
    }
  }, [capabilityAvailable, language, publishStatus, pushToast, role]);

  useEffect(() => {
    actionControllerRef.current?.abort();
    actionControllerRef.current = null;
    syncBusyRef.current = false;
    setAction("");
    setAutoSync(initialAutoSync(role));
    loadStatus({ quiet: true });
    return () => statusControllerRef.current?.abort();
  }, [loadStatus, refreshVersion, role]);

  useEffect(() => () => {
    statusControllerRef.current?.abort();
    actionControllerRef.current?.abort();
  }, []);

  const syncMailbox = useCallback(async ({ quiet = false } = {}) => {
    if (
      syncBusyRef.current
      || statusBusyRef.current
      || actionControllerRef.current
      || !statusRef.current?.connected
      || !statusRef.current?.selected_folder?.id
    ) return null;

    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    syncBusyRef.current = true;
    setAction("sync");
    try {
      const result = await dashboardApi.syncM365(role, controller.signal);
      if (controller.signal.aborted) return null;
      publishStatus({
        ...statusRef.current,
        selected_folder: result.folder || statusRef.current.selected_folder,
        cursor_ready: result.cursor_ready,
      });
      await onSyncedRef.current?.(result);
      if (!quiet) {
        const warningCount = result.warnings.length
          + result.skipped_files.length
          + result.unknown_files.length;
        const summary = `${translate(language, "m365Fetched")}: ${result.fetched_count} · ${translate(language, "m365Ingested")}: ${result.ingested} · ${translate(language, "m365Cases")}: ${result.case_ids.length}`;
        const suffix = [
          warningCount ? `${translate(language, "uploadWarnings")}: ${warningCount}` : "",
          result.has_more ? translate(language, "m365HasMore") : "",
        ].filter(Boolean).join(" · ");
        pushToast(
          translate(language, "m365SyncComplete"),
          suffix ? `${summary} · ${suffix}` : summary,
          warningCount || result.has_more ? "warning" : "success",
        );
      }
      return result;
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        if (!quiet) {
          pushToast(
            translate(language, "toastErrorTitle"),
            requestError.message,
            "error",
          );
        }
        if (requestError.status === 401 || requestError.status === 503) {
          setAutoSync(false);
          persistAutoSync(role, false);
          await loadStatus({ quiet: true });
        }
      }
      return null;
    } finally {
      syncBusyRef.current = false;
      if (actionControllerRef.current === controller) {
        actionControllerRef.current = null;
        if (!controller.signal.aborted) setAction("");
      }
    }
  }, [language, loadStatus, publishStatus, pushToast, role]);

  useEffect(() => {
    if (!autoSync || !status?.connected || !status?.selected_folder?.id) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !syncBusyRef.current) {
        syncMailbox({ quiet: true });
      }
    }, AUTO_SYNC_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [autoSync, status?.connected, status?.selected_folder?.id, syncMailbox]);

  async function connect() {
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setAction("connect");
    try {
      const result = await dashboardApi.connectM365(role, controller.signal);
      if (!result.authorization_url) throw new Error(translate(language, "m365AuthorizationInvalid"));
      window.location.assign(result.authorization_url);
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        pushToast(
          translate(language, "toastErrorTitle"),
          requestError.message,
          "error",
        );
      }
      if (requestError.status === 401 || requestError.status === 503) await loadStatus({ quiet: true });
    } finally {
      if (actionControllerRef.current === controller) {
        actionControllerRef.current = null;
        if (!controller.signal.aborted) setAction("");
      }
    }
  }

  async function disconnect() {
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setAutoSync(false);
    persistAutoSync(role, false);
    setAction("disconnect");
    try {
      await dashboardApi.disconnectM365(role, controller.signal);
      setFolders([]);
      await loadStatus({ quiet: true });
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "m365DisconnectedNotice"),
        "success",
      );
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        pushToast(
          translate(language, "toastErrorTitle"),
          requestError.message,
          "error",
        );
      }
      if (requestError.status === 401 || requestError.status === 503) await loadStatus({ quiet: true });
    } finally {
      if (actionControllerRef.current === controller) {
        actionControllerRef.current = null;
        if (!controller.signal.aborted) setAction("");
      }
    }
  }

  async function saveFolder() {
    if (!folderId || folderId === statusRef.current?.selected_folder?.id) return;
    const controller = new AbortController();
    actionControllerRef.current?.abort();
    actionControllerRef.current = controller;
    setAction("folder");
    try {
      const result = await dashboardApi.selectM365Folder(role, folderId, controller.signal);
      if (controller.signal.aborted) return;
      publishStatus({
        ...statusRef.current,
        selected_folder: result.selected_folder,
        cursor_ready: false,
      });
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "m365FolderSavedNotice"),
        "success",
      );
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        pushToast(
          translate(language, "toastErrorTitle"),
          requestError.message,
          "error",
        );
      }
      if (requestError.status === 401 || requestError.status === 503) await loadStatus({ quiet: true });
    } finally {
      if (actionControllerRef.current === controller) {
        actionControllerRef.current = null;
        if (!controller.signal.aborted) setAction("");
      }
    }
  }

  function changeAutoSync(event) {
    const enabled = event.target.checked;
    setAutoSync(enabled);
    persistAutoSync(role, enabled);
  }

  const busy = loading || Boolean(action);
  const selectedFolder = status?.selected_folder;
  const accountLabel = status?.account?.display_name || status?.account?.email || "—";
  const accountEmail = status?.account?.email;

  return (
    <section className="panel operation-panel m365-panel" aria-busy={busy ? "true" : undefined}>
      <div className="pdf-panel-heading">
        <div>
          <h3>{translate(language, "m365Title")} · {role === "tran" ? "TranNNB" : "NganTLT"}</h3>
          <p className="m365-subtitle">{translate(language, "m365Description")}</p>
        </div>
        <span className={`pdf-capability-badge ${status?.connected ? "" : "is-disabled"}`}>
          {translate(language, status?.connected ? "m365Connected" : "m365Disconnected")}
        </span>
      </div>

      {!capabilityAvailable || status?.configured === false ? (
        <div className="dialog-note is-warning" role="status">
          <p>{translate(language, "m365Unavailable")}</p>
        </div>
      ) : (
        <>
          <div className="m365-account-row">
            <div>
              <span>{translate(language, "m365Account")}</span>
              <strong>{status?.connected ? accountLabel : translate(language, "m365NoAccount")}</strong>
              {status?.connected && accountEmail && accountEmail !== accountLabel && <small>{accountEmail}</small>}
            </div>
            <div className="upload-actions">
              {!status?.connected ? (
                <button className="btn secondary" type="button" disabled={busy} onClick={connect}>
                  {translate(language, action === "connect" ? "m365Connecting" : "m365Connect")}
                </button>
              ) : (
                <button className="btn secondary" type="button" disabled={busy} onClick={disconnect}>
                  {translate(language, action === "disconnect" ? "m365Disconnecting" : "m365Disconnect")}
                </button>
              )}
              <button className="btn secondary button--compact" type="button" disabled={busy} onClick={() => loadStatus()}>
                {translate(language, "m365RefreshStatus")}
              </button>
            </div>
          </div>

          {status?.connected && (
            <div className="m365-folder-controls">
              <label htmlFor={`m365-folder-${role}`}>
                <span>{translate(language, "m365Folder")}</span>
                <select
                  id={`m365-folder-${role}`}
                  value={folderId}
                  disabled={busy || !folders.length}
                  onChange={(event) => setFolderId(event.target.value)}
                >
                  <option value="">{translate(language, folders.length ? "m365ChooseFolder" : "m365NoFolders")}</option>
                  {folders.map((folder) => (
                    <option value={folder.id} key={folder.id}>{folderLabel(folder)}</option>
                  ))}
                </select>
              </label>
              <div className="upload-actions">
                <button
                  className="btn secondary"
                  type="button"
                  disabled={busy || !folderId || folderId === selectedFolder?.id}
                  onClick={saveFolder}
                >
                  {translate(language, action === "folder" ? "m365SavingFolder" : "m365SaveFolder")}
                </button>
                <button
                  className="btn"
                  type="button"
                  disabled={busy || !selectedFolder?.id}
                  onClick={() => syncMailbox()}
                >
                  {translate(language, action === "sync" ? "m365Syncing" : "m365SyncNow")}
                </button>
              </div>
            </div>
          )}

          {status?.connected && selectedFolder && (
            <div className="m365-selected-folder" role="status">
              <span>{translate(language, "m365SelectedFolder")}</span>
              <strong>{folderLabel(selectedFolder)}</strong>
              <small>{translate(language, status.cursor_ready ? "m365CursorReady" : "m365CursorInitial")}</small>
            </div>
          )}

          <label className="upload-confirmation m365-auto-sync">
            <input
              type="checkbox"
              checked={autoSync}
              disabled={busy || !status?.connected || !selectedFolder?.id}
              onChange={changeAutoSync}
            />
            <span>{translate(language, "m365AutoSync")}</span>
          </label>

          <div className="m365-safety-note">
            <strong>{translate(language, "m365SafetyTitle")}</strong>
            <span>{translate(language, "m365Safety")}</span>
            <span>{translate(language, "m365MemoryWarning")}</span>
          </div>
        </>
      )}

      {loading && <div className="upload-progress" role="status"><span>{translate(language, "m365Loading")}</span><progress /></div>}
    </section>
  );
}
