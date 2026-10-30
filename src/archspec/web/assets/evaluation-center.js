const taskSelect = document.querySelector("#task-select");
const regulationCount = document.querySelector("#regulation-count");
const regulationList = document.querySelector("#regulation-list");
const startButton = document.querySelector("#start-button");
const pageStatus = document.querySelector("#page-status");
const requirementsSection = document.querySelector("#requirements-section");
const requirementsList = document.querySelector("#requirements-list");
const summarySection = document.querySelector("#summary-section");
const summaryBox = document.querySelector("#summary-box");
const reportLink = document.querySelector("#report-link");
const requirementTemplate = document.querySelector("#requirement-template");
const retrievalStatus = document.querySelector("#retrieval-status");

let confirmedCount = 0;
let regulationDocuments = [];
let currentReviewId = null;

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "操作没有成功，请稍后重试。");
  }
  return response.status === 204 ? null : response.json();
}

function showRetrievalStatus(mode, message = "") {
  const labels = {
    HYBRID_RRF_V1: "混合检索已启用",
    BM25_FALLBACK: "关键词检索回退",
    BM25_V1: "关键词检索",
  };
  const label = labels[mode] || "检索状态未知";
  retrievalStatus.dataset.mode = mode;
  retrievalStatus.querySelector("span:last-child").textContent = message
    ? `检索方式：${label}。${message}`
    : `检索方式：${label}`;
}

async function loadRetrievalStatus() {
  const status = await request("/api/assisted-labeling/retrieval-status");
  showRetrievalStatus(status.mode, status.message);
}

function option(label, value) {
  const item = document.createElement("option");
  item.textContent = label;
  item.value = value;
  return item;
}

function fillSelect(select, documents, preferredRole, placeholder) {
  select.replaceChildren(option(placeholder, ""));
  const ordered = [...documents].sort((left, right) => {
    return Number(right.role === preferredRole) - Number(left.role === preferredRole);
  });
  ordered.forEach((documentItem) => {
    const suffix = documentItem.role === preferredRole ? "" : `（${documentItem.role_label}）`;
    select.append(option(`${documentItem.original_name}${suffix}`, documentItem.id));
  });
}

async function loadDocuments(selected = {}) {
  const documents = await request("/api/assisted-labeling/documents");
  const taskCandidates = documents.filter((item) => item.role !== "REGULATION");
  fillSelect(taskSelect, taskCandidates, "TASK_BRIEF", "请选择设计任务书");
  regulationDocuments = documents.filter((item) => item.role === "REGULATION");
  regulationCount.textContent = regulationDocuments.length === 0
    ? "还没有可用的建筑规范"
    : `已识别 ${regulationDocuments.length} 份建筑规范，系统会自动检索全部规范。`;
  regulationList.replaceChildren();
  regulationDocuments.forEach((item) => {
    const row = document.createElement("li");
    row.textContent = item.original_name;
    regulationList.append(row);
  });
  if (selected.task) taskSelect.value = selected.task;
}

async function uploadFile(input, role, selectKey) {
  const file = input.files[0];
  if (!file) return;
  pageStatus.textContent = `正在上传“${file.name}”……`;
  const form = new FormData();
  form.append("file", file);
  try {
    const documentItem = await request("/api/documents", { method: "POST", body: form });
    await request(`/api/assisted-labeling/documents/${documentItem.id}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    });
    const selected = { task: selectKey === "task" ? documentItem.id : taskSelect.value };
    await loadDocuments(selected);
    pageStatus.textContent = `“${file.name}”已上传并识别。`;
  } catch (error) {
    pageStatus.textContent = error.message;
  } finally {
    input.value = "";
  }
}

function candidateOption(requirementId, suggestion, index) {
  const label = document.createElement("label");
  label.className = "candidate-option";
  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = `requirement-${requirementId}`;
  radio.value = suggestion.clause_number;
  radio.dataset.regulationDocumentId = suggestion.regulation_document_id;
  if (index === 0) radio.checked = true;
  const content = document.createElement("span");
  const number = document.createElement("span");
  number.className = "clause-number";
  const standardIdentity = suggestion.standard_code
    ? `${suggestion.standard_code} · ${suggestion.standard_title || suggestion.regulation_name}`
    : suggestion.regulation_name;
  number.textContent = `${standardIdentity} · 第 ${suggestion.clause_number} 条`;
  const source = document.createElement("span");
  source.className = suggestion.source_warning
    ? "source-badge source-unverified"
    : "source-badge source-verified";
  source.textContent = suggestion.source_warning || "官方来源与版本已记录";
  const heading = document.createElement("span");
  heading.className = "clause-heading";
  heading.textContent = suggestion.heading_path || "章节信息未标注";
  const text = document.createElement("span");
  text.className = "clause-text";
  text.textContent = suggestion.text;
  content.append(number, source, heading, text);
  label.append(radio, content);
  return label;
}

async function confirmRequirement(step, card, message) {
  const selected = card.querySelector("input[type=radio]:checked");
  if (!selected) {
    message.textContent = "请先选择一条你认为正确的规范条文。";
    return;
  }
  const button = card.querySelector(".confirm-button");
  button.disabled = true;
  message.textContent = "正在保存人工确认结果……";
  try {
    const job = await request(`/api/reviews/${currentReviewId}/steps/${step.id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        regulation_document_id: selected.dataset.regulationDocumentId,
        clause_numbers: [selected.value],
      }),
    });
    confirmedCount += 1;
    card.querySelector(".state-badge").textContent = "已确认";
    card.querySelector(".state-badge").classList.add("confirmed");
    card.querySelectorAll("input, button").forEach((element) => { element.disabled = true; });
    message.textContent = `已确认第 ${selected.value} 条，并加入检索评测。`;
    updateSummary(job.status === "COMPLETED");
  } catch (error) {
    button.disabled = false;
    message.textContent = error.message;
  }
}

async function markNoEvidence(step, card, message) {
  const button = card.querySelector(".no-evidence-button");
  button.disabled = true;
  try {
    const job = await request(`/api/reviews/${currentReviewId}/steps/${step.id}/no-evidence`, {
      method: "POST",
    });
    card.querySelector(".state-badge").textContent = "暂未找到依据";
    card.querySelectorAll("input, button").forEach((element) => { element.disabled = true; });
    message.textContent = "已记录。本条不会被当成正确答案。";
    updateSummary(job.status === "COMPLETED");
  } catch (error) {
    button.disabled = false;
    message.textContent = error.message;
  }
}

function renderReviewStep(step, index) {
  const card = requirementTemplate.content.firstElementChild.cloneNode(true);
  card.querySelector(".requirement-order").textContent = `设计要求 ${index + 1}`;
  const badge = card.querySelector(".state-badge");
  badge.textContent = step.status_label;
  if (step.status === "CONFIRMED") badge.classList.add("confirmed");
  card.querySelector(".source-heading").textContent = step.heading_path || "任务书正文";
  card.querySelector(".requirement-text").textContent = step.requirement_text;
  const list = card.querySelector(".candidate-list");
  const message = card.querySelector(".card-message");
  requirementsList.append(card);

  if (step.status === "CONFIRMED") {
    confirmedCount += 1;
    list.textContent = "这条要求已经人工确认过。";
    card.querySelector(".card-actions").classList.add("hidden");
    return;
  }

  if (step.status === "NO_EVIDENCE") {
    list.textContent = "这条要求已经标记为待人工核查。";
    card.querySelector(".card-actions").classList.add("hidden");
    return;
  }

  if (step.candidates.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-candidates";
    empty.textContent = "当前规范中没有找到明显相关的条文。";
    list.append(empty);
    card.querySelector(".confirm-button").disabled = true;
  } else {
    showRetrievalStatus(step.candidates[0].retrieval_mode);
    step.candidates.forEach((item, suggestionIndex) => {
      list.append(candidateOption(step.id, item, suggestionIndex));
    });
  }

  card.querySelector(".confirm-button").addEventListener("click", () => {
    confirmRequirement(step, card, message);
  });
  card.querySelector(".no-evidence-button").addEventListener("click", () => {
    markNoEvidence(step, card, message);
  });
}

function updateSummary(completed = false) {
  summaryBox.textContent = completed
    ? `本次审查已完成，其中 ${confirmedCount} 条设计要求确认了规范依据；审查记录已经保存。`
    : confirmedCount === 0
    ? "尚未确认条文。"
    : `已经确认 ${confirmedCount} 条设计要求，可以检查系统能否把这些条文检索出来。`;
  if (completed && currentReviewId) {
    reportLink.href = `/api/reviews/${currentReviewId}/report.html`;
    reportLink.classList.remove("hidden");
  } else {
    reportLink.removeAttribute("href");
    reportLink.classList.add("hidden");
  }
}

async function startAutomaticReview() {
  const taskId = taskSelect.value;
  if (!taskId) {
    pageStatus.textContent = "请先选择一份设计任务书。";
    return;
  }
  if (regulationDocuments.length === 0) {
    pageStatus.textContent = "请先上传至少一份建筑规范。";
    return;
  }
  startButton.disabled = true;
  pageStatus.textContent = "正在重新识别文档结构并整理设计要求……";
  requirementsList.replaceChildren();
  confirmedCount = 0;
  reportLink.removeAttribute("href");
  reportLink.classList.add("hidden");
  try {
    await request(`/api/documents/${taskId}/reprocess`, { method: "POST" });
    await Promise.all(regulationDocuments.map((item) => (
      request(`/api/documents/${item.id}/reprocess`, { method: "POST" })
    )));
    await request(`/api/assisted-labeling/documents/${taskId}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "TASK_BRIEF" }),
    });
    const review = await request("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_document_id: taskId }),
    });
    currentReviewId = review.job.id;
    requirementsSection.classList.remove("hidden");
    summarySection.classList.remove("hidden");
    pageStatus.textContent = `已从任务书中整理出 ${review.steps.length} 条设计要求，并保存审查记录。`;
    for (const [index, step] of review.steps.entries()) {
      renderReviewStep(step, index);
    }
    updateSummary();
    requirementsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    pageStatus.textContent = error.message;
  } finally {
    startButton.disabled = false;
  }
}

async function restoreReviewFromAddress() {
  const reviewId = new URLSearchParams(window.location.search).get("review");
  if (!reviewId) return;

  pageStatus.textContent = "正在恢复上次的审查记录……";
  const review = await request(`/api/reviews/${encodeURIComponent(reviewId)}`);
  currentReviewId = review.job.id;
  confirmedCount = 0;
  taskSelect.value = review.job.task_document_id;
  requirementsList.replaceChildren();
  requirementsSection.classList.remove("hidden");
  summarySection.classList.remove("hidden");
  review.steps.forEach((step, index) => renderReviewStep(step, index));
  updateSummary(review.job.status === "COMPLETED");
  pageStatus.textContent = `已恢复审查记录，共 ${review.steps.length} 条设计要求。`;
  requirementsSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

document.querySelector("#upload-task").addEventListener("click", () => document.querySelector("#task-file").click());
document.querySelector("#upload-regulation").addEventListener("click", () => document.querySelector("#regulation-file").click());
document.querySelector("#task-file").addEventListener("change", (event) => uploadFile(event.target, "TASK_BRIEF", "task"));
document.querySelector("#regulation-file").addEventListener("change", (event) => uploadFile(event.target, "REGULATION", "regulation"));
startButton.addEventListener("click", startAutomaticReview);

async function initializePage() {
  await Promise.all([loadDocuments(), loadRetrievalStatus()]);
  await restoreReviewFromAddress();
}

initializePage().catch((error) => {
  pageStatus.textContent = error.message;
});
