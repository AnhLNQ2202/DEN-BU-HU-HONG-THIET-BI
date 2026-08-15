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
  assert.match(component, /disabled=\{!resolution\?\.ready\}/);
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
