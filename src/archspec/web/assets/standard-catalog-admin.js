const totalCount = document.querySelector("#total-count");
const verifiedCount = document.querySelector("#verified-count");
const pendingCount = document.querySelector("#pending-count");
const fulltextCount = document.querySelector("#fulltext-count");
const syncButton = document.querySelector("#sync-button");
const syncStatus = document.querySelector("#sync-status");
const syncHistory = document.querySelector("#sync-history");

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "操作没有成功，请稍后再试。");
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return "尚未完成";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

async function loadStatus() {
  const status = await request("/api/standard-catalog/status");
  totalCount.textContent = status.total;
  verifiedCount.textContent = status.verified;
  pendingCount.textContent = status.pending_or_failed;
  fulltextCount.textContent = status.with_full_text;
}

function renderHistory(runs) {
  syncHistory.replaceChildren();
  if (runs.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有同步记录。";
    syncHistory.append(empty);
    return;
  }
  runs.forEach((run) => {
    const item = document.createElement("article");
    item.className = "history-item";
    const title = document.createElement("strong");
    title.textContent = `${run.provider_name} · ${run.status}`;
    const time = document.createElement("span");
    time.textContent = formatDate(run.started_at);
    const detail = document.createElement("p");
    detail.textContent = `计划 ${run.requested_count} 条，成功 ${run.success_count} 条，失败 ${run.failure_count} 条`;
    item.append(title, time, detail);
    syncHistory.append(item);
  });
}

async function loadHistory() {
  renderHistory(await request("/api/standard-catalog/sync-runs?limit=20"));
}

async function runSync() {
  syncButton.disabled = true;
  syncStatus.textContent = "正在核验已有官方公告链接……";
  try {
    const result = await request("/api/standard-catalog/sync-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_name: "MOHURD", limit: 20 }),
    });
    syncStatus.textContent = `刷新完成：成功 ${result.success_count} 条，失败 ${result.failure_count} 条。`;
    await Promise.all([loadStatus(), loadHistory()]);
  } catch (error) {
    syncStatus.textContent = error.message;
  } finally {
    syncButton.disabled = false;
  }
}

syncButton.addEventListener("click", runSync);
Promise.all([loadStatus(), loadHistory()]).catch((error) => {
  syncStatus.textContent = `读取规范库状态失败：${error.message}`;
});

// 正文缓存与题录统计分开：待审核正文不计入正式证据库。
const officialButton = document.querySelector("#official-acquire");
const officialStatus = document.querySelector("#official-status");
const officialPreview = document.querySelector("#official-preview");

function renderOfficial(result) {
  officialStatus.textContent = `${result.cache_hit ? "复用已有处理结果" : "已保存处理结果"}：${result.page_count} 页，${result.ocr_page_count} 页使用图片文字识别，${result.chunks.length} 个片段。可打开原页查阅。最后获取：${formatDate(result.last_fetched_at)}`;
  officialPreview.replaceChildren();
  const link = document.createElement("a");
  link.href = result.page_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "查看原始官方发布页面";
  officialPreview.append(link);
  // 混合页也会识别图片里的文字，不能把它误标成只直接提取文字。
  const methodLabels = {TEXT: "直接提取文字", OCR: "图片文字识别", HYBRID: "文字与图片混合提取"};
  result.pages.filter(page => /\d+\.\d+\.\d+/.test(page.text)).slice(0, 3).forEach(page => {
    const block = document.createElement("details");
    const heading = document.createElement("summary");
    heading.textContent = `PDF 第 ${page.pdf_page} 页 · ${methodLabels[page.method] || "直接提取文字"}${page.warnings.includes("LOW_CONFIDENCE_OCR") ? " · 存在低置信度文字" : ""}`;
    const text = document.createElement("pre");
    text.style.whiteSpace = "pre-wrap";
    text.textContent = page.text;
    block.append(heading, text);
    officialPreview.append(block);
  });
}

officialButton.addEventListener("click", async () => {
  officialButton.disabled = true;
  officialStatus.textContent = "正在获取或处理正文，首次扫描件识别可能需要等待，请勿重复点击……";
  try {
    renderOfficial(await request("/api/official-documents/gb55031/acquire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh: document.querySelector("#official-refresh").checked }),
    }));
  } catch (error) {
    officialStatus.textContent = error.message;
  } finally {
    officialButton.disabled = false;
  }
});

fetch("/api/official-documents/gb55031").then(async response => {
  if (response.status === 404) {
    officialStatus.textContent = "尚未获取。首次获取会联网并处理；之后普通读取不联网、不重复识别。";
  } else if (response.ok) {
    renderOfficial({ ...(await response.json()), cache_hit: true });
  } else {
    officialStatus.textContent = "已有正文暂时无法读取，请稍后重试。";
  }
}).catch(() => { officialStatus.textContent = "无法连接本地服务。"; });
