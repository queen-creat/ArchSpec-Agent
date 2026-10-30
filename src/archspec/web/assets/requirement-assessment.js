// 单独管理要求对照表；每次查看读取已保存的结果，不自动重新跑检索。
const assessmentStart = document.querySelector("#assessment-start");
const assessmentStatus = document.querySelector("#assessment-status");
const assessmentResults = document.querySelector("#assessment-results");
const assessmentFilter = document.querySelector("#assessment-filter");
const assessmentHistory = document.querySelector("#assessment-history");
let currentAssessment = null;

function resetAssessmentView() {
  // 项目切换或取消选择时同步清理共享视图，不请求空项目，也不保留旧批次入口。
  currentAssessment = null;
  assessmentResults.replaceChildren();
  assessmentHistory.replaceChildren();
  assessmentStart.disabled = !selectedProjectId;
  assessmentStatus.textContent = selectedProjectId ? "正在读取历史对照表……" : "请先选择项目。";
  updateAssessmentReportLink();
}

async function loadAssessment(projectId) {
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!isCurrent()) return;
  resetAssessmentView();
  assessmentStatus.textContent = "正在读取历史对照表……";
  try {
    const result = await request(`/api/projects/${encodeURIComponent(projectId)}/requirement-assessments/latest`);
    if (!isCurrent()) return;
    currentAssessment = result;
    await loadAssessmentHistory(projectId, isCurrent);
    if (!isCurrent()) return;
    renderAssessment();
    assessmentStatus.textContent = result
      ? `历史快照：${formatDate(result.created_at)}。资料或档案更新后请重新检索。`
      : "尚未生成对照表。上传任务书后点击上方按钮即可。";
  } catch (error) {
    if (isCurrent()) assessmentStatus.textContent = error.message;
  }
}

function renderAssessment() {
  assessmentResults.replaceChildren();
  if (!currentAssessment) return;
  const projectId = selectedProjectId, assessmentId = currentAssessment.id;
  const isCurrentProject = projectOperationIsCurrent(projectId);
  const isCurrent = () => isCurrentProject() && currentAssessment?.id === assessmentId;
  const counts = currentAssessment.summary;
  const decisionByIndex = new Map((currentAssessment.decisions || []).map((item) => [item.item_index, item]));
  const progress = currentAssessment.review_summary;
  if (progress) appendTextElement(assessmentResults, "p", "context-note", `人工处理：${progress.processed}/${progress.total} · 已确认依据 ${progress.confirmed} 条 · 待进一步核查 ${progress.needs_check} 条。`);
  appendTextElement(assessmentResults, "p", "context-note", `共 ${counts.total} 条要求：${counts.candidate_evidence} 条有候选正文，${counts.catalog_only} 条仅有目录，${counts.no_evidence} 条未找到依据。`);
  appendTextElement(assessmentResults, "p", "section-description", currentAssessment.notice);
  const items = currentAssessment.items.filter((item) => {
    const decision = decisionByIndex.get(item.index);
    if (assessmentFilter.value === "GAPS") return item.status !== "CANDIDATE_EVIDENCE";
    if (assessmentFilter.value === "PENDING") return !decision;
    if (assessmentFilter.value === "NEEDS_CHECK") return decision?.action === "NEEDS_CHECK";
    return true;
  });
  if (!items.length) appendTextElement(assessmentResults, "p", "empty-state", "此筛选范围没有要求。候选条文仍需逐条复核。");
  const labels = {CANDIDATE_EVIDENCE: "候选正文 · 待核查", CATALOG_ONLY: "只有目录 · 缺少正文", NO_EVIDENCE: "未找到依据"};
  items.forEach((item) => {
    const card = appendTextElement(assessmentResults, "article", "assessment-item", "");
    const top = appendTextElement(card, "div", "section-heading", "");
    appendTextElement(top, "h4", "", `${item.index + 1}. ${item.requirement.text}`);
    const badge = appendTextElement(top, "span", "method-badge", labels[item.status]);
    badge.classList.toggle("warning", item.status !== "CANDIDATE_EVIDENCE");
    appendTextElement(card, "p", "assessment-source", `要求出处：${item.requirement.source_name}`);
    item.search.results.forEach((evidence) => {
      const box = appendTextElement(card, "details", "assessment-evidence", "");
      appendTextElement(box, "summary", "", `${evidence.standard_code || evidence.regulation_name} · 第 ${evidence.clause_number} 条${evidence.page_start ? ` · 第 ${evidence.page_start} 页` : ""}`);
      appendTextElement(box, "blockquote", "", evidence.text);
      if (evidence.source_warning) appendTextElement(box, "p", "source-warning", evidence.source_warning);
      appendTextElement(box, "p", "assessment-source", `检索方法：${evidence.retrieval_mode} · 当前记录状态：${evidence.lifecycle_status}`);
      addAssessmentSource(box, evidence.source_url);
    });
    if (!item.search.results.length) {
      appendTextElement(card, "p", "empty-state", item.status === "CATALOG_ONLY"
        ? "可先通过以下官方链接核对规范名称、版本和原文。目录匹配不能作为符合规范的证据。"
        : "当前规范库没有找到相关正文。请检查要求表达、规范库覆盖范围，并由专业人员补充核查。");
    }
    item.search.catalog_suggestions.forEach((catalog) => {
      const box = appendTextElement(card, "div", "assessment-catalog", "");
      appendTextElement(box, "strong", "", `${catalog.standard_code} · ${catalog.title}`);
      appendTextElement(box, "p", "", catalog.reason);
      addAssessmentSource(box, catalog.official_url);
    });
    const query = appendTextElement(card, "button", "secondary-button", "调整问题后重新查找");
    query.type = "button";
    query.addEventListener("click", () => {
      if (!isCurrent()) return;
      const input = searchForm.querySelector("textarea[name=query]");
      input.value = item.requirement.query;
      // 程序回填与手动改问共用失效入口，不能留下上一题的人工检索结果。
      input.dispatchEvent(new Event("input", {bubbles: true}));
      searchForm.scrollIntoView({behavior: "smooth", block: "center"});
    });
    renderRequirementDecision(card, item, decisionByIndex.get(item.index));
  });
}

function renderRequirementDecision(card, item, existing) {
  // 按钮属于绘制时的项目和批次；旧节点不能拿当前项目身份发出新的人工决定。
  const projectId = selectedProjectId, assessmentId = currentAssessment?.id;
  const isCurrentProject = projectOperationIsCurrent(projectId);
  const isCurrent = () => isCurrentProject() && !!assessmentId && currentAssessment?.id === assessmentId;
  const box = appendTextElement(card, "div", "requirement-decision", "");
  const labels = {CONFIRMED: "已确认候选依据", NEEDS_CHECK: "待进一步核查", NOT_APPLICABLE: "本项目不适用"};
  if (existing) appendTextElement(box, "p", "decision-current", `已保存：${labels[existing.action]} · ${formatDate(existing.updated_at)}`);
  const evidenceLabel = appendTextElement(box, "label", "field", "");
  appendTextElement(evidenceLabel, "span", "", "选择经你核对的候选条文");
  const evidenceSelect = document.createElement("select");
  evidenceSelect.append(option("请选择候选依据", ""));
  item.search.results.forEach((evidence, index) => evidenceSelect.append(option(`${evidence.standard_code || evidence.regulation_name} · 第 ${evidence.clause_number} 条`, String(index))));
  evidenceSelect.disabled = item.search.results.length === 0;
  if (existing?.evidence_index != null) evidenceSelect.value = String(existing.evidence_index);
  evidenceLabel.append(evidenceSelect);
  const commentLabel = appendTextElement(box, "label", "field", "");
  appendTextElement(commentLabel, "span", "", "复核意见（待核查 / 不适用时必填）");
  const comment = document.createElement("textarea");
  comment.rows = 2;
  comment.maxLength = 1000;
  comment.value = existing?.comment || "";
  comment.placeholder = "例如：缺少平面图，需要核对入口坡道尺寸。";
  commentLabel.append(comment);
  const feedback = appendTextElement(box, "p", "inline-status", "");
  feedback.setAttribute("role", "status");
  const actions = appendTextElement(box, "div", "agent-decisions", "");
  [["CONFIRMED", "确认所选依据"], ["NEEDS_CHECK", "标记待核查"], ["NOT_APPLICABLE", "标记不适用"]].forEach(([action, label]) => {
    const button = appendTextElement(actions, "button", "secondary-button", label);
    button.type = "button";
    if (action === "CONFIRMED" && item.search.results.length === 0) button.disabled = true;
    button.addEventListener("click", async () => {
      if (!isCurrent()) return;
      if (action === "CONFIRMED" && evidenceSelect.value === "") { feedback.textContent = "请先选择你已核对的候选条文。"; return; }
      if (action !== "CONFIRMED" && !comment.value.trim()) { feedback.textContent = "请写明待核查或不适用的原因。"; return; }
      button.disabled = true;
      feedback.textContent = "正在保存……";
      try {
        const result = await request(`/api/projects/${encodeURIComponent(projectId)}/requirement-assessments/${encodeURIComponent(assessmentId)}/items/${item.index}/decision`, {
          method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action, evidence_index: action === "CONFIRMED" ? Number(evidenceSelect.value) : null, comment: comment.value.trim()}),
        });
        if (isCurrent()) {
          currentAssessment = result;
          renderAssessment();
          assessmentStatus.textContent = "人工意见已保存，将进入该批次报告。";
        }
      } catch (error) {
        if (isCurrent()) { feedback.textContent = error.message; button.disabled = false; }
      }
    });
  });
}

async function loadAssessmentHistory(projectId, isCurrent = projectOperationIsCurrent(projectId)) {
  if (!isCurrent()) return;
  const rows = await request(`/api/projects/${encodeURIComponent(projectId)}/requirement-assessments`);
  if (!isCurrent()) return;
  assessmentHistory.replaceChildren();
  if (!rows.length) assessmentHistory.append(option("暂无历史记录", ""));
  rows.forEach((row) => assessmentHistory.append(option(`${formatDate(row.created_at)} · ${row.total} 条要求`, row.id)));
  if (currentAssessment) assessmentHistory.value = currentAssessment.id;
  updateAssessmentReportLink();
}

function updateAssessmentReportLink() {
  if (!selectedProjectId) return;
  const suffix = currentAssessment ? `?assessment_id=${encodeURIComponent(currentAssessment.id)}` : "";
  document.querySelector("#dossier-report-link").href = `/api/projects/${encodeURIComponent(selectedProjectId)}/dossier/report.html${suffix}`;
}

assessmentHistory.addEventListener("change", async () => {
  const projectId = selectedProjectId;
  const assessmentId = assessmentHistory.value;
  const isCurrentProject = projectOperationIsCurrent(projectId);
  const isCurrent = () => isCurrentProject() && assessmentId === assessmentHistory.value;
  if (!projectId || !assessmentId || !isCurrent()) return;
  try {
    const result = await request(`/api/projects/${encodeURIComponent(projectId)}/requirement-assessments/${encodeURIComponent(assessmentId)}`);
    if (!isCurrent()) return;
    currentAssessment = result;
    renderAssessment();
    updateAssessmentReportLink();
    assessmentStatus.textContent = "已打开历史批次，报告和人工复核对应这个批次。";
  } catch (error) { if (isCurrent()) assessmentStatus.textContent = error.message; }
});

function addAssessmentSource(container, value) {
  // 来源只允许普通网页地址，上传字符串不会成为可执行链接。
  if (!value || !/^https?:\/\//i.test(value)) return;
  const link = appendTextElement(container, "a", "source-link", "打开来源页面");
  link.href = value;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
}

assessmentStart.addEventListener("click", async () => {
  const projectId = selectedProjectId;
  const isCurrent = projectOperationIsCurrent(projectId);
  if (!projectId || !isCurrent()) return;
  assessmentStart.disabled = true;
  assessmentStatus.textContent = "正在逐条查找依据；要求较多时需要等待，请勿重复点击……";
  try {
    const result = await request(`/api/projects/${encodeURIComponent(projectId)}/requirement-assessments`, {method: "POST"});
    if (!isCurrent()) return;
    currentAssessment = result;
    await loadAssessmentHistory(projectId, isCurrent);
    if (!isCurrent()) return;
    renderAssessment();
    assessmentStatus.textContent = `已保存 ${result.summary.total} 条要求的证据快照，可在完整报告中查看和打印。`;
  } catch (error) {
    if (isCurrent()) assessmentStatus.textContent = `本次检索未完成：${error.message}`;
  } finally { if (isCurrent()) assessmentStart.disabled = false; }
});
assessmentFilter.addEventListener("change", renderAssessment);
