import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");

test("Tran workspace is a gated three-step workflow instead of one long page", () => {
  const component = source("components/TranWorkspace.jsx");

  assert.match(component, /const \[workspaceStep, setWorkspaceStep\] = useState\("input"\)/);
  assert.match(component, /role="tablist"/);
  assert.match(component, /tranStepInput/);
  assert.match(component, /tranStepResult/);
  assert.match(component, /tranStepOutput/);
  assert.match(component, /disabled=\{Boolean\(busyAction\) \|\| !resolution\?\.ready\}/);
  assert.match(component, /setWorkspaceStep\("result"\)/);
  assert.match(component, /setWorkspaceStep\("output"\)/);
});

test("ready Tran result cards stay compact until the operator asks for detail", () => {
  const component = source("components/TranWorkspace.jsx");
  const styles = source("styles.css");

  assert.match(component, /<details className="tran-resolution-item" open=\{!item\.ready\}>/);
  assert.match(component, /tran-resolution-summary/);
  assert.match(styles, /\.tran-resolution-summary\s*\{/);
  assert.match(styles, /\.tran-resolution-body\s*\{/);
});

test("one shared multi-case picker stays available across all Tran workflow steps", () => {
  const component = source("components/TranWorkspace.jsx");
  const picker = source("components/TranCasePickerDialog.jsx");
  const styles = source("styles.css");

  assert.match(component, /const \[selectedGroupKeys, setSelectedGroupKeys\] = useState\(\[\]\)/);
  assert.match(component, /<div className="tran-batch-bar">/);
  assert.ok(component.indexOf("tran-batch-bar") < component.indexOf('id="tran-step-input"'));
  assert.match(component, /<TranCasePickerDialog/);
  assert.match(picker, /type="checkbox"/);
  assert.match(picker, /tranBatchSelectAllVisible/);
  assert.match(picker, /\.indeterminate = someVisibleSelected/);
  assert.match(component, /aria-live="polite"/);
  assert.match(styles, /\.tran-case-picker__list\s*\{/);
});

test("Tran resolves and exports one combined batch but drafts sequentially per source email", () => {
  const component = source("components/TranWorkspace.jsx");

  assert.match(component, /buildTranAssetsPayload\(forms, language\)/);
  assert.match(component, /buildTranSourceBatches/);
  assert.match(component, /runTranBatch\(/);
  assert.match(component, /buildTranAssetsPayload\(batch\.forms, language\)/);
  assert.match(component, /batch\.bindings/);
  assert.match(component, /batch\.handle/);
  assert.match(component, /TranDraftBatchResult/);
  assert.match(component, /setWorkspaceStep\("input"\)/);
});

test("Tran rejects stale async results and never auto-retries uncertain drafts", () => {
  const component = source("components/TranWorkspace.jsx");

  assert.match(component, /actionGenerationRef/);
  assert.match(component, /generation !== actionGenerationRef\.current/);
  assert.match(component, /disabled=\{Boolean\(busyAction\)\}/);
  assert.match(component, /isAmbiguousDraftError/);
  assert.match(component, /tranDraftBatchUncertainConfirm/);
  assert.match(component, /successfulDraftFingerprintsRef/);
  assert.match(component, /onSuccess: \(\{ batch \}\)/);
  assert.match(component, /<fieldset className="tran-form-lock" disabled=\{Boolean\(busyAction\)\}>/);
});
