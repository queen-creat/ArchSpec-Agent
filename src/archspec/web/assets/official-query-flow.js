// 首页先查缓存并消费本轮联网决策；网上找到的仍是公告线索和准备任务。
// 这里只组合已有接口；不能绕过服务端下载限制、版本核验和正式证据准入。
(() => {
  const enabled = document.querySelector("#pending-online-enabled");
  const panel = document.querySelector("#pending-online-panel");
  const summary = document.querySelector("#pending-online-summary");
  const status = document.querySelector("#pending-online-status");
  const results = document.querySelector("#pending-online-results");
  const input = document.querySelector("#pending-query");
  const scope = document.querySelector("#pending-source");
  const project = document.querySelector("#search-project");
  const officialLinks = document.querySelector("#pending-official-links");
  const jump = document.querySelector("#pending-online-jump");
  if (!enabled || !panel || !status || !results) return;
  // 同标签页仅记住用户的联网开关，不存问题/条件，也不恢复模型发送同意。
  // 必须在恢复历史或续题之前读取，避免HTML默认checked改变上次明确选择。
  const onlineChoiceKey = "archspec:official-online-choice";
  try {
    const saved = globalThis.sessionStorage?.getItem(onlineChoiceKey);
    if (saved === "false" || saved === "true") enabled.checked = saved === "true";
  } catch { /* 存储不可用时保留本页可操作状态，不自动发起新查询。 */ }
  let generation = 0, stopProgress = () => {}, currentFlow = null;
  const historyKey = () => `archspec:query-history:${project?.value ? `project:${project.value}` : "global"}`;
  function updateAvailability() { enabled.disabled = scope.value !== "__all__"; }
  function showOnline() { panel.hidden = false; summary.hidden = false; if(jump) jump.hidden = false; }
  function clearOfficialLinks() {
    if (!officialLinks) return;
    officialLinks.replaceChildren(); officialLinks.hidden = true;
  }
  function showOfficialLinks(data, flow, {query, sourceId, projectId, isCurrent, historical}) {
    if (!officialLinks || !isCurrent() || input.value.trim() !== query.trim()
        || scope.value !== sourceId || (project?.value || "") !== (projectId || "")) return;
    clearOfficialLinks();
    // 只给未取得正文/未找到对应条文的终态补导航，不把运行中或许可拒绝包装成可读正文。
    if (!["NO_BODY", "NO_EVIDENCE"].includes(flow.state)) return;
    const links = [], seen = new Set();
    function append(title, url) {
      if (links.length >= 3) return;
      const label = typeof title === "string" && title.trim() ? title.trim().slice(0, 160) : "打开官方页面";
      const link = globalThis.ArchspecOfficialDiscovery?.governmentLink?.(label, url);
      if (!link?.href || seen.has(link.href)) return;
      seen.add(link.href); links.push(link);
    }
    for (const item of Array.isArray(data.results) ? data.results : [])
      append(item?.standard_title || item?.title, item?.url);
    for (const clue of Array.isArray(data.external_clues) ? data.external_clues : [])
      if (clue?.kind === "SEARCH_SNIPPET" && clue.body_status === "NOT_ACQUIRED") append(clue.title, clue.url);
    for (const clue of Array.isArray(data.local_clues) ? data.local_clues : [])
      if (clue?.body_status === "NOT_ACQUIRED") append(clue.title, clue.url);
    const fileLinks = links.length > 0;
    if (!fileLinks) {
      // 已登记机关入口仅作导航；不凭城市名拼地址，也不编造某份文件的题名。
      for (const hint of [...(Array.isArray(data.source_hints) ? data.source_hints : []),
                          ...(Array.isArray(data.official_entries) ? data.official_entries : [])])
        if (hint?.kind === "OFFICIAL_ENTRY" && hint.body_status === "NOT_ACQUIRED") append(hint.name, hint.url);
    }
    if (!links.length) return;
    const heading = document.createElement("h3"), note = document.createElement("p"), list = document.createElement("ul");
    heading.textContent = fileLinks ? (historical ? "上次找到的官方文件线索" : "官方文件线索") : "到官网继续查找";
    note.textContent = flow.state === "NO_BODY" ? "正文暂未取得。以下链接仅供查找，版本和适用范围仍待核对。"
      : "尚未找到对应条文，可先到官方页面继续核对。";
    for (const link of links) { const row = document.createElement("li"); row.append(link); list.append(row); }
    officialLinks.append(heading, note, list); officialLinks.hidden = false;
  }
  function invalidate() {
    generation += 1;
    currentFlow = null;
    clearOfficialLinks();
    stopProgress(); stopProgress = () => {};
    results.replaceChildren(); panel.hidden = true; panel.open = false; summary.hidden = true; status.textContent = ""; status.setAttribute("data-tone", "neutral"); status.setAttribute("data-flow-state", ""); status.setAttribute("data-observation-current", ""); status.setAttribute("data-query-history", "");
    if(jump) jump.hidden = true;
    updateAvailability();
  }
  function remember(data, query) {
    if (!data.id) return;
    try {
      localStorage.setItem(historyKey(), JSON.stringify({run_id: data.id, batch_id: data.acquisition?.id || null, query,
        ...(data.query_flow ? {query_flow_version:"OFFICIAL_QUERY_FLOW_V1"} : {})}));
    } catch { /* 浏览器存储不可用，不影响后端继续处理。 */ }
  }
  async function read(url, options) {
    const response = await fetch(url, options);
    const body = await globalThis.ArchSpecHTTP.json(response, "官方查询或正文进度响应无法读取");
    if (!response.ok) {
      const error=new Error(typeof body?.detail === "string" ? body.detail : "官方查询或正文进度暂不可读取");
      // 分类只采用HTTP数字状态，不把提供方正文或异常文本当作提交结果。
      if(Number.isInteger(response.status) && response.status>=100 && response.status<=599)error.status=response.status;
      throw error;
    }
    return body;
  }
  const flowVersion="OFFICIAL_QUERY_FLOW_V1";
  const flowActive=new Set(["RUNNING","WAITING_BODY"]);
  const flowStates=new Set([...flowActive,"READY","NO_BODY","NO_EVIDENCE","FAILED","INTERRUPTED","STALE"]);
  const phaseNames={LOCAL_LOOKUP:"核对本地资料",OFFICIAL_DISCOVERY:"补查官方来源",ACQUISITION:"准备官方正文",
    RETRIEVE_AGAIN:"重新检索同题",REFERENCE_CHECK:"核对当前出处与许可"};
  const stateNames={RUNNING:"处理中",WAITING_BODY:"正文准备中",READY:"出处已核对，正在免费重检当前资料",
    NO_BODY:"未取得正文，不能据公告摘要填写规范数值",NO_EVIDENCE:"回答证据尚未确认；候选阅读不等于正式回答依据",
    FAILED:"查询流程失败，未自动重新联网或调用模型",INTERRUPTED:"查询流程已中断，未自动重新执行",
    STALE:"历史出处当前不可读 · 未重新联网；历史成功不代表当前仍有正文使用许可或原件可用。"};
  const traceNames={RUNNING:"处理中",DONE:"已完成",SKIPPED:"已跳过",FAILED:"未完成"};
  const uuid=value=>typeof value==="string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(value);
  function flowUrl(id,projectId) {
    return `/api/official-discovery/runs/${encodeURIComponent(id)}${projectId?`?project_id=${encodeURIComponent(projectId)}`:""}`;
  }
  function checkedFlow(data,id,revision=0) {
    const flow=data?.query_flow;
    if(!uuid(id) || data.id!==id || flow?.version!==flowVersion || flow.id!==id
      || !Number.isInteger(flow.revision) || flow.revision<1 || flow.revision<revision
      || !flowStates.has(flow.state) || !Object.hasOwn(phaseNames,flow.phase)
      // 历史成功与当前可读性分别核对；不可读状态不能退回READY重放旧摘录。
      || (flow.state==="STALE" && (flow.historical_state!=="READY" || !["RESTRICTED","UNAVAILABLE"].includes(flow.current_evidence)))
      || (flow.state==="READY" && (Object.hasOwn(flow,"historical_state") || Object.hasOwn(flow,"current_evidence"))
        && (flow.historical_state!=="READY" || flow.current_evidence!=="AVAILABLE"))
      || !Array.isArray(flow.trace) || flow.trace.length>5
      || new Set(flow.trace.map(item=>item?.step)).size!==flow.trace.length
      || flow.trace.some(item=>!item || !Object.hasOwn(phaseNames,item.step) || !Object.hasOwn(traceNames,item.state))
      || !["queries","results","errors","limitations"].every(key=>Array.isArray(data[key]))
      || (flow.acquisition_batch_id && data.acquisition?.id!==flow.acquisition_batch_id)
      || (data.acquisition && (!flow.acquisition_batch_id || !Array.isArray(data.acquisition.items))))
      throw Error("查询流程身份或进度无法核对；未重新联网或调用模型");
    if (Object.hasOwn(flow,"prepared_body_readback")) {
      const body=flow.prepared_body_readback,bindings=body?.bindings;
      const validBinding=b=>b && /^(?:gb55031|official-[a-f0-9]{32})$/.test(b.source_id)
        && /^[a-f0-9]{64}$/.test(b.snapshot_id)
        && data.acquisition?.items.some(item=>item.state==="READY" && item.kind==="FULL_TEXT"
          && item.source_id===b.source_id && item.snapshot_id===b.snapshot_id);
      if(flow.state!=="NO_EVIDENCE" || !["READABLE","RESTRICTED","UNAVAILABLE"].includes(body?.state)
        || !Array.isArray(bindings) || (body.state==="READABLE"
          ?!bindings.length || !bindings.every(validBinding)
            || new Set(bindings.map(b=>b.source_id+":"+b.snapshot_id)).size!==bindings.length
          :bindings.length!==0))
        throw Error("本轮已准备正文的当前读取事实无法核对，未继续问答");
    }
    const hasObservation = ["lookup_observations","lookup_observations_scope","current_observation_state"].some(key => Object.hasOwn(flow,key));
    if (hasObservation && (!globalThis.ArchSpecResultPresentation?.lookupObservations(flow.lookup_observations)
      || flow.lookup_observations_scope !== "HISTORICAL"
      || !["READABLE","RESTRICTED","UNAVAILABLE","NOT_RECHECKED"].includes(flow.current_observation_state)))
      throw Error("历史查阅观察或当前可读性无法核对；未重新联网或调用模型");
    return flow;
  }
  function presentFlow(initial,{query,isCurrent,historical=false,context,flowId,projectId,sourceId}) {
    let stopped=false,timer=null,childStop=()=>{},revision=0,completed=false;
    let latestData=null,retryAttempt=null,pollEpoch=0,reviewing=false,reviewControl=null;
    const readiedBodies = new Set();
    let partialReadable = false;
    let transientPollFailures=0;
    const current=()=>!stopped && isCurrent();
    function stop(){stopped=true;if(timer!==null)clearTimeout(timer);childStop();}
    const refusal=attempt=>`显式重试未被接受：${attempt.rejected===401 || attempt.rejected===403?"权限拒绝":attempt.rejected===409?"当前任务状态冲突":"请求被拒绝"}（HTTP ${attempt.rejected}）；未自动再次提交。`;
    const observed = flow => globalThis.ArchSpecResultPresentation?.lookupObservations(flow.lookup_observations);
    const currentObservationNames = {READABLE:"固定正文当前可读，候选仍须确认",RESTRICTED:"固定正文当前使用受限，历史摘录不能作为本轮可用出处",
      UNAVAILABLE:"固定正文当前不可用，未将历史摘录当作现有资料",NOT_RECHECKED:"当前正文尚未核对，历史观察不代表当前可用"};
    const automaticRereadAllowed = flow => !Object.hasOwn(flow,"lookup_observations")
      || flow.current_observation_state==="READABLE" && observed(flow)?.candidates.state==="MATCHED";
    const label=flow=>{
      const observation=observed(flow),state=flow.state==="READY" && observation && !automaticRereadAllowed(flow)
        ? "历史流程完成，当前候选须显式核对" : stateNames[flow.state];
      const originalOnly=flow.state==="NO_BODY" && latestData?.acquisition?.items?.some(item=>item.state==="ORIGINAL_ONLY");
      return (historical?"历史查询进度 · 未重新联网核对 · ":"")+(originalOnly?"获取受限，请手动查看 · 原件已保存，文本待辨认":state)
        +(flowActive.has(flow.state)?" · "+phaseNames[flow.phase]:"")
        +(observation?" · "+currentObservationNames[flow.current_observation_state]:"");
    };
    async function update(data,{manual=false}={}) {
      if(!current())return;
      const flow=checkedFlow(data,flowId,revision);revision=flow.revision;latestData=data;
      if(currentFlow?.id===flowId)Object.assign(currentFlow,{revision:flow.revision,state:flow.state,pendingReceipt:false});
      status.setAttribute("data-flow-state",flow.state);
      status.setAttribute("data-observation-current",flow.current_observation_state || "");
      status.setAttribute("data-query-history",String(historical));
      // 展示优先级来自本轮已校验流程状态，不能靠前端猜测正文或适用性。
      status.setAttribute("data-tone",flow.state==="FAILED"?"error":["NO_BODY","NO_EVIDENCE","INTERRUPTED","STALE"].includes(flow.state)?"warning":"neutral");
      remember(data,query);childStop();results.replaceChildren();
      showOfficialLinks(data, flow, {query,sourceId,projectId,isCurrent:current,historical});
      // 卡片只展示本次GET返回的真实批次；唯一轮询归flow所有，不启动批次GET或下载重试。
      if(flow.discovery_run_id) childStop=globalThis.ArchspecOfficialDiscovery.render(data,{
        targetResults:results,targetStatus:status,query,isCurrent:current,historical,watchProgress:false,onRetry:retryBody,
        retryDisabled:()=>Boolean(retryAttempt && (retryAttempt.pending || retryAttempt.observing || retryAttempt.unknown))});
      const detail=document.createElement("details"),heading=document.createElement("summary");
      detail.className="official-evidence";heading.textContent="本次真实进度";detail.append(heading);
      for(const item of flow.trace){const row=document.createElement("p");row.textContent=`${phaseNames[item.step]}：${traceNames[item.state]}`;detail.append(row);}
      results.append(detail);status.textContent=flow.state === "WAITING_BODY" && partialReadable
        ? "已有正文可查，其余资料仍在准备；版本和适用条件仍须核对。" : label(flow);
      const observation=observed(flow);
      if (observation && ["RESTRICTED","UNAVAILABLE"].includes(flow.current_observation_state)) {
        // 本轮已核的固定绑定否决传给同一阅读owner；不能借历史记录关闭另一查询/项目的正文。
        globalThis.ArchspecPendingSearch?.observeReadability?.({query,sourceId,projectId,
          bindings:observation.source.bindings,currentState:flow.current_observation_state,isCurrent:current});
      }
      if (observation) {
        const view=document.createElement("details"),title=document.createElement("summary");
        view.className="query-lookup-observations official-evidence";title.textContent="历史观察 · 当前可读性另列";view.append(title);
        const currentRow=document.createElement("p");currentRow.textContent=currentObservationNames[flow.current_observation_state];view.append(currentRow);
        globalThis.ArchSpecResultPresentation.observationDetails(observation).forEach(text=>{
          const row=document.createElement("p");row.textContent="历史观察："+text;view.append(row);
        });
        if(flow.current_observation_state==="READABLE" && observation.candidates.unconfirmed_count>0) {
          const control=document.createElement("button");control.type="button";control.className="query-review-candidates secondary-button";
          control.textContent="只读重检当前待确认候选";control.disabled=reviewing;reviewControl=control;
          control.addEventListener("click",async()=>{
            if(!current() || reviewing || flow.revision!==revision)return;
            const ownRevision=revision;reviewing=true;control.disabled=true;
            status.setAttribute("data-flow-state","");
            try {
              // 明确点击只复用当前缓存读取门；不恢复历史摘录，不派发联网/模型授权，不自动选择待确认原字。
              const updated=await globalThis.ArchspecPendingSearch?.restoreCached({query,sourceId,projectId,userAction:true,isCurrent:()=>current() && revision===ownRevision});
              if(current() && revision===ownRevision)status.textContent=updated
                ? "已只读重检当前缓存；请在候选列表显式选择待确认条文，历史观察未改写。"
                : "当前候选重检未完成；历史观察未改变，未重新联网或调用模型。";
            } catch {
              if(current() && revision===ownRevision)status.textContent="当前候选重检未完成；未重新联网或调用模型。";
            } finally {reviewing=false;if(current() && reviewControl)reviewControl.disabled=false;}
          });view.append(control);
        }
        results.append(view);
      }
      if (!manual && !retryAttempt && !historical && !context?.restored && flow.state === "WAITING_BODY") {
        const ready = Array.isArray(data.acquisition?.items) ? data.acquisition.items.filter(item =>
          item.state === "READY" && item.kind === "FULL_TEXT"
          && typeof item.source_id === "string" && /^[A-Za-z0-9_-]{1,100}$/.test(item.source_id)
          && typeof item.snapshot_id === "string" && /^[a-f0-9]{64}$/.test(item.snapshot_id)) : [];
        const additions = ready.map(item => `${item.source_id}:${item.snapshot_id}`).filter(key => !readiedBodies.has(key));
        if (additions.length) {
          // 已有正文不被其它下载/OCR拖住；每个新快照只自动试读一次，最终完成仍走原核对。
          additions.forEach(key => readiedBodies.add(key));
          await context?.localFinished;
          if (current()) {
            try {
              const updated = await globalThis.ArchspecPendingSearch?.restoreCached({query,sourceId,projectId,isCurrent:current});
              if (current()) {
                partialReadable = Boolean(updated);
                if (updated) status.textContent = "已有正文可查，其余资料仍在准备；版本和适用条件仍须核对。";
              }
            } catch {
              if (current()) status.textContent = "部分正文已准备，列表暂未更新；其余资料继续处理，可稍后重查。";
            }
          }
          // 部分批次只更新阅读列表，不调用onPrepared或触发问答/模型，也不提前结束流程。
        }
      }
      // 新正文可读但字面理解尚未匹配时，只续本轮已有的等待来源闭包。
      // 历史恢复没有该闭包，不能借READY任务或GET获得新的模型授权。
      const preparedUnmatched=flow.state==="NO_EVIDENCE" && flow.prepared_body_readback?.state==="READABLE"
        && !historical && !context?.restored && typeof context?.onPrepared==="function";
      if(!manual && !retryAttempt && !completed
          && (flow.state==="READY" && automaticRereadAllowed(flow) || preparedUnmatched)) {
        completed=true;
        // 先免费重读当前正文；只有本轮仍有效的显式问答闭包可续答，历史恢复没有该授权。
        await context?.localFinished;
        const updated=current() && await globalThis.ArchspecPendingSearch?.restoreCached({query,sourceId,projectId,isCurrent:current});
        if(current())status.textContent=updated?`${historical?"历史查询进度 · 未重新联网核对 · ":""}已按当前资料免费重检；版本和适用条件仍须核对。`
          :"查询进度已读取，但当前正文重检未完成；未重新联网或执行收费问答。";
        if(preparedUnmatched && updated && current())
          status.textContent="正文当前可读，继续核对原问题；尚未确认答案依据。";
        if(updated && current() && !historical && !context?.restored && typeof context?.onPrepared==="function") {
          // 计算缓存复用不等于来源已存在；QA端核同task/revision/非重放WAITING_SOURCE后仅执行一次新操作。
          try {await context.onPrepared(current,{waitingSourceOnly:true});}
          catch {if(current())status.textContent="新正文已准备，但回答更新未完成；保留此前出处，请重新整理。";}
        }
      }
      if(manual && current()) {
        const attempt=retryAttempt;
        const item=data.acquisition.items.find(row=>row.job_id===attempt.jobId);
        if(!item || item.source_id!==attempt.sourceId)throw Error("重试正文与原批次身份无法核对");
        const active=data.acquisition.items.some(row=>["QUEUED","DOWNLOADING","PROCESSING"].includes(row.state));
        attempt.observing=active;
        const prefix=`原查询${label(flow)} · 显式重试`;
        if(attempt.rejected) {
          attempt.observing=false;
          status.textContent=`原查询${label(flow)} · ${refusal(attempt)}已只读核对原批次，未重新搜索或调用模型。`;
        } else if(active) status.textContent=`${prefix}正在处理；仅观察原批次，不重新搜索或调用模型。`;
        else if(item.state==="READY") {
          if(!/^[0-9a-f]{64}$/.test(item.snapshot_id || ""))throw Error("重试正文缺少固定快照，未重检或调用模型");
          if(!attempt.refreshed) {
            attempt.refreshed=true;
            await context?.localFinished;
            const updated=current() && await globalThis.ArchspecPendingSearch?.restoreCached({query,sourceId,projectId,isCurrent:current});
            if(current())status.textContent=updated?`${prefix}正文已保存，已按当前许可免费重检；原查询历史状态保持。`
              :`${prefix}正文已保存，但当前正文重检未完成；原查询历史状态保持，未调用模型。`;
          }
        } else status.textContent=`${prefix}${attempt.unknown?"提交结果仍待核对":"未取得正文"}；保留原查询记录，未自动再次提交。`;
      }
      if(current() && (retryAttempt?.observing || (!retryAttempt && flowActive.has(flow.state))))timer=setTimeout(poll,2000);
    }
    async function retryBody(item) {
      // 手动动作只作用于本页当前已核批次，不重开终态flow，也不借用原QA授权。
      if(!current() || retryAttempt?.pending || retryAttempt?.observing || retryAttempt?.unknown)return;
      const batch=latestData?.acquisition;
      const saved=batch?.items.find(row=>row.job_id===item.job_id && row.source_id===item.source_id);
      if(!saved || !["FAILED","INTERRUPTED","ORIGINAL_ONLY"].includes(saved.state) || batch.id!==latestData.query_flow.acquisition_batch_id)return;
      if(timer!==null){clearTimeout(timer);timer=null;}pollEpoch+=1;
      const attempt=retryAttempt={jobId:saved.job_id,sourceId:saved.source_id,batchId:batch.id,pending:true,observing:false,unknown:false,refreshed:false};
      status.textContent="正在显式重试这份正文；原查询历史保留，未重新搜索或调用模型。";
      try {
        if(!current())return;
        const result=await read(`/api/official-acquisitions/jobs/${encodeURIComponent(attempt.jobId)}/retry`,{
          method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
        if(result.job_id!==attempt.jobId || result.source_id!==attempt.sourceId || result.state!=="QUEUED")throw Error("重试响应身份无法核对");
      } catch(error) {
        if(Number.isInteger(error?.status) && error.status>=400 && error.status<500)attempt.rejected=error.status;
        else attempt.unknown=true;
      }
      finally { attempt.pending=false; }
      if(!current())return;
      attempt.observing=true;
      // 不管POST确认是否收到，都只读取同一批次；未知提交绝不盲目重发。
      await poll();
    }
    async function poll() {
      timer=null;if(!current())return;
      const epoch=pollEpoch,attempt=retryAttempt;
      try {
        if(attempt) {
          const batch=await read(`/api/official-acquisitions/${encodeURIComponent(attempt.batchId)}`);
          if(!current() || epoch!==pollEpoch || attempt!==retryAttempt)return;
          if(batch.id!==attempt.batchId || !Array.isArray(batch.items))throw Error("重试批次身份无法核对");
          transientPollFailures=0;
          await update({...latestData,acquisition:batch},{manual:true});
        } else {
          const data=await read(flowUrl(flowId,projectId));
          if(current() && epoch===pollEpoch){transientPollFailures=0;await update(data);}
        }
      } catch(error) {
        if(!current() || epoch!==pollEpoch)return;
        // 短暂网关/停写忙只影响这次GET，不能把仍在运行的任务当作失败。
        // 最多继续读取三次，同一flow/批次和原提交保持；不重放搜索、正文重试或模型POST。
        if(!attempt?.rejected && [502,503,504].includes(error?.status) && transientPollFailures<3) {
          const delay=2000*(2**transientPollFailures++);
          status.textContent="服务暂忙，正在继续读取原查询进度；未重新搜索、下载或调用模型。";
          timer=setTimeout(poll,delay);
          return;
        }
        status.textContent=attempt?.rejected
          ?`${refusal(attempt)}原批次暂不可读取，未重新搜索或调用模型。`
          :`进度读取未完成：${error.message}；已停止自动读取，可刷新恢复原任务；未重新搜索、下载或调用模型。`;
      }
    }
    void update(initial).catch(error=>{if(current())status.textContent=`查询进度未完成：${error.message}`;});
    return stop;
  }
  function present(data, {query, isCurrent, historical = false, context, flowId, projectId=null, sourceId="__all__"}) {
    if(Object.hasOwn(data,"query_flow")) {
      const flow=checkedFlow(data,flowId);
      // 只保留当前问题的流程身份；接续必须GET核新进度/许可，不借旧结果作答。
      currentFlow={id:flowId,query,sourceId,projectId,revision:flow.revision,state:flow.state,pendingReceipt:false,historical};
      showOnline();stopProgress=presentFlow(data,{query,isCurrent,historical,context,flowId,projectId,sourceId});
      return;
    }
    showOnline();
    const seen = new Set();
    // 串行合并进度中的新增快照，防止同一READY反复重查或刷新过程中发生交叉覆盖。
    let refreshing = false, dirty = false, newBody = false, settled = false, notified = false;
    async function notifyAnswer() {
      if(!context?.onPrepared || !newBody || !settled || dirty || refreshing || notified || !isCurrent()) return;
      // 在异步等待之前消费本次更新机会；同一进度不能重复启动收费回答。
      notified=true;
      try {await context.onPrepared(isCurrent);}
      catch {
        if(isCurrent()) status.textContent='新正文已准备，但回答更新未完成；保留此前出处，请重新整理。';
      }
    }
    async function refreshReady() {
      if (refreshing || !dirty || !isCurrent()) return;
      refreshing = true; dirty = false;
      try {
        await context.localFinished;
        const updated=isCurrent() && await context.refreshCached();
        // 列表读取失败不是新证据已经接通，不自动重试模型。
        if(!updated) {
          notified=true;
          if(context.restored && isCurrent()) status.textContent='历史进度已恢复，但当前正文重检未完成；请重新查询，未重新联网或执行收费问答。';
          return;
        }
        if(context.restored && isCurrent() && !status.textContent.includes('已按当前资料免费重检'))
          status.textContent+=' · 已按当前资料免费重检';
      } catch {
        notified=true;
        if(isCurrent()) status.textContent='新正文已准备，但结果更新未完成；保留此前出处，请重新查询。';
      } finally {
        refreshing = false;
        if (dirty && isCurrent()) void refreshReady();
        else if(isCurrent()) void notifyAnswer();
      }
    }
    const onUpdate = context ? batch => {
      if (!isCurrent()) return;
      settled=batch.worker_status!=='STORAGE_UNAVAILABLE'
        && !(batch.items || []).some(item=>['QUEUED','DOWNLOADING','PROCESSING'].includes(item.state));
      for (const item of batch.items || []) {
        if (item.state !== "READY" || item.kind !== "FULL_TEXT" || !item.source_id || !item.snapshot_id) continue;
        const key = `${item.source_id}:${item.snapshot_id}`;
        if (!seen.has(key)) { seen.add(key); dirty = true; }
        // 缺少复用字段时保持未知；只有明确的本次新正文才更新回答。
        if(item.cache_hit===false) newBody=true;
      }
      if (dirty) void refreshReady();
      else if(!refreshing) void notifyAnswer();
    } : undefined;
    stopProgress = globalThis.ArchspecOfficialDiscovery.render(data, {
      targetResults: results, targetStatus: status, query, isCurrent, historical, onUpdate,
    });
  }
  function checkedNetwork(body) {
    const decision = body?.lookup?.network;
    if (decision?.version !== "NETWORK_ROUTE_V1" || typeof decision.required !== "boolean"
        || !["REGION", "REGION_REQUIRED", "LATEST", "LOCAL_GAP", "CACHE_MATCHED", "UNVERIFIED", "CANDIDATE_UNCONFIRMED", "SCOPE_REVIEW_REQUIRED", "REFERENCE_ONLY", "LOCAL_READING_REQUIRED"].includes(decision.reason)
        || decision.required !== (!["CACHE_MATCHED", "REGION_REQUIRED", "SCOPE_REVIEW_REQUIRED", "REFERENCE_ONLY", "LOCAL_READING_REQUIRED"].includes(decision.reason))
        || typeof decision.message !== "string" || !decision.message.trim()) return null;
    return decision;
  }
  async function search(context) {
    // 同题仍在执行或收据未知时，重复提交只核原操作；终态主动再查仍创建新查询。
    // 改问、来源、项目及开关继续使旧绑定失效，不把别题结果当成本题进度。
    const previous=(context.reuseQueryFlow===true || currentFlow?.pendingReceipt===true || flowActive.has(currentFlow?.state))
      && currentFlow?.historical===false && currentFlow.query===context.query
      && currentFlow.sourceId===context.sourceId && currentFlow.projectId===(project?.value || null)
      ? {...currentFlow} : null;
    invalidate();
    if (!enabled.checked || context.sourceId !== "__all__") return;
    summary.hidden = false;
    // 单独条款号在不同规范中会重名，不将“3.1.1”当成全国标准的查询词。
    if (/^\d+(?:\.\d+){1,3}$/.test(context.query)) {
      status.textContent = "已按条款号查缓存；若要联网找资料，请补充规范名称或规范编号。";
      return;
    }
    const ownGeneration = generation, key = historyKey(), projectId=project?.value || null;
    const isCurrent = () => generation === ownGeneration && key === historyKey() && enabled.checked && context.isCurrent();
    status.textContent = "正在核对缓存匹配结果，再决定是否补查官方来源……";
    // 缺少新合同仍兼容旧调用；本地失败不能阻断独立官方查询。
    let localResult = null;
    try { localResult = await context.localFinished; } catch { /* 保留旧独立联网路径。 */ }
    if (!isCurrent()) return;
    const decision = checkedNetwork(localResult);
    let publicTitleHints=[];
    const hintPlan=localResult?.lookup?.public_plan;
    if(['SCOPE_REVIEW_REQUIRED','CACHE_MATCHED','LOCAL_GAP','CANDIDATE_UNCONFIRMED','UNVERIFIED'].includes(decision?.reason)
        && (hintPlan?.route==='PUBLIC_WORDS_NOT_IDENTIFIED'
          ||hintPlan?.route==='MOHURD' && hintPlan.region===null)
        && !context.restored && context.publicTitlesReady?.then) {
      status.textContent="正在核对本轮理解建议的公开规范名称……";
      try {
        const titles=await context.publicTitlesReady;
        if(Array.isArray(titles) && titles.length<=3
            && titles.every(title=>typeof title==='string' && title.length>=2 && title.length<=100))
          publicTitleHints=[...new Set(titles)];
      } catch { /* 无有效建议沿原范围提示；不重试模型。 */ }
      if(!isCurrent())return;
    }
    if (decision?.required === false && !publicTitleHints.length) {
      panel.hidden = true;
      if(jump) jump.hidden = true;
      status.textContent = decision.message;
      return;
    }
    if(previous) {
      currentFlow=previous;
      showOnline();status.textContent="正在读取本题已有官方查询与当前正文；不重复搜索……";
      try {
        const data=await read(flowUrl(previous.id,projectId));
        if(!isCurrent())return;
        checkedFlow(data,previous.id,previous.revision);
        present(data,{query:context.query,isCurrent,context,flowId:previous.id,projectId,
          sourceId:context.sourceId,historical:previous.historical});
      } catch(error) {
        if(isCurrent())status.textContent=`原查询进度读取未完成：${error.message}；未重复搜索，可刷新核对原任务。`;
      }
      return;
    }
    showOnline();
    status.textContent = "正在补找登记的官方来源；只用公共编号或主题，已有正文结果会先显示……";
    try {
      const operationId=globalThis.crypto?.randomUUID?.();
      if(!uuid(operationId))throw Error("无法生成安全操作编号，请在安全连接中重新查询");
      // 发送前只保存本次操作身份；响应丢失后也只能GET核对，不能猜测提交失败并重发。
      remember({id:operationId,query_flow:{version:flowVersion}},context.query);
      // POST回执丢失也只接续原操作GET，不因整理回答猜测失败后再提交。
      currentFlow={id:operationId,query:context.query,sourceId:context.sourceId,projectId,revision:0,state:null,pendingReceipt:true,historical:false};
      const data = await read("/api/official-discovery/search", {method: "POST", headers: {"Content-Type": "application/json","X-Archspec-Operation-Id":operationId},
        body: JSON.stringify({query: context.query,...(projectId?{project_id:projectId}:{}),auto_acquire:true,query_flow:true,
          refresh:["REGION","LATEST"].includes(decision?.reason),
          ...(publicTitleHints.length?{public_titles:publicTitleHints}:{})})});
      if (!isCurrent()) return;
      if(!Object.hasOwn(data,"query_flow"))remember(data,context.query);
      present(data,{query:context.query,isCurrent,context,flowId:operationId,projectId,sourceId:context.sourceId});
    } catch (error) {
      // 明确4xx是已收到拒绝；传输失败/5xx仍不能推定未提交，下次只读原收据。
      if(isCurrent() && error.status>=400 && error.status<500 && currentFlow)
        Object.assign(currentFlow,{pendingReceipt:false,state:"REFUSED"});
      if (isCurrent()) {status.setAttribute("data-tone", "error"); status.textContent = `官方补找未完成：${error.message}。已有缓存检索结果不受影响；可稍后重新查询。`;}
    }
  }
  async function restore() {
    // 当前全文阅读优先恢复；旧联网历史不能覆盖后来手动查阅的问题与范围。
    if(globalThis.ArchspecPendingSearch?.restoreReadingPosition) {
      const resumed=globalThis.ArchspecPendingSearch.restoreReadingPosition();
      if(resumed!==false) {
        const own=generation;
        if(await resumed) return;
        if(own!==generation) return;
      }
    }
    if (!enabled.checked || scope.value !== "__all__") return;
    let saved;
    try { saved = JSON.parse(localStorage.getItem(historyKey()) || "null"); } catch { return; }
    if (!saved?.run_id || typeof saved.query !== "string") return;
    const ownGeneration = generation, key = historyKey(), originalQuery=input.value.trim();
    const sourceId=scope.value, projectId=project?.value || null;
    let restored=false;
    const ownerCurrent = () => generation === ownGeneration && key === historyKey() && enabled.checked
      && scope.value === sourceId && sourceId === "__all__";
    // 同题QA恢复可先填回共享输入；真实input/change仍由generation使旧恢复彻底失效。
    const isCurrent = () => ownerCurrent() && (input.value.trim() === saved.query.trim()
      || (!restored && input.value.trim() === originalQuery));
    function restoreCurrent() {
      if(isCurrent()) return true;
      if(ownerCurrent()) status.textContent="历史官方查询与当前问题不同，未恢复旧出处；请重新查询，未重新联网或调用模型。";
      return false;
    }
    showOnline(); status.textContent = "正在恢复上次官方查询与正文进度；只读记录，不重新下载……";
    try {
      const data=await read(flowUrl(saved.run_id,projectId));
      if(!restoreCurrent())return;
      if(saved.query_flow_version && (!data.query_flow || saved.query_flow_version!==flowVersion))
        throw Error("查询流程记录无法核对");
      if(!Object.hasOwn(data,"query_flow") && saved.batch_id)data.acquisition=await read(`/api/official-acquisitions/${encodeURIComponent(saved.batch_id)}`);
      if (!restoreCurrent()) return;
      input.value = saved.query; restored=true;
      // 复用现有READY去重与轮询；历史恢复只接免费重检，绝不恢复收费回调。
      const context={restored:true, localFinished:Promise.resolve(null), refreshCached:()=>
        globalThis.ArchspecPendingSearch?.restoreCached({query:saved.query,sourceId,projectId,isCurrent})
          ?? Promise.resolve(false)};
      present(data,{query:saved.query,isCurrent,historical:true,context,flowId:saved.run_id,projectId,sourceId});
    } catch (error) {
      if (restoreCurrent()) {status.setAttribute("data-tone", "error"); status.textContent = `恢复未完成：${error.message}；未自动重新下载，请重新查询。`;}
    }
  }
  let awaitingInitialProject = Boolean(globalThis.ArchspecProjectSelectionReady?.then);
  document.addEventListener("archspec:query-start", event => { void search(event.detail); });
  document.addEventListener("archspec:project-change", () => { invalidate(); if (!awaitingInitialProject) void restore(); });
  document.addEventListener("archspec:official-source-ready", invalidate);
  scope.addEventListener("change", invalidate);
  enabled.addEventListener("change", () => {
    try { globalThis.sessionStorage?.setItem(onlineChoiceKey, String(enabled.checked)); }
    catch { /* 不阻断本次明确关闭或开启操作。 */ }
    invalidate();
  });
  input.addEventListener("input", invalidate);
  document.querySelector("#pending-online-jump").addEventListener("click", () => {
    if (!panel.hidden) {
      panel.open = true; panel.scrollIntoView({behavior: typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
      document.querySelector("#pending-online-title")?.focus({preventScroll: true});
    }
  });
  // 页面刷新恢复真实进度并重检当前正文；不能把旧公告当成新的联网或模型授权。
  updateAvailability();
  if (awaitingInitialProject) {
    globalThis.ArchspecProjectSelectionReady.then(ready => {
      awaitingInitialProject = false;
      if (ready) void restore();
    });
  } else void restore();
})();

