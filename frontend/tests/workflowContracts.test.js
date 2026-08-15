import test from "node:test";
import assert from "node:assert/strict";

import {
  buildTranSourceBatches,
  classifySupplierFiles,
  hasExactSupplierRoles,
  isAmbiguousDraftError,
  resolveLostDate,
  runTranBatch,
  selectTranGroupEntries,
  supplierRoleFromFilename,
  tranDraftRequestFingerprint,
} from "../src/workflowContracts.js";

test("supplier role detection checks Inactive before Active without using selection order", () => {
  assert.equal(supplierRoleFromFilename("Supplier Inactive.XLS"), "inactive");
  assert.equal(supplierRoleFromFilename("supplier_active.csv"), "active");
  assert.equal(supplierRoleFromFilename("SupplierInactive.xlsx"), "inactive");
  assert.equal(supplierRoleFromFilename("supplier-export.xlsx"), "");

  const inactive = { name: "2026 Supplier Inactive.xlsx" };
  const active = { name: "2026 Supplier Active.xlsx" };
  const result = classifySupplierFiles([inactive, active]);
  assert.deepEqual(result.active, [active]);
  assert.deepEqual(result.inactive, [inactive]);
  assert.deepEqual(result.unknown, []);
  assert.equal(hasExactSupplierRoles([inactive, active], result), true);
  assert.equal(hasExactSupplierRoles([active]), false);
  assert.equal(hasExactSupplierRoles([active, { name: "backup Active.xlsx" }]), false);
  assert.equal(hasExactSupplierRoles([active, inactive, { name: "extra.csv" }]), false);
});

test("supplier classification keeps missing and ambiguous roles visible", () => {
  const first = { name: "north Active.xlsx" };
  const second = { name: "south Active.xlsx" };
  const unknown = { name: "supplier.xlsx" };
  const result = classifySupplierFiles([first, second, unknown]);
  assert.deepEqual(result.active, [first, second]);
  assert.deepEqual(result.inactive, []);
  assert.deepEqual(result.unknown, [unknown]);
});

test("Tran loss-date rule preserves an explicit source date and otherwise uses today", () => {
  assert.equal(resolveLostDate("2026-08-01", "2026-08-15"), "2026-08-01");
  assert.equal(resolveLostDate("", "2026-08-15"), "2026-08-15");
});

test("Tran multi-case selection follows dashboard order and enforces the 100-asset cap", () => {
  const groups = [
    { key: "mail-a", entries: [{ id: "a-1" }, { id: "a-2" }] },
    { key: "mail-b", entries: [{ id: "b-1" }] },
    { key: "mail-c", entries: Array.from({ length: 99 }, (_, index) => ({ id: `c-${index}` })) },
  ];
  const selected = selectTranGroupEntries(groups, ["mail-b", "mail-a", "mail-a"], null, 100);
  assert.deepEqual(selected.groups.map((group) => group.key), ["mail-a", "mail-b"]);
  assert.deepEqual(selected.entries.map((entry) => entry.id), ["a-1", "a-2", "b-1"]);
  assert.equal(selected.overLimit, false);

  const overLimit = selectTranGroupEntries(groups, groups.map((group) => group.key), null, 100);
  assert.equal(overLimit.assetCount, 102);
  assert.equal(overLimit.overLimit, true);

  const exactLimit = selectTranGroupEntries(
    [{ key: "exact", entries: Array.from({ length: 100 }, (_, index) => ({ id: index })) }],
    ["exact"],
    null,
    100,
  );
  assert.equal(exactLimit.assetCount, 100);
  assert.equal(exactLimit.overLimit, false);
  const oneOver = selectTranGroupEntries(
    [{ key: "over", entries: Array.from({ length: 101 }, (_, index) => ({ id: index })) }],
    ["over"],
    null,
    100,
  );
  assert.equal(oneOver.assetCount, 101);
  assert.equal(oneOver.overLimit, true);
});

test("Tran draft batches preserve form and source-binding alignment per original email", () => {
  const groups = [
    { key: "mail-a", handle: "a".repeat(64), cases: [{ id: "case-a" }] },
    { key: "mail-b", handle: "b".repeat(64), cases: [{ id: "case-b" }] },
  ];
  const forms = [{ tag: "A1" }, { tag: "A2" }, { tag: "B1" }];
  const bindings = [
    { case_id: "case-a", source_row_index: 0 },
    { case_id: "case-a", source_row_index: 1 },
    { case_id: "case-b", source_row_index: null },
  ];
  const batches = buildTranSourceBatches(groups, ["mail-a", "mail-b"], forms, bindings);
  assert.deepEqual(batches.map((batch) => batch.forms.map((form) => form.tag)), [["A1", "A2"], ["B1"]]);
  assert.deepEqual(batches.map((batch) => batch.bindings), [bindings.slice(0, 2), bindings.slice(2)]);
  assert.deepEqual(batches.map((batch) => batch.handle), ["a".repeat(64), "b".repeat(64)]);
});

test("Tran draft batch construction fails closed on misaligned or ambiguous provenance", () => {
  const group = { key: "mail-a", handle: "a".repeat(64), cases: [{ id: "case-a" }] };
  const binding = { case_id: "case-a", source_row_index: 0 };
  assert.throws(
    () => buildTranSourceBatches([group], ["mail-a"], [{ tag: "A" }], []),
    /aligned/,
  );
  assert.throws(
    () => buildTranSourceBatches([group], ["mail-a"], [{ tag: "A" }, { tag: "A" }], [binding, binding]),
    /unique/,
  );
  assert.throws(
    () => buildTranSourceBatches([group], ["mail-a"], [{ tag: "A" }], [{ case_id: "orphan", source_row_index: null }]),
    /map to one selected source group/,
  );
  assert.throws(
    () => buildTranSourceBatches([{ ...group, handle: "" }], ["mail-a"], [{ tag: "A" }], [binding]),
    /retained email handle/,
  );
});

test("Tran batch actions continue after a case failure and report partial success", async () => {
  const progress = [];
  const outcome = await runTranBatch(
    [{ key: "mail-a" }, { key: "mail-b" }, { key: "mail-c" }],
    async (batch) => {
      if (batch.key === "mail-b") throw new Error("synthetic failure");
      return { id: batch.key };
    },
    { onProgress: (state) => progress.push(`${state.completed}/${state.total}`) },
  );
  assert.deepEqual(outcome.successes.map((item) => item.result.id), ["mail-a", "mail-c"]);
  assert.deepEqual(outcome.failures.map((item) => item.batch.key), ["mail-b"]);
  assert.equal(outcome.attempted, 3);
  assert.equal(outcome.stopped, false);
  assert.deepEqual(outcome.remaining, []);
  assert.equal(progress.at(-1), "3/3");
});

test("Tran batch actions rethrow AbortError and never start later sources", async () => {
  const attempted = [];
  await assert.rejects(
    () => runTranBatch(
      [{ key: "mail-a" }, { key: "mail-b" }],
      async (batch) => {
        attempted.push(batch.key);
        const error = new Error("cancelled");
        error.name = "AbortError";
        throw error;
      },
    ),
    { name: "AbortError" },
  );
  assert.deepEqual(attempted, ["mail-a"]);
});

test("Tran batch records each success before a later source is aborted", async () => {
  const recorded = [];
  await assert.rejects(
    () => runTranBatch(
      [{ key: "mail-a" }, { key: "mail-b" }, { key: "mail-c" }],
      async (batch) => {
        if (batch.key === "mail-b") {
          const error = new Error("cancelled");
          error.name = "AbortError";
          throw error;
        }
        return { id: batch.key };
      },
      { onSuccess: ({ batch }) => recorded.push(batch.key) },
    ),
    { name: "AbortError" },
  );
  assert.deepEqual(recorded, ["mail-a"]);
});

test("Tran batch actions expose sources that were not started after a systemic failure", async () => {
  const outcome = await runTranBatch(
    [{ key: "mail-a" }, { key: "mail-b" }, { key: "mail-c" }],
    async (batch) => {
      if (batch.key === "mail-b") throw Object.assign(new Error("server unavailable"), { status: 503 });
      return batch.key;
    },
    { shouldStop: (error) => error.status === 503 },
  );
  assert.deepEqual(outcome.successes.map((item) => item.batch.key), ["mail-a"]);
  assert.deepEqual(outcome.failures.map((item) => item.batch.key), ["mail-b"]);
  assert.deepEqual(outcome.remaining.map((item) => item.key), ["mail-c"]);
  assert.equal(outcome.stopped, true);
});

test("Tran draft fingerprints change only when one source request changes", () => {
  const batch = {
    key: "mail-a",
    handle: "a".repeat(64),
    forms: [{ tag_number: "MOU1", domain: "demo.user" }],
    bindings: [{ case_id: "case-a", source_row_index: 0 }],
  };
  const first = tranDraftRequestFingerprint("outlook", batch, {
    bodyIntro: "Approved",
    processingDate: "2026-08-16",
    yearSheet: "2026",
  });
  assert.equal(first, tranDraftRequestFingerprint("outlook", batch, {
    bodyIntro: " Approved ",
    processingDate: "2026-08-16",
    yearSheet: "2026",
  }));
  assert.notEqual(first, tranDraftRequestFingerprint("outlook", {
    ...batch,
    forms: [{ tag_number: "MOU1", domain: "changed.user" }],
  }, {
    bodyIntro: "Approved",
    processingDate: "2026-08-16",
    yearSheet: "2026",
  }));
});

test("status-less and server failures are uncertain and require confirmed retry", () => {
  assert.equal(isAmbiguousDraftError(new Error("network response lost")), true);
  assert.equal(isAmbiguousDraftError(Object.assign(new Error("provider failed"), { status: 502 })), true);
  assert.equal(isAmbiguousDraftError(Object.assign(new Error("invalid input"), { status: 400 })), false);
  assert.equal(isAmbiguousDraftError(Object.assign(new Error("sign in"), { status: 401 })), false);
});
