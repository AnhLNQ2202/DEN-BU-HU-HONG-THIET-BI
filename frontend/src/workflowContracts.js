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
