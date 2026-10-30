// 只整理页面主次，不改变检索资格，也不在切换页面时下载或调用模型。
(() => {
  if (!document.querySelector("#workbench-navigation")) return;
  const buttons = [...document.querySelectorAll("[data-workbench-view]")];
  const sections = [...document.querySelectorAll("[data-workbench-section]")];
  const indicator = document.querySelector("#workbench-current-project");
  const projectSelect = document.querySelector("#search-project");
  const emptyHint = document.querySelector("#projects-empty-hint");
  const projectContent = document.querySelector("#project-content");
  const projectName = document.querySelector("#project-current-name");
  const tabs = [...document.querySelectorAll("[data-project-tab]")];
  const panes = [...document.querySelectorAll("[data-project-pane]")];
  let projectId = projectSelect.value;
  let selectedTask = "conditions";
  const purposes = {
    search: ["查条文，核对出处", ""],
    projects: ["我的项目", "先选工程，再查看条件或上传任务书；查规范无需先建项目。"],
    library: ["规范库", "查看已准备正文与未取得资料。"],
  };

  function projectTask(task) {
    if (!["conditions", "materials"].includes(task)) return;
    selectedTask = task;
    // 不覆盖业务面板的 .hidden；项目未选择时，上传表单也不能露出来。
    if (projectContent) projectContent.hidden = !projectId;
    panes.forEach(pane => { pane.hidden = !projectId || pane.dataset.projectPane !== task; });
    tabs.forEach(tab => {
      tab.disabled = !projectId;
      tab.setAttribute("aria-pressed", String(tab.dataset.projectTab === task));
    });
  }

  function show(view) {
    if (!["search", "projects", "library"].includes(view)) return;
    document.body.dataset.workbenchView = view;
    // hidden 属性负责当前页面；原来的 .hidden 仍负责资料是否可用，两者互不覆盖。
    sections.forEach(section => { section.hidden = section.dataset.workbenchSection !== view; });
    buttons.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.workbenchView === view)));
    const title = document.querySelector("#workbench-title");
    const purpose = document.querySelector("#workbench-purpose");
    if (title) title.textContent = purposes[view][0];
    if (purpose) {
      purpose.textContent = purposes[view][1];
      purpose.hidden = !purposes[view][1];
    }
    // 提示同时取决于“当前区域”和“有没有项目”，不能被区域切换直接展开。
    projectIndicator(projectId);
    if (view === "library") {
      const question = document.querySelector("#pending-query").value.trim();
      const official = document.querySelector("#official-query");
      if (question && !official.value.trim()) official.value = question;
    }
    // 只通知展示视图；库存模块可按需读取本地清单，不启动获取或模型。
    document.dispatchEvent(new CustomEvent("archspec:workbench-view", {detail: {view}}));
  }
  function projectIndicator(projectId) {
    const name = projectSelect.selectedOptions?.[0]?.textContent || "当前项目";
    indicator.textContent = projectId
      ? `项目：${name}` : "";
    indicator.hidden = !projectId;
    emptyHint.hidden = Boolean(projectId) || document.body.dataset.workbenchView !== "projects";
    if (projectName) projectName.textContent = name;
    projectTask(selectedTask);
  }
  buttons.forEach(button => button.addEventListener("click", () => show(button.dataset.workbenchView)));
  document.querySelector("#show-project-form").addEventListener("click", () => show("projects"));
  document.addEventListener("archspec:official-source-ready", () => show("search"));
  tabs.forEach(tab => tab.addEventListener("click", () => projectTask(tab.dataset.projectTab)));
  document.addEventListener("archspec:project-change", event => {
    projectId = event.detail?.projectId || "";
    selectedTask = "conditions";
    document.querySelectorAll("[data-project-fold]").forEach(fold => { fold.open = false; });
    projectIndicator(projectId);
  });
  // 旧资料库的程序化检索仍可用：先展开所在工具，才聚焦或显示结果。
  document.querySelector("#search-form")?.addEventListener("submit", () => {
    const tools = document.querySelector("#project-advanced-tools");
    if (tools) tools.open = true;
  });
  show("search");
  projectIndicator(projectSelect.value);
})();
