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
const resultImage = document.getElementById("resultImage");
const downloadButton = document.getElementById("downloadButton");
const currentUser = document.getElementById("currentUser");
const logoutButton = document.getElementById("logoutButton");

const allowedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxBytes = 20 * 1024 * 1024;
const currentJobStorageKey = "jewelry.currentJobId";
const draftDatabaseName = "jewelry-workflow";
const draftStoreName = "uploads";
let selectedFile = null;
let previewUrl = null;
let eventSource = null;
let currentUserId = null;

async function initializeSession() {
  const response = await fetch("/api/session", { cache: "no-store" });
  if (!response.ok) throw new Error("无法初始化前端会话");
  const data = await response.json();
  if (!data.session_id) throw new Error("前端会话标识无效");
  if (!data.user_id) throw new Error("当前用户标识无效");
  currentUserId = String(data.user_id);
  if (currentUser && data.username) currentUser.textContent = data.username;
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
    return new File([value.blob], value.name || "uploaded-jewelry-image", { type: value.type || value.blob.type });
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
  resultPlaceholder.hidden = true;
  resultImage.hidden = true;
  resultImage.removeAttribute("src");
  resultLoading.hidden = false;
  downloadButton.classList.add("disabled");
  downloadButton.setAttribute("aria-disabled", "true");
  downloadButton.removeAttribute("download");
  downloadButton.href = "#";
}

function showResult(data) {
  resultLoading.hidden = true;
  resultImage.src = `${data.result_url}?v=${Date.now()}`;
  resultImage.hidden = false;
  downloadButton.href = data.download_url;
  downloadButton.setAttribute("download", "");
  downloadButton.classList.remove("disabled");
  downloadButton.setAttribute("aria-disabled", "false");
  setBusy(false);
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
    const restoredFile = new File([blob], "uploaded-jewelry-image", { type: blob.type || "image/jpeg" });
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
  clearError();
  setBusy(true);
  prepareResultState();
  updateProgress({ progress: 2, stage: "正在上传珠宝图片" });

  const formData = new FormData();
  formData.append("image", selectedFile, selectedFile.name);
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: formData });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "任务创建失败");
    await deleteDraft();
    try { window.sessionStorage.setItem(currentJobStorageKey, payload.job_id); } catch { /* ignore */ }
    updateProgress(payload);
    subscribe(payload.job_id);
  } catch (error) {
    failJob(error.message);
  }
}

dropZone.addEventListener("click", () => {
  if (selectedFile) {
    openPreview();
  } else if (!fileInput.disabled) {
    fileInput.click();
  }
});
dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (selectedFile) openPreview();
    else if (!fileInput.disabled) fileInput.click();
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
  if (!fileInput.disabled && event.dataTransfer.files[0]) await setSelectedFile(event.dataTransfer.files[0]);
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
generateButton.addEventListener("click", startGeneration);
downloadButton.addEventListener("click", (event) => {
  if (downloadButton.classList.contains("disabled")) event.preventDefault();
});

refreshIcons();
logoutButton?.addEventListener("click", async (event) => {
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
});
initializePage();
