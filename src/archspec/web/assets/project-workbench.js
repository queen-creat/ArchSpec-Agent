const projectFormPanel = document.querySelector("#project-form-panel");
const projectForm = document.querySelector("#project-form");
const projectFormStatus = document.querySelector("#project-form-status");
const projectList = document.querySelector("#project-list");
const projectSelect = document.querySelector("#search-project");
const searchForm = document.querySelector("#search-form");
const searchButton = document.querySelector("#search-button");
const searchStatus = document.querySelector("#search-status");
const resultsPanel = document.querySelector("#results-panel");
const resultList = document.querySelector("#result-list");
const resultCount = document.querySelector("#result-count");
const queryContext = document.querySelector("#query-context");
const factorArea = document.querySelector("#factor-area");
const factorList = document.querySelector("#factor-list");
const catalogSection = document.querySelector("#catalog-section");
const catalogList = document.querySelector("#catalog-list");
const reviewList = document.querySelector("#review-list");
const materialsPanel = document.querySelector("#materials-panel");
const materialProjectName = document.querySelector("#material-project-name");
const materialUploadForm = document.querySelector("#material-upload-form");
const materialUploadButton = document.querySelector("#material-upload-button");
const materialStatus = document.querySelector("#material-status");
const materialList = document.querySelector("#material-list");
const materialRecommendations = document.querySelector("#material-recommendations");
const recommendationSource = document.querySelector("#recommendation-source");
const materialTopicList = document.querySelector("#material-topic-list");
const recommendationNotice = document.querySelector("#recommendation-notice");
const materialRecommendationList = document.querySelector("#material-recommendation-list");
const projectCardTemplate = document.querySelector("#project-card-template");
const resultCardTemplate = document.querySelector("#result-card-template");
const materialCardTemplate = document.querySelector("#material-card-template");
const catalogCardTemplate = document.querySelector("#catalog-card-template");
const recommendationCardTemplate = document.querySelector("#recommendation-card-template");
const agentPanel = document.querySelector("#agent-panel");
const agentForm = document.querySelector("#agent-form");
const agentProviderState = document.querySelector("#agent-provider-state");
const agentConfigurationBox = document.querySelector("#agent-configuration");
const agentMaterialConsent = document.querySelector("#agent-material-consent");
const agentMaterialLimit = document.querySelector("#agent-material-limit");
const agentStartButton = document.querySelector("#agent-start-button");
const agentStatus = document.querySelector("#agent-status");
const agentOutput = document.querySelector("#agent-output");
const agentHistoryList = document.querySelector("#agent-history-list");

let projects = [];
let selectedProjectId = "";
let librarySearchGeneration = 0;
let projectViewGeneration = 0;

function projectOperationIsCurrent(projectId) {
  // 项目ID相同也可能经历过切走再切回；只允许起始选择代次更新共享界面。
  // 已发送操作仍归原项目完成，不取消、不重放，只阻止旧界面副作用。
  const owner = projectViewGeneration;
  return () => owner === projectViewGeneration && selectedProjectId === projectId;
}
let agentConfiguration = null;

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = response.status === 204 ? null
    : await globalThis.ArchSpecHTTP.json(response, "服务响应暂不可读取", "any");
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string"
      ? detail
      : detail?.message || "操作没有成功，请稍后重试。";
    const error = new Error(message);
    error.code = detail && typeof detail === "object" ? detail.code : null;
    error.status = response.status;
    throw error;
  }
  return body;
}

function formatDate(value) {
  if (!value) return "尚未完成";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

function agentElapsedTime(run) {
  if (!run.started_at || !run.finished_at) return "尚未结束";
  const milliseconds = Math.max(0, new Date(run.finished_at) - new Date(run.started_at));
  return `${(milliseconds / 1000).toFixed(2)} 秒`;
}

function option(label, value) {
  const element = document.createElement("option");
  element.textContent = label;
  element.value = value;
  return element;
}

function projectMeta(project) {
  const location = project.city || project.province || "地区未填写";
  return `${location} · ${project.building_type_label} · ${project.design_stage_label}`;
}

// 同标签页只保存项目身份；资料、条件和权限始终由服务器重新读取。
const selectedProjectStorageKey = "archspec:selected-project";
function savedProjectIdentity() {
  try {
    const value = globalThis.sessionStorage?.getItem(selectedProjectStorageKey);
    return typeof value === "string" && /^[A-Za-z0-9_-]{1,100}$/.test(value) ? value : "";
  } catch { return ""; }
}
function rememberProjectIdentity(projectId) {
  try {
    if (projectId) globalThis.sessionStorage?.setItem(selectedProjectStorageKey, projectId);
    else globalThis.sessionStorage?.removeItem(selectedProjectStorageKey);
  } catch { /* 存储禁用时本轮仍可使用，不改用永久保存项目资料。 */ }
}

async function selectProject(projectId, {restoring=false}={}) {
  const projectChanged = selectedProjectId !== projectId;
  selectedProjectId = projectId;
  projectSelect.value = projectId;
  rememberProjectIdentity(projectId);
  if (projectChanged) {
    projectViewGeneration += 1;
    // 文件选择框是共享的；换工程必须清空，避免把 A 的任务书误上传给 B。
    // 已发出的 A 上传仍继续归 A，后续回调有起始项目检查。
    materialUploadForm.reset();
    materialUploadButton.disabled = false;
    materialStatus.textContent = "";
    // 分析目标和发送资料同意属于原工程，不能随着共享表单带到新工程。
    agentForm.reset(); agentMaterialConsent.checked = false;
    if (typeof resetAssessmentView === "function") resetAssessmentView();
    // 人工资料库也带项目条件；切工程立即清理原问题、出处和在途展示归属。
    searchForm.querySelector('textarea[name=query]').value = "";
    invalidateLibrarySearch();
  }
  const selectionIsCurrent = projectOperationIsCurrent(projectId);
  // 通知独立的官方发现面板，避免切换项目后沿用旧项目查询结果。
  document.dispatchEvent(new CustomEvent("archspec:project-change", { detail: { projectId, restoring } }));
  document.querySelector("#dossier-panel").classList.toggle("hidden", !projectId);
  agentOutput.classList.add("hidden");
  document.querySelectorAll(".project-card").forEach((card) => {
    card.classList.toggle("active", card.dataset.projectId === projectId);
  });
  if (projectId) {
    materialRecommendations.classList.add("hidden");
    const project = projects.find((item) => item.id === projectId);
    materialProjectName.textContent = project ? project.name : "当前项目";
    materialsPanel.classList.remove("hidden");
    agentPanel.classList.remove("hidden");
    updateAgentAvailability();
    await Promise.all([loadMaterials(projectId), loadAgentRuns(projectId), loadDossier(projectId), loadAssessment(projectId)]);
  } else {
    materialsPanel.classList.add("hidden");
    agentPanel.classList.add("hidden");
    agentOutput.classList.add("hidden");
  }
  if (!selectionIsCurrent()) return;
  // 选择项目后看它的档案，不再跳到已经折叠的高级检索标题。
  const overview = document.querySelector("#dossier-panel");
  if (projectId && !overview.hidden) overview.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderMaterialRecommendations(material, report) {
  recommendationSource.textContent = `根据《${material.document_name}》自动识别`;
  recommendationNotice.textContent = report.notice;
  materialTopicList.replaceChildren();
  materialRecommendationList.replaceChildren();

  report.topics.forEach((topic) => {
    const chip = document.createElement("span");
    chip.className = "topic-chip";
    chip.textContent = `${topic.label}：${topic.matched_terms.join("、")}`;
    materialTopicList.append(chip);
  });

  if (report.standards.length === 0) {
    emptyMessage(materialRecommendationList, "暂时没有自动匹配到规范，可以在上方“快速查规范”中补充描述设计问题。");
  }
  report.standards.forEach((standard) => {
    const card = recommendationCardTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector(".recommendation-code").textContent = standard.standard_code;
    card.querySelector(".recommendation-name").textContent = standard.title;
    card.querySelector(".recommendation-reason").textContent = standard.reason;
    const availability = card.querySelector(".recommendation-availability");
    availability.textContent = standard.full_text_available ? "正文已入库" : "正文尚未入库";
    availability.classList.toggle("available", standard.full_text_available);
    const verification = document.createElement("span");
    verification.className = "recommendation-verification";
    if (standard.verification_status === "VERIFIED") {
      verification.textContent = `官方已核验${standard.verified_at ? ` · ${formatDate(standard.verified_at)}` : ""}`;
      verification.classList.add("verified");
    } else if (standard.verification_status === "FAILED") {
      verification.textContent = "最近核验失败，使用缓存";
    } else {
      verification.textContent = "题录待核验";
    }
    card.querySelector(".recommendation-topline").append(verification);
    const link = card.querySelector(".recommendation-link");
    if (standard.official_url) link.href = standard.official_url;
    else link.remove();
    materialRecommendationList.append(card);
  });
  materialRecommendations.classList.remove("hidden");
  materialRecommendations.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function recommendStandards(projectId, material, button) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  button.disabled = true;
  materialStatus.textContent = "正在读取资料并整理可能相关的规范……";
  try {
    const report = await request(`/api/projects/${encodeURIComponent(projectId)}/materials/${encodeURIComponent(material.id)}/recommendations`, {
      method: "POST",
    });
    // 请求已归属于起始工程，返回时不能把它显示到后来选择的工程里。
    if (!isCurrent()) return;
    renderMaterialRecommendations(material, report);
    materialStatus.textContent = "规范清单已生成，请到官方页面核对现行原文。";
  } catch (error) {
    if (isCurrent()) materialStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderProjects() {
  projectList.replaceChildren();
  projectSelect.replaceChildren(option("不关联具体项目", ""));
  if (projects.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "还没有项目。先建立项目，检索时就能自动带上建筑类型和设计阶段。";
    projectList.append(empty);
    return;
  }
  projects.forEach((project) => {
    projectSelect.append(option(project.name, project.id));
    const card = projectCardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.projectId = project.id;
    card.querySelector(".project-name").textContent = project.name;
    card.querySelector(".project-meta").textContent = projectMeta(project);
    card.querySelector(".project-status").textContent = project.status_label;
    card.addEventListener("click", () => selectProject(project.id));
    projectList.append(card);
  });
}

async function loadProjects(preferredProjectId = "") {
  const target = preferredProjectId || savedProjectIdentity();
  projects = await request("/api/projects?limit=20");
  // 最近列表不含旧项目时，按保存ID只读核对；不能改选列表第一项。
  if (target && !projects.some(project => project.id === target)) {
    try {
      const restored = await request(`/api/projects/${encodeURIComponent(target)}`);
      if (!restored || restored.id !== target) throw Error("原项目身份无法核对");
      projects.push(restored);
    } catch {
      renderProjects();
      const message = "项目恢复未完成：原项目暂不可读取，请重新选择项目。";
      searchStatus.textContent = message;
      document.querySelector("#pending-search-status").textContent = message;
      return false;
    }
  }
  renderProjects();
  if (target) await selectProject(target, {restoring: !preferredProjectId});
  return true;
}

function materialStateLabel(material) {
  if (material.classification_status === "CONFIRMED") return "类型已确认";
  if (material.classification_status === "NEEDS_REVIEW") return "请确认资料类型";
  return "系统自动识别";
}

function emptyMessage(container, message) {
  container.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "empty-state";
  empty.textContent = message;
  container.append(empty);
}

async function confirmMaterialType(projectId, material, select, button) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  button.disabled = true;
  materialStatus.textContent = "正在保存资料类型……";
  try {
    await request(`/api/projects/${encodeURIComponent(projectId)}/materials/${encodeURIComponent(material.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_type: select.value }),
    });
    if (!isCurrent()) return;
    materialStatus.textContent = "资料类型已确认。";
    await loadMaterials(projectId);
  } catch (error) {
    if (isCurrent()) materialStatus.textContent = error.message;
  } finally {
    if (isCurrent()) button.disabled = false;
  }
}

async function startMaterialReview(projectId, material, button) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  button.disabled = true;
  materialStatus.textContent = "正在拆分资料并查找相关规范……";
  try {
    const review = await request(`/api/projects/${encodeURIComponent(projectId)}/materials/${encodeURIComponent(material.id)}/reviews`, {
      method: "POST",
    });
    if (isCurrent()) window.location.href = `/evaluation-center.html?review=${encodeURIComponent(review.job.id)}`;
  } catch (error) {
    if (isCurrent()) materialStatus.textContent = error.message;
    if (isCurrent()) button.disabled = false;
  }
}

async function reprocessMaterial(projectId, material, button) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  button.disabled = true;
  materialStatus.textContent = "正在用最新规则重新解析资料……";
  try {
    await request(`/api/projects/${encodeURIComponent(projectId)}/materials/${encodeURIComponent(material.id)}/reprocess`, {
      method: "POST",
    });
    if (!isCurrent()) return;
    materialStatus.textContent = "重新解析完成。";
    await loadMaterials(projectId);
  } catch (error) {
    if (isCurrent()) materialStatus.textContent = error.message;
  } finally {
    if (isCurrent()) button.disabled = false;
  }
}

function renderMaterials(projectId, materials) {
  materialList.replaceChildren();
  if (materials.length === 0) {
    emptyMessage(materialList, "这个项目还没有资料。可以上传任务书、项目要求或设计说明，系统会自动分类。");
    return;
  }
  materials.forEach((material) => {
    const card = materialCardTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector(".material-name").textContent = material.document_name;
    card.querySelector(".material-reason").textContent = material.classification_reasons.join("；") || "没有识别说明";
    const state = card.querySelector(".material-state");
    state.textContent = materialStateLabel(material);
    state.classList.toggle("warning", material.classification_status === "NEEDS_REVIEW");
    const typeSelect = card.querySelector(".material-type");
    typeSelect.value = material.document_type;
    const confirmButton = card.querySelector(".confirm-material");
    confirmButton.addEventListener("click", () => confirmMaterialType(projectId, material, typeSelect, confirmButton));
    const reviewButton = card.querySelector(".start-review");
    const recommendButton = card.querySelector(".recommend-standards");
    const reprocessButton = card.querySelector(".reprocess-material");
    if (material.document_status === "FAILED") {
      reprocessButton.classList.remove("hidden");
      reprocessButton.addEventListener("click", () => reprocessMaterial(projectId, material, reprocessButton));
    }
    reviewButton.disabled = !material.can_start_review;
    recommendButton.disabled = !material.can_start_review;
    if (!material.can_start_review) reviewButton.textContent = "资料尚未解析";
    if (!material.can_start_review) recommendButton.textContent = "资料尚未解析";
    recommendButton.addEventListener("click", () => recommendStandards(projectId, material, recommendButton));
    reviewButton.addEventListener("click", () => startMaterialReview(projectId, material, reviewButton));
    materialList.append(card);
  });
}

async function loadMaterials(projectId) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  emptyMessage(materialList, "正在读取项目资料……");
  try {
    const materials = await request(`/api/projects/${encodeURIComponent(projectId)}/materials`);
    if (isCurrent()) renderMaterials(projectId, materials);
  } catch (error) {
    if (isCurrent()) emptyMessage(materialList, `资料读取失败：${error.message}`);
  }
}

async function uploadMaterial(event) {
  event.preventDefault();
  // 固定这次上传的归属：等待网络返回期间，用户可能已经切到另一个项目。
  const projectId = selectedProjectId;
  if (!projectId) {
    materialStatus.textContent = "请先选择一个项目。";
    return;
  }
  const isCurrent = projectOperationIsCurrent(projectId);
  const formData = new FormData(materialUploadForm);
  materialUploadButton.disabled = true;
  materialStatus.textContent = "正在上传、读取并自动识别资料类型……";
  try {
    await request(`/api/projects/${encodeURIComponent(projectId)}/materials`, {
      method: "POST",
      body: formData,
    });
    if (!isCurrent()) return;
    materialUploadForm.reset();
    materialStatus.textContent = "资料已整理到当前项目。";
    await Promise.all([loadMaterials(projectId), loadDossier(projectId)]);
    if (isCurrent()) document.querySelector("#assessment-status").textContent = "资料已变化。已有对照表是历史快照，请重新检索以包含新资料。";
  } catch (error) {
    if (isCurrent()) materialStatus.textContent = error.message;
  } finally {
    // 旧上传结束不能解锁新工程仍在上传的按钮。
    if (isCurrent()) materialUploadButton.disabled = false;
  }
}

function numberOrNull(value) {
  return value === "" ? null : Number(value);
}

// 项目总览只显示当前项目的资料，不将候选数字直接覆盖档案。
// 项目资料只是检索线索；转交原官方输入入口，保留工程与当前范围，不把要求当规范证据。
function stageDossierQuery(projectId, query, isCurrent, status) {
  if (!isCurrent()) return;
  try {
    if (!globalThis.ArchSpecQuestionInput?.stage) throw Error("规范查阅入口尚未就绪，请刷新页面后再试。");
    const opened = globalThis.ArchSpecQuestionInput.stage({projectId, query});
    if (!opened) throw Error("问题尚未带入，请先核对当前任务状态。");
    document.querySelector('[data-workbench-view="search"]')?.click?.();
    const message = "已带入项目资料中的待查内容；请核对范围后主动查条文或整理，项目要求不是规范依据。";
    document.querySelector('#pending-search-status').textContent = message;
    status.textContent = message;
    document.querySelector('#pending-search-panel')?.scrollIntoView?.({block:'start',behavior:'auto'});
    document.querySelector('#pending-query')?.focus?.({preventScroll:true});
  } catch (error) { if (isCurrent()) status.textContent = error.message || "待查内容暂不可打开。"; }
}

async function loadDossier(projectId) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  const content = document.querySelector("#dossier-content");
  emptyMessage(content, "正在整理项目条件与要求……");
  document.querySelector("#dossier-report-link").href = `/api/projects/${encodeURIComponent(projectId)}/dossier/report.html`;
  try {
    const dossier = await request(`/api/projects/${encodeURIComponent(projectId)}/dossier`);
    if (!isCurrent()) return;
    content.replaceChildren();
    const metrics = document.createElement("div");
    metrics.className = "dossier-metrics";
    [["项目资料", dossier.materials.length], ["设计要求", dossier.requirements.length], ["核查专题", dossier.topics.length]].forEach(([label, count]) => {
      const card = appendTextElement(metrics, "div", "dossier-metric", "");
      appendTextElement(card, "strong", "", count);
      appendTextElement(card, "span", "", label);
    });
    content.append(metrics);
    if (dossier.missing_fields.length) appendTextElement(content, "p", "context-note", `档案待补充：${dossier.missing_fields.join("、")}`);
    const grid = document.createElement("div");
    grid.className = "dossier-grid";
    const conditions = appendTextElement(grid, "section", "", "");
    appendTextElement(conditions, "h3", "", "资料中的条件候选");
    if (!dossier.conditions.length) appendTextElement(conditions, "p", "empty-state", "上传任务书后自动识别明确数值条件。");
    dossier.conditions.forEach((item) => {
      const card = appendTextElement(conditions, "article", "dossier-item", "");
      appendTextElement(card, "strong", "", `${item.label}：${item.value} ${item.unit}`);
      appendTextElement(card, "p", "", `原文：${item.source_text} · ${item.source_name}`);
      const button = appendTextElement(card, "button", "secondary-button", "确认并写入项目档案");
      button.type = "button";
      button.addEventListener("click", async () => {
        if (!isCurrent()) return;
        button.disabled = true;
        try {
          await request(`/api/projects/${encodeURIComponent(projectId)}`, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({[item.field]: item.value})});
          if (isCurrent()) await loadDossier(projectId);
        } catch (error) { if (isCurrent()) { button.textContent = error.message; button.disabled = false; } }
      });
    });
    const topics = appendTextElement(grid, "section", "", "");
    appendTextElement(topics, "h3", "", "建议核对的专题");
    if (!dossier.topics.length) appendTextElement(topics, "p", "empty-state", "尚无明确专题线索，可以上传资料或手动查规范。");
    dossier.topics.forEach((item) => {
      const card = appendTextElement(topics, "article", "dossier-item", "");
      appendTextElement(card, "strong", "", item.label);
      appendTextElement(card, "p", "", `${item.reason} 来源：${item.source_names.join("、")}`);
      const button = appendTextElement(card, "button", "secondary-button", "带到规范查阅");
      const status = appendTextElement(card, "p", "reader-section-note", "");status.setAttribute('role','status');
      button.type = "button";
      button.addEventListener("click", () => stageDossierQuery(projectId,item.query,isCurrent,status));
    });
    content.append(grid);
    const details = appendTextElement(content, "details", "dossier-requirements", "");
    appendTextElement(details, "summary", "", `查看全部设计要求（${dossier.requirements.length} 条）`);
    appendTextElement(details,"p","reader-section-note","勾选要核对的要求，按首页当前检索范围保存到本项目问题清单。每次最多10条，之后逐题查阅。");
    const batchBar=appendTextElement(details,"div","qa-history-toolbar","");
    const batchAdd=appendTextElement(batchBar,"button","secondary-button","加入问题清单（0）");batchAdd.type='button';batchAdd.setAttribute('id','dossier-worklist-add');batchAdd.disabled=true;
    const batchOpen=appendTextElement(batchBar,"button","quiet-button","查看问题清单");batchOpen.type='button';
    const batchStatus=appendTextElement(details,"p","reader-section-note","");batchStatus.setAttribute('role','status');batchStatus.setAttribute('aria-live','polite');
    const choices=[];
    function paintBatch(){
      const count=choices.filter(row=>row.box.checked).length;batchAdd.disabled=count===0||count>10;
      batchAdd.textContent='加入问题清单（'+count+'）';
    }
    batchAdd.addEventListener('click',()=>{
      if(!isCurrent())return;
      try{
        const selected=choices.filter(row=>row.box.checked);
        if(!globalThis.ArchSpecQuestionWorklist?.enqueue)throw Error('问题清单尚未就绪，请刷新页面后再试。');
        const result=globalThis.ArchSpecQuestionWorklist.enqueue({projectId,queries:selected.map(row=>row.query)});
        selected.forEach(row=>{row.box.checked=false;});paintBatch();
        batchStatus.textContent='已加入'+result.added+'题。'+(result.skipped?'相同问题和范围跳过'+result.skipped+'题。':'')+(result.persistent?'已保存在当前浏览器；可打开问题清单逐题查阅。':'浏览器无法保存，仅本页有效。');
      }catch(error){if(isCurrent())batchStatus.textContent=error.message||'本次要求暂不可加入清单。';}
    });
    batchOpen.addEventListener('click',()=>{
      if(!isCurrent())return;
      document.querySelector('[data-workbench-view="search"]')?.click?.();
      const list=document.querySelector('#question-worklist');if(list){list.open=true;list.scrollIntoView?.({block:'start',behavior:'auto'});}
    });
    dossier.requirements.forEach((item) => {
      const card = appendTextElement(details, "article", "dossier-item", "");
      const label=appendTextElement(card,"label","official-qa-project-choice-label","");
      const box=appendTextElement(label,"input","","");box.type='checkbox';box.checked=false;box.setAttribute('aria-label','选入问题清单：'+item.text.slice(0,120));
      appendTextElement(label,"span","","选入问题清单");choices.push({box,query:item.query});
      box.addEventListener('change',()=>{
        if(!isCurrent())return;paintBatch();const selected=choices.filter(row=>row.box.checked).length;
        const source=document.querySelector('#pending-source');const range=source?.selectedOptions?.[0]?.textContent||'首页当前检索范围';
        batchStatus.textContent=selected>10?'每次最多选择10条，可分批加入。':'已选'+selected+'条；范围：'+range+'。加入后不会自动查询。';
      });
      appendTextElement(card, "p", "", item.text);
      appendTextElement(card, "small", "", `来源：${item.source_name}`);
      const button = appendTextElement(card,"button","secondary-button","按这条要求查规范");button.type='button';
      const status = appendTextElement(card,"p","reader-section-note","");status.setAttribute('role','status');
      button.addEventListener('click',()=>stageDossierQuery(projectId,item.query,isCurrent,status));
    });
    updateAssessmentReportLink();
  } catch (error) { if (isCurrent()) emptyMessage(content, `总览读取失败：${error.message}`); }
}

async function createProject(event) {
  event.preventDefault();
  const data = new FormData(projectForm);
  const payload = {
    name: data.get("name").trim(),
    project_code: data.get("project_code").trim() || null,
    city: data.get("city").trim() || null,
    building_type: data.get("building_type"),
    design_stage: data.get("design_stage"),
    height_m: numberOrNull(data.get("height_m")),
    above_ground_floors: numberOrNull(data.get("above_ground_floors")),
  };
  const submit = projectForm.querySelector("button[type=submit]");
  submit.disabled = true;
  projectFormStatus.textContent = "正在保存项目……";
  try {
    const created = await request("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    projectForm.reset();
    projectFormPanel.classList.add("hidden");
    await loadProjects(created.id);
    projectFormStatus.textContent = "";
  } catch (error) {
    projectFormStatus.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
}

function sourceIdentity(result) {
  if (result.standard_code) return `${result.standard_code} · ${result.standard_title || result.regulation_name}`;
  return result.standard_title || result.regulation_name;
}

function renderFactors(searchResponse) {
  factorList.replaceChildren();
  const factors = [
    ...searchResponse.project_factors.map((item) => `${item.label}：${item.value}`),
    ...searchResponse.material_factors.map((item) => `资料：${item.name}（${item.type_label}）`),
  ];
  factorArea.classList.toggle("hidden", factors.length === 0);
  factors.forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "factor-chip";
    chip.textContent = text;
    factorList.append(chip);
  });
}

function renderCatalogSuggestions(suggestions) {
  catalogList.replaceChildren();
  catalogSection.classList.toggle("hidden", suggestions.length === 0);
  suggestions.forEach((suggestion) => {
    const card = catalogCardTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector(".catalog-code").textContent = suggestion.standard_code;
    card.querySelector(".catalog-name").textContent = suggestion.title;
    card.querySelector(".catalog-reason").textContent = suggestion.reason;
    const link = card.querySelector(".catalog-link");
    if (suggestion.official_url) link.href = suggestion.official_url;
    else link.remove();
    catalogList.append(card);
  });
}

function renderSearchResults(searchResponse) {
  resultList.replaceChildren();
  resultCount.textContent = `已核验正文 ${searchResponse.results.length} 条`;
  queryContext.textContent = `检索问题：${searchResponse.query_context}`;
  searchStatus.textContent = ArchSpecAdmissionUI.searchNotice(searchResponse);
  renderFactors(searchResponse);
  renderCatalogSuggestions(searchResponse.catalog_suggestions || []);
  if (searchResponse.results.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = ArchSpecAdmissionUI.searchNotice(searchResponse);
    resultList.append(empty);
  }
  searchResponse.results.forEach((result) => {
    const card = resultCardTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector(".standard-name").textContent = sourceIdentity(result);
    const state = card.querySelector(".source-state");
    state.textContent = ArchSpecAdmissionUI.evidenceLabel(result);
    state.classList.toggle("warning", result.is_citable !== true);
    card.querySelector(".clause-title").textContent = `第 ${result.clause_number} 条`;
    card.querySelector(".clause-path").textContent = result.heading_path || "章节信息未标注";
    card.querySelector(".clause-text").textContent = result.text;
    card.querySelector(".page-number").textContent = result.page_start ? `原文第 ${result.page_start} 页` : "页码未标注";
    card.querySelector(".source-warning").textContent = result.source_warning || "";
    const link = card.querySelector(".source-link");
    if (result.source_url) link.href = result.source_url;
    else link.remove();
    resultList.append(card);
  });
  resultsPanel.classList.remove("hidden");
  resultsPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function invalidateLibrarySearch() {
  // 项目、问题、专业或新提交共用一个代次，旧响应与finally都不能写进新查询。
  librarySearchGeneration += 1;
  resultsPanel.classList.add("hidden");
  resultList.replaceChildren(); resultCount.textContent = ""; queryContext.textContent = "";
  factorList.replaceChildren(); factorArea.classList.add("hidden");
  catalogList.replaceChildren(); catalogSection.classList.add("hidden");
  searchStatus.textContent = ""; searchButton.disabled = false;
}

async function searchRegulations(event) {
  event.preventDefault();
  invalidateLibrarySearch();
  const data = new FormData(searchForm), owner = selectedProjectId, own = librarySearchGeneration;
  const query = data.get("query").trim(), specialty = data.get("specialty") || null;
  const isCurrent = () => {
    const current = new FormData(searchForm);
    return own === librarySearchGeneration && owner === selectedProjectId
      && current.get("query").trim() === query && (current.get("specialty") || null) === specialty;
  };
  searchButton.disabled = true;
  searchStatus.textContent = "正在从规范库中查找相关条文……";
  try {
    const response = await request("/api/regulations/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({query, project_id: owner || null, specialty, top_k: 8}),
    });
    if (isCurrent()) renderSearchResults(response);
  } catch (error) {
    if (isCurrent()) searchStatus.textContent = error.message;
  } finally {
    if (isCurrent()) searchButton.disabled = false;
  }
}

function agentRunStatusLabel(status) {
  return {
    CREATED: "已创建",
    RUNNING: "分析中",
    WAITING_HUMAN: "等待人工确认",
    COMPLETED: "人工确认完成",
    FAILED: "运行失败",
  }[status] || status;
}

function agentFindingStatusLabel(status) {
  return {
    SUPPORTED: "有规范依据",
    RISK: "发现风险",
    INSUFFICIENT_EVIDENCE: "证据不足",
    NEEDS_DRAWING: "需要结合图纸",
  }[status] || status;
}

function agentOverallStatusLabel(status) {
  return {
    DRAFT: "审查草稿",
    INSUFFICIENT_EVIDENCE: "部分证据不足",
    NEEDS_DRAWING: "需要结合图纸",
  }[status] || status;
}

function decisionLabel(decision) {
  return {
    ACCEPTED: "已采纳",
    REJECTED: "已驳回",
    NEEDS_REVIEW: "待进一步复核",
  }[decision] || decision;
}

function agentErrorCodeMessage(code, fallback = "模型或工具调用失败，请稍后重试。") {
  return {
    LLM_NOT_CONFIGURED: "服务端尚未配置 DeepSeek API Key，请完成配置后再运行。",
    INVALID_API_KEY: "DeepSeek API Key 无效，请在服务端更换后重启。",
    INSUFFICIENT_BALANCE: "DeepSeek 账户余额不足，请充值或更换可用账户。",
    RATE_LIMITED: "DeepSeek 当前请求较多，请稍后重试。",
    NETWORK_ERROR: "暂时无法连接 DeepSeek，请检查服务器网络后重试。",
    PROVIDER_ERROR: "DeepSeek 服务暂时异常，请稍后重试。",
    PROVIDER_OVERLOADED: "DeepSeek 服务繁忙，请稍后重试。",
    STEP_LIMIT_REACHED: "Agent 已达到最大执行步数并安全停止，请把分析目标写得更具体。",
    TOOL_CALL_LIMIT_REACHED: "Agent 已达到工具调用次数上限并安全停止，请缩小分析范围。",
    REPEATED_TOOL_CALL: "Agent 连续请求了相同工具，系统已阻止循环；可以调整目标后重试。",
    INVALID_MODEL_OUTPUT: "模型返回的结果不符合约定结构，可以重新运行一次。",
  }[code] || fallback;
}

function updateAgentAvailability() {
  const configured = Boolean(agentConfiguration?.configured);
  agentStartButton.disabled = !selectedProjectId || !configured;
  if (!selectedProjectId) {
    agentStatus.textContent = "请先在“最近项目”中选择一个项目。";
  } else if (!configured) {
    agentStatus.textContent = "服务端尚未配置 DeepSeek API Key，暂时不能启动 AI 分析。";
  } else {
    agentStatus.textContent = "已选择项目，可以填写目标并开始分析。";
  }
}

async function loadAgentStatus() {
  try {
    agentConfiguration = await request("/api/agent/status");
    agentProviderState.textContent = agentConfiguration.configured
      ? `${agentConfiguration.provider} 已连接`
      : `${agentConfiguration.provider} 未配置`;
    agentProviderState.classList.toggle("warning", !agentConfiguration.configured);
    agentConfigurationBox.textContent = agentConfiguration.configured
      ? `模型：${agentConfiguration.model}；最多自主执行 ${agentConfiguration.max_steps} 轮、调用 ${agentConfiguration.max_tool_calls} 次只读工具。API Key 只保存在服务端，不会进入浏览器。`
      : "尚未配置 API Key。请在服务端环境变量中配置后重启，网页不会要求填写或显示密钥。";
    agentMaterialLimit.textContent = `最多发送 ${agentConfiguration.material_character_limit} 个项目资料字符，不发送原文件；勾选即确认资料可以发送给 DeepSeek。`;
  } catch (error) {
    agentProviderState.textContent = "配置读取失败";
    agentProviderState.classList.add("warning");
    agentConfigurationBox.textContent = `AI 配置读取失败：${error.message}`;
  }
  updateAgentAvailability();
}

function appendTextElement(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text;
  parent.append(element);
  return element;
}

function renderCitation(citation) {
  const box = document.createElement("article");
  box.className = "agent-citation";
  const identity = [citation.standard_code, citation.standard_title || citation.regulation_name]
    .filter(Boolean).join(" · ");
  appendTextElement(box, "strong", "agent-citation-source", identity || "规范正文");
  appendTextElement(box, "p", "source-warning", ArchSpecAdmissionUI.evidenceLabel(citation));
  appendTextElement(
    box,
    "span",
    "agent-citation-clause",
    `第 ${citation.clause_number} 条${citation.page_start ? ` · 第 ${citation.page_start} 页` : ""}`,
  );
  appendTextElement(box, "p", "agent-citation-text", citation.text);
  if (citation.source_warning) {
    appendTextElement(box, "p", "source-warning", citation.source_warning);
  }
  if (citation.source_url) {
    const link = appendTextElement(box, "a", "source-link", "查看记录的来源页面");
    link.href = citation.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return box;
}

async function saveAgentDecision(runId, findingIndex, decision, button) {
  const projectId = selectedProjectId, isCurrent = projectOperationIsCurrent(selectedProjectId);
  if (!projectId) return;
  const buttons = button.parentElement.querySelectorAll("button");
  buttons.forEach((item) => { item.disabled = true; });
  agentStatus.textContent = "正在保存人工判断……";
  try {
    const detail = await request(`/api/agent-runs/${encodeURIComponent(runId)}/findings/${findingIndex}/decision`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, comment: null }),
    });
    if (!isCurrent()) return;
    if (detail.run.project_id !== projectId) throw new Error("返回的人工判断不属于当前项目。");
    renderAgentDetail(detail);
    await loadAgentRuns(projectId);
    if (!isCurrent()) return;
    agentStatus.textContent = detail.run.status === "COMPLETED"
      ? "所有结论已完成人工确认，本次运行已归档。"
      : "人工判断已保存，请继续确认其他结论。";
  } catch (error) {
    if (isCurrent()) {
      agentStatus.textContent = `保存失败：${error.message}`;
      buttons.forEach((item) => { item.disabled = false; });
    }
  }
}

function renderFinding(run, finding, index, decisionByIndex) {
  const card = document.createElement("article");
  card.className = "agent-finding";
  const top = document.createElement("div");
  top.className = "agent-finding-top";
  appendTextElement(top, "h4", "", finding.title);
  const badge = appendTextElement(top, "span", "agent-finding-state", agentFindingStatusLabel(finding.status));
  badge.dataset.status = finding.status;
  card.append(top);
  appendTextElement(card, "p", "agent-finding-statement", finding.statement);
  appendTextElement(card, "p", "agent-action", `建议处理：${finding.suggested_action}`);
  if (finding.guard_message) {
    appendTextElement(card, "p", "agent-guard-message", `证据保护提示：${finding.guard_message}`);
  }

  const citations = document.createElement("div");
  citations.className = "agent-citations";
  if ((finding.citations || []).length === 0) {
    appendTextElement(citations, "p", "empty-state", "没有可核验的正文证据，系统不会把它包装成确定结论。");
  } else {
    finding.citations.forEach((citation) => citations.append(renderCitation(citation)));
  }
  card.append(citations);

  const existing = decisionByIndex.get(index);
  const decisionBox = document.createElement("div");
  decisionBox.className = "agent-decisions";
  if (existing) {
    appendTextElement(decisionBox, "strong", "agent-decision-saved", `人工判断：${decisionLabel(existing.decision)}`);
  } else if (run.status === "WAITING_HUMAN") {
    [
      ["ACCEPTED", "采纳"],
      ["REJECTED", "驳回"],
      ["NEEDS_REVIEW", "待进一步复核"],
    ].forEach(([value, label]) => {
      const button = appendTextElement(decisionBox, "button", "secondary-button", label);
      button.type = "button";
      button.addEventListener("click", () => saveAgentDecision(run.id, index, value, button));
    });
  }
  card.append(decisionBox);
  return card;
}

function renderAgentSteps(steps) {
  const details = document.createElement("details");
  details.className = "agent-steps";
  appendTextElement(details, "summary", "", `查看技术执行记录（${steps.length} 步）`);
  const list = document.createElement("ol");
  steps.forEach((step) => {
    const item = document.createElement("li");
    appendTextElement(item, "strong", "", `${step.sequence}. ${step.name}`);
    appendTextElement(item, "span", "", ` · ${step.duration_ms} ms · ${step.status}`);
    const data = document.createElement("pre");
    data.textContent = JSON.stringify({ input: step.input_summary, output: step.output_summary }, null, 2);
    item.append(data);
    list.append(item);
  });
  details.append(list);
  return details;
}

function renderAgentDetail(detail) {
  const { run, steps, decisions } = detail;
  agentOutput.replaceChildren();
  agentOutput.classList.remove("hidden");
  const heading = document.createElement("div");
  heading.className = "agent-result-heading";
  appendTextElement(heading, "div", "agent-result-status", agentRunStatusLabel(run.status));
  appendTextElement(heading, "span", "", `运行于 ${formatDate(run.started_at || run.created_at)}`);
  agentOutput.append(heading);

  if (run.status === "FAILED") {
    appendTextElement(agentOutput, "h3", "", "本次分析没有完成");
    appendTextElement(
      agentOutput,
      "p",
      "agent-error",
      agentErrorCodeMessage(run.error_code, run.error_message || undefined),
    );
    const retry = appendTextElement(agentOutput, "button", "secondary-button", "重新运行");
    retry.type = "button";
    retry.addEventListener("click", () => retryAgentRun(run.id, retry));
    agentOutput.append(renderAgentSteps(steps));
    return;
  }

  const result = run.final_result;
  if (!result) {
    appendTextElement(agentOutput, "p", "empty-state", "本次运行还没有形成结果。请稍后重新查看。");
    agentOutput.append(renderAgentSteps(steps));
    return;
  }
  appendTextElement(agentOutput, "h3", "", "AI 审查草稿");
  appendTextElement(agentOutput, "p", "agent-summary", result.summary);
  appendTextElement(agentOutput, "p", "agent-overall", `整体判断：${agentOverallStatusLabel(result.overall_status)}`);
  const decisionByIndex = new Map(decisions.map((item) => [item.finding_index, item]));
  const findings = document.createElement("div");
  findings.className = "agent-findings";
  result.findings.forEach((finding, index) => findings.append(renderFinding(run, finding, index, decisionByIndex)));
  agentOutput.append(findings);

  if ((result.limitations || []).length > 0) {
    const limitations = document.createElement("section");
    limitations.className = "agent-limitations";
    appendTextElement(limitations, "strong", "", "使用边界");
    const list = document.createElement("ul");
    result.limitations.forEach((text) => appendTextElement(list, "li", "", text));
    limitations.append(list);
    agentOutput.append(limitations);
  }
  appendTextElement(
    agentOutput,
    "p",
    "agent-usage",
    `模型：${run.actual_model || run.requested_model} · Token：输入 ${run.prompt_tokens} / 输出 ${run.completion_tokens}${run.cached_tokens ? ` / 缓存 ${run.cached_tokens}` : ""} · 用时 ${agentElapsedTime(run)}`,
  );
  agentOutput.append(renderAgentSteps(steps));
  agentOutput.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function showAgentRun(runId) {
  const projectId = selectedProjectId;
  if (!projectId) return;
  const isCurrent = projectOperationIsCurrent(projectId);

  agentStatus.textContent = "正在读取分析记录……";
  try {
    const detail = await request(`/api/agent-runs/${encodeURIComponent(runId)}`);
    if (!isCurrent()) return;
    if (detail.run.project_id !== projectId) throw new Error("这条分析记录不属于当前项目。");
    renderAgentDetail(detail);
    agentStatus.textContent = "已打开运行记录。";
  } catch (error) {
    if (isCurrent()) agentStatus.textContent = `记录读取失败：${error.message}`;
  }
}

function renderAgentRuns(runs) {
  agentHistoryList.replaceChildren();
  if (runs.length === 0) {
    emptyMessage(agentHistoryList, "当前项目还没有 AI 分析记录。第一次运行后会保留目标、结果、证据和人工判断。 ");
    return;
  }
  runs.forEach((run) => {
    const button = document.createElement("button");
    button.className = "agent-run-card";
    button.type = "button";
    appendTextElement(button, "strong", "", run.goal);
    appendTextElement(button, "span", "", `${agentRunStatusLabel(run.status)} · ${formatDate(run.created_at)}`);
    button.addEventListener("click", () => showAgentRun(run.id));
    agentHistoryList.append(button);
  });
}

async function loadAgentRuns(projectId) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  emptyMessage(agentHistoryList, "正在读取 AI 分析记录……");
  try {
    const runs = await request(`/api/projects/${encodeURIComponent(projectId)}/agent-runs?limit=8`);
    if (isCurrent()) renderAgentRuns(runs);
  } catch (error) {
    if (isCurrent()) emptyMessage(agentHistoryList, `运行记录读取失败：${error.message}`);
  }
}

function agentErrorMessage(error) {
  if (error.status === 429 && !error.code) return agentErrorCodeMessage("RATE_LIMITED");
  return agentErrorCodeMessage(error.code, error.message);
}

async function startAgentRun(event) {
  event.preventDefault();
  const projectId = selectedProjectId;
  if (!projectId) {
    agentStatus.textContent = "请先选择一个项目。";
    return;
  }
  const isCurrent = projectOperationIsCurrent(projectId);
  const data = new FormData(agentForm);
  const includeMaterials = agentMaterialConsent.checked;
  agentStartButton.disabled = true;
  agentStatus.textContent = "AI 正在查看项目、调用只读工具并核对证据，请不要重复点击……";
  try {
    const detail = await request(`/api/projects/${encodeURIComponent(projectId)}/agent-runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal: data.get("goal").trim(),
        include_project_materials: includeMaterials,
        material_sharing_confirmed: includeMaterials,
      }),
    });
    if (!isCurrent()) return;
    if (detail.run.project_id !== projectId) throw new Error("返回的分析记录不属于当前项目。");
    renderAgentDetail(detail);
    await loadAgentRuns(projectId);
    if (!isCurrent()) return;
    agentStatus.textContent = detail.run.status === "FAILED"
      ? "本次分析没有完成，请查看失败原因后重试。"
      : "AI 草稿已生成。请逐条选择采纳、驳回或待复核。";
  } catch (error) {
    if (isCurrent()) agentStatus.textContent = `分析失败：${agentErrorMessage(error)}`;
  } finally {
    if (isCurrent()) updateAgentAvailability();
  }
}

async function retryAgentRun(runId, button) {
  const projectId = selectedProjectId;
  if (!projectId) return;
  const isCurrent = projectOperationIsCurrent(projectId);

  button.disabled = true;
  agentStatus.textContent = "正在重新运行分析……";
  try {
    const detail = await request(`/api/agent-runs/${encodeURIComponent(runId)}/retry`, { method: "POST" });
    if (!isCurrent()) return;
    if (detail.run.project_id !== projectId) throw new Error("返回的分析记录不属于当前项目。");
    renderAgentDetail(detail);
    await loadAgentRuns(projectId);
    if (!isCurrent()) return;
    agentStatus.textContent = detail.run.status === "FAILED"
      ? "重新运行仍未完成，请查看新的失败原因。"
      : "重新运行完成，请核对新结果。";
  } catch (error) {
    if (isCurrent()) agentStatus.textContent = `重新运行失败：${agentErrorMessage(error)}`;
    button.disabled = false;
  }
}

function reviewLink(item) {
  const link = document.createElement("a");
  link.className = "text-link";
  if (item.can_resume) {
    link.textContent = "继续确认";
    link.href = `/evaluation-center.html?review=${encodeURIComponent(item.id)}`;
  } else if (item.status === "COMPLETED") {
    link.textContent = "查看报告";
    link.href = `/api/reviews/${encodeURIComponent(item.id)}/report.html`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  } else {
    link.textContent = item.status_label;
    link.removeAttribute("href");
  }
  return link;
}

async function loadReviews() {
  const reviews = await request("/api/reviews?limit=6");
  reviewList.replaceChildren();
  if (reviews.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "还没有审查记录。上传项目要求或设计说明后，可以发起逐条规范审查。";
    reviewList.append(empty);
    return;
  }
  reviews.forEach((item) => {
    const card = document.createElement("article");
    card.className = "review-card";
    const title = document.createElement("h3");
    title.textContent = item.task_document_name;
    const progress = document.createElement("span");
    progress.className = "review-progress";
    progress.textContent = `${item.status_label} · 已确认 ${item.confirmed}/${item.total}`;
    const time = document.createElement("span");
    time.className = "review-time";
    time.textContent = `开始于 ${formatDate(item.started_at)}`;
    card.append(title, progress, time, reviewLink(item));
    reviewList.append(card);
  });
}

document.querySelector("#show-project-form").addEventListener("click", () => {
  projectFormPanel.classList.remove("hidden");
  projectForm.querySelector("input[name=name]").focus();
});
document.querySelector("#close-project-form").addEventListener("click", () => projectFormPanel.classList.add("hidden"));
document.querySelector("#close-material-recommendations").addEventListener("click", () => materialRecommendations.classList.add("hidden"));
projectForm.addEventListener("submit", createProject);
projectSelect.addEventListener("change", () => selectProject(projectSelect.value));
materialUploadForm.addEventListener("submit", uploadMaterial);
searchForm.addEventListener("submit", searchRegulations);
searchForm.querySelector('textarea[name=query]').addEventListener("input", invalidateLibrarySearch);
searchForm.querySelector('select[name=specialty]').addEventListener("change", invalidateLibrarySearch);
agentForm.addEventListener("submit", startAgentRun);
document.querySelectorAll(".query-examples button").forEach((button) => {
  button.addEventListener("click", () => {
    const query = searchForm.querySelector("textarea[name=query]");
    query.value = button.textContent;
    query.dispatchEvent(new Event("input", {bubbles: true}));
  });
});

// 阅读/联网恢复必须等待当前项目身份核对，避免先恢复全局历史再切项目。
const initialProjectSelection = loadProjects();
globalThis.ArchspecProjectSelectionReady = initialProjectSelection.then(ready => ready !== false, () => false);
Promise.all([initialProjectSelection, loadReviews(), loadAgentStatus()]).catch((error) => {
  searchStatus.textContent = `页面数据读取失败：${error.message}`;
});
