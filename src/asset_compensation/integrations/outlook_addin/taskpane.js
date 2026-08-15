"use strict";

const API = Object.freeze({
  exchange: "/api/companion/exchange",
  uploadEmail: "/api/companion/client/emails",
  draftPackages: "/api/companion/client/draft-packages",
});

const HEADERS = Object.freeze({
  uploadIntent: "companion-email-v1",
  acknowledgeIntent: "companion-ack-v1",
});

const STORAGE = Object.freeze({
  token: "asset-hub.addin.token",
  identity: "asset-hub.addin.identity",
  itemSources: "asset-hub.addin.item-sources",
});

const LIMITS = Object.freeze({
  apiResponseCharacters: 2 * 1024 * 1024,
  draftPackageResponseCharacters: 37 * 1024 * 1024,
  base64EmailCharacters: 2_796_204,
  emailBytes: 2 * 1024 * 1024,
  workbookBase64Characters: 34_865_152,
  workbookBytes: 25 * 1024 * 1024,
  htmlBodyCharacters: 32 * 1024,
  storedItemMappings: 50,
  requestTimeoutMs: 45 * 1000,
  packagePollMs: 15 * 1000,
});

const state = {
  officeReady: false,
  itemKey: null,
  artifactHandle: null,
  token: null,
  identity: null,
  packages: [],
  packageRequestRunning: false,
  pollTimer: null,
  openedPackageIds: new Set(),
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  bindEvents();
  restoreSession();
  render();
  initializeOffice();
});

function bindElements() {
  const ids = [
    "global-status",
    "pairing-panel",
    "pairing-form",
    "pairing-code",
    "pair-button",
    "disconnect-button",
    "connection-badge",
    "session-summary",
    "current-subject",
    "mail-badge",
    "upload-button",
    "draft-panel",
    "refresh-button",
    "draft-empty",
    "draft-list",
  ];
  for (const id of ids) {
    elements[toCamelCase(id)] = document.getElementById(id);
  }
}

function bindEvents() {
  elements.pairingForm.addEventListener("submit", handlePairing);
  elements.disconnectButton.addEventListener("click", disconnect);
  elements.uploadButton.addEventListener("click", uploadCurrentEmail);
  elements.refreshButton.addEventListener("click", () => refreshDraftPackages(false));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.identity?.role === "tran") {
      refreshDraftPackages(true);
    }
  });
}

function initializeOffice() {
  if (typeof Office === "undefined" || typeof Office.onReady !== "function") {
    showStatus("Không tìm thấy Office.js. Hãy mở trang này từ Outlook.", "error");
    return;
  }

  Office.onReady()
    .then((info) => {
      if (info.host !== Office.HostType.Outlook) {
        throw new Error("Add-in này chỉ hoạt động trong Outlook.");
      }
      state.officeReady = true;
      registerItemChangedHandler();
      updateCurrentItem();
      showStatus("Outlook đã sẵn sàng. Ghép nối hoặc nạp email đang mở.", "success");
      startPackagePolling();
      render();
    })
    .catch((error) => {
      showStatus(safeErrorMessage(error, "Không thể khởi tạo Outlook Add-in."), "error");
    });
}

function registerItemChangedHandler() {
  const mailbox = Office.context?.mailbox;
  if (!mailbox || typeof mailbox.addHandlerAsync !== "function" || !Office.EventType?.ItemChanged) {
    return;
  }
  mailbox.addHandlerAsync(Office.EventType.ItemChanged, updateCurrentItem);
}

function updateCurrentItem() {
  const item = Office.context?.mailbox?.item;
  if (!item) {
    state.itemKey = null;
    state.artifactHandle = null;
    state.packages = [];
    elements.currentSubject.textContent = "Hãy mở một email báo mất hoặc hư hỏng.";
    render();
    return;
  }

  state.itemKey = itemKeyFor(item);
  state.artifactHandle = state.itemKey ? uniqueMappedHandleForItem(state.itemKey) : null;
  state.packages = [];

  const subject = typeof item.subject === "string" ? item.subject.trim() : "";
  elements.currentSubject.textContent = truncateText(subject || "Email không có tiêu đề", 240);
  render();

  if (state.artifactHandle && state.identity?.role === "tran") {
    refreshDraftPackages(true);
  }
}

async function handlePairing(event) {
  event.preventDefault();
  const code = normalizePairingCode(elements.pairingCode.value);
  if (!code) {
    showStatus("Mã ghép nối phải có 6–64 ký tự chữ, số hoặc dấu gạch.", "error");
    elements.pairingCode.focus();
    return;
  }

  setBusy(elements.pairButton, true);
  try {
    const response = await requestJson(API.exchange, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const payload = unwrapData(response);
    const token = validateBearerToken(payload.token);
    const role = payload.role;
    const clientType = payload.client_type;

    if (!token || !["ngan", "tran"].includes(role) || clientType !== "outlook_addin") {
      throw new Error("Product trả về phiên ghép nối không hợp lệ.");
    }

    const expiresAt = resolveExpiry(payload);
    state.token = token;
    state.identity = { role, client_type: clientType, expires_at: expiresAt };
    persistSession();
    elements.pairingCode.value = "";
    showStatus(`Đã ghép nối cho phân hệ ${roleLabel(role)}.`, "success");
    startPackagePolling();
    render();

    if (state.artifactHandle && role === "tran") {
      await refreshDraftPackages(true);
    }
  } catch (error) {
    showStatus(safeErrorMessage(error, "Ghép nối không thành công."), "error");
  } finally {
    setBusy(elements.pairButton, false);
  }
}

function disconnect() {
  clearSession();
  state.packages = [];
  state.openedPackageIds.clear();
  stopPackagePolling();
  showStatus("Đã ngắt kết nối. Token trong task pane đã được xóa.", "success");
  render();
}

async function uploadCurrentEmail() {
  if (!requireUsableSession() || !state.officeReady || !state.itemKey) {
    return;
  }
  if (!isMailboxSetSupported("1.14")) {
    showStatus("Outlook này chưa hỗ trợ xuất EML (cần Mailbox 1.14).", "error");
    return;
  }

  const sourceItem = Office.context?.mailbox?.item;
  const operationItemKey = itemKeyFor(sourceItem);
  if (!operationItemKey || operationItemKey !== state.itemKey) {
    updateCurrentItem();
    showStatus("Email vừa thay đổi. Hãy bấm Nạp lại trên đúng email đang mở.", "warning");
    return;
  }

  setBusy(elements.uploadButton, true);
  try {
    const encodedEml = await getEmailAsBase64(sourceItem);
    const emlBlob = base64ToBlob(encodedEml, "message/rfc822", {
      maxCharacters: LIMITS.base64EmailCharacters,
      maxBytes: LIMITS.emailBytes,
      label: "Email",
    });
    const formData = new FormData();
    formData.append("file", emlBlob, "current-email.eml");

    const response = await requestJson(
      API.uploadEmail,
      {
        method: "POST",
        headers: { "X-Asset-Hub-Upload": HEADERS.uploadIntent },
        body: formData,
      },
      true,
    );
    const payload = unwrapData(response);
    const source = payload.source_eml;
    const handle = validateOpaqueHandle(source?.handle);
    if (!handle) {
      throw new Error("Product không trả về mã email nguồn hợp lệ.");
    }

    storeItemSourceMapping(operationItemKey, handle);
    if (isCurrentItemBinding(operationItemKey, handle)) {
      state.artifactHandle = handle;
      state.packages = [];
      showStatus("Email đang mở đã được nạp vào Product.", "success");
      render();
      if (state.identity.role === "tran") {
        await refreshDraftPackages(true);
      }
    } else {
      updateCurrentItem();
      if (isHandleAmbiguous(handle)) {
        showStatus(
          "Hai mail Outlook trong phiên này có nội dung giống hệt nhau. Add-in dừng để không Reply-All nhầm bản sao; hãy ngắt kết nối rồi chỉ nạp mail gốc cần xử lý.",
          "warning",
        );
      } else {
        showStatus(
          "Email ban đầu đã nạp xong, nhưng bạn đã mở mail khác. Product không gắn kết quả sang mail mới.",
          "warning",
        );
      }
    }
  } catch (error) {
    showStatus(safeErrorMessage(error, "Không thể nạp email đang mở."), "error");
  } finally {
    setBusy(elements.uploadButton, false);
    render();
  }
}

function getEmailAsBase64(item) {
  return new Promise((resolve, reject) => {
    if (!item || typeof item.getAsFileAsync !== "function") {
      reject(new Error("Email đang mở không hỗ trợ xuất EML."));
      return;
    }
    item.getAsFileAsync((result) => {
      if (result.status !== Office.AsyncResultStatus.Succeeded) {
        reject(new Error(result.error?.message || "Outlook không thể xuất email này."));
        return;
      }
      if (typeof result.value !== "string" || !result.value) {
        reject(new Error("Outlook trả về email rỗng."));
        return;
      }
      resolve(result.value);
    });
  });
}

async function refreshDraftPackages(silent) {
  if (
    state.packageRequestRunning ||
    !requireUsableSession(silent) ||
    state.identity?.role !== "tran" ||
    !state.artifactHandle
  ) {
    return;
  }

  state.packageRequestRunning = true;
  if (!silent) {
    setBusy(elements.refreshButton, true);
  }
  try {
    const response = await requestJson(API.draftPackages, { method: "GET" }, true);
    const payload = unwrapData(response);
    if (payload.role !== "tran" || !Array.isArray(payload.packages)) {
      throw new Error("Danh sách draft từ Product không hợp lệ.");
    }

    state.packages = payload.packages.filter(isPackageForCurrentEmail);
    renderDraftPackages();
  } catch (error) {
    if (!silent) {
      showStatus(safeErrorMessage(error, "Không thể tải danh sách draft."), "error");
    }
  } finally {
    state.packageRequestRunning = false;
    setBusy(elements.refreshButton, false);
    render();
  }
}

function isPackageForCurrentEmail(candidate) {
  return Boolean(
    candidate &&
      validatePackageId(candidate.id) &&
      candidate.source_eml_handle === state.artifactHandle &&
      candidate.sent === false &&
      !state.openedPackageIds.has(candidate.id),
  );
}

function renderDraftPackages() {
  elements.draftList.replaceChildren();
  elements.draftEmpty.hidden = state.packages.length > 0;
  if (!state.packages.length) {
    elements.draftEmpty.textContent = state.artifactHandle
      ? "Chưa có draft cho đúng email này. Hãy tạo draft TranNNB trên Product rồi bấm Làm mới."
      : "Nạp email này trước, sau đó xử lý TranNNB trên Product để nhận draft.";
    return;
  }

  for (const draftPackage of state.packages) {
    const item = document.createElement("article");
    item.className = "draft-item";

    const name = document.createElement("div");
    name.className = "draft-item__name";
    name.textContent = truncateText(draftPackage.workbook_filename || "Workbook đền bù", 180);

    const meta = document.createElement("div");
    meta.className = "draft-item__meta";
    const assetCount = Number.isInteger(draftPackage.asset_count) ? draftPackage.asset_count : 0;
    const expires = Number.isFinite(draftPackage.expires_in_seconds)
      ? Math.max(0, Math.ceil(draftPackage.expires_in_seconds / 60))
      : null;
    meta.textContent = `${assetCount} tài sản${expires === null ? "" : ` · còn ${expires} phút`}`;

    const button = document.createElement("button");
    button.className = "button button--primary button--wide";
    button.type = "button";
    button.textContent = "Mở Reply All + workbook";
    if (!isMailboxSetSupported("1.15")) {
      button.disabled = true;
      button.title = "Cần Outlook hỗ trợ Mailbox 1.15 để đính kèm workbook từ dữ liệu Base64.";
    } else {
      button.addEventListener("click", () => openReplyAll(draftPackage.id, button));
    }

    item.append(name, meta, button);
    if (button.disabled) {
      const requirement = document.createElement("p");
      requirement.className = "hint";
      requirement.textContent =
        "Outlook này nạp EML được nhưng chưa đính kèm Base64 (cần Mailbox 1.15). Dùng Local Bridge hoặc tải workbook từ Product.";
      item.append(requirement);
    }
    elements.draftList.append(item);
  }
}

async function openReplyAll(packageId, button) {
  if (!requireUsableSession() || !validatePackageId(packageId) || !state.artifactHandle) {
    return;
  }
  if (!isMailboxSetSupported("1.9") || !isMailboxSetSupported("1.15")) {
    showStatus("Outlook cần Mailbox 1.15 để mở Reply All kèm workbook an toàn.", "error");
    return;
  }

  const operationItemKey = state.itemKey;
  const operationArtifactHandle = state.artifactHandle;
  if (!isCurrentItemBinding(operationItemKey, operationArtifactHandle)) {
    updateCurrentItem();
    showStatus("Email vừa thay đổi. Draft chưa được mở để tránh trả lời nhầm người.", "warning");
    return;
  }

  setBusy(button, true);
  try {
    const detailUrl = `${API.draftPackages}/${packageId}`;
    const response = await requestJson(
      detailUrl,
      { method: "GET" },
      true,
      LIMITS.draftPackageResponseCharacters,
    );
    const payload = unwrapData(response);
    const draftPackage = payload.package;
    validateDraftPackage(draftPackage, packageId, operationArtifactHandle);

    const attachmentType = Office.MailboxEnums?.AttachmentType?.Base64;
    if (!attachmentType) {
      throw new Error("Outlook báo hỗ trợ Mailbox 1.15 nhưng thiếu kiểu đính kèm Base64.");
    }
    const attachment = {
      type: attachmentType,
      name: validateWorkbookName(draftPackage.workbook.filename),
      base64file: validateWorkbookBase64(draftPackage.workbook.content_base64),
      inLine: false,
    };

    requireCurrentItemBinding(operationItemKey, operationArtifactHandle);
    await displayReplyAll(
      operationItemKey,
      operationArtifactHandle,
      { htmlBody: draftPackage.body_html, attachments: [attachment] },
    );
    state.openedPackageIds.add(packageId);
    state.packages = state.packages.filter((item) => item.id !== packageId);
    renderDraftPackages();

    try {
      await requestJson(
        `${detailUrl}/ack`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Asset-Hub-Action": HEADERS.acknowledgeIntent,
          },
          body: "{}",
        },
        true,
      );
      showStatus("Outlook đã mở Reply All kèm workbook. Hãy kiểm tra trước khi bấm Send.", "success");
    } catch (ackError) {
      showStatus(
        "Reply All đã mở, nhưng Product chưa ghi nhận được trạng thái. Không bấm mở draft lần nữa trong phiên này.",
        "warning",
      );
    }
  } catch (error) {
    showStatus(safeErrorMessage(error, "Không thể mở Reply All."), "error");
  } finally {
    setBusy(button, false);
  }
}

function validateDraftPackage(draftPackage, expectedId, expectedSourceHandle) {
  if (
    !draftPackage ||
    draftPackage.id !== expectedId ||
    draftPackage.source_eml_handle !== expectedSourceHandle ||
    draftPackage.sent !== false
  ) {
    throw new Error("Draft không khớp với email đang mở.");
  }
  if (
    typeof draftPackage.body_html !== "string" ||
    !draftPackage.body_html.trim() ||
    draftPackage.body_html.length > LIMITS.htmlBodyCharacters ||
    new Blob([draftPackage.body_html]).size > LIMITS.htmlBodyCharacters
  ) {
    throw new Error("Nội dung draft rỗng hoặc vượt giới hạn 32 KB của Outlook.");
  }
  if (
    /<(?:script|iframe|object|embed|form|meta|link)\b/i.test(draftPackage.body_html) ||
    /\son[a-z]+\s*=/i.test(draftPackage.body_html) ||
    /javascript\s*:/i.test(draftPackage.body_html)
  ) {
    throw new Error("Nội dung draft chứa HTML không an toàn.");
  }
  if (!draftPackage.workbook || typeof draftPackage.workbook !== "object") {
    throw new Error("Draft không có workbook hợp lệ.");
  }
  const workbookName = validateWorkbookName(draftPackage.workbook.filename);
  const expectedContentType = workbookName.toLowerCase().endsWith(".xlsm")
    ? "application/vnd.ms-excel.sheet.macroEnabled.12"
    : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  if (draftPackage.workbook.content_type !== expectedContentType) {
    throw new Error("Draft trả về sai định dạng workbook Excel.");
  }
}

function displayReplyAll(expectedItemKey, expectedArtifactHandle, formData) {
  return new Promise((resolve, reject) => {
    const currentItem = requireCurrentItemBinding(expectedItemKey, expectedArtifactHandle);
    if (!currentItem || typeof currentItem.displayReplyAllFormAsync !== "function") {
      reject(new Error("Email đang mở không hỗ trợ Reply All."));
      return;
    }
    currentItem.displayReplyAllFormAsync(formData, (result) => {
      if (result.status !== Office.AsyncResultStatus.Succeeded) {
        reject(new Error(result.error?.message || "Outlook không thể mở Reply All."));
        return;
      }
      resolve();
    });
  });
}

function itemKeyFor(item) {
  const candidate = firstNonEmptyString(item?.itemId, item?.internetMessageId);
  return candidate && candidate.length <= 2048 ? candidate : null;
}

function isCurrentItemBinding(expectedItemKey, expectedArtifactHandle) {
  if (!expectedItemKey || !validateOpaqueHandle(expectedArtifactHandle)) {
    return false;
  }
  const currentKey = itemKeyFor(Office.context?.mailbox?.item);
  return (
    currentKey === expectedItemKey &&
    state.itemKey === expectedItemKey &&
    uniqueMappedHandleForItem(expectedItemKey) === expectedArtifactHandle
  );
}

function uniqueMappedHandleForItem(itemKey) {
  const mappings = readItemSourceMappings();
  const handle = mappings[itemKey];
  if (!validateOpaqueHandle(handle) || isHandleAmbiguous(handle, mappings)) {
    return null;
  }
  return handle;
}

function isHandleAmbiguous(artifactHandle, mappings = readItemSourceMappings()) {
  if (!validateOpaqueHandle(artifactHandle)) {
    return false;
  }
  return Object.values(mappings).filter((handle) => handle === artifactHandle).length > 1;
}

function requireCurrentItemBinding(expectedItemKey, expectedArtifactHandle) {
  if (!isCurrentItemBinding(expectedItemKey, expectedArtifactHandle)) {
    throw new Error("Email đang mở đã thay đổi. Draft bị dừng để tránh Reply All nhầm mail.");
  }
  return Office.context.mailbox.item;
}

async function requestJson(
  path,
  options,
  authenticated = false,
  maxResponseCharacters = LIMITS.apiResponseCharacters,
) {
  if (typeof path !== "string" || !path.startsWith("/api/companion/")) {
    throw new Error("Endpoint companion không hợp lệ.");
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), LIMITS.requestTimeoutMs);
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (authenticated) {
    if (!state.token) {
      window.clearTimeout(timeout);
      throw new Error("Phiên ghép nối đã hết. Hãy ghép nối lại.");
    }
    headers.set("Authorization", `Bearer ${state.token}`);
  }

  try {
    const response = await fetch(path, {
      ...options,
      headers,
      signal: controller.signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "same-origin",
    });
    const declaredLength = Number(response.headers.get("Content-Length") || 0);
    if (declaredLength > maxResponseCharacters) {
      throw new Error("Phản hồi từ Product vượt giới hạn an toàn.");
    }
    const text = await response.text();
    if (text.length > maxResponseCharacters) {
      throw new Error("Phản hồi từ Product vượt giới hạn an toàn.");
    }
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      throw new Error("Product trả về dữ liệu không hợp lệ.");
    }

    if (response.status === 401 && authenticated) {
      clearSession();
      stopPackagePolling();
      render();
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(readApiError(payload, response.status));
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error("Product phản hồi quá lâu. Hãy thử lại.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function render() {
  const connected = Boolean(state.token && state.identity);
  elements.connectionBadge.textContent = connected ? `Đã nối · ${roleLabel(state.identity.role)}` : "Chưa ghép nối";
  elements.connectionBadge.className = `badge ${connected ? "badge--success" : "badge--muted"}`;
  elements.pairingForm.hidden = connected;
  elements.disconnectButton.hidden = !connected;
  elements.sessionSummary.hidden = !connected;
  elements.sessionSummary.textContent = connected
    ? `Phiên ${roleLabel(state.identity.role)} chỉ tồn tại trong task pane này${formatExpiry(state.identity.expires_at)}.`
    : "";

  const canUpload = connected && state.officeReady && Boolean(state.itemKey) && isMailboxSetSupported("1.14");
  elements.uploadButton.disabled = !canUpload;
  elements.mailBadge.textContent = state.artifactHandle ? "Đã nạp email này" : "Chưa nạp";
  elements.mailBadge.className = `badge ${state.artifactHandle ? "badge--success" : "badge--muted"}`;

  elements.draftPanel.hidden = !connected || state.identity?.role !== "tran";
  elements.refreshButton.disabled = !state.artifactHandle || state.packageRequestRunning;
  if (!elements.draftPanel.hidden) {
    renderDraftPackages();
  }
}

function showStatus(message, variant) {
  elements.globalStatus.textContent = truncateText(message, 360);
  elements.globalStatus.className = `notice notice--${variant}`;
}

function setBusy(button, busy) {
  if (!button) {
    return;
  }
  button.classList.toggle("is-busy", busy);
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}

function startPackagePolling() {
  stopPackagePolling();
  if (!state.token || state.identity?.role !== "tran") {
    return;
  }
  state.pollTimer = window.setInterval(() => {
    if (!document.hidden && state.artifactHandle) {
      refreshDraftPackages(true);
    }
  }, LIMITS.packagePollMs);
}

function stopPackagePolling() {
  if (state.pollTimer !== null) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function restoreSession() {
  try {
    const token = validateBearerToken(sessionStorage.getItem(STORAGE.token));
    const identity = JSON.parse(sessionStorage.getItem(STORAGE.identity) || "null");
    if (
      !token ||
      !identity ||
      !["ngan", "tran"].includes(identity.role) ||
      identity.client_type !== "outlook_addin" ||
      (identity.expires_at && Date.parse(identity.expires_at) <= Date.now())
    ) {
      clearSession();
      return;
    }
    state.token = token;
    state.identity = identity;
  } catch {
    clearSession();
  }
}

function persistSession() {
  sessionStorage.setItem(STORAGE.token, state.token);
  sessionStorage.setItem(STORAGE.identity, JSON.stringify(state.identity));
}

function clearSession() {
  state.token = null;
  state.identity = null;
  state.artifactHandle = null;
  sessionStorage.removeItem(STORAGE.token);
  sessionStorage.removeItem(STORAGE.identity);
  sessionStorage.removeItem(STORAGE.itemSources);
}

function requireUsableSession(silent = false) {
  if (!state.token || !state.identity) {
    if (!silent) {
      showStatus("Hãy ghép nối bằng mã dùng một lần từ Product.", "error");
    }
    return false;
  }
  if (state.identity.expires_at && Date.parse(state.identity.expires_at) <= Date.now()) {
    clearSession();
    stopPackagePolling();
    render();
    if (!silent) {
      showStatus("Phiên ghép nối đã hết hạn. Hãy tạo mã mới trên Product.", "error");
    }
    return false;
  }
  return true;
}

function readItemSourceMappings() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(STORAGE.itemSources) || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const safe = {};
    for (const [itemKey, handle] of Object.entries(parsed).slice(-LIMITS.storedItemMappings)) {
      if (itemKey.length <= 2048 && validateOpaqueHandle(handle)) {
        safe[itemKey] = handle;
      }
    }
    return safe;
  } catch {
    return {};
  }
}

function storeItemSourceMapping(itemKey, artifactHandle) {
  const mappings = readItemSourceMappings();
  delete mappings[itemKey];
  mappings[itemKey] = artifactHandle;
  const bounded = Object.fromEntries(Object.entries(mappings).slice(-LIMITS.storedItemMappings));
  sessionStorage.setItem(STORAGE.itemSources, JSON.stringify(bounded));
}

function normalizePairingCode(value) {
  const code = typeof value === "string" ? value.trim().toUpperCase() : "";
  return /^[A-Z0-9-]{6,64}$/.test(code) ? code : null;
}

function validateBearerToken(value) {
  return typeof value === "string" && /^[A-Za-z0-9._~-]{32,2048}$/.test(value) ? value : null;
}

function validateOpaqueHandle(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{16,256}$/.test(value) ? value : null;
}

function validatePackageId(value) {
  return typeof value === "string" && /^[a-f0-9]{32}$/.test(value) ? value : null;
}

function validateWorkbookName(value) {
  if (
    typeof value !== "string" ||
    value.length < 6 ||
    value.length > 180 ||
    !/^[^\\/:*?"<>|\x00-\x1f]+\.xls(?:x|m)$/i.test(value)
  ) {
    throw new Error("Tên workbook không hợp lệ.");
  }
  return value;
}

function validateWorkbookBase64(value) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > LIMITS.workbookBase64Characters ||
    value.length % 4 !== 0 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(value)
  ) {
    throw new Error("Dữ liệu workbook không hợp lệ hoặc quá lớn.");
  }
  const bytes = decodedBase64Length(value);
  if (bytes <= 0 || bytes > LIMITS.workbookBytes) {
    throw new Error("Workbook rỗng hoặc vượt giới hạn 25 MB của Outlook.");
  }
  return value;
}

function base64ToBlob(value, contentType, bounds) {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > bounds.maxCharacters ||
    value.length % 4 !== 0 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(value)
  ) {
    throw new Error(`${bounds.label} không hợp lệ hoặc quá lớn.`);
  }
  const byteLength = decodedBase64Length(value);
  if (byteLength <= 0 || byteLength > bounds.maxBytes) {
    throw new Error(`${bounds.label} rỗng hoặc vượt giới hạn ${formatBytes(bounds.maxBytes)}.`);
  }

  const byteArrays = [];
  const chunkCharacters = 4 * 8192;
  for (let offset = 0; offset < value.length; offset += chunkCharacters) {
    const binary = window.atob(value.slice(offset, offset + chunkCharacters));
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    byteArrays.push(bytes);
  }
  return new Blob(byteArrays, { type: contentType });
}

function decodedBase64Length(value) {
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return Math.floor((value.length * 3) / 4) - padding;
}

function resolveExpiry(payload) {
  if (typeof payload.expires_at === "string" && Number.isFinite(Date.parse(payload.expires_at))) {
    return new Date(payload.expires_at).toISOString();
  }
  if (Number.isFinite(payload.expires_in_seconds) && payload.expires_in_seconds > 0) {
    return new Date(Date.now() + Math.min(payload.expires_in_seconds, 24 * 60 * 60) * 1000).toISOString();
  }
  return null;
}

function formatExpiry(value) {
  if (!value || !Number.isFinite(Date.parse(value))) {
    return "";
  }
  const minutes = Math.max(0, Math.ceil((Date.parse(value) - Date.now()) / 60000));
  return ` · còn khoảng ${minutes} phút`;
}

function isMailboxSetSupported(version) {
  try {
    return Boolean(Office.context?.requirements?.isSetSupported("Mailbox", version));
  } catch {
    return false;
  }
}

function unwrapData(value) {
  return value && typeof value.data === "object" && value.data !== null ? value.data : value;
}

function readApiError(payload, status) {
  const candidate = firstNonEmptyString(payload?.message, payload?.error, payload?.detail);
  if (candidate) {
    return truncateText(candidate.replace(/[\r\n\t]+/g, " "), 260);
  }
  if (status === 401) {
    return "Phiên không hợp lệ hoặc đã hết hạn.";
  }
  if (status === 413) {
    return "Email hoặc workbook vượt giới hạn của Product.";
  }
  return `Product trả về lỗi HTTP ${status}.`;
}

function safeErrorMessage(error, fallback) {
  const message = typeof error?.message === "string" ? error.message.trim() : "";
  return truncateText(message || fallback, 320);
}

function truncateText(value, limit) {
  const text = String(value || "");
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

function firstNonEmptyString(...values) {
  return values.find((value) => typeof value === "string" && value.trim())?.trim() || null;
}

function roleLabel(role) {
  return role === "tran" ? "TranNNB" : "NganTLT";
}

function formatBytes(bytes) {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

function toCamelCase(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}
