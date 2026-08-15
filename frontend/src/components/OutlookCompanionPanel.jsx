import React, { useEffect, useState } from "react";

import { dashboardApi } from "../api.js";
import { API } from "../constants.js";
import { translate } from "../i18n.js";
import { useToast } from "./Feedback.jsx";

const METHODS = Object.freeze([
  {
    clientType: "outlook_addin",
    titleKey: "companionAddinTitle",
    bestForKey: "companionAddinBestFor",
    outcomeKey: "companionAddinOutcome",
    downloadKey: "companionDownloadAddin",
    downloadUrl: API.outlookAddinManifest,
    stepKeys: [
      "companionAddinStep1",
      "companionAddinStep2",
      "companionAddinStep3",
      "companionAddinStep4",
      "companionAddinStep5",
    ],
  },
  {
    clientType: "local_bridge",
    titleKey: "companionBridgeTitle",
    bestForKey: "companionBridgeBestFor",
    outcomeKey: "companionBridgeOutcome",
    downloadKey: "companionDownloadBridge",
    downloadUrl: API.localBridgeDownload,
    stepKeys: [
      "companionBridgeStep1",
      "companionBridgeStep2",
      "companionBridgeStep3",
      "companionBridgeStep4",
      "companionBridgeStep5",
    ],
  },
]);

export function OutlookCompanionPanel({ capabilities, language, refreshVersion = 0, role }) {
  const [busyType, setBusyType] = useState("");
  const [pairings, setPairings] = useState({});
  const pushToast = useToast();
  const available = capabilities?.companion_pairing === true;

  useEffect(() => {
    setBusyType("");
    setPairings({});
  }, [refreshVersion]);

  async function createPairing(clientType) {
    setBusyType(clientType);
    try {
      const pairing = await dashboardApi.createCompanionPairing(role, clientType);
      setPairings((current) => ({ ...current, [clientType]: pairing }));
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "companionCodeCreatedNotice"),
        "success",
      );
    } catch (requestError) {
      pushToast(
        translate(language, "toastErrorTitle"),
        requestError.message,
        "error",
      );
    } finally {
      setBusyType("");
    }
  }

  async function copyCode(code) {
    try {
      await navigator.clipboard.writeText(code);
      pushToast(
        translate(language, "toastSuccessTitle"),
        translate(language, "companionCodeCopiedNotice"),
        "success",
      );
    } catch {
      pushToast(
        translate(language, "toastErrorTitle"),
        translate(language, "companionCopyFailedNotice"),
        "error",
      );
    }
  }

  return (
    <section className="panel outlook-companion-panel" aria-labelledby={`companion-${role}-title`}>
      <div className="companion-heading">
        <div>
          <h3 id={`companion-${role}-title`}>{translate(language, "companionTitle")}</h3>
          <p>{translate(language, "companionSubtitle")}</p>
        </div>
        <span className="pdf-capability-badge">
          {role === "ngan" ? "NganTLT" : "TranNNB"}
        </span>
      </div>

      <div className="companion-plain-explanation">
        <strong>{translate(language, "companionHowTitle")}</strong>
        <span>{translate(language, role === "ngan" ? "companionNganHowText" : "companionTranHowText")}</span>
      </div>

      <div className="companion-choice">
        <strong>{translate(language, "companionChooseTitle")}</strong>
        <span>• {translate(language, "companionChooseAddin")}</span>
        <span>• {translate(language, "companionChooseBridge")}</span>
      </div>

      <div className="companion-method-grid">
        {METHODS.map((method, index) => {
          const pairing = pairings[method.clientType];
          const isBusy = busyType === method.clientType;
          const isNgan = role === "ngan";
          const outcomeKey = isNgan
            ? (method.clientType === "outlook_addin"
              ? "companionNganAddinOutcome"
              : "companionNganBridgeOutcome")
            : method.outcomeKey;
          const stepKeys = isNgan
            ? method.stepKeys.map((key, stepIndex) => (
              stepIndex === 3 && method.clientType === "local_bridge"
                ? "companionNganBridgeStep4"
                : stepIndex === method.stepKeys.length - 1
                ? (method.clientType === "outlook_addin"
                  ? "companionNganAddinStep5"
                  : "companionNganBridgeStep5")
                : key
            ))
            : method.stepKeys.map((key, stepIndex) => (
              stepIndex === 3 && method.clientType === "local_bridge"
                ? "companionTranBridgeStep4"
                : key
            ));
          return (
            <article className="companion-method-card" key={method.clientType}>
              <div className="companion-method-number">{index + 1}</div>
              <div className="companion-method-title">
                <h4>{translate(language, method.titleKey)}</h4>
                <p>{translate(language, method.bestForKey)}</p>
              </div>
              <div className="companion-outcome">
                <strong>{translate(language, "companionWhatHappens")}</strong>
                <span>{translate(language, outcomeKey)}</span>
              </div>
              <ol className="companion-steps">
                {stepKeys.map((key) => <li key={key}>{translate(language, key)}</li>)}
              </ol>
              <div className="companion-actions">
                <a
                  className="btn secondary"
                  href={method.downloadUrl}
                  download
                  onClick={() => pushToast(
                    translate(language, "toastInfoTitle"),
                    translate(language, "companionDownloadStartedNotice"),
                    "info",
                  )}
                >
                  ↓ {translate(language, method.downloadKey)}
                </a>
                <button
                  className="btn"
                  type="button"
                  disabled={!available || Boolean(busyType)}
                  onClick={() => createPairing(method.clientType)}
                >
                  {translate(language, isBusy ? "companionCreatingCode" : "companionCreateCode")}
                </button>
              </div>
              {pairing && (
                <div className="companion-code" role="status">
                  <span>{translate(language, "companionYourCode")}</span>
                  <strong>{pairing.code}</strong>
                  <button className="btn secondary button--compact" type="button" onClick={() => copyCode(pairing.code)}>
                    {translate(language, "companionCopyCode")}
                  </button>
                  <small>{translate(language, "companionCodeExpiry")}</small>
                </div>
              )}
            </article>
          );
        })}
      </div>

      {!available && (
        <div className="disabled-note">{translate(language, "companionUnavailable")}</div>
      )}
      <div className="m365-safety-note">
        <strong>{translate(language, "companionSafetyTitle")}</strong>
        <span>{translate(language, "companionSafety")}</span>
      </div>
    </section>
  );
}
