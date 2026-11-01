// 只展示后台任务，不替系统批准规范版本。下载/识别不占用页面提交请求。
(() => {
  const active = new Set(["QUEUED", "DOWNLOADING", "PROCESSING"]);
  const stateLabels = {ORIGINAL_ONLY: "仅原件/待辨认", QUEUED: "排队中", DOWNLOADING: "正在下载", PROCESSING: "正在识别与切分", READY: "正文已准备", FAILED: "获取失败", INTERRUPTED: "处理被中断", NEEDS_CONFIRMATION: "附件用途待确认", NO_ATTACHMENT: "未找到可获取附件", SKIPPED: "本次不自动获取", LIMIT_REACHED: "达到排队上限"};
  const kindLabels = {FULL_TEXT: "规范正文", REVISION: "修订附件（不是整本规范）", WITHDRAWAL: "废止条文清单（不是规范正文）", UNCONFIRMED: "用途未确认的附件"};
  function node(tag, text, className = "") {
    const element = document.createElement(tag);
    element.textContent = text;
    element.className = className;
    return element;
  }
  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await globalThis.ArchSpecHTTP.json(response, "正文进度响应暂不可读取");
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "后台进度请求未完成");
    return data;
  }

  function watch(initial, {cards, query, isCurrent, onUpdate, readOnly = false, historical = false, pollProgress = true, onRetry, retryDisabled = () => false}) {
    let batch = initial, timer = null, stopped = false, stateRevision = 0, latestPoll = 0, restoredReader = false;
    const current = () => !stopped && isCurrent();
    const storagePaused = () => batch.worker_status === "STORAGE_UNAVAILABLE";
    function stop() { stopped = true; if (timer !== null) clearTimeout(timer); }
    function schedule() {
      if (timer !== null) clearTimeout(timer);
      if (!readOnly && pollProgress && current() && !storagePaused() && (batch.items || []).some(item => active.has(item.state))) timer = setTimeout(poll, 2000);
    }
    function render() {
      if (!current()) return;
      for (const container of cards.values()) container.replaceChildren();
      if (storagePaused()) {
        // 存储持续不可写时，重复点击重试无法恢复；已保存的正文仍可试查。
        const warning = batch.worker_error || "正文准备已暂停；请检查本地存储并重启服务，旧成功缓存保留。";
        for (const container of cards.values()) container.append(node("p", warning, "source-warning"));
      }
      (batch.items || []).forEach(item => {
        const container = cards.get(item.notice_url);
        if (!container) return;
        const row = node("section", "", "official-acquisition-item");
        row.append(node("strong", `${kindLabels[item.kind] || "正文准备"} · ${stateLabels[item.state] || "状态待核对"}`));
        if (item.title) row.append(node("p", item.title, "page-number"));
        if (item.reason || item.error) row.append(node("p", item.error || item.reason, "source-warning"));
        const progress = item.progress || {};
        if (progress.page_count && progress.page_number) row.append(node("p", `处理进度：第 ${progress.page_number} / ${progress.page_count} 页`, "page-number"));
        if (item.state === "ORIGINAL_ONLY") {
          row.append(node("p", "获取受限，请手动查看；官方PDF原件已保存，未生成可检索条文。", "source-warning"));
          const view = node("button", "查看原件与原页", "secondary-button");
          view.type = "button";
          view.addEventListener("click", () => {
            if(current())void globalThis.ArchSpecOriginalReader?.open(item);
          });
          row.append(view);
          // 历史展示可恢复同一pin；是否允许手动重试仍由readOnly单独控制。
          const saved=globalThis.ArchspecPendingSearch?.readingPosition?.();
          if(historical && !restoredReader && saved?.kind==="ORIGINAL_ONLY"
            && saved.source_id===item.source_id && saved.snapshot_id===item.snapshot_id){
            restoredReader=true;
            if(current())void globalThis.ArchSpecOriginalReader?.open(item);
          }
        }
        if (item.state === "READY") {
          if (item.review_status === "PENDING_RECOGNITION") row.append(node("p", "已取得文字层 · 图片待辨认，请查看原页对照。", "source-warning"));
          row.append(node("p", `${item.cache_hit ? "复用已保存结果，未重复识别" : "已保存处理结果"}${item.finished_at ? ` · 完成：${new Date(item.finished_at).toLocaleString("zh-CN")}` : ""}`, "page-number"));
          row.append(node("p", "打开原页查看完整内容。", "source-warning"));
          if (item.kind === "FULL_TEXT" && item.source_id) {
            const trial = node("button", "试查这份正文", "secondary-button");
            trial.type = "button";
            trial.addEventListener("click", () => {
              if (!current()) return;
              document.dispatchEvent(new CustomEvent("archspec:official-source-ready", {detail: {source_id: item.source_id, title: item.title, standard_code: item.standard_code, query}}));
            });
            row.append(trial);
          }
        }
        if (!readOnly && !storagePaused() && ["FAILED", "INTERRUPTED", "ORIGINAL_ONLY"].includes(item.state) && item.job_id) {
          // 资源或处理能力改善后，由用户明确重开原任务；读取历史不会触发。
          const retry = node("button", item.state === "ORIGINAL_ONLY" ? "重新准备正文" : "重试获取", "secondary-button");
          retry.type = "button";
          retry.disabled = retryDisabled(item);
          retry.addEventListener("click", async () => {
            // 旧节点也必须在发送前核归属；查询流程可接管动作及唯一观察句柄。
            if (!current() || retry.disabled || retryDisabled(item)) return;
            retry.disabled = true;
            if (onRetry) {
              try { await onRetry(item); }
              catch { if (current()) row.append(node("p", "重试提交结果暂不可核对；未自动再次提交。", "source-warning")); }
              return;
            }
            // 重试属于状态修改，旧的进度读取不能把已重试任务改回失败。
            stateRevision += 1;
            if (timer !== null) { clearTimeout(timer); timer = null; }
            try {
              const job = await request(`/api/official-acquisitions/jobs/${encodeURIComponent(item.job_id)}/retry`, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
              if (!current()) return;
              stateRevision += 1;
              batch = {...batch, items: batch.items.map(old => old.job_id === item.job_id ? {...old, ...job} : old)};
              render(); schedule();
            } catch (error) {
              if (current()) row.append(node("p", `重试未完成：${error.message}`, "source-warning"));
            } finally { retry.disabled = false; if (current()) schedule(); }
          });
          row.append(retry);
        }
        container.append(row);
      });
      if (!(batch.items || []).length && batch.reason) {
        for (const container of cards.values()) container.append(node("p", batch.reason, "source-warning"));
      }
      // 当前任务登记/就绪变化只失效共用库存，不冒充用户试查动作。
      if(current()) void globalThis.ArchSpecOfficialInventory?.observeBatch(batch);
      // 原回调独立负责一次同题回答更新，库存刷新不能增加模型/问答调用。
      if (current()) onUpdate?.(batch);
    }
    async function poll() {
      if (!current()) return;
      const revision = stateRevision, pollId = ++latestPoll;
      const ownsResult = () => current() && revision === stateRevision && pollId === latestPoll;
      try {
        const updated = await request(`/api/official-acquisitions/${encodeURIComponent(batch.id)}`);
        if (!ownsResult()) return;
        batch = updated; render(); schedule();
      } catch (error) {
        if (!ownsResult()) return;
        for (const container of cards.values()) {
          const warning = node("p", `进度读取失败：${error.message}。这不代表后台处理成功或失败。`, "source-warning");
          const recover = node("button", "重新读取进度", "secondary-button");
          recover.type = "button";
          recover.addEventListener("click", poll);
          container.append(warning, recover);
        }
      }
    }
    render(); schedule();
    return stop;
  }
  globalThis.ArchspecOfficialAcquisition = {watch};
})();
