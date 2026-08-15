import test from "node:test";
import assert from "node:assert/strict";

import {
  classifySupplierFiles,
  hasExactSupplierRoles,
  resolveLostDate,
  supplierRoleFromFilename,
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
