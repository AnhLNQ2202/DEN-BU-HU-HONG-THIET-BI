import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");

test("one global bottom-right toast provider serves action feedback", () => {
  const feedback = source("components/Feedback.jsx");
  const main = source("main.jsx");
  const styles = source("styles.css");

  assert.match(feedback, /export function ToastProvider/);
  assert.match(feedback, /export function useToast/);
  assert.match(main, /<ToastProvider>[\s\S]*<App \/>[\s\S]*<\/ToastProvider>/);
  assert.match(styles, /\.toast-region\s*\{[\s\S]*position:\s*fixed/);
  assert.match(styles, /\.toast-region\s*\{[\s\S]*right:\s*20px/);
  assert.match(styles, /\.toast-region\s*\{[\s\S]*bottom:\s*20px/);
});

test("command-heavy workspaces send transient outcomes to the toast provider", () => {
  for (const component of [
    "components/M365MailboxPanel.jsx",
    "components/MailPdfPanel.jsx",
    "components/OutlookCompanionPanel.jsx",
    "components/TranWorkspace.jsx",
    "components/UploadWorkspace.jsx",
  ]) {
    const contents = source(component);
    assert.match(contents, /useToast\(\)/, `${component} must use shared toasts`);
  }
});
