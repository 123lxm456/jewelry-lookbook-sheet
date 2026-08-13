const fileInput = document.getElementById("fileInput");
const dropZone = document.getElementById("dropZone");
const emptyUpload = document.getElementById("emptyUpload");
const inputPreview = document.getElementById("inputPreview");
const expandPreview = document.getElementById("expandPreview");
const previewDialog = document.getElementById("previewDialog");
const dialogImage = document.getElementById("dialogImage");
const closePreview = document.getElementById("closePreview");
const clearButton = document.getElementById("clearButton");
const fileMeta = document.getElementById("fileMeta");
const fileName = document.getElementById("fileName");
const fileSize = document.getElementById("fileSize");
const generateButton = document.getElementById("generateButton");
const errorMessage = document.getElementById("errorMessage");
const progressSection = document.getElementById("progressSection");
const progressPercent = document.getElementById("progressPercent");
const progressBar = document.getElementById("progressBar");
const stageText = document.getElementById("stageText");
const loadingStage = document.getElementById("loadingStage");
const stageItems = [...document.querySelectorAll(".stage-list li")];
const resultPlaceholder = document.getElementById("resultPlaceholder");
const resultLoading = document.getElementById("resultLoading");
const resultGallery = document.getElementById("resultGallery");
const displayImageGrid = document.getElementById("displayImageGrid");
const resultImage = document.getElementById("resultImage");
const downloadButton = document.getElementById("downloadButton");
const saveDialog = document.getElementById("saveDialog");
const saveImage = document.getElementById("saveImage");
const closeSaveDialog = document.getElementById("closeSaveDialog");
const currentUser = document.getElementById("currentUser");
const mobileCurrentUser = document.getElementById("mobileCurrentUser");
const logoutButtons = [...document.querySelectorAll("[data-logout]")];
const balanceBadge = document.getElementById("balanceBadge");
const mobileBalanceBadge = document.getElementById("mobileBalanceBadge");
const rechargeDialog = document.getElementById("rechargeDialog");
const closeRechargeDialog = document.getElementById("closeRechargeDialog");

const allowedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxBytes = 20 * 1024 * 1024;
const currentJobStorageKey = "product-visual.currentJobId";
const draftDatabaseName = "product-visual-workflow";
const draftStoreName = "uploads";
let selectedFile = null;
let previewUrl = null;
let eventSource = null;
let currentUserId = null;
let currentResultData = null;
let paymentRequired = true;
let currentServiceStatus = "unpaid";
let remainingUses = 0;

async function initializeSession() {
  const response = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
  if (response.status === 401) {
    location.replace("/?next=%2Fapp");
    throw new Error("登录状态已过期，正在重新登录");
  }
  if (!response.ok) throw new Error("无法初始化前端会话");
  const data = await response.json();
  if (!data.session_id) throw new Error("前端会话标识无效");
  if (!data.user_id) throw new Error("当前用户标识无效");
  paymentRequired = Boolean(data.payment_required);
  currentServiceStatus = data.service_status || "unpaid";
  remainingUses = Number(data.remaining_uses) || 0;
  const balanceText = `余额 ¥${(Number(data.balance_cent || 0) / 100).toFixed(2)} · ${remainingUses} 次`;
  if (balanceBadge) balanceBadge.textContent = balanceText;
  if (mobileBalanceBadge) mobileBalanceBadge.textContent = balanceText;
  currentUserId = String(data.user_id);
  if (data.service_job_id) {
    try { window.sessionStorage.setItem(currentJobStorageKey, data.service_job_id); } catch { /* ignore */ }
  }
  if (currentUser && data.openid_masked) currentUser.textContent = data.openid_masked;
  if (mobileCurrentUser && data.openid_masked) mobileCurrentUser.textContent = data.openid_masked;
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorMessage.hidden = true;
  errorMessage.textContent = "";
}

function showRechargeDialog() {
  if (rechargeDialog && !rechargeDialog.open) rechargeDialog.showModal();
}

async function hasAvailableUse() {
  if (!paymentRequired) return true;
  const response = await fetch("/api/account", { cache: "no-store", credentials: "same-origin" });
  if (response.status === 401) { location.replace("/?next=%2Fapp"); return false; }
  if (!response.ok) throw new Error("无法查询账户余额，请稍后重试");
  const data = await response.json();
  remainingUses = Number(data.remaining_uses) || 0;
  const balanceText = `余额 ¥${(Number(data.balance_cent || 0) / 100).toFixed(2)} · ${remainingUses} 次`;
  if (balanceBadge) balanceBadge.textContent = balanceText;
  if (mobileBalanceBadge) mobileBalanceBadge.textContent = balanceText;
  if (remainingUses < 1) showRechargeDialog();
  return remainingUses > 0;
}

function updatePreviewLayout() {
  if (!selectedFile || !inputPreview.naturalWidth || !inputPreview.naturalHeight) return;
  const naturalRatio = inputPreview.naturalHeight / inputPreview.naturalWidth;
  const idealHeight = dropZone.clientWidth * naturalRatio;
  const viewportRatio = window.innerWidth <= 820 ? 0.72 : 0.64;
  const viewportLimit = Math.min(window.innerHeight * viewportRatio, 640);
  dropZone.style.height = `${Math.max(240, Math.min(idealHeight, viewportLimit))}px`;
}

function openPreview() {
  if (!previewUrl) return;
  dialogImage.src = previewUrl;
  if (!previewDialog.open) previewDialog.showModal();
}

async function clearClientState() {
  try {
    for (const storage of [window.localStorage, window.sessionStorage]) {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        storage.removeItem(storage.key(index));
      }
    }
  } catch {
    // Storage can be unavailable in private browsing.
  }
  if (window.indexedDB) {
    await new Promise((resolve) => {
      const request = window.indexedDB.deleteDatabase(draftDatabaseName);
      request.onsuccess = request.onerror = request.onblocked = resolve;
    });
  }
}

function openDraftDatabase() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) { resolve(null); return; }
    const request = window.indexedDB.open(draftDatabaseName, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(draftStoreName);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function saveDraft(file) {
  if (!currentUserId) return;
  try {
    const database = await openDraftDatabase();
    if (!database) return;
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(draftStoreName, "readwrite");
      transaction.objectStore(draftStoreName).put({
        blob: file, name: file.name, type: file.type,
      }, `user-${currentUserId}`);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  } catch { /* private browsing or storage quotas may deny persistence */ }
}

async function loadDraft() {
  if (!currentUserId) return null;
  try {
    const database = await openDraftDatabase();
    if (!database) return null;
    const value = await new Promise((resolve, reject) => {
      const transaction = database.transaction(draftStoreName, "readonly");
      const request = transaction.objectStore(draftStoreName).get(`user-${currentUserId}`);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error);
    });
    database.close();
    if (!value?.blob) return null;
    return new File([value.blob], value.name || "uploaded-product-image", { type: value.type || value.blob.type });
  } catch { return null; }
}

async function deleteDraft() {
  if (!currentUserId) return;
  try {
    const database = await openDraftDatabase();
    if (!database) return;
    await new Promise((resolve, reject) => {
      const transaction = database.transaction(draftStoreName, "readwrite");
      transaction.objectStore(draftStoreName).delete(`user-${currentUserId}`);
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  } catch { /* ignore unavailable storage */ }
}

async function setSelectedFile(file, { persist = true } = {}) {
  clearError();
  if (!allowedTypes.has(file.type)) {
    showError("请选择 JPEG、PNG 或 WebP 图片");
    return;
  }
  if (file.size > maxBytes) {
    showError("图片文件不能超过 20 MB");
    return;
  }
  selectedFile = file;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  inputPreview.src = previewUrl;
  dialogImage.src = previewUrl;
  inputPreview.hidden = false;
  emptyUpload.hidden = true;
  clearButton.hidden = false;
  expandPreview.hidden = false;
  dropZone.classList.add("has-image");
  fileMeta.hidden = false;
  fileName.textContent = file.name;
  fileSize.textContent = formatBytes(file.size);
  generateButton.disabled = false;
  if (persist) await saveDraft(file);
}

function resetFile() {
  selectedFile = null;
  fileInput.value = "";
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  inputPreview.removeAttribute("src");
  dialogImage.removeAttribute("src");
  inputPreview.hidden = true;
  emptyUpload.hidden = false;
  clearButton.hidden = true;
  expandPreview.hidden = true;
  dropZone.classList.remove("has-image");
  dropZone.style.removeProperty("height");
  if (previewDialog.open) previewDialog.close();
  fileMeta.hidden = true;
  generateButton.disabled = true;
  clearError();
  deleteDraft();
}

function setBusy(busy) {
  generateButton.disabled = busy || !selectedFile;
  clearButton.disabled = busy;
  dropZone.setAttribute("aria-disabled", String(busy));
  fileInput.disabled = busy;
}

function updateProgress(data) {
  const progress = Math.max(0, Math.min(100, Number(data.progress) || 0));
  progressSection.hidden = false;
  progressPercent.textContent = `${progress}%`;
  progressBar.style.width = `${progress}%`;
  stageText.textContent = data.stage || "正在生成";
  loadingStage.textContent = data.stage || "正在生成";
  stageItems.forEach((item) => {
    item.classList.toggle("active", progress >= Number(item.dataset.threshold));
  });
}

function prepareResultState() {
  currentResultData = null;
  resultPlaceholder.hidden = true;
  resultGallery.hidden = true;
  displayImageGrid.replaceChildren();
  resultImage.removeAttribute("src");
  resultLoading.hidden = false;
  downloadButton.classList.add("disabled");
  downloadButton.setAttribute("aria-disabled", "true");
  downloadButton.removeAttribute("download");
  downloadButton.href = "#";
}

function showResult(data) {
  currentResultData = data;
  resultLoading.hidden = true;
  const images = Array.isArray(data.images) ? data.images : [];
  const displayImages = images.filter((image) => image.type === "display");
  const finalImage = images.find((image) => image.type === "final");
  displayImageGrid.replaceChildren(...displayImages.map((image, index) => {
    const figure = document.createElement("figure");
    figure.className = "display-image-card";
    const preview = document.createElement("a");
    preview.href = image.url;
    preview.target = "_blank";
    preview.rel = "noopener";
    const img = document.createElement("img");
    img.src = `${image.url}?v=${Date.now()}`;
    img.alt = image.label || `商品展示图_${String(index + 1).padStart(2, "0")}`;
    img.loading = index < 2 ? "eager" : "lazy";
    const caption = document.createElement("figcaption");
    caption.textContent = image.label || `商品展示图_${String(index + 1).padStart(2, "0")}`;
    preview.append(img);
    figure.append(preview, caption);
    return figure;
  }));
  resultImage.src = `${finalImage?.url || data.result_url}?v=${Date.now()}`;
  resultGallery.hidden = false;
  resultImage.addEventListener("load", () => {
    // Let the document own vertical scrolling on mobile/WebView. This avoids
    // a nested overflow region swallowing WeChat touch gestures.
    if (window.innerWidth <= 820) resultImage.scrollIntoView({ block: "start", behavior: "smooth" });
  }, { once: true });
  downloadButton.href = data.download_url;
  downloadButton.setAttribute("download", "");
  downloadButton.classList.remove("disabled");
  downloadButton.setAttribute("aria-disabled", "false");
  window.ProductZipDownload.prepare(
    data.download_url,
    data.download_transfer_url,
    data.download_transfer_expires_in,
  ).catch(() => {});
  const label = generateButton.querySelector("span");
  if (label) label.textContent = "重新生成";
  setBusy(false);
  remainingUses = Math.max(0, remainingUses - 1);
  // Completion only refreshes the account badge. An exhausted balance must
  // not interrupt a result the user has just paid for; the next explicit
  // image-selection action is the only place that opens the recharge dialog.
  initializeSession().catch(() => {});
}

function isMobileSaveContext() {
  return window.matchMedia("(pointer: coarse)").matches ||
    /MicroMessenger|Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
}

function openSaveDialog() {
  if (!currentResultData?.result_url) return;
  saveImage.src = `${currentResultData.result_url}?save=${Date.now()}`;
  if (!saveDialog.open) saveDialog.showModal();
}

function closePhoneSaveDialog() {
  if (saveDialog.open) saveDialog.close();
  saveImage.removeAttribute("src");
}

async function shareResultFile() {
  const response = await fetch(currentResultData.result_url, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("无法读取生成图片，请刷新页面后重试");
  const blob = await response.blob();
  const safeName = (currentResultData.product?.product_name || "商品长图")
    .replace(/[\\/:*?"<>|]/g, "-")
    .slice(0, 48);
  const file = new File([blob], `${safeName}.png`, { type: blob.type || "image/png" });
  if (!navigator.canShare({ files: [file] })) return false;
  await navigator.share({ files: [file], title: "商品长图" });
  return true;
}

async function saveResultToPhone() {
  if (!currentResultData?.result_url) return;
  clearError();
  const label = downloadButton.querySelector("span");
  const originalLabel = label.textContent;
  try {
    if (navigator.share && navigator.canShare) {
      label.textContent = "准备图片…";
      try {
        if (await shareResultFile()) return;
      } catch (error) {
        if (error?.name === "AbortError") return;
        // Some embedded browsers expose the Share API but reject file shares.
        // Keep the user in the authenticated page and fall back to long-press saving.
      }
    }
    openSaveDialog();
  } finally {
    label.textContent = originalLabel;
  }
}

function failJob(message) {
  if (eventSource) eventSource.close();
  eventSource = null;
  resultLoading.hidden = true;
  resultPlaceholder.hidden = false;
  showError(message || "生成失败，请检查服务日志后重试");
  setBusy(false);
}

function subscribe(jobId) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/${jobId}/events`);
  eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    updateProgress(data);
    if (data.status === "completed") {
      eventSource.close();
      eventSource = null;
      showResult(data);
    } else if (data.status === "failed") {
      failJob(data.error || "生成失败");
    }
  };
  eventSource.onerror = () => {
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      failJob("进度连接已断开，请重新生成");
    }
  };
}

async function restoreCurrentJob() {
  let jobId = null;
  try { jobId = window.sessionStorage.getItem(currentJobStorageKey); } catch { return; }
  if (!jobId) return;
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
  if (!response.ok) {
    try { window.sessionStorage.removeItem(currentJobStorageKey); } catch { /* ignore */ }
    return;
  }
  const data = await response.json();
  const inputResponse = await fetch(data.input_url, { cache: "no-store" });
  if (inputResponse.ok) {
    const blob = await inputResponse.blob();
    const restoredFile = new File([blob], "uploaded-product-image", { type: blob.type || "image/jpeg" });
    await setSelectedFile(restoredFile, { persist: false });
  }
  updateProgress(data);
  if (data.status === "completed") showResult(data);
  else if (data.status === "queued" || data.status === "running") {
    prepareResultState();
    setBusy(true);
    subscribe(jobId);
  } else if (data.status === "failed") failJob(data.error || "生成失败");
}

async function initializePage() {
  if (eventSource) eventSource.close();
  eventSource = null;
  try {
    await initializeSession();
    const requestedJobId = new URLSearchParams(window.location.search).get("job");
    if (/^[0-9a-f]{32}$/.test(requestedJobId || "")) {
      try { window.sessionStorage.setItem(currentJobStorageKey, requestedJobId); } catch { /* ignore */ }
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("job");
      window.history.replaceState(null, "", `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
    }
    let jobId = null;
    try { jobId = window.sessionStorage.getItem(currentJobStorageKey); } catch { /* ignore */ }
    if (jobId) await restoreCurrentJob();
    else {
      const draft = await loadDraft();
      if (draft) await setSelectedFile(draft, { persist: false });
    }
  } catch (error) {
    showError(error.message || "无法初始化前端会话");
    return;
  }
}

async function startGeneration() {
  if (!selectedFile) return;
  if (!(await hasAvailableUse())) return;
  clearError();
  setBusy(true);
  prepareResultState();
  updateProgress({ progress: 2, stage: "正在上传商品图片" });

  const formData = new FormData();
  formData.append("image", selectedFile, selectedFile.name);
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: formData });
    const payload = await response.json();
    if (response.status === 402) {
      showRechargeDialog();
      return;
    }
    if (!response.ok) throw new Error(payload.detail || "任务创建失败");
    currentServiceStatus = "processing";
    await deleteDraft();
    try { window.sessionStorage.setItem(currentJobStorageKey, payload.job_id); } catch { /* ignore */ }
    updateProgress(payload);
    subscribe(payload.job_id);
  } catch (error) {
    failJob(error.message);
  }
}

dropZone.addEventListener("click", async () => {
  if (selectedFile) {
    openPreview();
  } else if (!fileInput.disabled && await hasAvailableUse()) {
    fileInput.click();
  }
});
dropZone.addEventListener("keydown", async (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (selectedFile) openPreview();
    else if (!fileInput.disabled && await hasAvailableUse()) fileInput.click();
  }
});
fileInput.addEventListener("change", async () => {
  if (fileInput.files[0]) await setSelectedFile(fileInput.files[0]);
});
["dragenter", "dragover"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    if (!fileInput.disabled) dropZone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach((name) => {
  dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});
dropZone.addEventListener("drop", async (event) => {
  if (!fileInput.disabled && event.dataTransfer.files[0] && await hasAvailableUse()) await setSelectedFile(event.dataTransfer.files[0]);
});
clearButton.addEventListener("click", (event) => {
  event.stopPropagation();
  resetFile();
});
inputPreview.addEventListener("load", updatePreviewLayout);
expandPreview.addEventListener("click", (event) => {
  event.stopPropagation();
  openPreview();
});
closePreview.addEventListener("click", () => previewDialog.close());
previewDialog.addEventListener("click", (event) => {
  if (event.target === previewDialog) previewDialog.close();
});
window.addEventListener("resize", updatePreviewLayout);
window.addEventListener("pageshow", () => { initializeSession().catch(() => {}); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) initializeSession().catch(() => {});
});
generateButton.addEventListener("click", startGeneration);
downloadButton.addEventListener("click", async (event) => {
  if (downloadButton.classList.contains("disabled") || downloadButton.getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    return;
  }
  event.preventDefault();
  const label = downloadButton.querySelector("span");
  const originalLabel = label?.textContent;
  downloadButton.setAttribute("aria-disabled", "true");
  if (label) label.textContent = "正在下载 ZIP…";
  try {
    await window.ProductZipDownload.download(downloadButton.href, "product-images.zip");
  } catch (error) {
    showError(error.message || "ZIP 下载失败，请稍后重试");
  } finally {
    downloadButton.setAttribute("aria-disabled", "false");
    if (label) label.textContent = originalLabel;
  }
});
closeSaveDialog.addEventListener("click", closePhoneSaveDialog);
closeRechargeDialog?.addEventListener("click", () => rechargeDialog.close());
saveDialog.addEventListener("click", (event) => {
  if (event.target === saveDialog) closePhoneSaveDialog();
});

refreshIcons();
logoutButtons.forEach((logoutButton) => logoutButton.addEventListener("click", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error("退出请求失败");
    await clearClientState();
    location.replace("/?logged-out=" + Date.now());
  } catch (error) {
    showError(error.message || "退出失败，请刷新页面后重试");
  }
}));
initializePage();
