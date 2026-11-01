// 实际联网发现与正式证据检索分开：找到公告不等于证明当前合规。
(() => {
  const form = document.querySelector("#official-discovery-form");
  const input = document.querySelector("#official-query");
  const button = document.querySelector("#official-discovery-button");
  const status = document.querySelector("#official-discovery-status");
  const results = document.querySelector("#official-discovery-results");
  const useProject = document.querySelector("#official-use-project");
  const refresh = document.querySelector("#official-refresh");
  const project = document.querySelector("#search-project");
  let projectGeneration = 0;
  let stopProgress = () => {};
  function historyKey() { return `archspec:official-history:${useProject.checked ? `project:${project.value}` : "global"}`; }
  function invalidate() { projectGeneration += 1; stopProgress(); stopProgress = () => {}; button.disabled = false; }
  document.addEventListener("archspec:project-change", () => {
    if (useProject.checked) {
      invalidate();
      results.replaceChildren();
      status.textContent = "项目已切换，请为当前项目重新查询官方来源。";
      restoreHistory();
    }
  });
  useProject.addEventListener("change", () => { invalidate(); results.replaceChildren(); restoreHistory(); });

  function remember(data, query) {
    if (!data.id) return;
    try {
      localStorage.setItem(historyKey(), JSON.stringify({run_id: data.id, batch_id: data.acquisition?.id || null, query}));
      localStorage.setItem("archspec:official-last-context", JSON.stringify({project_id: useProject.checked ? project.value : null}));
    } catch { /* 浏览器禁止存储时仍可使用，后台任务不受影响。 */ }
  }
  async function restoreHistory() {
    if (useProject.checked && !project.value) return;
    let saved;
    try { saved = JSON.parse(localStorage.getItem(historyKey()) || "null"); } catch { return; }
    if (!saved?.run_id) return;
    const generation = projectGeneration, key = historyKey();
    const isCurrent = () => generation === projectGeneration && key === historyKey();
    status.textContent = "正在恢复上次查询和正文准备进度，不重新下载……";
    try {
      const response = await fetch(`/api/official-discovery/runs/${encodeURIComponent(saved.run_id)}`);
      const data = await globalThis.ArchSpecHTTP.json(response, "官方来源响应暂不可读取");
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "上次查询记录不可读取，请重新查询");
      if (saved.batch_id) {
        const progress = await fetch(`/api/official-acquisitions/${encodeURIComponent(saved.batch_id)}`);
        const batch = await globalThis.ArchSpecHTTP.json(progress, "官方来源响应暂不可读取");
        if (!progress.ok) throw new Error(typeof batch.detail === "string" ? batch.detail : "上次正文进度不可读取，请重新查询");
        data.acquisition = batch;
      }
      if (!isCurrent()) return;
      input.value = saved.query || "";
      stopProgress = render(data, {query: input.value, isCurrent, historical: true});
    } catch (error) {
      if (isCurrent()) status.textContent = `恢复未完成：${error.message}；不会自动重试下载。`;
    }
  }

  function node(tag, text, className = "") {
    const element = document.createElement(tag);
    element.textContent = text;
    element.className = className;
    return element;
  }

  function sourceLink(title, url) {
    const link = node("a", title, "source-link");
    // 远程文字与链接都不可信，不能执行其中的 HTML 或脚本。
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:" && !parsed.username && !parsed.password &&
          (!parsed.port || parsed.port === "443") &&
          (["www.mohurd.gov.cn", "mohurd.gov.cn"].includes(parsed.hostname)
            || ["https://img.yichang.gov.cn/upload2022/2025/1029/20251029034657227.pdf",
                "https://img.yichang.gov.cn/upload2022/2026/0626/20260626061011241.pdf"].includes(parsed.href)
            || (parsed.hostname === "zrzy.yichang.gov.cn" && !parsed.search && !parsed.hash
              && (parsed.pathname === "/" || /^\/content-\d+-\d+-1\.html$/.test(parsed.pathname))))) {
        link.href = parsed.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
    } catch { /* 不合格链接保留名称，不可点击。 */ }
    return link;
  }

  function externalClueLink(title, url) {
    const link = node("a", title, "source-link");
    try {
      const parsed = new URL(url);
      const label = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
      if (parsed.protocol === "https:" && !parsed.username && !parsed.password
          && (!parsed.port || parsed.port === "443") && !parsed.hash
          && parsed.hostname.endsWith(".gov.cn")
          && parsed.hostname.split(".").every(part => label.test(part))) {
        link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      }
    } catch { /* 搜索线索不获任意地址权限；保留纯文字名称供人工核对。 */ }
    return link;
  }

  function renderExternalClues(data, target) {
    if (!Array.isArray(data.external_clues) && !data.external_status) return;
    const detail = node("details", "", "official-external-clues");
    const clues = (Array.isArray(data.external_clues) ? data.external_clues : [])
      .filter(item => item?.kind === "SEARCH_SNIPPET" && item.body_status === "NOT_ACQUIRED").slice(0,5);
    detail.append(node("summary", "外部官方搜索线索（"+clues.length+"）"));
    detail.append(node("p","以下是搜索摘要，未取得正文。摘要中的数值不能作为规范回答；版本、机关和适用地域仍待核对。","source-warning"));
    for (const item of clues) {
      const card = node("article", "", "official-external-clue");
      card.append(node("strong",typeof item.title === "string" ? item.title : "名称待核对"));
      card.append(node("p","官方搜索线索 · 未取得正文","official-pending-badge"));
      if (typeof item.excerpt === "string" && item.excerpt) {
        const excerpt = node("details", "", "official-evidence");
        excerpt.append(node("summary","查看搜索摘要（非正文）"),node("blockquote",item.excerpt,"clause-text"));
        card.append(excerpt);
      }
      card.append(externalClueLink("前往官方页面核对",item.url));
      detail.append(card);
    }
    if (!clues.length) detail.append(node("p","本次没有取得可核对的外部线索，不代表没有地方规定。"));
    for (const search of (Array.isArray(data.external_search) ? data.external_search : []).slice(0,3)) {
      if (typeof search.term === "string")
        detail.append(node("p",(search.status === "SKIPPED" ? "未发送公共词：" : "外部实际公共查询：")+search.term+" · "+(search.status === "SKIPPED" ? "已跳过" : search.status === "COMPLETE" ? "线索查询完成" : "未完成")));
      if (typeof search.message === "string") detail.append(node("p",search.message,"source-warning"));
    }
    target.append(detail);
  }

  // 两个入口共用同一套公告/进度卡片，避免首页把公告摘要误当成条文答案。
  function render(data, {query = input.value, isCurrent = () => true, historical = false,
      targetResults = results, targetStatus = status, onUpdate, watchProgress = true,
      readOnly = false, onRetry, retryDisabled} = {}) {
    const results = targetResults, status = targetStatus;
    results.replaceChildren();
    const cards = new Map();
    const states = {COMPLETE: "本次查询完成（不代表规范齐全）", PARTIAL: "部分查询未完成", FAILED: "官网查询失败", STALE_CACHE: "本次官网查询失败，以下是过期缓存"};
    const mode = historical ? "历史查询快照 · 未重新联网核对" : data.status === "STALE_CACHE" ? "过期缓存" : data.from_cache ? "一小时内缓存" : "实际联网查询";
    const clueOnly = ['ENTRY_ONLY', 'CLUES_ONLY'].includes(data.local_coverage);
    const local = clueOnly || data.local_coverage === 'ATTACHMENTS_DISCOVERED';
    const label = clueOnly && data.status === 'COMPLETE' ? '本次线索读取完成，尚未取得正文' : states[data.status] || '查询待核对';
    status.textContent = `${label} · ${mode} · 原始获取时间：${new Date(data.fetched_at).toLocaleString("zh-CN")}`;
    if (["FAILED","PARTIAL"].includes(data.external_status))
      status.textContent += " · 外部官方补查未完成";
    const terms = node("div", "", "official-query-summary");
    terms.append(node("strong", local ? "公共查找主题（只访问固定地方入口，不外发问题）" : "实际发送的公共查询词"));
    data.queries.forEach(q => terms.append(node("p", `${q.term} — ${q.reason}`)));
    if (data.omitted_query_count) terms.append(node("p", `另有 ${data.omitted_query_count} 个主题本次未查询，请缩小问题后再查。`, "source-warning"));
    if (data.omitted_notice_count) terms.append(node("p", `另有 ${data.omitted_notice_count} 份候选公告本次未读取。`, "source-warning"));
    (data.searches || []).forEach(search => terms.append(sourceLink(`到官网查看“${search.term}”的其他结果`, search.search_url)));
    results.append(terms);
    (data.errors || []).forEach(error => results.append(node("p", error.message, "source-warning")));
    if (clueOnly) {
      const navigation = node('article', '', 'result-card');
      navigation.append(node('strong', '地方正式正文仍未取得；以下只供继续查找。'));
      for (const hint of (Array.isArray(data.source_hints) ? data.source_hints : []).slice(0, 3)) {
        if (hint?.kind !== 'OFFICIAL_ENTRY' || hint.body_status !== 'NOT_ACQUIRED') continue;
        navigation.append(node('p', `可能文件：《${hint.target_title || '地方正式文件'}》`));
        navigation.append(sourceLink(hint.name || '地方官方入口', hint.url));
      }
      for (const clue of (Array.isArray(data.local_clues) ? data.local_clues : []).slice(0, 3)) {
        if (clue?.body_status !== 'NOT_ACQUIRED') continue;
        const detail = node('details', '', 'official-evidence');
        detail.append(node('summary', `文件线索：${clue.title || '名称待核'}（非正文）`));
        detail.append(sourceLink('查看该份官方页面', clue.url));
        detail.append(node('p', '正文、附件、版本和日期仍需取得与核对。'));
        if (clue.observation_id) detail.append(node('small', `观察编号：${clue.observation_id}；获取时间：${clue.fetched_at || '未记录'}`));
        navigation.append(detail);
      }
      results.append(navigation);
    } else if (!data.results.length) results.append(node("p", "本次没有读到可核对的标准公告，不代表该设计问题没有规范要求。", "empty-state"));
    renderExternalClues(data, results);
    data.results.forEach(item => {
      const card = node("article", "", "result-card official-notice-card");
      card.append(node("span", "公告线索 · 待复核", "official-pending-badge"));
      // 只显示共同发现合同提供的主规范判断，不用搜索摘要背书目标身份。
      if (["TARGET","ASSOCIATED","UNKNOWN"].includes(item.target_match)
          && typeof item.target_match_message === "string") {
        card.append(node("p",item.target_match_message,"official-target-assessment"));
      }
      card.append(node("h3", `${item.standard_title || item.title}${item.standard_code ? ` · ${item.standard_code}` : ""}`));
      card.append(node("p", `查询来源：${item.matched_queries.join("、")}`, "page-number"));
      if (item.version_check === "ANNOUNCEMENT_ONLY") {
        const type = item.notice_type === "REVISION" ? "局部修订公告（不是新整本标准）" : "发布公告";
        const scope = item.scope === "LOCAL" ? `地方文件 · ${item.region || '地域待核对'}`
          : item.scope === "NATIONAL" ? "国家标准" : "行业标准";
        card.append(node("p", `${type} · ${scope}`));
        card.append(node("p", `发文日期：${item.published_at || "未识别"}　实施日期：${item.effective_at || "未识别"}`));
        if (item.announcement_number) card.append(node("p", item.announcement_number, "page-number"));
        if (item.effective_state === "NOT_YET_EFFECTIVE") card.append(node("p", "尚未到公告实施日，不能按已实施规范使用。", "source-warning"));
        if (item.change_scope === "PARTIAL_CLAUSES") card.append(node("p", "公告废止旧规范的部分条文，不是整本废止；须核对具体条文清单。", "source-warning"));
        if (item.change_scope === "FULL_STANDARD") card.append(node("p", `公告有整体废止线索：${(item.withdrawn_codes || []).join("、")}。须核对日期及后续公告。`, "source-warning"));
        if (item.change_evidence || item.evidence_excerpt) {
          const detail = node("details", "", "official-evidence");
          detail.append(node("summary", "查看公告原文摘录"));
          detail.append(node("blockquote", item.change_evidence || item.evidence_excerpt, "clause-text"));
          card.append(detail);
        }
        if (item.transport_status === 'HTTP_OBSERVED') {
          const detail = node('details', '', 'official-evidence');
          detail.append(node('summary', '发布页传输情况'));
          detail.append(node('p', '登记发布页跳转为HTTP，已保存页面观察；正文附件仍只用HTTPS。页面观察不等于版本、适用性或使用许可核验。'));
          card.append(detail);
        }
      } else {
        card.append(node("p", "公告全文尚未读到或解析失败，暂不能填写版本字段。", "source-warning"));
      }
      card.append(node("p", "现行性与项目适用性尚未确认；发布过不等于截至今天仍是最新版本。", "source-warning"));
      const links = node("div", "", "official-links");
      links.append(sourceLink("查看官方公告", item.url));
      (item.attachments || []).forEach(a => links.append(sourceLink(`官网附件：${a.title}`, a.url)));
      card.append(links, node("small", "获取正文与核验版本是两件事；下面显示本次附件的准备状态。", "pending-snapshot"));
      const preparation = node("div", "", "official-acquisition-list");
      card.append(preparation);
      cards.set(item.url, preparation);
      results.append(card);
    });
    const limitations = node("details", "", "official-evidence");
    limitations.append(node("summary", "本次覆盖范围与限制"));
    (data.limitations || []).forEach(text => {
      // 历史公告快照不改写；已关联正文任务时，页面提示按当前功能展示。
      const label = data.acquisition && text.startsWith("附件仅发现链接")
        ? "公开附件的正文准备情况见任务状态；处理完成后可打开原页，也没有调用大模型"
        : text;
      limitations.append(node("p", label));
    });
    results.append(limitations);
    if (data.acquisition && globalThis.ArchspecOfficialAcquisition) {
      return globalThis.ArchspecOfficialAcquisition.watch(data.acquisition, {cards, query, isCurrent, onUpdate, readOnly, historical, pollProgress: watchProgress, onRetry, retryDisabled});
    }
    return () => {};
  }
  // 主结果区与详情共用同一政府链接校验，不增加另一套来源权限。
  globalThis.ArchspecOfficialDiscovery = {render, governmentLink: externalClueLink};

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const query = input.value.trim();
    const projectId = useProject.checked ? project.value : "";
    if (useProject.checked && !projectId) { status.textContent = "请先在“最近项目”中选择项目，或取消结合项目主题。"; return; }
    if (!query && !projectId) { status.textContent = "请填写问题或规范名称。"; return; }
    invalidate();
    const generation = projectGeneration;
    const key = historyKey();
    const isCurrent = () => generation === projectGeneration && key === historyKey();
    button.disabled = true;
    results.replaceChildren();
    status.textContent = "正在查登记的官方来源；只使用公共规范编号、题名或固定主题，不发送问题原文或资料全文……";
    try {
      const response = await fetch("/api/official-discovery/search", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({query, ...(projectId ? {project_id: projectId} : {}), refresh: refresh.checked, auto_acquire: true}),
      });
      const data = await globalThis.ArchSpecHTTP.json(response, "官方来源响应暂不可读取");
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "查询未完成，请检查问题或项目资料。");
      if (!isCurrent()) {
        if (generation === projectGeneration) status.textContent = "项目已切换，未将旧项目的结果显示在当前项目下。请重新查询。";
        return;
      }
      remember(data, query);
      stopProgress = render(data, {query, isCurrent});
    } catch (error) {
      if (isCurrent()) status.textContent = `未完成：${error.message || "无法连接服务"}`;
    } finally {
      if (generation === projectGeneration) button.disabled = false;
    }
  });
  // 关联项目的历史只在重新选择对应项目后恢复，不能挂到别的项目下。
  try {
    const last = JSON.parse(localStorage.getItem("archspec:official-last-context") || "null");
    if (last?.project_id) {
      useProject.checked = true;
      status.textContent = "上次查询关联了项目。请重新选择该项目以恢复正文准备进度。";
    }
  } catch { /* 本地历史不可用时不妨碍正常查询。 */ }
  restoreHistory();
})();
