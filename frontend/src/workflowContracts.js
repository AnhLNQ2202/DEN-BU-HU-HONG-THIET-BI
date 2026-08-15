export function supplierRoleFromFilename(filename) {
  const basename = String(filename || "")
    .replace(/\.[^.]+$/, "")
    .toLowerCase();
  // "inactive" contains "active", so the longer role must always be checked first.
  if (basename.includes("inactive")) return "inactive";
  if (basename.includes("active")) return "active";
  return "";
}

export function classifySupplierFiles(files) {
  const classification = {
    active: [],
    inactive: [],
    unknown: [],
  };
  Array.from(files || []).forEach((file) => {
    const role = supplierRoleFromFilename(file?.name);
    if (role) classification[role].push(file);
    else classification.unknown.push(file);
  });
  return classification;
}

export function hasExactSupplierRoles(files, classification = classifySupplierFiles(files)) {
  return Array.from(files || []).length === 2
    && classification.active.length === 1
    && classification.inactive.length === 1
    && classification.unknown.length === 0;
}

export function resolveLostDate(explicitDate, fallbackToday) {
  return String(explicitDate || "").trim() || String(fallbackToday || "").trim();
}

export function selectTranGroupEntries(
  groups,
  selectedKeys,
  expandGroup,
  maxAssets = 100,
) {
  const availableGroups = Array.isArray(groups) ? groups : [];
  const keySet = new Set(
    (Array.isArray(selectedKeys) ? selectedKeys : [])
      .map((key) => String(key || ""))
      .filter(Boolean),
  );
  const selectedGroups = availableGroups.filter((group) => keySet.has(String(group?.key || "")));
  const entries = selectedGroups.flatMap((group) => {
    const expanded = typeof expandGroup === "function" ? expandGroup(group) : group?.entries;
    return Array.isArray(expanded) ? expanded : [];
  });
  const numericLimit = Number.isInteger(maxAssets) && maxAssets > 0 ? maxAssets : 100;
  return {
    groups: selectedGroups,
    entries,
    assetCount: entries.length,
    overLimit: entries.length > numericLimit,
  };
}

export function buildTranSourceBatches(groups, selectedKeys, forms, bindings) {
  const availableGroups = Array.isArray(groups) ? groups : [];
  const safeForms = Array.isArray(forms) ? forms : [];
  const safeBindings = Array.isArray(bindings) ? bindings : [];
  const keySet = new Set(
    (Array.isArray(selectedKeys) ? selectedKeys : [])
      .map((key) => String(key || ""))
      .filter(Boolean),
  );
  if (!keySet.size) return [];
  if (safeForms.length !== safeBindings.length || !safeForms.length) {
    throw new Error("Tran forms and source bindings must be non-empty and aligned");
  }

  const selectedGroups = availableGroups.filter((group) => keySet.has(String(group?.key || "")));
  const caseOwners = new Map();
  selectedGroups.forEach((group) => {
    const key = String(group?.key || "");
    const handle = String(group?.handle || "");
    if (!key || !handle) throw new Error("Every selected Tran source group requires a retained email handle");
    (Array.isArray(group?.cases) ? group.cases : []).forEach((caseItem) => {
      const caseId = String(caseItem?.id || "");
      if (!caseId || caseOwners.has(caseId)) {
        throw new Error("Tran cases must belong to exactly one selected source group");
      }
      caseOwners.set(caseId, key);
    });
  });

  const bindingKeys = new Set();
  const indicesByGroup = new Map(selectedGroups.map((group) => [String(group.key), []]));
  safeBindings.forEach((binding, index) => {
    const caseId = String(binding?.case_id || "");
    const bindingKey = `${caseId}:${binding?.source_row_index ?? "case"}`;
    if (!caseId || bindingKeys.has(bindingKey)) {
      throw new Error("Tran source bindings must be unique");
    }
    bindingKeys.add(bindingKey);
    const owner = caseOwners.get(caseId);
    if (!owner || !indicesByGroup.has(owner)) {
      throw new Error("Every Tran form must map to one selected source group");
    }
    indicesByGroup.get(owner).push(index);
  });

  const batches = selectedGroups.map((group) => {
    const indices = indicesByGroup.get(String(group.key)) || [];
    if (!indices.length) throw new Error("Every selected Tran source group requires at least one asset");
    return {
      key: String(group.key),
      handle: String(group.handle),
      cases: Array.isArray(group.cases) ? group.cases : [],
      forms: indices.map((index) => safeForms[index]),
      bindings: indices.map((index) => safeBindings[index]),
    };
  });
  if (batches.reduce((total, batch) => total + batch.forms.length, 0) !== safeForms.length) {
    throw new Error("Tran source batch construction dropped one or more assets");
  }
  return batches;
}

export async function runTranBatch(batches, createOne, options = {}) {
  const queue = Array.isArray(batches) ? batches : [];
  const successes = [];
  const failures = [];
  let attempted = 0;

  for (let index = 0; index < queue.length; index += 1) {
    const batch = queue[index];
    options.onProgress?.({ completed: attempted, total: queue.length, batch, index });
    try {
      const result = await createOne(batch, index);
      const success = { batch, result };
      successes.push(success);
      options.onSuccess?.(success, index);
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      failures.push({ batch, error });
      attempted += 1;
      options.onProgress?.({ completed: attempted, total: queue.length, batch, index });
      if (options.shouldStop?.(error, batch, index)) break;
      continue;
    }
    attempted += 1;
    options.onProgress?.({ completed: attempted, total: queue.length, batch, index });
  }

  return {
    successes,
    failures,
    remaining: queue.slice(attempted),
    attempted,
    total: queue.length,
    stopped: attempted < queue.length,
  };
}

export function tranDraftRequestFingerprint(
  mode,
  batch,
  { bodyIntro = "", processingDate = "", yearSheet = "" } = {},
) {
  return JSON.stringify({
    mode: String(mode || ""),
    handle: String(batch?.handle || ""),
    forms: Array.isArray(batch?.forms) ? batch.forms : [],
    bindings: Array.isArray(batch?.bindings) ? batch.bindings : [],
    body_intro: String(bodyIntro || "").trim(),
    processing_date: String(processingDate || ""),
    year_sheet: String(yearSheet || "").trim(),
  });
}

export function isAmbiguousDraftError(error) {
  const status = Number(error?.status || 0);
  return !Number.isInteger(status) || status <= 0 || status >= 500;
}
