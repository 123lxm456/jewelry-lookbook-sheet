(function () {
  "use strict";

  const userAgent = navigator.userAgent || "";
  const isWechat = /MicroMessenger/i.test(userAgent);
  const isAndroid = /Android/i.test(userAgent);
  const isIOS = /iPhone|iPad|iPod/i.test(userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isMobile = isAndroid || isIOS || /Mobile/i.test(userAgent);
  const transferCache = new Map();

  function filenameFromDisposition(value, fallback) {
    if (!value) return fallback;
    const utf8 = value.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
    if (utf8) {
      try {
        return decodeURIComponent(utf8[1].trim().replace(/^"|"$/g, ""));
      } catch (_) {
        // Continue with the ASCII fallback when a proxy mangles the header.
      }
    }
    const ascii = value.match(/filename\s*=\s*"([^"]+)"/i);
    return ascii?.[1] || fallback;
  }

  async function fetchZip(url, fallbackFilename) {
    const response = await fetch(url, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/zip" },
    });
    if (!response.ok) {
      let message = "ZIP 下载失败，请稍后重试";
      try {
        const payload = await response.json();
        if (payload.detail) message = payload.detail;
      } catch (_) {
        // The response status is sufficient if an intermediary returned HTML.
      }
      throw new Error(message);
    }
    const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
    if (!contentType.startsWith("application/zip")) {
      throw new Error("服务器未返回 ZIP 文件，请刷新页面后重试");
    }
    const blob = await response.blob();
    const filename = filenameFromDisposition(
      response.headers.get("Content-Disposition"), fallbackFilename || "product-images.zip",
    );
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  }

  function normalizedDownloadUrl(downloadUrl) {
    const parsed = new URL(downloadUrl, window.location.href);
    const match = parsed.pathname.match(/^(.*\/api\/jobs\/[0-9a-f]{32})\/download$/i);
    if (!match || parsed.origin !== window.location.origin) {
      throw new Error("当前任务下载地址无效，请刷新页面后重试");
    }
    return { key: parsed.href, endpoint: `${match[1]}/download-transfer` };
  }

  function externalOpenUrl(transferUrl) {
    const parsed = new URL(transferUrl, window.location.href);
    parsed.pathname = parsed.pathname.replace("/api/download-transfer/", "/download-open/");
    return parsed.href;
  }

  async function requestTransfer(downloadUrl) {
    const { key, endpoint } = normalizedDownloadUrl(downloadUrl);
    const response = await fetch(endpoint, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.transfer_url) {
      throw new Error(payload.detail || "无法创建跨浏览器下载链接");
    }
    const entry = {
      url: new URL(payload.transfer_url, window.location.href).href,
      expiresAt: Date.now() + Math.max(0, Number(payload.expires_in || 0)) * 1000,
    };
    transferCache.set(key, entry);
    return entry;
  }

  function cachedTransfer(downloadUrl) {
    const { key } = normalizedDownloadUrl(downloadUrl);
    const entry = transferCache.get(key);
    return entry?.url && entry.expiresAt - Date.now() > 30_000 ? entry : null;
  }

  function prepare(downloadUrl, transferUrl, expiresIn) {
    if (!isMobile) return Promise.resolve(null);
    const { key } = normalizedDownloadUrl(downloadUrl);
    if (transferUrl) {
      const entry = {
        url: new URL(transferUrl, window.location.href).href,
        expiresAt: Date.now() + Math.max(0, Number(expiresIn || 0)) * 1000,
      };
      transferCache.set(key, entry);
      return Promise.resolve(entry);
    }
    const current = cachedTransfer(downloadUrl);
    if (current) return Promise.resolve(current);
    const pending = transferCache.get(key)?.promise;
    if (pending) return pending;
    const promise = requestTransfer(downloadUrl).catch((error) => {
      transferCache.delete(key);
      throw error;
    });
    transferCache.set(key, { promise });
    return promise;
  }

  function androidIntentUrl(url) {
    const target = new URL(url);
    const scheme = target.protocol.replace(":", "");
    return `intent://${target.host}${target.pathname}${target.search}` +
      `#Intent;scheme=${scheme};action=android.intent.action.VIEW;` +
      "category=android.intent.category.BROWSABLE;" +
      `S.browser_fallback_url=${encodeURIComponent(url)};end`;
  }

  function shareOpenUrl(openUrl) {
    if (typeof navigator.share !== "function") return false;
    navigator.share({
      title: "下载商品视觉图片",
      text: "在所选浏览器中打开后，将直接下载本次任务的 6 张图片 ZIP。",
      url: openUrl,
    }).catch((error) => {
      if (error?.name !== "AbortError") window.location.assign(openUrl);
    });
    return true;
  }

  function openAndroidSystemPicker(openUrl) {
    try {
      window.location.href = androidIntentUrl(openUrl);
    } catch (_) {
      window.location.assign(openUrl);
      return;
    }
    // WeChat commonly swallows intent:// without navigating or hiding the
    // document. Move to the signed handoff page; its top-right instruction
    // opens this exact URL externally and then starts the original ZIP.
    window.setTimeout(() => {
      if (document.visibilityState === "visible") window.location.assign(openUrl);
    }, 900);
  }

  async function download(url, fallbackFilename) {
    if (!isMobile) {
      await fetchZip(url, fallbackFilename);
      return;
    }

    const prepared = cachedTransfer(url);
    if (!prepared) {
      const entry = await prepare(url);
      window.location.assign(externalOpenUrl(entry.url));
      return;
    }
    const openUrl = externalOpenUrl(prepared.url);
    if (isAndroid) {
      openAndroidSystemPicker(openUrl);
      return;
    }
    if (shareOpenUrl(openUrl)) return;
    window.location.assign(openUrl);
  }

  window.ProductZipDownload = {
    download,
    prepare,
    filenameFromDisposition,
    environment: { isWechat, isAndroid, isIOS, isMobile },
  };
}());
