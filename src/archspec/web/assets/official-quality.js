// 独立检查官方缓存，不改老师上传的题库，不重新下载或识别规范。
(() => {
  const panel = document.querySelector("#official-quality-panel");
  if (!panel) return;
  const status = document.querySelector("#official-quality-status");
  const coverage = document.querySelector("#official-quality-coverage");
  const metrics = document.querySelector("#official-quality-metrics");
  const refresh = document.querySelector("#official-quality-refresh");
  const run = document.querySelector("#official-quality-run");
  let generation = 0;
  let running = false;
  let available = false;
  let dirty = true;
  let pendingRead = null;

  function visible() {
    // 内层展开也可能仍在隐藏的规范库或折叠维护区内，不能据此启动全库深检。
    for (let node = panel; node; node = node.parentElement) {
      if (node.hidden || (node.tagName === "DETAILS" && !node.open)) return false;
    }
    return true;
  }
  function publish(passed, switchAllowed = false) {
    document.dispatchEvent(new CustomEvent("archspec:official-quality", {
      // 定位门槛和实际切换许可分开，旧接口没有明确许可时保持原排序。
      detail: {quality_gate_passed: passed === true,
        default_switch_allowed: switchAllowed === true,
        current_ranking_version: switchAllowed === true ? "V2" : "V1"},
    }));
  }
  function render(data) {
    coverage.replaceChildren();
    metrics.replaceChildren();
    available = data.evaluation_available === true;
    const states = {READY: "正文已准备", NOT_ACQUIRED: "未取得正文", NOT_DISCOVERED: "未覆盖", CACHE_ERROR: "缓存不可读取"};
    (data.coverage || []).forEach(item => {
      const row = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = `${item.title || "未识别资料"}${item.standard_code ? ` · ${item.standard_code}` : ""}`;
      const reason = document.createElement("p");
      reason.textContent = `${states[item.state] || "状态待确认"}：${item.reason || "请核对来源"}`;
      row.append(title, reason);
      coverage.append(row);
    });
    const result = data.latest_run;
    const passed = available && data.quality_gate_passed === true && result?.status === "CURRENT";
    const switchAllowed = passed && data.default_switch_allowed === true && data.current_ranking_version === "V2";
    if (result) {
      const score = document.createElement("p");
      const format = value => typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "尚无有效成绩";
      score.textContent = `保留验收题（${result.holdout_count || 0} 道）：前五条找到目标条款的问题占比，候选排序 ${format(result.optimized_hit_at_5)}，原排序 ${format(result.baseline_hit_at_5)}。`;
      if (result.status === "STALE" || !available) {
        // 百分比保留在折叠历史中，不能在当前成绩位置展示过期结果。
        const history = document.createElement("details");
        const heading = document.createElement("summary");
        heading.textContent = "历史定位成绩（已过期，不作为当前成绩）";
        history.append(heading, score);
        metrics.append(history);
      } else metrics.append(score);
      const record = document.createElement("details");
      const label = document.createElement("summary");
      label.textContent = "题库与核对记录";
      const note = document.createElement("p");
      note.textContent = `当前题库 ${data.dataset_version || "版本待确认"} · ${Number.isInteger(data.reviewed_binding_count) ? data.reviewed_binding_count : 0} 项原页绑定复核。检查记录 ${result.id} · 记录题库 ${result.dataset_version || "历史版本未记录"}。开发者核对不代表老师或专业人员审核。`;
      record.append(label, note);
      metrics.append(record);
      status.textContent = result.status === "STALE"
        ? "旧检查已过期，当前保留原排序；具体原因见检查详情。"
        : !available ? "当前资料或核对记录不可用，暂不生成新成绩。"
          : passed ? switchAllowed ? "本次定位检查完成，可以打开原页查阅。"
          : "本次定位检查达标，当前保留原排序；达标不等于新排序更好。"
          : "本次检查未达到全部验收条件，当前保留原排序，不自动改题。";
    } else {
      status.textContent = !available ? "当前资料或核对记录不可用，暂不生成新成绩。"
        : "尚未运行固定题库检查，当前使用原排序。";
    }
    // 主状态只说当前能不能检查；原件、许可等技术原因保留在默认折叠的详情。
    const problems = [...new Set([...(Array.isArray(result?.stale_reasons) ? result.stale_reasons : []),
      !available ? data.dataset_reason : null].filter(value => typeof value === "string" && value))];
    if (problems.length) {
      const detail = document.createElement("details");
      const heading = document.createElement("summary");
      const list = document.createElement("ul");
      heading.textContent = "查看检查详情";
      problems.forEach(value => {
        const item = document.createElement("li");
        item.textContent = value;
        list.append(item);
      });
      detail.append(heading, list);
      metrics.append(detail);
    }
    const notice = document.createElement("p");
    notice.className = "source-warning";
    notice.textContent = "只检查固定问题是否找到出处，不表示回答正确，也不确认现行性或适用性。";
    metrics.append(notice);
    run.disabled = running || !available;
    publish(passed, switchAllowed);
  }
  async function load() {
    // 一次实际读取完成前合并刷新/展开通知；不能为同份资料并发做多次全库深检。
    if (pendingRead) return pendingRead;
    const current = ++generation;
    dirty = false;
    available = false;
    run.disabled = true;
    publish(false); // 读取失败或旧请求迟到，都不能沿用之前的通过标志。
    status.textContent = "正在读取正文覆盖与检查记录……";
    pendingRead = (async () => {
      try {
        const response = await fetch("/api/official-retrieval-evaluations/status");
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "检查状态暂时不可读取");
        if (current === generation) render(data);
      } catch (error) {
        if (current === generation) {
          dirty = true;
          available = false;
          run.disabled = true;
          status.textContent = `${error.message || "无法读取状态"}；当前保留原排序。`;
          publish(false);
        }
      } finally {
        pendingRead = null;
      }
      // 期间资料变化只让旧结果失效；可见时再串行补一次最新读取，隐藏时仍只标待刷新。
      if (current !== generation && dirty && visible() && !running) await load();
    })();
    return pendingRead;
  }
  function loadVisible() {
    if (dirty && visible() && !running) return load();
  }
  function invalidate() {
    generation += 1;
    dirty = true;
    available = false;
    run.disabled = true;
    publish(false);
    status.textContent = "资料已变化，重新读取后再确认检查状态；当前保留原排序。";
  }
  // 原生toggle不冒泡；同时监听两个折叠层，切回规范库使用已有展示事件。
  for (let node = panel; node; node = node.parentElement) {
    if (node.tagName === "DETAILS") node.addEventListener("toggle", loadVisible);
  }
  document.addEventListener("archspec:workbench-view", loadVisible);
  refresh.addEventListener("click", () => {
    if (!running) return load();
  });
  document.addEventListener("archspec:official-source-ready", () => {
    invalidate();
    return loadVisible();
  });
  run.addEventListener("click", async () => {
    if (running || !available) return;
    running = true;
    generation += 1;
    run.disabled = true;
    refresh.disabled = true;
    publish(false);
    status.textContent = "正在用固定题库比较新旧排序；不会调用模型、下载正文或重新识别……";
    try {
      const response = await fetch("/api/official-retrieval-evaluations/runs", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "固定题库检查未成功");
      await load();
    } catch (error) {
      status.textContent = `${error.message || "检查未成功"}；未生成新成绩，当前保留原排序。`;
      publish(false);
    } finally {
      running = false;
      refresh.disabled = false;
      run.disabled = !available;
    }
  });
  publish(false);
  run.disabled = true;
  if (visible()) load();
  else status.textContent = "打开此面板后读取正文覆盖与检查记录；当前保留原排序。";
})();
