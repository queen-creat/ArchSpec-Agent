// 待审核缓存检索独立于正式证据库，不会把候选文字自动升级成已核验依据。
(() => {
  const bounds=globalThis.ArchSpecPageBounds;
  const form = document.querySelector("#pending-search-form");
  const input = document.querySelector("#pending-query");
  const button = document.querySelector("#pending-search-button");
  const status = document.querySelector("#pending-search-status");
  const results = document.querySelector("#pending-search-results");
  const sourceSelect = document.querySelector("#pending-source");
  const coverageHint = document.querySelector("#pending-coverage-hint");
  const lookupStatus = document.querySelector("#pending-lookup-status");
  const reader = document.querySelector("#pending-page-reader");
  const presentation = globalThis.ArchSpecResultPresentation;
  const workspace = document.querySelector("#pending-evidence-workspace");
  const readingDialog = document.querySelector("#pending-reading-dialog");
  const readingToggle = document.querySelector("#pending-reading-toggle");
  const workspaceHost = document.querySelector("#pending-workspace-host");
  // 默认沿用后端同一范围的相关度；按数值条件整理仅由用户主动选择。
  let rawResultSet = null, displayGroup = "AUTO", displayTopic = "ALL", displaySort = "ORIGINAL";
  let includeRelated = false;
  // 当前读取门的否决只归属同一次查询和固定绑定；历史原响应保留，不写回后台观察。
  let readContext = null, readabilityEpoch = 0, readabilityDenial = null;
  function currentRetrievalMode(query,sourceId,projectId){
    return readContext?.generation===searchGeneration && readContext.query===query
      &&readContext.sourceId===sourceId && readContext.projectId===projectId
      &&readContext.retrievalMode==='SEMANTIC' ? 'SEMANTIC' : 'KEYWORD';
  }
  let qaLookupOwner = null;
  const readingTrail = globalThis.ArchSpecReadingTrail?.create();
  const currentProject = () => document.querySelector("#search-project")?.value || "";
  // 仅在当前标签页保存阅读身份和位置；恢复仍须使用服务器重新返回的候选/批次。
  function readingPosition() {
    try {
      const saved=JSON.parse(sessionStorage.getItem("archspec:reading-position") || "null");
      return saved?.query===input.value.trim() && saved.scope===sourceSelect?.value
        && saved.projectId===currentProject() ? saved : null;
    } catch { return null; }
  }
  function fixedChunkIdentity(item) {
    // 单本旧ID为“快照:序号”，统一检索为“来源:快照:序号”；两者核同一固定身份。
    if (!item || !/^(gb55031|official-[a-f0-9]{32})$/.test(item.source_id || "")
      || !/^[a-f0-9]{64}$/.test(item.snapshot_id || "") || typeof item.id !== "string") return null;
    const prefix = item.source_id + ":" + item.snapshot_id + ":";
    const legacy = item.snapshot_id + ":";
    const index = item.id.startsWith(prefix) ? item.id.slice(prefix.length)
      : item.id.startsWith(legacy) ? item.id.slice(legacy.length) : "";
    return /^(0|[1-9]\d{0,5})$/.test(index) ? {id:prefix + index,index} : null;
  }
  function discardReadingPosition() {
    // 用户已明确改问或改范围，旧位置不再代表本次意图；不触碰固定正文或任务。
    try { sessionStorage.removeItem("archspec:reading-position"); } catch { /* 无存储仍可继续本轮。 */ }
  }
  function rememberReadingPosition(item, {kind="TEXT",page=item.page_start,view="TEXT",bookPage=false}={}) {
    try {
      const fixed = fixedChunkIdentity(item);
      if (kind === "TEXT" && !fixed) return;
      sessionStorage.setItem("archspec:reading-position",JSON.stringify({query:input.value.trim(),
        scope:sourceSelect?.value,projectId:currentProject(),kind,id:fixed?.id || item.id,
        source_id:item.source_id,snapshot_id:item.snapshot_id,page,view,...(item.page_count!==undefined?{page_count:item.page_count}:{}),...(bookPage?{bookPage:true}:{}),
        group:displayGroup,topic:displayTopic,includeRelated,expanded:readingDialog?.open===true}));
    } catch { /* 禁用浏览器存储时仍能正常阅读。 */ }
  }
  // 只保存当前固定出处的大阅读开关；返回结果不会丢页码，也不触发任何查找。
  function rememberReadingExpansion(expanded) {
    try {
      const saved=readingPosition(),fixed=fixedChunkIdentity(selectedItem);
      if(!saved||!fixed||saved.id!==fixed.id||saved.source_id!==selectedItem.source_id
        ||saved.snapshot_id!==selectedItem.snapshot_id)return;
      sessionStorage.setItem("archspec:reading-position",JSON.stringify({...saved,expanded}));
    } catch { /* 无存储时仍可在当前页面正常阅读。 */ }
  }
  function activeDenial() {
    return readabilityDenial && readabilityDenial.generation === searchGeneration
      && readabilityDenial.query === input.value.trim() && readabilityDenial.sourceId === sourceSelect?.value
      && readabilityDenial.projectId === currentProject() ? readabilityDenial : null;
  }
  function denied(item) {
    return Boolean(item && activeDenial()?.bindings.some(binding => binding.source_id === item.source_id
      && binding.snapshot_id === item.snapshot_id));
  }
  function denialText() {
    return activeDenial()?.state === "RESTRICTED" ? "同一固定正文当前使用受限；原记录保留，重新查阅须通过当前读取门。"
      : "同一固定正文当前不可用；原记录保留，重新查阅须通过当前读取门。";
  }
  function observeReadability({query, sourceId, projectId=null, bindings, currentState, isCurrent}={}) {
    if (typeof isCurrent !== "function" || !isCurrent() || !["RESTRICTED","UNAVAILABLE"].includes(currentState)
      || !readContext || readContext.generation !== searchGeneration || query !== readContext.query
      || query !== input.value.trim() || sourceId !== readContext.sourceId || sourceId !== sourceSelect?.value
      || (projectId || "") !== readContext.projectId || (projectId || "") !== currentProject()
      || !Array.isArray(bindings) || !bindings.length || bindings.length > 20
      || !bindings.every(binding => binding && Object.keys(binding).length === 3
        && typeof binding.source_id === "string" && binding.source_id.trim().length > 0 && binding.source_id.length <= 100
        && typeof binding.snapshot_id === "string" && /^[a-f0-9]{64}$/.test(binding.snapshot_id)
        && (binding.metadata_id === null || typeof binding.metadata_id === "string" && /^[a-f0-9]{64}$/.test(binding.metadata_id)))
      || new Set(bindings.map(binding => binding.source_id+":"+binding.snapshot_id)).size !== bindings.length) return false;
    // 许可归属固定原件与快照；候选档案缺失或更新不能绕过同一pin的当前否决。
    // metadata_id仍按原观察保全，但不把档案字段当作前端授权条件。
    const targets = rawResultSet ? bindings.filter(binding => rawResultSet.results.some(item => binding.source_id === item.source_id
      && binding.snapshot_id === item.snapshot_id)) : bindings;
    if (!targets.length) return false;
    const previous = activeDenial()?.bindings || [];
    readabilityDenial = {...readContext, state:currentState,
      bindings:[...previous,...targets.filter(binding => !previous.some(old => old.source_id === binding.source_id
        && old.snapshot_id === binding.snapshot_id && old.metadata_id === binding.metadata_id))]};
    readabilityEpoch += 1;
    if (rawResultSet) render(rawResultSet, true, true);
    if (!selectedItem && reader) {
      clearReader(); const note=document.createElement("p"); note.className="reader-current-unavailable";
      note.textContent=denialText(); reader.replaceChildren(note);
    }
    return true;
  }
  let plannedResultSet = null, relatedResultSet = null, relatedLoading = false, lastLookupRequest = null;
  function checkedLookup(data) {
    if (!Object.prototype.hasOwnProperty.call(data, "lookup")) return null;
    const value = data.lookup, count = data.results.length;
    const valid = value?.version === "LOOKUP_CONTEXT_V1"
      && ["MATCHED","NO_MATCH","MISSING_SOURCE","REGION_GAP"].includes(value.state)
      && Array.isArray(value.primary_indices) && Array.isArray(value.related_indices)
      && Array.isArray(value.notes) && value.notes.every(note => typeof note === "string")
      && [...value.primary_indices,...value.related_indices].every(index => Number.isInteger(index) && index >= 0 && index < count)
      && new Set([...value.primary_indices,...value.related_indices]).size === count
      && value.primary_indices.length + value.related_indices.length === count;
    // 说明损坏时保留原结果；缺少可信分区不能自动展开词面候选。
    return valid ? value : {version:"LOOKUP_CONTEXT_V1",state:"UNVERIFIED",primary_indices:[],
      related_indices:data.results.map((_,index)=>index),notes:["匹配说明尚不能核对，请在关联候选中查看保留原文。"]};
  }
  function parallelSummary(value) {
    const parts=value?.question_parts;
    if(!Array.isArray(parts)||parts.length<2||parts.length>4||parts.some(part=>
      typeof part?.query!=="string"||!part.query.trim()||part.query.length>500
      ||!Array.isArray(part.primary_indices)||part.primary_indices.some(index=>!value.primary_indices.includes(index))))return null;
    const found=parts.filter(part=>part.primary_indices.length).length,complete=found===parts.length;
    return {complete,text:`并列要求：${found}/${parts.length}部分有候选，${complete?'仍需核对':'其余未查齐'}`};
  }
  function showLookup(value) {
    if (!lookupStatus) return;
    lookupStatus.hidden = !value;
    const observation = value?.observations && presentation?.lookupObservations(value.observations, value);
    lookupStatus.textContent = observation ? presentation.observationSummary(observation)
      : value ? (value.state === "MATCHED" ? "找到 "+value.primary_indices.length+" 条候选" : value.notes[0]) : "";
    const parallel=parallelSummary(value);
    // 已有可读候选不能遮住原问明确要求的另一书或条号；只显示共同需求给出的缺口说明，不造引用。
    if (observation && Array.isArray(value?.evidence_requirements)
        && value.evidence_requirements.some(row => row?.state !== "FOUND_COMPLETE" && (row?.source_state
          || row?.kind === "EXPLICIT_REFERENCE" && row.trigger === "BEFORE_SEARCH"
            && row.necessity === "ANSWER_REQUIRED" && row.completeness === "FIXED_EXPLICIT_CLAUSE")))
      lookupStatus.textContent = value.notes[0] + " · " + lookupStatus.textContent;
    lookupStatus.setAttribute("data-observation-source", observation?.source.state || "");
    if (activeDenial() && rawResultSet?.results.some(denied)) {
      lookupStatus.textContent = denialText()+" 上次检索观察见详情。";
      lookupStatus.setAttribute("data-observation-source", activeDenial().state);
    }
    const unknownObservation = value && Object.hasOwn(value, "observations") && !observation;
    if (unknownObservation) lookupStatus.textContent += " · 正文与候选观察尚不能核对，未新增可读性结论。";
    // 新观察与既有对应分区分开：观察损坏不能抹掉原候选，也不能提升可读性或答案可信度。
    lookupStatus.setAttribute("data-tone",value?.state === "MATCHED" && !unknownObservation && !activeDenial() && (!parallel||parallel.complete) ? "success" : "warning");
  }
  function renderLookupControls(data) {
    if (!data.lookupView) return;
    const value=data.lookupView, generation=searchGeneration;
    const controls=document.createElement("div");controls.className="pending-lookup-groups";
    controls.setAttribute("role","group");controls.setAttribute("aria-label","对应与关联候选");
    const observation = value.observations && presentation?.lookupObservations(value.observations, value);
    const currentRelatedCount = value.related_indices.filter(index => !denied(rawResultSet.results[index])).length;
    const availableCount = rawResultSet.results.filter(item => !denied(item)).length;
    const readablePending = observation?.source.state === "READABLE" && currentRelatedCount > 0;
    const pendingLabel = readablePending ? (value.primary_indices.length ? "含待确认候选 " : "待确认候选 ")
      + currentRelatedCount + " · 阅读原字" : "含关联候选 " + rawResultSet.results.length;
    for (const [kind,label] of [["MATCHED","对应候选 "+value.primary_indices.length], ["ALL",pendingLabel]]) {
      const control=document.createElement("button");control.type="button";control.textContent=label;
      control.setAttribute("data-lookup-group",kind);
      if(kind === "ALL" && readablePending) control.setAttribute("data-readable-unconfirmed-count",String(currentRelatedCount));
      control.setAttribute("aria-pressed",String(kind === (includeRelated ? "ALL" : "MATCHED")));
      control.disabled = kind === "ALL" && (relatedLoading || activeDenial() && !availableCount);
      control.addEventListener("click",async()=>{
        if(control.disabled || generation!==searchGeneration || data!==resultSet)return;
        includeRelated=kind==="ALL";
        if (includeRelated && !readablePending && plannedResultSet?.lookup?.scope === "PLANNED"
            && plannedResultSet.lookup.public_plan?.region && !relatedResultSet) {
          await loadRelatedCache();
        } else render(rawResultSet,true);
        results.querySelector?.('[data-lookup-group="'+kind+'"]')?.focus({preventScroll:true});
      });
      controls.append(control);
    }
    // 已有待确认原字时，ALL只切换本地列表；跨范围参考仍须有独立、明确的用户动作。
    if (sourceSelect?.value === "__all__" && plannedResultSet?.lookup?.scope === "PLANNED"
        && plannedResultSet.lookup.public_plan?.region && !relatedResultSet) {
      const other = document.createElement("button"); other.type = "button"; other.textContent = "查看其他参考条文";
      other.title = "只查已有缓存，不联网或调用模型；参考不替代地方规定。";
      other.disabled = relatedLoading || Boolean(activeDenial());
      other.addEventListener("click", async () => {
        if (other.disabled || generation !== searchGeneration || data !== resultSet || relatedLoading) return;
        other.disabled = true; includeRelated = true; await loadRelatedCache();
      });
      controls.append(other);
    }
    if (!value.primary_indices.length && data.retrieval_options?.local_semantic === true
        && !["SEMANTIC_ONLY", "EXACT_CLAUSE"].includes(data.method) && !activeDenial()) {
      const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "按意思再找";
      retry.title = "只在当前范围使用本机模型查找，不联网或调用付费模型。";
      retry.addEventListener("click", async () => {
        if (retry.disabled || button.disabled || generation !== searchGeneration || data !== resultSet) return;
        retry.disabled = true;
        // 沿原请求的项目/选书和读取门，不发query-start，不触发发现、问答或下载。
        await submitSearch({notifyOnline:false, preserveResults:true, reuseGeneration:true, retrievalMode:"SEMANTIC", explicitRead:true});
        if (generation === searchGeneration) retry.disabled = false;
      });
      controls.append(retry);
    }
    results.append(controls);
    const originalObservation = rawResultSet === relatedResultSet && plannedResultSet?.lookup?.observations
      && presentation?.lookupObservations(plannedResultSet.lookup.observations, plannedResultSet.lookup);
    if (observation || originalObservation || Object.hasOwn(value, "observations")) {
      const details = document.createElement("details"); details.className = "pending-lookup-observations pending-search-coverage";
      const summary = document.createElement("summary"); summary.textContent = originalObservation
        ? "原查询范围观察（关联补查另列）" : activeDenial() ? "上次检索观察（当前可读性已变化）" : "正文、候选与核验观察";
      details.append(summary);
      if (activeDenial()) {const row=document.createElement("p");row.textContent=denialText();details.append(row);}
      presentation.observationDetails(observation || originalObservation).forEach(text => {const row=document.createElement("p");row.textContent=(activeDenial() ? "上次检索观察：" : "")+text;details.append(row);});
      results.append(details);
    }
  }
  async function loadRelatedCache() {
    if (relatedLoading || !plannedResultSet || !lastLookupRequest || activeDenial()) return;
    const planned = plannedResultSet, ownGeneration = searchGeneration;
    const query = input.value.trim(), source = sourceSelect?.value;
    const current = () => ownGeneration === searchGeneration && planned === plannedResultSet
      && query === input.value.trim() && source === sourceSelect?.value;
    relatedLoading = true;
    status.textContent = "正在补查关联缓存；保留地方对应条文和当前阅读……";
    try {
      // 用户主动查阅关联参考，保留项目归属与显式筛选；由后台单独处理关联范围，不借RAW丢失或重套主地域。
      // 只读允许范围内的现有缓存，不重复联网、下载或调用模型。
      const requestBody = {...lastLookupRequest, lookup_scope:"RELATED"};
      const request = () => fetch("/api/official-documents/search", {method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify(requestBody)});
      let response = await request(), body = await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");
      if (requestBody.ranking_version === "V2" && response.status === 409 && current()) {
        requestBody.ranking_version = "V1"; response = await request(); body = await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");
      }
      if (!response.ok || !Array.isArray(body.results))
        throw new Error(typeof body.detail === "string" ? body.detail : "关联缓存无法核对");
      if (!current()) return;
      const lookup = checkedLookup(planned);
      const combined = [...planned.results];
      for (const item of body.results) if (!combined.some(old => sameEvidence(old,item))) combined.push(item);
      // 两个范围的分数不能相互比较；保留地方原检索顺序，追加项始终为关联参考。
      // 合并显示不制造一份新的后台观察：原范围观察原字保留在planned，并在详情明确范围。
      const originalLookup = {...planned.lookup}; delete originalLookup.observations;
      relatedResultSet = {...planned, results:combined, related_coverage:body.coverage,
        lookup:{...originalLookup, primary_indices:[...lookup.primary_indices],
          related_indices:combined.map((_,index)=>index).filter(index=>!lookup.primary_indices.includes(index)),
          notes:[...planned.lookup.notes,"关联候选另查允许范围的缓存，不替代地方对应出处；不同范围的词面分数不能直接比较。"]}};
      relatedLoading = false;
      render(relatedResultSet,true);
    } catch (error) {
      if (current()) {
        relatedLoading = false; includeRelated = false; render(planned,true);
        status.textContent = "关联补查未完成："+(error.message || "请稍后重试")+"；已保留地方条文。";
      }
    } finally {
      if (current()) relatedLoading = false;
    }
  }

  // 同一个阅读区在手机主动选条时进入原生弹窗；返回仍是原列表，不复制正文。
  const readingClose=document.querySelector('#pending-reading-close');
  let readingReturnFocus=null;
  function expandReading(){
    if(!readingToggle||!readingDialog||!workspaceHost||!workspace)return false;
    if(!readingDialog.open){
      readingReturnFocus=selectedReadButton||document.activeElement||readingToggle;
      readingDialog.append(workspace);readingDialog.showModal();
      readingToggle.textContent='收起阅读区';readingToggle.setAttribute('aria-expanded','true');
      rememberReadingExpansion(true);
    }
    return true;
  }
  function presentReader(title,userAction,original=false){
    if(!userAction||typeof matchMedia!=='function')return;
    if((original||matchMedia('(max-width: 760px)').matches)&&expandReading()){
      title.focus({preventScroll:true});return;
    }
    if(matchMedia('(max-width: 1099px)').matches){
      reader.scrollIntoView({behavior:scrollBehavior(),block:'start'});title.focus({preventScroll:true});
    }
  }
  if(readingToggle&&readingDialog&&workspaceHost&&workspace){
    readingToggle.addEventListener('click',()=>{if(readingDialog.open){rememberReadingExpansion(false);readingDialog.close();}else{expandReading();readingClose?.focus();}});
    // 点击返回立即记住收起，避免随后的刷新早于原生close事件。
    readingClose?.addEventListener('click',()=>{rememberReadingExpansion(false);readingDialog.close();});
    readingDialog.addEventListener('cancel',()=>rememberReadingExpansion(false));
    readingDialog.addEventListener('close',()=>{
      // 固定出处切换可能已打开新的阅读；旧关闭事件不能把新阅读区搬走。
      if(readingDialog.open)return;
      workspaceHost.append(workspace);rememberReadingExpansion(false);readingToggle.textContent='展开阅读区';readingToggle.setAttribute('aria-expanded','false');
      (readingReturnFocus?.isConnected===false?readingToggle:readingReturnFocus||readingToggle)?.focus({preventScroll:true});
    });
  }
  let readerGeneration = 0;
  let releaseReaderLayout = null;
  let selectedCard = null, selectedButton = null, selectedItem = null;
  let selectedReadButton = null, selectedIndex = -1;
  let readerMode = "TEXT";
  let resultSet = null, listPage = 0, updateReaderNavigation = null;
  const pageSize = 5;
  const renderedRows = new Map();
  const cardEvidence = new WeakMap();
  let searchGeneration = 0;
  // 问题owner跨免费重检保留；结果写入另按每次HTTP读取排序，不能互相取消。
  let cacheRequestSerial = 0;
  let readingProjectId = "";
  let optimizedRanking = false;
  document.addEventListener("archspec:official-quality", event => {
    // 定位成绩不等于新方法更优，必须同时具备明确的默认切换许可。
    optimizedRanking = event.detail?.quality_gate_passed === true
      && event.detail?.default_switch_allowed === true
      && event.detail?.current_ranking_version === "V2";
  });
  function invalidateSearch({discardPosition=false,preserveTrail=false}={}) {
    if(!preserveTrail)readingTrail?.clear();
    qaLookupOwner = null;
    if (discardPosition) discardReadingPosition();
    searchGeneration += 1; readContext = null; readabilityDenial = null;
    plannedResultSet = null; relatedResultSet = null; relatedLoading = false; lastLookupRequest = null;
    rawResultSet = null; displayGroup = "AUTO"; displayTopic = "ALL"; includeRelated = false; showLookup(null);
    if (readingDialog?.open) readingDialog.close();
    resultSet = null; listPage = 0; renderedRows.clear();
    results.replaceChildren();
    clearReader();
    button.disabled = false;
  }

  // 原页区域只读固定来源、固定处理快照。切换问题/项目后不能留下上一份图像。
  function clearReader() {
    readerGeneration += 1;
    releaseReaderLayout?.(); releaseReaderLayout = null;
    if (selectedCard) selectedCard.className = "result-card pending-result-card";
    selectedButton?.setAttribute("aria-pressed", "false");
    selectedReadButton?.setAttribute("aria-current", "false");
    selectedCard = null; selectedButton = null; selectedReadButton = null; selectedItem = null;
    selectedIndex = -1; updateReaderNavigation = null;
    readerMode = "TEXT";
    if (!reader) return;
    const placeholder = document.createElement("div");
    placeholder.className = "reader-placeholder";
    const heading = document.createElement("h3"); heading.textContent = "找到条文，也找到它的出处";
    const note = document.createElement("p"); note.textContent = "输入设计问题或条款号，选择左侧候选条文，在这里阅读全文、核对适用条件并对照原页。";
    placeholder.append(heading, note); reader.replaceChildren(placeholder);
  }

  // 用户设置减少动态效果时，包括程序触发的滚动也必须立即完成。
  function scrollBehavior() {
    return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  }

  function hasLayoutChunk(item) {
    if (typeof item.id !== "string" || !item.id) return false;
    // 单本和跨书检索沿用各自现有的片段身份，旧结果没有 id 时只读整页。
    return [item.snapshot_id, `${item.source_id}:${item.snapshot_id}`].some(prefix =>
      item.id.startsWith(`${prefix}:`) && /^\d+$/.test(item.id.slice(prefix.length + 1)));
  }

  function validPageLayout(layout, item, page) {
    const validBox = box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
      && box[0] >= 0 && box[1] >= 0 && box[0] < box[2] && box[1] < box[3] && box[2] <= 1 && box[3] <= 1;
    if (!layout || layout.source_id !== item.source_id || layout.snapshot_id !== item.snapshot_id
      || layout.pdf_page !== page || layout.chunk_id !== item.id
      || !Number.isFinite(layout.width) || layout.width <= 0 || !Number.isFinite(layout.height) || layout.height <= 0
      || !["LOCATED", "NOT_LOCATED"].includes(layout.location_status)
      || !Array.isArray(layout.highlights) || !Array.isArray(layout.tables)) return false;
    const tableIds = new Set();
    return layout.highlights.every(highlight => validBox(highlight?.box)) && layout.tables.every(table => {
      if (typeof table?.id !== "string" || !table.id.trim() || tableIds.has(table.id)
        || typeof table.label !== "string" || !table.label.trim() || !validBox(table.box)) return false;
      tableIds.add(table.id); return true;
    });
  }

  function openReader(item, card, openButton, original = false, userAction = true, initialPage, tableReading = null, view = null, bookPages = null) {
    if (!reader || denied(item)) return;
    // 仅恢复已被当前候选重新核对的固定身份；未主动放大过的阅读不自动弹窗。
    const savedPosition=readingPosition(),fixedPosition=fixedChunkIdentity(item);
    const restoreLarge=!userAction&&(view==="ORIGINAL"||original)&&savedPosition?.expanded===true
      &&fixedPosition?.id===savedPosition.id&&savedPosition.source_id===item.source_id
      &&savedPosition.snapshot_id===item.snapshot_id;
    // 同一条在文字与原页间切换时记住当前页；换条或换项目仍由旧代次失效处理。
    let readingPage = initialPage === undefined ? item.page_start : initialPage;
    clearReader();
    selectedCard = card; selectedButton = openButton; selectedItem = item;
    readerMode = view || (original ? "ORIGINAL" : "TEXT");
    original = readerMode === "ORIGINAL";
    let bookRange=original&&globalThis.ArchSpecBookPages?.valid(bookPages,item)?bookPages:null;
    if(!Number.isInteger(readingPage)||readingPage<1||readingPage>(bounds?bounds.limit(item,bookRange):200)
      ||(readingPage<item.page_start||readingPage>item.page_end)&&(!bookRange||readingPage>bookRange.page_count))readingPage=item.page_start;
    selectedIndex = resultSet?.results.findIndex(candidate => sameEvidence(candidate, item)) ?? -1;
    selectedReadButton = renderedRows.get(selectedIndex)?.read || null;
    card.className += " is-selected";
    selectedReadButton?.setAttribute("aria-current", "true");
    const compare = renderedRows.get(selectedIndex)?.compare;
    compare?.setAttribute("aria-pressed", original ? "true" : "false");
    rememberReadingPosition(item,{page:readingPage,view:readerMode,bookPage:!!bookRange});
    const generation = readerGeneration;
    const heading = document.createElement("div"); heading.className = "reader-heading";
    // 阅读头只说明当前是原文查阅，出处与页码继续保留。
    const label = document.createElement("small"); label.className = "reader-state"; label.textContent = "原文查阅";
    const title = document.createElement("h3"); title.textContent = `${item.title} · ${item.standard_code || "文号待核"}`;
    title.tabIndex = -1;
    // 只展示服务器从同一固定文档派生的分区，不凭条文措辞推断效力。
    const sectionKind=item.document_section?.kind;
    const textLabel=sectionKind==="COMMENTARY"?"条文说明":sectionKind==="MIXED"?"正文与说明":"条文正文";
    const sectionNote=sectionKind==="COMMENTARY"?" · 条文说明（仅供参考）":sectionKind==="MIXED"?" · 正文与说明（跨分区）":"";
    const clause = document.createElement("p"); clause.textContent = `${item.clause_number ? `条款 ${item.clause_number}` : "未识别条款号"} · ${filePageLabel(item)}${sectionNote}`;
    heading.append(label, title, clause);
    const previousPlace=readingTrail?.peek();
    if(previousPlace){
      const backToReading=document.createElement('button');backToReading.type='button';backToReading.className='reader-history-return quiet-button';
      backToReading.textContent='← 返回上一处 · '+(previousPlace.clause||previousPlace.title||'固定出处');
      backToReading.addEventListener('click',async()=>{
        const current=()=>generation===readerGeneration&&selectedItem===item&&readingTrail.peek()===previousPlace;
        if(!current())return;backToReading.disabled=true;status.textContent='正在重核上一处固定出处……';
        try{
          const response=await fetch('/api/official-documents/'+encodeURIComponent(previousPlace.source_id)+'/snapshots/'+previousPlace.snapshot_id+'/chunks/'+previousPlace.id.split(':').at(-1),{method:'GET',cache:'no-store'});
          if(!current())return;
          if(!response.ok){
            if(previousPlace.source_id===item.source_id&&previousPlace.snapshot_id===item.snapshot_id){
              invalidateSearch();status.textContent='同一固定出处当前无法读取；已保留阅读位置，未改用其它版本。';return;
            }
            throw Error('上一处固定出处当前无法读取，未改用其它版本。');
          }
          const restored=await globalThis.ArchSpecHTTP.json(response,'上一处固定出处暂不可读取');if(!current())return;
          if(!fixedCitationReady(restored)||restored.id!==previousPlace.id||restored.source_id!==previousPlace.source_id||restored.snapshot_id!==previousPlace.snapshot_id
            ||previousPlace.fingerprint&&restored.snapshot_fingerprint!==previousPlace.fingerprint
            ||!previousPlace.bookPage&&(previousPlace.page<restored.page_start||previousPlace.page>restored.page_end))throw Error('上一处固定身份无法核对，未切换阅读。');
          let bookPages=null;
          if(previousPlace.bookPage){bookPages=await globalThis.ArchSpecBookPages?.read(restored,{isCurrent:current});if(!current())return;if(!bookPages)throw Error('上一处本书原页范围无法核对。');}
          readingTrail.pop(previousPlace);
          if(!openFixedCitation(restored,previousPlace.page,null,{view:previousPlace.view,userAction:true,recordTrail:false,bookPages}))throw Error('上一处固定出处暂不能打开。');
          status.textContent='已重新核读并返回上一处；未重新检索、联网或调用模型。';
        }catch(error){if(current())status.textContent=error.message||'上一处固定出处暂不可读取。';}
        finally{if(current())backToReading.disabled=false;}
      });
      heading.append(backToReading);
    }

    const bookmarkButton=globalThis.ArchSpecBookmarks?.bind(document)?.button(item);

    if (item.source_format === "HTML") {
      const format = document.createElement("p"); format.className = "source-warning";
      format.textContent = "网页排版视图（非官方PDF页码）";
      heading.append(format);
    }
    // 所有完整正文都能取得同一固定原件；扫描识别不足时保留人工查看路径，不另行下载官网或改用latest。
    const originalLink=document.createElement('a');originalLink.className='secondary-button reader-original-download';
    originalLink.textContent=item.source_format==='HTML'?'下载网页原件':'下载完整PDF原件';
    originalLink.href=`/api/official-documents/${encodeURIComponent(item.source_id)}/snapshots/${encodeURIComponent(item.snapshot_id)}/original`;
    originalLink.target='_blank';originalLink.rel='noopener';
    heading.append(originalLink);
    const assessment=rawResultSet && checkedLookup(rawResultSet);
    if(assessment && !assessment.primary_indices.some(index=>sameEvidence(rawResultSet.results[index],item))){
      const relation=document.createElement("p");relation.className="reader-lookup-gap";
      const observation=assessment.observations && presentation?.lookupObservations(assessment.observations,assessment);
      relation.textContent=observation?.source.state === "READABLE"
        ? "待确认候选 · 已有可读原字，尚未确认对应本次问题；不能作为正式回答依据。"
        : "关联候选 · 尚未证明对应本次查询，请结合上下文核对。";heading.append(relation);
    }
    const sourceProfile = globalThis.ArchSpecSourceMetadata?.profile(item);
    // 默认直接读条文；完整来源和扩展工具按需展开，不删除来源或改变许可。
    const sourceTools=document.createElement('details');sourceTools.className='reader-source-tools';
    const sourceSummary=document.createElement('summary');sourceSummary.textContent='来源与阅读工具';sourceTools.append(sourceSummary);
    if (sourceProfile) sourceTools.append(sourceProfile);
    const noticeChanges=globalThis.ArchSpecNoticeChanges?.controls(document,item,{
      isCurrent:()=>generation===readerGeneration&&selectedItem===item&&!denied(item),
      onUnavailable:()=>{
        if(generation!==readerGeneration||selectedItem!==item)return;
        invalidateSearch();status.textContent='固定出处当前无法读取；已保留阅读位置，未沿用旧公告或更换版本。';
      },
    });
    if(noticeChanges)sourceTools.append(noticeChanges);
    const readerActions=document.createElement('div');readerActions.className='reader-actions';
    if(bookmarkButton)readerActions.append(bookmarkButton);
    const fixedMetadata=globalThis.ArchSpecSourceMetadata?.fixed(item);
    const copyControls=globalThis.ArchSpecCitationCopy?.controls(document,item,{
      profileLines:sourceProfile?[...sourceProfile.querySelectorAll('p')].map(node=>node.textContent):[],
      sourceUrl:fixedMetadata?globalThis.ArchSpecSourceMetadata.safeUrl(fixedMetadata.source_url,item):null,
      isCurrent:()=>generation===readerGeneration&&selectedItem===item&&!denied(item),
      clipboard:globalThis.navigator?.clipboard,
    });
    if(copyControls)readerActions.append(copyControls);
    if(bookmarkButton||copyControls)sourceTools.append(readerActions);
    const bookIndex=globalThis.ArchSpecBookIndex?.controls(document,item,{
      isCurrent:()=>generation===readerGeneration&&selectedItem===item&&!denied(item),
      onOpen:(selected,page)=>{
        if(generation!==readerGeneration||selectedItem!==item||denied(item))return false;
        const opened=openFixedCitation(selected,page,null,{view:'TEXT',userAction:true});
        if(opened)status.textContent='已从本书索引打开固定条文；这不证明它对应当前问题或适用于工程。';
        return opened;
      },
      onUnavailable:()=>{
        if(generation!==readerGeneration||selectedItem!==item)return;
        // 当前读取门明确不可用时撤下旧结果；位置身份保留，不借旧缓存维持可读外观。
        invalidateSearch();status.textContent='本书固定出处当前无法读取；已保留阅读位置，未切换版本或获取新资料。';
      },
    });
    if(bookIndex)sourceTools.append(bookIndex);
    const mentions=globalThis.ArchSpecStandardMentions?.controls(document,item,{
      isCurrent:()=>generation===readerGeneration&&selectedItem===item&&!denied(item),
      onOpen:(selected,page)=>{
        if(generation!==readerGeneration||selectedItem!==item||denied(item))return false;
        const opened=openFixedCitation(selected,page,null,{view:'TEXT',userAction:true});
        if(opened)status.textContent='已打开原文提及书目的固定片段；对应条款、版本及工程适用性仍须核对。';
        return opened;
      },
      onUnavailable:()=>{
        if(generation!==readerGeneration||selectedItem!==item)return;
        invalidateSearch();status.textContent='原固定条文当前无法读取，已保留位置，未获取新资料。';
      },
    });
    if(mentions)sourceTools.append(mentions);
    const contextReader=globalThis.ArchSpecReaderContext?.controls(document,item,{
      isCurrent:()=>generation===readerGeneration&&selectedItem===item&&!denied(item),
      onOpen:(selected,page,options={})=>{
        if(generation!==readerGeneration||selectedItem!==item||denied(item))return false;
        const opened=openFixedCitation(selected,page,null,{view:options.view==='ORIGINAL'?'ORIGINAL':'TEXT',userAction:true});
        if(opened)status.textContent='已打开同一固定版本的补读片段；仍须核原页、版本与工程适用条件。';
        return opened;
      },
      onUnavailable:()=>{
        if(generation!==readerGeneration||selectedItem!==item)return;
        invalidateSearch();status.textContent='固定上下文当前无法读取；已保留阅读位置，未切换版本或获取新资料。';
      },
    });
    if(contextReader)sourceTools.append(contextReader);
    heading.append(sourceTools);
    const navigation = document.createElement("div"); navigation.className = "reader-navigation";
    const previous = document.createElement("button"); previous.type = "button"; previous.className = "reader-previous"; previous.textContent = "← 上一条";
    const position = document.createElement("span"); position.className = "reader-position";
    const next = document.createElement("button"); next.type = "button"; next.className = "reader-next"; next.textContent = "下一条 →";
    updateReaderNavigation = () => {
      position.textContent = `第 ${selectedIndex + 1} / ${resultSet?.results.length || 0} 条`;
      previous.disabled = selectedIndex <= 0;
      next.disabled = !resultSet || selectedIndex >= resultSet.results.length - 1;
    };
    updateReaderNavigation();
    function navigate(delta) {
      if (generation !== readerGeneration || !resultSet) return;
      const index = selectedIndex + delta;
      if (index < 0 || index >= resultSet.results.length) return;
      listPage = Math.floor(index / pageSize); renderResultsPage();
      const row = renderedRows.get(index);
      openReader(resultSet.results[index], row.card, row.read, false, false);
      // 控件随详情重建时恢复到同一导航动作，键盘不会掉回页面顶部。
      const control = reader.querySelector?.(delta < 0 ? ".reader-previous" : ".reader-next");
      (control?.disabled ? reader.querySelector?.(".reader-heading h3") : control)?.focus({preventScroll: true});
    }
    previous.addEventListener("click", () => navigate(-1)); next.addEventListener("click", () => navigate(1));
    navigation.append(previous, position, next);
    const tabs = document.createElement("div"); tabs.className = "reader-tabs";
    tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "阅读方式");
    const views = [["TEXT", textLabel, "reader-text-toggle"],
      ["OUTLINE", "章节目录", "reader-outline-toggle"],
      ["OTHER", "待辨认片段", "reader-fragments-toggle"],
      ["ORIGINAL", "原页对照", "reader-original-toggle"]];
    const activeView = readerMode;
    function switchMode(mode) {
      if (generation !== readerGeneration || !resultSet) return;
      openReader(item, selectedCard, selectedButton, mode === "ORIGINAL", mode === "ORIGINAL", readingPage, tableReading, mode, mode==="ORIGINAL"?bookRange:null);
      reader.querySelector?.("." + views.find(entry => entry[0] === mode)[2])?.focus({preventScroll: true});
    }
    views.forEach(([mode, label, className], index) => {
      const control = document.createElement("button"); control.type = "button";
      control.className = className; control.textContent = label;
      control.id = `pending-reader-tab-${mode.toLowerCase()}`;
      control.setAttribute("role", "tab");
      control.setAttribute("aria-controls", "pending-reader-active-view");
      control.setAttribute("aria-selected", String(activeView === mode));
      control.tabIndex = activeView === mode ? 0 : -1;
      control.addEventListener("click", () => switchMode(mode));
      // 原生按钮可用 Enter/Space；方向键按标签顺序切换，旧代次仍由同一守卫阻断。
      control.addEventListener("keydown", event => {
        const next = event.key === "Home" ? 0 : event.key === "End" ? views.length - 1
          : event.key === "ArrowRight" ? (index + 1) % views.length
          : event.key === "ArrowLeft" ? (index + views.length - 1) % views.length : -1;
        if (next < 0) return;
        event.preventDefault(); switchMode(views[next][0]);
      });
      tabs.append(control);
    });
    function viewPanel() {
      const panel = document.createElement("section"); panel.id = "pending-reader-active-view";
      panel.className = "reader-view-panel"; panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `pending-reader-tab-${activeView.toLowerCase()}`);
      panel.tabIndex = 0; return panel;
    }
    const warning = createRecognitionWarning(item, "reader-warning");
    const back = document.createElement("button"); back.type = "button"; back.className = "reader-return quiet-button"; back.textContent = "返回所选条文 ↑";
    back.addEventListener("click", () => {
      if (generation !== readerGeneration) return;
      selectedCard?.scrollIntoView({behavior: scrollBehavior(), block: "center"});
      selectedButton?.focus({preventScroll: true});
    });
    const metadata = createMetadata(item);
    if (!original) {
      const panel = viewPanel();
      const text = document.createElement("blockquote"); text.className = "reader-clause-text";
      // 当前片段的完整原字始终保留；分类只决定在哪个阅读分区陈列，不改写或删除正文。
      text.textContent = item.text;
      if (activeView === "TEXT") {
        panel.append(text);
        const current=()=>generation===readerGeneration&&selectedItem===item&&!denied(item);
        const figures=globalThis.ArchSpecBookPages?.controls?.(document,item,{isCurrent:current,onOpen:(page,bookPages)=>{
          if(current()&&openFixedCitation(item,page,null,{bookPages}))status.textContent='已打开原文提及附图的实际图题页；图示及续页仍须核对。';
        }});
        if(figures)panel.append(figures);
      }
      else {
        const kind = activeView === "OUTLINE" ? "NAVIGATION" : "OTHER";
        const note = document.createElement("p"); note.className = "reader-section-note";
        note.textContent = activeView === "OUTLINE"
          ? "同次检索、同一快照中的章节 / 目录片段，并非整本目录。"
          : "同次检索、同一快照中的待辨认片段；保留识别原字，不补猜条号。";
        panel.append(note);
        const sections = (rawResultSet?.results || []).filter(candidate => !denied(candidate) &&
          candidate.source_id === item.source_id && candidate.snapshot_id === item.snapshot_id
          && candidate.title === item.title && candidate.standard_code === item.standard_code
          && presentation?.describe(candidate).kind === kind);
        if (presentation?.describe(item).kind === kind) panel.append(text);
        const list = document.createElement("div"); list.className = "reader-section-list";
        sections.forEach(candidate => {
          const entry = document.createElement("button"); entry.type = "button";
          entry.className = "reader-section-open";
          entry.textContent = `${filePageLabel(candidate)} · ${candidate.text.replace(/\s+/g, " ").trim().slice(0, 120)}`;
          entry.setAttribute("aria-label", `阅读${activeView === "OUTLINE" ? "章节目录" : "待辨认"}片段完整原字：${filePageLabel(candidate)}`);
          entry.setAttribute("aria-current", String(sameEvidence(candidate, item)));
          entry.addEventListener("click", () => {
            if (generation !== readerGeneration || !rawResultSet) return;
            // 复用现有列表分类和候选身份；仅显示已有片段，不重新检索或跨快照补目录。
            displayGroup = kind; displayTopic = "ALL";
            const lookup = checkedLookup(rawResultSet);
            if (lookup && !lookup.primary_indices.some(index => sameEvidence(rawResultSet.results[index], candidate))) includeRelated = true;
            render(rawResultSet, true);
            const index = resultSet.results.findIndex(row => sameEvidence(row, candidate));
            if (index < 0) return;
            listPage = Math.floor(index / pageSize); renderResultsPage();
            const row = renderedRows.get(index);
            if (row) {
              openReader(resultSet.results[index], row.card, row.read, false, false, undefined, null, activeView);
              reader.querySelector?.("." + views.find(entry => entry[0] === activeView)[2])?.focus({preventScroll: true});
            }
          });
          list.append(entry);
        });
        // 图片沿同快照页面记录展示，不伪造图片文字、条号或OCR行号。
        const images = activeView === "OTHER" ? (item.recognition?.unrecognized_images || []) : [];
        images.forEach((image, index) => {
          if (!Number.isInteger(image.pdf_page) || image.pdf_page < item.page_start || image.pdf_page > item.page_end) return;
          const entry = document.createElement("button"); entry.type = "button";
          entry.className = "reader-section-open reader-image-gap-open";
          entry.textContent = `文件第 ${image.pdf_page} 页 · 待辨认图片 ${index + 1} · 查看原页`;
          entry.addEventListener("click", () => {
            if (generation !== readerGeneration || !resultSet) return;
            openReader(item, selectedCard, selectedButton, true, false, image.pdf_page, null, "ORIGINAL");
          });
          list.append(entry);
        });
        const omitted = item.recognition?.unrecognized_image_omitted_count || 0;
        if (activeView === "OTHER" && omitted) {
          const more = document.createElement("p"); more.className = "reader-section-note";
          more.textContent = `另有 ${omitted} 处待辨认图片，请逐页查看原件。`; list.append(more);
        }
        if (!sections.length && !images.length) {
          const empty = document.createElement("p"); empty.className = "reader-section-empty";
          empty.textContent = activeView === "OUTLINE" ? "本次未取得这一快照的章节目录片段，可返回条文正文或原页核对。"
            : "本次未取得这一快照的待辨认片段；这不代表整份文件没有识别疑点。";
          panel.append(empty);
        }
        panel.append(list);
      }
      reader.replaceChildren(navigation, heading, tabs, ...(warning ? [warning] : []), panel, back, metadata);
      presentReader(title,userAction);
      return;
    }
    const toolbar = document.createElement("div"); toolbar.className = "reader-toolbar";
    const pageLabel = document.createElement("label"); pageLabel.textContent = item.source_format === "HTML" ? "排版页码" : "文件页码";
    const pageSelect = document.createElement("select");
    pageSelect.setAttribute("aria-label", item.source_format === "HTML" ? "选择网页排版页码" : "选择待核对的 PDF 页码");
    function pageOptions(page){
      pageSelect.replaceChildren();
      for(let number=bookRange?1:item.page_start;number<=(bookRange?bookRange.page_count:item.page_end);number++){
        const option=document.createElement('option');option.value=String(number);option.textContent='第 '+number+' 页';pageSelect.append(option);
      }
      pageSelect.value=String(page);
    }
    pageOptions(readingPage);
    pageLabel.append(pageSelect);
    const retry = document.createElement("button"); retry.type = "button"; retry.className = "reader-retry secondary-button"; retry.textContent = "重新读取原页";
    toolbar.append(retry, back);
    const bookToggle=document.createElement('button');bookToggle.type='button';bookToggle.className='reader-book-pages-toggle secondary-button';
    const bookPrevious=document.createElement('button'),bookNext=document.createElement('button');
    for(const [control,label,cls] of [[bookPrevious,'上一原页','reader-book-previous'],[bookNext,'下一原页','reader-book-next']]){
      control.type='button';control.className=cls+' quiet-button';control.textContent=label;
    }
    const bookNote=document.createElement('p');bookNote.className='reader-book-page-note reader-section-note';bookNote.setAttribute('role','status');
    function bookControls(){
      bookToggle.textContent=bookRange?'返回所选条文原页':'浏览本书原页';
      bookPrevious.hidden=bookNext.hidden=!bookRange;bookPrevious.disabled=readingPage<=1;
      bookNext.disabled=!bookRange||readingPage>=bookRange.page_count;
      bookNote.textContent=bookRange?(readingPage<item.page_start||readingPage>item.page_end
        ?'正在浏览本书第 '+readingPage+' 页，不在所选条文范围内；未证明与该条文的关系。'
        :'正在浏览本书原页，共 '+bookRange.page_count+' 页；所选条文在第 '+item.page_start+(item.page_end!==item.page_start?'—'+item.page_end:'')+' 页。'):'';
      bookNote.hidden=!bookRange;
    }
    bookToggle.addEventListener('click',async()=>{
      if(generation!==readerGeneration||bookToggle.disabled)return;
      if(bookRange){bookRange=null;pageOptions(item.page_start);showPage();return;}
      bookToggle.disabled=true;bookNote.hidden=false;bookNote.textContent='正在核对本书固定原页范围……';
      try{
        const range=await globalThis.ArchSpecBookPages?.read(item,{isCurrent:()=>generation===readerGeneration});
        if(generation!==readerGeneration||!range)return;bookRange=range;pageOptions(readingPage);showPage();
      }catch(error){if(generation===readerGeneration){bookNote.hidden=false;bookNote.textContent=error.message||'本书原页范围暂不可读取，当前条文保留。';}}
      finally{if(generation===readerGeneration)bookToggle.disabled=false;}
    });
    function moveBookPage(delta){if(generation!==readerGeneration||!bookRange)return;
      const page=Number(pageSelect.value)+delta;if(page<1||page>bookRange.page_count)return;pageSelect.value=String(page);showPage();}
    bookPrevious.addEventListener('click',()=>moveBookPage(-1));bookNext.addEventListener('click',()=>moveBookPage(1));
    toolbar.append(bookToggle,bookPrevious,bookNext);bookControls();
    const originalTools = document.createElement("div"); originalTools.className = "reader-original-tools";
    originalTools.setAttribute("role", "group"); originalTools.setAttribute("aria-label", "原页阅读工具");
    const toolButtons = ["原页定位", "原表放大", "整页还原", "跨页阅读"].map(label => {
      const control = document.createElement("button"); control.type = "button"; control.textContent = label;
      control.disabled = true; control.setAttribute("aria-controls", "pending-original-page-view");
      originalTools.append(control); return control;
    });
    const [locateTool, tableTool, restoreTool, crossTool] = toolButtons;
    // 缩放只作用于当前固定原图和它的百分比图层，不触发新查询或图片请求。
    const zoomControls=document.createElement('div');zoomControls.className='reader-zoom-controls';
    zoomControls.setAttribute('role','group');zoomControls.setAttribute('aria-label','原页显示大小');
    const zoomOut=document.createElement('button'),zoomIn=document.createElement('button');
    const fitPage=document.createElement('button'),largePage=document.createElement('button');
    const zoomStatus=document.createElement('span');zoomStatus.className='reader-zoom-status';
    zoomStatus.setAttribute('aria-live','polite');
    for(const [control,label,name] of [[zoomOut,'−','缩小原页'],[zoomIn,'＋','放大原页'],
      [fitPage,'适合宽度','原页适合窗口宽度'],[largePage,'放大阅读','放大原页阅读区']]){
      control.type='button';control.textContent=label;control.setAttribute('aria-label',name);control.disabled=true;
    }
    // 页码和缩放同栏常驻；高级操作移到纸面后，不再挤占看图高度。
    pageLabel.className='reader-page-select';
    zoomControls.append(pageLabel,zoomOut,zoomStatus,zoomIn,fitPage,largePage);
    let zoomLevel=1,fitWidth=false,zoomActions=null,revealOriginal=userAction||restoreLarge;
    [zoomOut,zoomIn,fitPage,largePage].forEach((control,index)=>control.addEventListener('click',()=>{
      if(generation!==readerGeneration||control.disabled)return;zoomActions?.[index]();
    }));
    let toolActions = null;
    toolButtons.forEach((control, index) => control.addEventListener("click", () => {
      if (generation !== readerGeneration || control.disabled) return;
      toolActions?.[index]();
    }));
    const imageStatus = document.createElement("p"); imageStatus.className = "reader-status";
    imageStatus.setAttribute("role", "status");
    const wrap = document.createElement("div"); wrap.className = "reader-image-wrap";
    wrap.id = "pending-original-page-view"; wrap.tabIndex = 0;
    wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "原页图像，可滚动查看条文与放大的表格");
    const textTable = document.createElement("div"); textTable.className = "reader-text-table-wrap";
    textTable.id = "pending-text-table-view"; textTable.hidden = true;
    const regions = document.createElement("div"); regions.className = "reader-region-controls";
    regions.setAttribute("role", "group"); regions.setAttribute("aria-label", "选择原页阅读区域");
    const layoutStatus = document.createElement("p"); layoutStatus.className = "reader-layout-status";
    layoutStatus.setAttribute("role", "status");
    const recognitionDetails = document.createElement('details'); recognitionDetails.className = 'reader-recognition-details';
    recognitionDetails.hidden = true; recognitionDetails.open = false;
    const layoutRetry = document.createElement("button"); layoutRetry.type = "button";
    layoutRetry.className = "reader-layout-retry quiet-button"; layoutRetry.textContent = "重试定位";
    layoutRetry.setAttribute("aria-label", "重新读取当前原页的条文与表格位置"); layoutRetry.hidden = true;
    const hasChunk = hasLayoutChunk(item);
    regions.hidden = !hasChunk; layoutStatus.hidden = !hasChunk;
    toolbar.append(layoutRetry);
    let pageGeneration = 0;
    let retryLayout = null;
    let releasePageLayout = null;
    releaseReaderLayout = () => releasePageLayout?.();
    layoutRetry.addEventListener("click", () => retryLayout?.());
    function showPage() {
      // 选中来源和页码双重隔离；旧图片的 load/error 不能污染新选择。
      const selected = Number(pageSelect.value);
      if (!Number.isInteger(selected) || selected < (bookRange?1:item.page_start) || selected > (bookRange?bookRange.page_count:item.page_end)) return;
      const onClausePage=selected>=item.page_start&&selected<=item.page_end;
      readingPage = selected;
      bookControls();regions.hidden=!hasChunk||!onClausePage;layoutStatus.hidden=!hasChunk||!onClausePage;
      if(warning)warning.hidden=!onClausePage;
      rememberReadingPosition(item,{page:readingPage,view:readerMode,bookPage:!!bookRange});
      releasePageLayout?.();
      recognitionDetails.replaceChildren(); recognitionDetails.hidden = true; recognitionDetails.open = false;
      const ownPage = ++pageGeneration;
      const isCurrent = () => generation === readerGeneration && ownPage === pageGeneration;
      imageStatus.textContent = `正在读取 PDF 第 ${selected} 页……`;
      textTable.hidden = true; textTable.replaceChildren(); wrap.hidden = false;
      retry.disabled = true;
      layoutRetry.hidden = true;
      const stage = document.createElement("div"); stage.className = "reader-image-stage";
      const highlights = document.createElement("div"); highlights.className = "reader-highlight-layer";
      highlights.setAttribute("aria-hidden", "true");
      const image = document.createElement("img"); image.className = "pending-page-image";
      image.alt = `${item.title} ${item.standard_code} PDF 第 ${selected} 页原始图像（保留水印）`;
      let layout = null, layoutGeneration = 0, imageFailed = false, imageLoaded = false, userRegion = null, tableFocus = null;
      let activeRegion = "PAGE", regionGeneration = 0,regionWidth=null;
      const regionButtons = new Map();
      function applyZoom(){
        if(!isCurrent()||imageFailed)return;
        // 手机保持可读纸宽，在图片区横移；适合宽度由用户主动选择。
        const width=fitWidth?'100%':`${Math.round((regionWidth||Math.max(960,wrap.clientWidth||960))*zoomLevel)}px`;
        if(stage.style.width!==width)stage.style.width=width;
        zoomStatus.textContent=fitWidth?'适宽':`${Math.round(zoomLevel*100)}%`;
        zoomOut.disabled=!imageLoaded||(!fitWidth&&zoomLevel<=.5);
        zoomIn.disabled=!imageLoaded||(!fitWidth&&zoomLevel>=2.5);
        fitPage.disabled=largePage.disabled=!imageLoaded;
      }
      function changeZoom(delta){revealOriginal=true;fitWidth=false;zoomLevel=Math.max(.5,Math.min(2.5,zoomLevel+delta));applyZoom();scheduleLocalView();}
      zoomActions=[()=>changeZoom(-.25),()=>changeZoom(.25),()=>{revealOriginal=true;fitWidth=true;zoomLevel=1;applyZoom();scheduleLocalView();},()=>{
        if(expandReading()){zoomControls.scrollIntoView?.({block:'start'});wrap.focus({preventScroll:true});scheduleLocalView();}
      }];
      function currentTable() {
        if (!layout) return null;
        // 明确选表优先，其次沿用已经核对的行列证明，最后才用唯一条文位置。
        const chosen = /^(?:TABLE|GRID):(.+)$/.exec(userRegion || "");
        return (chosen && layout.tables.find(table => table.id === chosen[1]))
          || layout.tables.find(table => table.id === tableFocus?.table_id)
          || globalThis.ArchSpecTableReading?.highlightedTable(layout) || null;
      }
      function updateTools() {
        // 仅在当前原图和固定布局均可用时声称定位/放大可用，未知布局不伪造按钮成功。
        const ready = isCurrent() && imageLoaded && !imageFailed;
        if(ready)applyZoom();
        else [zoomOut,zoomIn,fitPage,largePage].forEach(control=>{control.disabled=true;});
        locateTool.disabled = !ready || layout?.location_status !== "LOCATED" || !layout.highlights.length;
        locateTool.title = locateTool.disabled ? "当前页未取得条文定位，可核对整页" : "标出当前条文在原页的位置";
        const table = ready && currentTable();
        tableTool.disabled = !table;
        tableTool.title = !ready || !layout?.tables.length ? "当前页未取得可核对的原表位置"
          : !table ? "当前页有多个原表，请先在下方选择一个原表区域" : "放大所选原表";
        restoreTool.disabled = !ready; restoreTool.title = ready ? "还原当前完整原页" : "等待当前原页图像";
        crossTool.disabled = !ready || !onClausePage || item.page_start === item.page_end;
        crossTool.title = item.page_start === item.page_end ? "当前片段在同一页，没有跨页范围"
          : `在当前片段第 ${item.page_start}—${item.page_end} 页间阅读；末页返回起始页`;
      }
      function manualRegion(key) { if (isCurrent() && !imageFailed) { userRegion = key; chooseRegion(key); } }
      toolActions = [() => manualRegion("CLAUSE"),
        () => { const table = currentTable(); if (table) manualRegion(`TABLE:${table.id}`); },
        () => manualRegion("PAGE"), () => {
          if (!isCurrent()) return;
          pageSelect.value = String(selected < item.page_end ? selected + 1 : item.page_start);
          showPage(); pageSelect.focus({preventScroll: true});
        }];
      updateTools();
      const percent = value => `${Number((value * 100).toFixed(6))}%`;
      function scheduleLocalView() {
        if (!isCurrent() || imageFailed || typeof requestAnimationFrame !== "function") return;
        const ownRegion = regionGeneration;
        requestAnimationFrame(() => {
          // 图片、位置和区域都属于当前选择才可滚动；旧绘制帧不能操作新页或已移除的阅读区。
          if (!isCurrent() || imageFailed || ownRegion !== regionGeneration) return;
          const viewport = wrap.getBoundingClientRect?.(), readerBox = reader.getBoundingClientRect?.();
          if (!viewport || !readerBox || !(reader.clientHeight > 0)) return;
          const windowHeight = typeof innerHeight === "number" && innerHeight > 0 ? innerHeight : reader.clientHeight + 64;
          // 仅为常用工具和返回栏留空间；翻页、缩放仍复用当前固定原图。
          const controlsHeight=zoomControls.getBoundingClientRect?.().height || 52;
          const pictureHeight=`${Math.round(Math.max(320,Math.min(1200,windowHeight-controlsHeight-68)))}px`;
          wrap.style.height=pictureHeight;wrap.style.maxHeight=pictureHeight;
          applyZoom();
          // 刷新时原图可能迟到；等纸面有高度再露出常用栏，避免标题再次挤走原页。
          if(revealOriginal&&imageLoaded&&readingDialog?.open){
            revealOriginal=false;zoomControls.scrollIntoView?.({block:'start'});
          }
          if (activeRegion === `TABLE:${tableFocus?.table_id}` && layout && image.naturalWidth > 0) {
            const table=layout.tables.find(table=>table.id===tableFocus.table_id);
            const value=tableFocus.marks.find(mark=>mark.role==='VALUE').cell.box;
            const rect=stage.getBoundingClientRect?.();
            if(!table||!rect||!(rect.width>0)||!(rect.height>0)||!(wrap.clientWidth>0)||!(wrap.clientHeight>0))return;
            const left=Math.max(0,table.box[0]-.02),top=Math.max(0,table.box[1]-.02);
            const width=Math.min(1,table.box[2]+.02)-left,height=Math.min(1,table.box[3]+.02)-top;
            // 小屏不能同时容纳整张放大表；将已核对数值移入图片区，而非滚动整张网页。
            const x=rect.left-viewport.left+wrap.scrollLeft+rect.width*((value[0]+value[2])/2-left)/width;
            const y=rect.top-viewport.top+wrap.scrollTop+rect.height*((value[1]+value[3])/2-top)/height;
            function position(axis,origin,size,cropStart,cropSize,centre,viewportSize,total) {
              const boxes=tableFocus.marks.map(mark=>mark.cell.box);
              const first=origin+size*(Math.min(...boxes.map(box=>box[axis]))-cropStart)/cropSize;
              const last=origin+size*(Math.max(...boxes.map(box=>box[axis+2]))-cropStart)/cropSize;
              let target=centre-viewportSize/2;
              // 桌面空间足够时一并留住表头和房间，不因“数值居中”截掉条件；小屏才优先数值。
              if(last-first+16<=viewportSize)target=Math.max(last-viewportSize+8,Math.min(first-8,target));
              return Math.round(Math.max(0,Math.min(total-viewportSize,target)));
            }
            wrap.scrollLeft=position(0,rect.left-viewport.left+wrap.scrollLeft,rect.width,left,width,x,wrap.clientWidth,wrap.scrollWidth);
            wrap.scrollTop=position(1,rect.top-viewport.top+wrap.scrollTop,rect.height,top,height,y,wrap.clientHeight,wrap.scrollHeight);
            return;
          }
          const marks = activeRegion === 'CLAUSE' ? layout?.highlights
            : activeRegion.startsWith('ISSUE:') ? layout?.recognition?.issues.filter(row => `ISSUE:${row.row_index}` === activeRegion && row.box) : [];
          if (!marks?.length || !(image.naturalWidth > 0)) return;
          const imageBox = image.getBoundingClientRect?.();
          if (!imageBox || !(imageBox.width > 0) || !(imageBox.height > 0) || !(wrap.clientHeight > 0)) return;
          const firstTop = imageBox.top - viewport.top + wrap.scrollTop + imageBox.height * marks[0].box[1];
          const maximum = Math.max(0, wrap.scrollHeight - wrap.clientHeight);
          // 采用实际呈现高度而非PNG原始像素；保留上缘16px，较高条文可继续在图片区内阅读。
          wrap.scrollTop = Math.max(0, Math.min(maximum, Math.round(firstTop - 16)));
        });
      }
      function chooseRegion(key) {
        if (!isCurrent() || imageFailed) return;
        activeRegion = key; regionGeneration += 1;
        stage.className = "reader-image-stage";
        stage.style.width = ""; stage.style.aspectRatio = "";regionWidth=null;
        image.style.position = ""; image.style.width = ""; image.style.left = ""; image.style.top = "";
        highlights.replaceChildren();
        textTable.hidden = true; textTable.replaceChildren(); wrap.hidden = false;
        regionButtons.forEach((control, value) => control.setAttribute("aria-pressed", String(value === key)));
        if (key.startsWith("GRID:") && layout) {
          const table = layout.tables.find(candidate => `GRID:${candidate.id}` === key);
          if (!table || !globalThis.ArchSpecTableReading?.valid(table, layout)) return;
          const ownRegion = regionGeneration, ownLayout = layoutGeneration;
          const currentCell = () => isCurrent() && !imageFailed && ownRegion === regionGeneration && ownLayout === layoutGeneration;
          globalThis.ArchSpecTableReading.render(textTable, table, tableFocus, {
            current: currentCell,
            original: () => { if (currentCell()) { userRegion = `TABLE:${table.id}`; chooseRegion(userRegion); } },
            reread: async cell => {
              if (!currentCell()) throw new Error('STALE');
              const response = await fetch(imageURL.replace(/\.png$/, '/reread-cell'), {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({chunk_id:item.id, table_id:table.id, row:cell.row, column:cell.column})});
              if (!currentCell()) throw new Error('STALE');
              if (!response.ok) throw new Error(response.status === 409 ? 'BUSY' : 'FAILED');
              const result = await globalThis.ArchSpecHTTP.json(response, '补读响应暂不可读取');
              if (!currentCell()) throw new Error('STALE');
              if (!result || result.source_id !== item.source_id || result.snapshot_id !== item.snapshot_id
                || result.pdf_page !== selected || result.chunk_id !== item.id || result.table_id !== table.id
                || result.is_citable !== false || result.review_status !== 'PENDING_ORIGINAL_CONFIRMATION'
                || !/^[a-f0-9]{64}$/.test(result.reread_id || '') || typeof result.cache_hit !== 'boolean'
                || !result.cell || !['row','column','row_span','column_span'].every(key => result.cell[key] === cell[key])
                || !Array.isArray(result.cell.box) || result.cell.box.length !== 4 || !cell.box.every((v,i) => v === result.cell.box[i])
                || !Array.isArray(result.candidates) || result.candidates.length > 1
                || result.candidates.some(row => !row || typeof row.text !== 'string' || !row.text.trim()
                  || row.text.length > 500 || !Number.isFinite(row.score) || row.score < 0 || row.score > 1)
                || result.status !== (result.candidates.length ? 'CANDIDATE' : 'NO_TEXT')) throw new Error('IDENTITY');
              return result;
            }
          });
          textTable.hidden = false; wrap.hidden = true;
        } else if (key === "CLAUSE" && layout) {
          layout.highlights.forEach(highlight => {
            const box = document.createElement("span"); box.className = "reader-clause-highlight";
            const [x0, y0, x1, y1] = highlight.box;
            box.style.left = percent(x0); box.style.top = percent(y0);
            box.style.width = percent(x1 - x0); box.style.height = percent(y1 - y0);
            highlights.append(box);
          });
        } else if (key.startsWith('ISSUE:') && layout) {
          const issue = layout.recognition?.issues.find(row => `ISSUE:${row.row_index}` === key && row.box);
          if (!issue) return;
          const mark = document.createElement('span'); mark.className = 'reader-recognition-highlight';
          const [x0,y0,x1,y1] = issue.box;
          mark.style.left = percent(x0); mark.style.top = percent(y0);
          mark.style.width = percent(x1-x0); mark.style.height = percent(y1-y0); highlights.append(mark);
        } else if (key.startsWith("TABLE:") && layout) {
          const table = layout.tables.find(candidate => `TABLE:${candidate.id}` === key);
          if (!table) return;
          // 给原表保留少量边缘；同一原图按两个轴同比例缩放，不重建表格文字。
          const [x0, y0, x1, y1] = table.box;
          const left = Math.max(0, x0 - 0.02), top = Math.max(0, y0 - 0.02);
          const width = Math.min(1, x1 + 0.02) - left, height = Math.min(1, y1 + 0.02) - top;
          stage.className += " is-table-view";
          regionWidth=Math.max(720, Math.min(1200, layout.width * width));
          stage.style.width = `${regionWidth}px`;
          stage.style.aspectRatio = `${layout.width * width} / ${layout.height * height}`;
          image.style.position = "absolute"; image.style.width = percent(1 / width);
          image.style.left = percent(-left / width); image.style.top = percent(-top / height);
          if (tableFocus?.table_id === table.id) {
            tableFocus.marks.forEach(mark => {
              const box = document.createElement('span');
              box.className = `reader-cell-highlight reader-cell-highlight-${mark.role.toLowerCase()}`;
              box.setAttribute('data-cell-role',mark.role);box.setAttribute('aria-label',mark.label);
              box.textContent = mark.label;
              const [cx0,cy0,cx1,cy1] = mark.cell.box;
              // 图层位于裁切后的视窗；必须将整页比例坐标换算到这一张放大原图。
              box.style.left = percent((cx0-left)/width);box.style.top = percent((cy0-top)/height);
              box.style.width = percent((cx1-cx0)/width);box.style.height = percent((cy1-cy0)/height);
              highlights.append(box);
            });
          }
        }
        applyZoom();
        wrap.scrollLeft = 0; wrap.scrollTop = 0;
        updateTools();
        scheduleLocalView();
      }
      function renderRegions() {
        const focusedRegion = [...regionButtons].find(([, control]) => control === document.activeElement)?.[0];
        regionButtons.clear(); regions.replaceChildren();
        function addRegion(key, label) {
          const control = document.createElement("button"); control.type = "button";
          control.textContent = label; control.setAttribute("aria-label", `查看${label}`);
          control.setAttribute("aria-controls", key.startsWith("GRID:") ? textTable.id : wrap.id);
          control.addEventListener("click", () => {
            if (!isCurrent() || imageFailed) return;
            userRegion = key; chooseRegion(key);
          });
          regionButtons.set(key, control); regions.append(control);
        }
        if (hasChunk && onClausePage) {
          addRegion("PAGE", "整页");
          if (layout?.location_status === "LOCATED" && layout.highlights.length) addRegion("CLAUSE", "条文定位");
          layout?.tables.forEach((table, index) => {
            addRegion(`TABLE:${table.id}`, table.label);
            if (globalThis.ArchSpecTableReading?.valid(table, layout)) addRegion(`GRID:${table.id}`, `文字表格 ${index + 1}`);
          });
        }
        // 用户主动选整页或其他表后不抢回；只有初次打开或重新定位才自动选已核对原表。
        chooseRegion(regionButtons.has(userRegion) ? userRegion : tableFocus ? `TABLE:${tableFocus.table_id}`
          : regionButtons.has("CLAUSE") ? "CLAUSE" : "PAGE");
        regionButtons.get(focusedRegion)?.focus({preventScroll: true});
        updateTools();
      }
      const imageURL = `/api/official-documents/${encodeURIComponent(item.source_id)}/snapshots/${encodeURIComponent(item.snapshot_id)}/pages/${selected}.png`;
      function renderRecognition(risk) {
        recognitionDetails.replaceChildren(); recognitionDetails.hidden = true;
        const count = value => Number.isInteger(value) && value >= 0 && value <= 100000;
        const validBox = box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
          && box[0]>=0 && box[1]>=0 && box[0]<box[2] && box[1]<box[3] && box[2]<=1 && box[3]<=1;
        // 风险详情不能跨页借用。缺字段或坏字段只保留旧提醒，不影响原图和表格。
        if(!risk || !['CURRENT_FRAGMENT','UNKNOWN'].includes(risk.scope)
          || ![risk.low_count,risk.other_low_count,risk.unlocated_count,risk.omitted_count].every(count)
          || !Array.isArray(risk.issues) || risk.issues.length>20
          || risk.low_count!==risk.issues.length+risk.omitted_count
          || risk.unlocated_count>risk.low_count) return false;
        const seen=new Set();
        if(risk.issues.some(row=>{
          if(!row || row.pdf_page!==selected || !count(row.row_index) || seen.has(row.row_index)
            || typeof row.text!=='string' || !row.text.trim() || row.text.length>5000
            || !Number.isFinite(row.score) || row.score<0 || row.score>1
            || !(row.box===null || validBox(row.box)))return true;
          seen.add(row.row_index);return false;
        }))return false;
        if(risk.scope==='CURRENT_FRAGMENT' && item.page_start===item.page_end) {
          // 问答打开的旧摘要可能没有新风险字段。固定原页已证明单页范围后，
          // 同步当前候选卡片的提醒，不改其原字、不影响另一书或另一页。
          const cardWarning=selectedCard?.querySelector?.('.source-warning');
          if(cardWarning) {
            const fresh=createRecognitionWarning({...item,recognition:risk,
              warnings:risk.low_count ? item.warnings : item.warnings.filter(code=>code!=='LOW_CONFIDENCE_OCR')},'source-warning');
            cardWarning.hidden=!fresh;if(fresh)cardWarning.textContent=fresh.textContent;
          }
        }
        if(warning && item.warnings.includes('LOW_CONFIDENCE_OCR') && risk.scope==='CURRENT_FRAGMENT') {
          if(risk.low_count) {
            warning.hidden=false;
            const combined=createRecognitionWarning({...item,recognition:risk},'reader-warning');
            // 具体低分数量不能覆盖已有位置冲突、缺字或补识别风险。
            warning.textContent=combined.textContent.replace('当前条文有','本页当前条文有');
          } else if(item.page_start===item.page_end) {
            const remaining=createRecognitionWarning({...item,warnings:item.warnings.filter(code=>code!=='LOW_CONFIDENCE_OCR')},'reader-warning');
            warning.hidden=!remaining;if(remaining)warning.textContent=remaining.textContent;
          } else {
            warning.hidden=false;
            const remaining=createRecognitionWarning({...item,warnings:item.warnings.filter(code=>code!=='LOW_CONFIDENCE_OCR')},'reader-warning');
            warning.textContent=(remaining?.textContent || '')+'本页未发现当前条文低分行；本条其他页仍需逐页核对。';
          }
        }
        if(!risk.low_count && risk.scope==='CURRENT_FRAGMENT')return true;
        recognitionDetails.hidden=false;
        const summary=document.createElement('summary');summary.textContent=risk.scope==='UNKNOWN'
          ? '识别疑点范围尚不能完整确认' : `本页识别疑点 ${risk.low_count} 处`;
        recognitionDetails.append(summary);
        risk.issues.forEach((row,index)=>{
          const line=document.createElement('div');line.className='reader-recognition-row';
          const text=document.createElement('span');
          text.textContent=`${index+1}. ${row.text.length>240?row.text.slice(0,240)+'…（识别行摘录）':row.text}`;
          line.append(text);
          if(row.box) {
            const locate=document.createElement('button');locate.type='button';locate.className='reader-recognition-locate quiet-button';
            locate.textContent='定位疑点';locate.setAttribute('aria-controls',wrap.id);
            locate.addEventListener('click',()=>{if(isCurrent()&&!imageFailed){userRegion=`ISSUE:${row.row_index}`;chooseRegion(userRegion);}});
            line.append(locate);
            if(risk.scope==='CURRENT_FRAGMENT') {
              const reread=document.createElement('button');reread.type='button';reread.className='reader-recognition-reread quiet-button';
              reread.textContent='放大重读';
              const comparison=document.createElement('p');comparison.className='reader-reread-result';comparison.hidden=true;
              const ownRisk=layoutGeneration;
              const currentRisk=()=>isCurrent()&&!imageFailed&&ownRisk===layoutGeneration;
              reread.addEventListener('click',async()=>{
                if(!currentRisk()||reread.disabled)return;
                reread.disabled=true;comparison.hidden=false;comparison.textContent='正在本机放大重读……';
                try {
                  const response=await fetch(imageURL.replace(/\.png$/,'/reread'),{
                    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chunk_id:item.id,row_index:row.row_index})});
                  if(!currentRisk())return;
                  if(!response.ok)throw new Error(response.status===409?'BUSY':'FAILED');
                  const result=await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");if(!currentRisk())return;
                  // 只接受同书、同固定快照、同页、同行的对照，重读不能替换原字或取消提醒。
                  if(!result||result.source_id!==item.source_id||result.snapshot_id!==item.snapshot_id
                    ||result.pdf_page!==selected||result.chunk_id!==item.id||result.row_index!==row.row_index
                    ||typeof result.reread_id!=='string'||!/^[a-f0-9]{64}$/.test(result.reread_id)
                    ||result.review_status!=='PENDING_ORIGINAL_CONFIRMATION'||typeof result.cache_hit!=='boolean'
                    ||!['SAME','DIFFERENT','NO_TEXT'].includes(result.status)
                    ||result.original?.text!==row.text||result.original?.score!==row.score
                    ||!Array.isArray(result.candidates)||result.candidates.length>20
                    ||result.candidates.some(candidate=>!candidate||typeof candidate.text!=='string'||!candidate.text.trim()
                      ||candidate.text.length>5000||!Number.isFinite(candidate.score)||candidate.score<0||candidate.score>1)
                    ||(result.status==='NO_TEXT')!==(result.candidates.length===0))throw new Error('IDENTITY');
                  const original=row.text.length>240?row.text.slice(0,240)+'…（摘录）':row.text;
                  const candidate=result.candidates.map(c=>c.text).join('\n');
                  const text=result.status==='NO_TEXT'?'本次未读出文字。':result.status==='SAME'?'本次与原识别相同。':'本次与原识别不同。';
                  comparison.textContent=`${result.cache_hit?'复用重读结果。':''}${text}\n原识别：${original}`
                    +(candidate?`\n本次重读：${candidate.length>1000?candidate.slice(0,1000)+'…（摘录）':candidate}`:'')+'\n请对照原页确认。';
                  reread.textContent='再次查看';
                }catch(error){if(currentRisk())comparison.textContent=error.message==='BUSY'
                  ?'另一个局部重读正在运行，请稍后重试。':'重读未完成或对照身份不符，原文与原页保留，可重试。';
                }finally{if(currentRisk())reread.disabled=false;}
              });
              line.append(reread,comparison);
            }
          } else {const missing=document.createElement('small');missing.textContent='位置未取得，请看整页。';line.append(missing);}
          recognitionDetails.append(line);
        });
        const note=document.createElement('small');note.textContent='这是识别行的疑点，不是已确认的错字；原字和原页没有改写。';recognitionDetails.append(note);
        if(risk.omitted_count){const extra=document.createElement('small');extra.textContent=`另有${risk.omitted_count}处未展开，请继续核对原页。`;recognitionDetails.append(extra);}
        return true;
      }
      async function readLayout() {
        if (!hasChunk || !onClausePage || !isCurrent() || imageFailed) return;
        const ownLayout = ++layoutGeneration;
        const currentLayout = () => isCurrent() && !imageFailed && ownLayout === layoutGeneration;
        layoutStatus.textContent = "正在查找位置……"; layoutRetry.disabled = true;
        try {
          const query = new URLSearchParams({chunk_id: item.id});
          const response = await fetch(`${imageURL.replace(/\.png$/, "/layout")}?${query}`);
          if (!currentLayout()) return;
          if (!response.ok) throw new Error("位置读取失败");
          const body = await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");
          if (!currentLayout()) return;
          if (!validPageLayout(body, item, selected)) throw new Error("位置读取失败");
          layout = body;
          if(!renderRecognition(body.recognition)) layout={...body,recognition:null};
          tableFocus = globalThis.ArchSpecTableReading?.focus(tableReading,item,selected,layout) || null;
          renderRegions(); layoutRetry.hidden = true;
          layoutStatus.textContent = tableFocus ? `蓝框：${tableFocus.marks.find(mark=>mark.role==='ROW').label} · 紫框：表头 · 橙框：原表读数。`
            : body.location_status === "LOCATED" && body.highlights.length ? "" : "条文未定位，可看整页。";
        } catch {
          if (!currentLayout()) return;
          layout = null; tableFocus = null; recognitionDetails.replaceChildren(); recognitionDetails.hidden=true; renderRegions();
          layoutStatus.textContent = "位置读取失败，可看整页。"; layoutRetry.hidden = false;
        } finally {
          if (currentLayout()) layoutRetry.disabled = false;
        }
      }
      retryLayout = readLayout;
      const geometryObserver = typeof ResizeObserver === "function" ? new ResizeObserver(scheduleLocalView) : null;
      geometryObserver?.observe(reader); geometryObserver?.observe(wrap); geometryObserver?.observe(stage);
      // 窗口变化后仍让原页贴近常用栏；只改变本地阅读布局。
      const resizedOriginal=()=>{revealOriginal=true;scheduleLocalView();};
      globalThis.addEventListener?.("resize", resizedOriginal);
      releasePageLayout = () => {
        geometryObserver?.disconnect();
        globalThis.removeEventListener?.("resize", resizedOriginal);
      };
      image.addEventListener("load", () => {
        if (!isCurrent()) return;
        imageLoaded = true; updateTools();
        imageStatus.textContent = `${item.source_format === "HTML" ? "排版第" : "文件第"} ${selected} 页`;
        retry.disabled = false;
        scheduleLocalView();
      });
      image.addEventListener("error", () => {
        if (!isCurrent()) return;
        imageFailed = true; updateTools(); regions.replaceChildren(); layoutStatus.textContent = ""; layoutRetry.hidden = true;
        recognitionDetails.replaceChildren(); recognitionDetails.hidden=true;
        releasePageLayout?.();
        wrap.replaceChildren();
        textTable.replaceChildren(); textTable.hidden = true; wrap.hidden = false;
        imageStatus.textContent = `PDF 第 ${selected} 页读取失败。可重新读取，或通过结果中的发布公告核对；候选文字不受影响。`;
        retry.disabled = false;
      });
      // 浏览器优先选同页高清图；固定基础URL仍供布局/疑点重读使用，不能带图像质量参数。
      // 在入DOM前设置候选，避免先取小图再换图；缩放仍仅改变本地显示。
      image.srcset = `${imageURL}?quality=reading 1x`;
      stage.append(image, highlights); wrap.replaceChildren(stage); renderRegions();
      image.src = imageURL;
      if (hasChunk && onClausePage) readLayout();
    }
    pageSelect.addEventListener("change", showPage); retry.addEventListener("click", showPage);
    const originalOptions=document.createElement('details');originalOptions.className='reader-original-options';
    const originalSummary=document.createElement('summary');originalSummary.textContent='更多原页工具';
    originalOptions.append(originalSummary,toolbar,bookNote,originalTools,regions,layoutStatus,recognitionDetails);
    const moveHint=document.createElement('p');moveHint.className='reader-zoom-hint';moveHint.textContent='左右滑动看整行';
    const panel = viewPanel(); panel.append(zoomControls, wrap, imageStatus, moveHint, originalOptions, textTable);
    reader.replaceChildren(navigation, heading, tabs, ...(warning ? [warning] : []), panel, metadata); showPage();
    // 延续用户主动选择的大原页；返回结果后不再自动展开，页码仍沿原固定记录。
    if(restoreLarge)expandReading();
    presentReader(title,userAction,true);
    if((userAction||restoreLarge)&&readingDialog?.open)zoomControls.scrollIntoView?.({block:'start'});
  }

  function filePageLabel(item) {
    return `${item.source_format === "HTML" ? "排版第" : "文件第"} ${item.page_start}${item.page_end !== item.page_start ? `—${item.page_end}` : ""} 页`;
  }

  function currentRecognitionCount(item) {
    const risk=item.recognition;
    const count=value=>Number.isInteger(value) && value>=0 && value<=100000;
    // 缺失或互相矛盾的旧摘要不能凭一个“0”取消提醒，仍显示范围未定位。
    if(!risk || risk.scope!=='CURRENT_FRAGMENT'
      || ![risk.low_count,risk.other_low_count,risk.unlocated_count,risk.omitted_count].every(count)
      || !Array.isArray(risk.issues) || risk.issues.length>20
      || risk.low_count!==risk.issues.length+risk.omitted_count
      || risk.unlocated_count>risk.low_count)return null;
    return risk.low_count;
  }

  function createRecognitionWarning(item, className) {
    // 只把真实识别异常留在前景，不用每条正文重复解释处理方法。
    const count=currentRecognitionCount(item),located=count!==null;
    const issues = [
      ["LOW_CONFIDENCE_OCR", located ? `当前条文有${count}处识别疑点` : "识别疑点范围未定位"],
      ["IMAGE_TEXT_NOT_DETECTED", "图片中未读出文字"],
      ["UNRECOGNIZED_IMAGES", Number.isInteger(item.recognition?.unrecognized_image_count)
        ? `已取得文字层 · ${item.recognition.unrecognized_image_count}处图片待辨认` : "已取得文字层 · 同页图片待辨认"],
      ["OCR_POSITION_MISSING", "部分文字缺少位置"],
      ["OCR_OVERLAP_CONFLICT", "同位置识别有差异"],
      ["OCR_NUMBERED_ITEM_GAP", "条目编号不连续"],
      ["OCR_GAP_RECOVERED_NEEDS_REVIEW", "局部补识别已完成，文字待核对"],
    ].filter(([code]) => item.warnings.includes(code)
      // 已对应当前片段的零疑点只取消页级低分提示，不清除其他异常或原记录。
      && !(code==='LOW_CONFIDENCE_OCR' && located && count===0)).map(([, message]) => message);
    if (!issues.length) return null;
    const warning = document.createElement("p"); warning.className = className;
    warning.textContent = `${issues.join("；")}，可对照原页。`;
    return warning;
  }

  function createMetadata(item) {
    const details = document.createElement("details"); details.className = "pending-result-details";
    const summary = document.createElement("summary"); summary.textContent = "来源与处理记录";
    const source = document.createElement("a"); source.className = "source-link"; source.textContent = "查看发布公告";
    if (globalThis.ArchSpecSourceMetadata?.safeUrl(item.source_url, item)) {
      source.href = item.source_url; source.target = "_blank"; source.rel = "noopener noreferrer";
    }
    const trace = document.createElement("small"); trace.className = "pending-snapshot";
    const originalRank = rawResultSet?.results.findIndex(candidate => sameEvidence(candidate, item)) ?? -1;
    trace.textContent = `处理快照 ${item.snapshot_id.slice(0, 12)}${originalRank >= 0 ? ` · 原检索第 ${originalRank + 1} 条` : ""}`;
    const method = document.createElement("p");
    method.textContent = item.source_format === "HTML"
      ? "文字来自保存的官方网页正文；图片是网页排版视图，非官方PDF页码。版本与使用范围尚未核验。"
      : "文字由文件提取或图片识别得到；页码为文件页码，原页保留水印。版本与使用范围尚未核验，相关度只用于排序。";
    details.append(summary, source, trace, method);
    if(item.warnings.includes('LOW_CONFIDENCE_OCR')) {
      const note=document.createElement('p');note.className='pending-recognition-note';
      const risk=item.recognition;
      const count=currentRecognitionCount(item),located=count!==null;
      if(located && count===0) {
        note.textContent='当前条文未发现低分行；旧原页的识别提醒仍保存在处理记录中，不代表整页文字已经核验。';
      } else if(located) {
        note.textContent=`当前条文有${count}处识别疑点，原页对照可定位疑点并放大重读。识别评分不是文字正确率。`;
        // 折叠详情只展示真实返回的短疑字，不用模型补字或把外书疑点带进来。
        for(const row of (Array.isArray(risk.issues)?risk.issues:[]).slice(0,2)) {
          if(typeof row?.text!=='string' || !Number.isInteger(row.pdf_page)
            || row.pdf_page<item.page_start || row.pdf_page>item.page_end)continue;
          const line=document.createElement('small');
          line.textContent=`文件第${row.pdf_page}页疑点：“${row.text.slice(0,60)}”`;note.append(line);
        }
      } else {
        note.textContent='原页存在低分识别记录，但无法确认是否属于当前条文。可打开原页对照查看；识别评分不是文字正确率。';
      }
      details.append(note);
    }
    const fixedDetails = globalThis.ArchSpecSourceMetadata?.details(item);
    if (fixedDetails) details.append(fixedDetails);
    return details;
  }

  function addSource(source) {
    if (!sourceSelect) return;
    if (![...sourceSelect.children].some(option => option.value === source.source_id)) {
      const option = document.createElement("option");
      option.value = source.source_id;
      option.textContent = `${source.title} · ${source.standard_code || "文号待核"}`;
      sourceSelect.append(option);
    }
  }
  const inventory=globalThis.ArchSpecOfficialInventory;
  let inventoryGeneration=0;
  function presentSources(sources) {
    const prepared=sources.filter(inventory?.prepared || (source=>source.acquired===true && source.kind==="FULL_TEXT" && !source.cache_error));
    prepared.forEach(addSource);
    // 地方覆盖仍核对精确登记公告与附件，准备状态不是适用性确认。
    const yichang=prepared.some(source=>
      source.page_url==="https://zrzy.yichang.gov.cn/content-42395-986231-1.html"
      && source.pdf_url==="https://img.yichang.gov.cn/upload2022/2025/1029/20251029034657227.pdf");
    if(coverageHint) coverageHint.textContent="已准备 "+prepared.length+" 份完整正文 · 宜昌地方正文"+(yichang?"已准备":"未准备");
  }
  async function loadSources({refresh=false, ownerCurrent=()=>true}={}) {
    if(!sourceSelect) return;
    const own=++inventoryGeneration;
    try {
      let sources;
      if(inventory) sources=await inventory.read({refresh});
      else {
        // 历史嵌入页面单独加载旧脚本时保留原接口，新读取失败不回退到旧结果。
        const response=await fetch("/api/official-documents");
        if(!response.ok) throw Error("来源清单读取失败");
        sources=await globalThis.ArchSpecHTTP.json(response, "正文清单响应暂不可读取", "array");
        if(!Array.isArray(sources)) throw Error("来源清单格式无效");
      }
      if(!Array.isArray(sources)) throw Error("来源清单格式无效");
      if(own!==inventoryGeneration || !ownerCurrent()) return false;
      presentSources(sources);
      return true;
    } catch {
      if(own===inventoryGeneration && ownerCurrent() && coverageHint) coverageHint.textContent="正文清单暂不可读取，覆盖范围尚不能确认。";
      return false;
    }
  }
  inventory?.subscribe(snapshot=>{
    if(snapshot.status==="READY") presentSources(snapshot.sources);
    else if(snapshot.status==="UNKNOWN") {
      if(coverageHint) coverageHint.textContent="正文清单暂不可读取，覆盖范围尚不能确认。";
    } else {
      if(coverageHint) coverageHint.textContent="正在更新正文清单；准备数量尚待确认。";
      // LOADING可能由当前loadSources的refresh同步发布；复用库存任务，不能让订阅重新认领并取消外层读取。
      void inventory.read().catch(()=>{});
    }
  });
  document.addEventListener("archspec:project-change", event => {
    readingProjectId = event?.detail?.projectId || "";
    invalidateSearch({discardPosition: !event?.detail?.restoring}); status.textContent = "项目已切换，可为当前项目重新试查正文。";
  });
  // 问答出处直接进入同一个阅读区，不再查询latest，也不触发联网发现。
  function fixedCitationReady(item) {
    if(!item) return false;
    if (typeof item.source_id !== "string" || !/^(gb55031|official-[a-f0-9]{32})$/.test(item.source_id)
      || typeof item.snapshot_id !== "string" || !/^[a-f0-9]{64}$/.test(item.snapshot_id)
      || typeof item.id !== "string" || !item.id.startsWith(`${item.source_id}:${item.snapshot_id}:`)
      || !/^(0|[1-9]\d{0,5})$/.test(item.id.slice(item.source_id.length + item.snapshot_id.length + 2))
      || !Number.isInteger(item.page_start) || !Number.isInteger(item.page_end)
      || item.page_start < 1 || item.page_end < item.page_start || item.page_end > (bounds?bounds.limit(item):200)
      || typeof item.text !== "string" || !Array.isArray(item.warnings)) return false;
    return true;
  }
  function openFixedCitation(item, initialPage, tableReading=null, {view="ORIGINAL",userAction=true,recordTrail=true,bookPages=null}={}) {
    if(!fixedCitationReady(item) || !["TEXT","OUTLINE","OTHER","ORIGINAL"].includes(view)) return false;
    const wholeBook=view==='ORIGINAL'&&globalThis.ArchSpecBookPages?.valid(bookPages,item);
    if(initialPage!==undefined && (!Number.isInteger(initialPage)||initialPage<1
      ||(wholeBook?initialPage>bookPages.page_count:initialPage<item.page_start||initialPage>item.page_end)))return false;
    const before=fixedChunkIdentity(selectedItem),position=readingPosition();
    const previous=recordTrail&&userAction&&before&&selectedItem?{
      id:before.id,source_id:selectedItem.source_id,snapshot_id:selectedItem.snapshot_id,
      page:position?.page||selectedItem.page_start,view:readerMode,title:selectedItem.title,
      ...(selectedItem.page_count!==undefined?{page_count:selectedItem.page_count}:{}),
      clause:selectedItem.clause_number,fingerprint:selectedItem.snapshot_fingerprint,bookPage:position?.bookPage===true}:null;
    const destinationPage=initialPage===undefined?item.page_start:initialPage;
    invalidateSearch({preserveTrail:true});
    render({method:"FIXED_CITATION",results:[item]},false,true);
    const row=renderedRows.get(0);
    if(!row) return false;
    if(previous&&(previous.id!==item.id||previous.page!==destinationPage||previous.view!==view))readingTrail?.push(previous);
    openReader(item,row.card,row.compare,view==="ORIGINAL",userAction,initialPage,tableReading,view,bookPages);
    return true;
  }
  document.addEventListener("archspec:qa-evidence-open", event => {
    const {item,query,projectId,initialPage,tableReading,bookPages}=event.detail || {};
    if(query!==input.value.trim() || (projectId || "")!==readingProjectId) return;
    if(openFixedCitation(item,initialPage,tableReading,{bookPages}))
      status.textContent="已打开问答的固定出处，未重新查询或更换规范版本。";
  });
  document.addEventListener('archspec:qa-history-restored',event=>{
    const owner=event.detail;
    // 只认已恢复且仍占有当前输入的历史题，不把上一题的“请重新查询”留作当前状态。
    if(owner?.query===input.value.trim()&&owner.sourceId===sourceSelect?.value&&(owner.projectId||'')===currentProject())
      status.textContent='已恢复历史问题与等待状态；需要重新整理时，请在本题中继续。';
  });
  document.addEventListener("archspec:official-source-ready", async event => {
    void loadSources({refresh:true});
    invalidateSearch();
    addSource(event.detail);
    if (sourceSelect) sourceSelect.value = event.detail.source_id;
    input.value = event.detail.query || "";
    document.querySelector("#pending-search-panel")?.scrollIntoView({behavior: scrollBehavior(), block: "start"});
    if (input.value.trim()) await submitSearch();
    else status.textContent = "已选择这份正文，请填写问题或条款号进行试查。";
  });
  if (sourceSelect) sourceSelect.addEventListener("change", () => { invalidateSearch({discardPosition: true}); status.textContent = "已切换正文，请重新查询。"; });
  input.addEventListener("input", () => { invalidateSearch({discardPosition: true}); status.textContent = "问题已修改，请重新查询。"; });

  function renderCoverage(data) {
    if (!Array.isArray(data.coverage)) return;
    // 范围是本次实际读取的缓存清单，不是全国规范目录或现行性证明。
    const details = document.createElement("details");
    details.className = "pending-search-coverage";
    const summary = document.createElement("summary");
    summary.textContent = "检索范围与处理记录";
    const notice = document.createElement("p");
    notice.className = "source-warning";
    notice.textContent = data.notice || "仅检索本机已准备正文，可打开原页查阅完整内容。";
    const list = document.createElement("ul");
    const states = {EXCLUDED_METADATA:"不在本次筛选范围", METADATA_ERROR:"来源档案无法核对", SEARCHED: "已检索", NOT_ACQUIRED: "正文未准备", CACHE_ERROR: "缓存读取失败", EXCLUDED_ATTACHMENT: "不是完整正文", LIMIT_REACHED: "超过本次检索上限"};
    data.coverage.forEach(item => {
      const row = document.createElement("li");
      row.className = item.state === "SEARCHED" ? "coverage-searched" : "coverage-skipped";
      const title = document.createElement("strong");
      title.textContent = `${item.title || "未识别名称"}${item.standard_code ? ` · ${item.standard_code}` : ""}`;
      const reason = document.createElement("p");
      reason.textContent = `${states[item.state] || "状态待核对"}${item.reason ? `：${item.reason}` : ""}`;
      row.append(title, reason);
      list.append(row);
    });
    if (!data.coverage.length) {
      const empty = document.createElement("li");
      empty.textContent = "本次没有可评估的已登记来源，不代表没有规范要求。";
      list.append(empty);
    }
    const processing = document.createElement("p");
    processing.textContent = `本次读取 ${data.searched_sources} 份正文、${data.searched_chunks} 个缓存片段；缓存查询不重新下载或识别。`;
    details.append(summary, processing, notice, list);
    if (Array.isArray(data.related_coverage)) {
      const relatedNote = document.createElement("p");
      relatedNote.textContent = "关联候选已另查全库缓存；上面的读取数量只统计本次地方范围，两次分数不合并比较。";
      details.append(relatedNote);
    }
    results.append(details);
  }

  function sameEvidence(a, b) {
    // 缓存补全后只保留仍存在的同一来源、快照、片段及原文；同号条款并不等同。
    return a && b && a.source_id === b.source_id && a.snapshot_id === b.snapshot_id && a.id === b.id
      && a.clause_number === b.clause_number && a.page_start === b.page_start && a.page_end === b.page_end && a.text === b.text;
  }

  function renderResultsPage() {
    // 后台补全会重建结果卡片；仅恢复仍存在的同一证据控件，避免键盘焦点掉回页面顶部。
    const active = document.activeElement;
    const filterSettingsOpen = results.querySelector?.('.pending-display-details')?.open === true;
    const resultToolsOpen=results.querySelector?.('.pending-result-tools')?.open===true;
    // 分类与筛选也随卡片重建；按固定控件标识恢复，不去猜用户接下来想读哪条。
    const focusedFilter = active?.matches?.('[data-result-group]')
      ? `[data-result-group="${active.getAttribute('data-result-group')}"]`
      : active?.matches?.('.pending-topic-filter') ? '.pending-topic-filter'
      : active?.matches?.('.pending-display-order') ? '.pending-display-order'
      : active?.matches?.('.pending-display-details > summary') ? '.pending-display-details > summary' : null;
    const restoreFilterFocus = () => focusedFilter && results.querySelector?.(focusedFilter)?.focus({preventScroll:true});
    const focusedCard = active?.closest?.(".pending-result-card");
    const focusEvidence = focusedCard && cardEvidence.get(focusedCard);
    const focusKind = focusEvidence && (active.matches(".pending-page-open") ? "page"
      : active.matches(".pending-result-open") ? "read"
      : active.matches(".pending-result-details > summary") ? "details" : active.matches(".source-link") ? "source" : null);
    const detailsOpen = focusKind && active.closest(".pending-result-details")?.open;
    let restoreFocus = null;
    results.replaceChildren(); renderedRows.clear();
    const data = resultSet;
    if (!data) return;
    renderLookupControls(data);
    renderPresentationControls(data);
    const filterSettings = results.querySelector?.('.pending-display-details');
    if (filterSettings) filterSettings.open = filterSettingsOpen;
    const resultTools=results.querySelector?.('.pending-result-tools');if(resultTools)resultTools.open=resultToolsOpen;
    const unified = Array.isArray(data.coverage);
    if (data.results.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = activeDenial() && rawResultSet?.results.some(denied)
        ? denialText()+" 当前列表没有可阅读候选；可明确提交新查询核对当前许可。"
        : data.lookupView && !includeRelated && !data.lookupView.primary_indices.length
        ? "尚未找到对应主题的候选。可查看关联候选中的保留原文；联网补充的正文准备后会更新。"
        : rawResultSet?.results.length
        ? "当前分类或主题筛选中没有候选；可切换分类、选择全部主题，查看本次其余结果。不代表没有相关规范要求。"
        : unified
        ? "本次已准备的缓存正文中未找到匹配结果，不代表没有规范要求。请展开检索范围查看未查来源，也可以换关键词。"
        : "这份缓存正文中未找到匹配结果，不代表其他规范没有要求。可以换关键词或补充其他规范来源。";
      results.append(empty);
      renderCoverage(data);
      restoreFilterFocus();
      return;
    }
    const pagination = document.createElement("nav"); pagination.className = "pending-pagination";
    pagination.setAttribute("aria-label", "本次候选结果分页");
    const pagePosition = document.createElement("span"); pagePosition.className = "pending-page-position";
    const start = listPage * pageSize, end = Math.min(start + pageSize, data.results.length);
    pagePosition.textContent = `${start + 1}—${end} / ${data.results.length} 条候选`;
    const previousPage = document.createElement("button"); previousPage.type = "button"; previousPage.className = "pending-page-previous"; previousPage.textContent = "上一页";
    const nextPage = document.createElement("button"); nextPage.type = "button"; nextPage.className = "pending-page-next"; nextPage.textContent = "下一页";
    previousPage.disabled = listPage === 0; nextPage.disabled = end >= data.results.length;
    const generation = searchGeneration;
    function movePage(delta) {
      // 翻页只操作本次已有候选，不重新查询、联网、下载或识别；旧控件也不能复活旧项目。
      if (generation !== searchGeneration || data !== resultSet) return;
      const page = listPage + delta;
      if (page < 0 || page * pageSize >= data.results.length) return;
      listPage = page; renderResultsPage();
      const row = renderedRows.get(page * pageSize);
      openReader(data.results[page * pageSize], row.card, row.read, false, false);
      row.read.focus({preventScroll: true});
    }
    previousPage.addEventListener("click", () => movePage(-1)); nextPage.addEventListener("click", () => movePage(1));
    pagination.append(pagePosition, previousPage, nextPage); results.append(pagination);
    data.results.slice(start, end).forEach((item, offset) => {
      const index = start + offset;
      const card = document.createElement("article");
      card.className = "result-card pending-result-card";
      cardEvidence.set(card, item);
      const name = document.createElement("strong");
      name.className = "standard-name";
      name.textContent = item.title+' · '+(item.standard_code || '文号待核');
      const sourceBadge=globalThis.ArchSpecSourceMetadata?.badgeElement(item);
      if(sourceBadge)name.append(sourceBadge);
      const cardStatus=document.createElement('p');cardStatus.className='pending-card-status';
      cardStatus.textContent=(globalThis.ArchSpecSourceMetadata?.edition(item) || '正式版本待核')+' · '+(globalThis.ArchSpecSourceMetadata?.category(item)||'类别待核');
      const sourceValidity=globalThis.ArchSpecSourceMetadata?.validity(item);
      if(sourceValidity && ['WITHDRAWN','SUPERSEDED'].includes(sourceValidity.version_status.value)){
        cardStatus.className+=' norm-state-retired';cardStatus.textContent+=' · 核验记录标记废止或被替代';
      }
      if(typeof item.score==='number' && Number.isFinite(item.score)){
        const match=document.createElement('span');match.className='pending-match-score';
        match.textContent='相关度 '+item.score.toFixed(2);match.title='仅用于候选排序，非正确率或适用性';cardStatus.append(match);
      }
      const heading = document.createElement("h3");
      heading.className = "clause-title";
      // 分类与标题使用同一文字线索，不能把“1.0倍”的续文又标成章节。
      // 这里只区分显示，不删原文、不猜改 OCR（图片文字识别）得到的编号。
      const description = presentation?.describe(item);
      heading.textContent = description?.kind === 'CLAUSE'
        ? `候选条款 ${item.clause_number}`
        : description?.kind === 'NAVIGATION'
          ? `章节 / 目录片段 ${item.clause_number || '未识别编号'}`
          : item.clause_number ? `待辨认片段 ${item.clause_number}`
          : "未识别条款号的候选片段";
      const read = document.createElement("button"); read.type = "button"; read.className = "pending-result-open";
      read.textContent = heading.textContent;
      read.setAttribute("aria-controls", "pending-page-reader"); read.setAttribute("aria-current", "false");
      read.setAttribute("aria-label", `阅读 ${item.title} ${item.clause_number || "候选片段"} 的完整文字`);
      heading.replaceChildren(read);
      const page = document.createElement("p");
      page.className = "page-number";
      page.textContent = filePageLabel(item);
      const text = document.createElement("blockquote");
      text.className = "clause-text";
      // 列表是真正的短摘录，不把整段原文藏在CSS里；完整文字只出现在选中阅读区。
      const excerpt = item.text.replace(/\s+/g, " ").trim();
      text.textContent = excerpt.length > 130 ? `${excerpt.slice(0, 130)}…（摘录）` : excerpt;
      const warning = createRecognitionWarning(item, "source-warning");
      const cues = document.createElement("p"); cues.className = "pending-design-cues";
      const info = data.viewInfo;
      const labels = (info?.topics || []).filter(topic => description?.topics.includes(topic.id)).map(topic => topic.label);
      cues.textContent = `${labels.join(" · ")}${description?.numericCue ? `${labels.length ? " · " : ""}含数值条件` : ""}`;
      // 公告和处理记录用于追溯，不与条文文字争抢注意力；风险提示仍放在卡片外层。
      const metadata = createMetadata(item);
      const compare = document.createElement("button");
      compare.type = "button"; compare.className = "pending-page-open";
      compare.textContent = "对照原页";
      compare.setAttribute("aria-controls", "pending-page-reader");
      compare.setAttribute("aria-pressed", "false");
      compare.setAttribute("aria-label", `对照 ${item.title} ${item.clause_number || "候选片段"} 的原始页面`);
      const current = () => generation === searchGeneration && data === resultSet;
      compare.addEventListener("click", () => { if (current()) openReader(item, card, compare, true); });
      read.addEventListener("click", () => { if (current()) openReader(item, card, read); });
      if (sameEvidence(item, selectedItem)) {
        card.className += " is-selected"; read.setAttribute("aria-current", "true");
        const usingOriginal = readerMode === "ORIGINAL";
        compare.setAttribute("aria-pressed", usingOriginal ? "true" : "false");
        selectedCard = card; selectedButton = usingOriginal ? compare : read; selectedReadButton = read;
      }
      if (!restoreFocus && focusKind && sameEvidence(item, focusEvidence)) {
        if (detailsOpen) metadata.open = true;
        restoreFocus = focusKind === "page" ? compare : focusKind === "read" ? read
          : focusKind === "details" ? metadata.children[0] : metadata.children[1];
      }
      card.append(name, heading, cardStatus, page, ...(cues.textContent ? [cues] : []), text, ...(warning ? [warning] : []), compare, metadata);
      renderedRows.set(index, {card, read, compare});
      results.append(card);
    });
    renderCoverage(data);
    restoreFocus?.focus({preventScroll: true});
    restoreFilterFocus();
  }

  function renderPresentationControls(data) {
    if (!data.viewInfo) return;
    const ownGeneration = searchGeneration;
    const controls = document.createElement("details"); controls.className = "pending-result-tools";
    const toolsTitle=document.createElement("summary");toolsTitle.textContent="筛选结果";controls.append(toolsTitle);
    const groups = document.createElement("div"); groups.className = "pending-result-groups";
    groups.setAttribute("role", "group"); groups.setAttribute("aria-label", "候选内容分类");
    const current = () => ownGeneration === searchGeneration && data === resultSet;
    data.viewInfo.groups.forEach(group => {
      const control = document.createElement("button"); control.type = "button";
      control.textContent = `${group.label} ${group.count}`;
      control.setAttribute("data-result-group",group.id);
      control.setAttribute("aria-pressed",String(group.id === data.viewInfo.group));
      control.addEventListener("click", () => {
        if (!current()) return;
        displayGroup = group.id; displayTopic = "ALL";
        render(rawResultSet, true);
        results.querySelector?.(`[data-result-group="${group.id}"]`)?.focus({preventScroll:true});
      });
      groups.append(control);
    });
    const filters = document.createElement("div"); filters.className = "pending-display-filters";
    const settings = document.createElement('details'); settings.className = 'pending-display-details';
    const settingsTitle = document.createElement('summary'); settingsTitle.textContent = '主题筛选与显示顺序';
    const topicLabel = document.createElement("label"); topicLabel.textContent = "设计主题";
    const topic = document.createElement("select"); topic.className = "pending-topic-filter";
    topic.setAttribute("aria-label","筛选本次候选的设计主题");
    const all = document.createElement("option"); all.value = "ALL"; all.textContent = "全部主题"; topic.append(all);
    data.viewInfo.topics.forEach(entry => {
      const option = document.createElement("option"); option.value = entry.id;
      option.textContent = `${entry.label}（${entry.count}）`; topic.append(option);
    });
    topic.value = displayTopic;
    topic.addEventListener("change", () => { if (current()) {
      displayTopic = topic.value; render(rawResultSet, true);
      results.querySelector?.(".pending-topic-filter")?.focus({preventScroll:true});
    }});
    const sortLabel = document.createElement("label"); sortLabel.textContent = "显示顺序";
    const sort = document.createElement("select"); sort.className = "pending-display-order";
    sort.setAttribute("aria-label","本次结果显示顺序");
    for (const [value,label] of [["DESIGN","相关指标优先"],["ORIGINAL","原检索顺序"]]) {
      const option = document.createElement("option"); option.value=value;option.textContent=label;sort.append(option);
    }
    sort.value=displaySort;
    sort.addEventListener("change", () => { if(current()) {
      displaySort=sort.value;render(rawResultSet,true);
      results.querySelector?.(".pending-display-order")?.focus({preventScroll:true});
    }});
    topicLabel.append(topic); sortLabel.append(sort); filters.append(topicLabel,sortLabel);
    const note = document.createElement("p"); note.className="pending-view-summary";
    note.textContent=`本次共 ${data.viewInfo.total} 条，当前显示 ${data.results.length} 条。分类和排序只调整本次列表，不修改原文。`;
    settings.append(settingsTitle,filters,note);
    controls.append(groups,settings);results.append(controls);
  }

  function render(data, preserveReader = false, suppressAutoOpen = false) {
    rawResultSet = data;
    if (!preserveReader) { displayGroup="AUTO";displayTopic="ALL";includeRelated=false; }
    const saved=!preserveReader ? readingPosition() : null;
    const restore=saved?.kind==="TEXT" && data.results.some(item=>item.id===saved.id
      && item.source_id===saved.source_id && item.snapshot_id===saved.snapshot_id) ? saved : null;
    if(restore){
      if(["AUTO","ALL","CLAUSE","NAVIGATION","OTHER"].includes(restore.group))displayGroup=restore.group;
      displayTopic=typeof restore.topic==="string" ? restore.topic : "ALL";
      includeRelated=restore.includeRelated===true;
    }
    const lookup=checkedLookup(data);showLookup(lookup);
    // 正文可读与问题已确认分别展示；默认列出待确认候选，不自动打开或采纳。
    const observed=lookup?.observations && presentation?.lookupObservations(lookup.observations,lookup);
    if(!preserveReader && !restore && lookup?.primary_indices.length===0
      && observed?.source.state==="READABLE" && observed?.candidates.state==="UNCONFIRMED")includeRelated=true;
    if(data.method === "SEMANTIC_ONLY" && !lookup?.primary_indices.length) includeRelated = true;
    if(lookup){
      const chosen=includeRelated ? data.results : lookup.primary_indices.map(index=>data.results[index]);
      data={...data,results:chosen,lookupView:lookup};
    }
    // 只移除本轮已被当前读取门否决的展示，rawResultSet原字及观察不被删除或篡改。
    data = {...data, results:data.results.filter(item => !denied(item))};
    if (presentation) {
      const view = presentation.present(data.results, input.value.trim(), {
        // 手动纳入另一范围时AUTO不能因新增带条号参考而隐藏原地方命名段；手选分类仍保留。
        group:Array.isArray(data.related_coverage) && includeRelated && displayGroup === "AUTO" ? "ALL" : displayGroup,
        topic:displayTopic, sort:Array.isArray(data.related_coverage) ? "ORIGINAL" : displaySort});
      // AUTO是持续的默认策略，不是用户选择。初次只有空目录时，不能锁住后来新取得的条文。
      // 只有显式点击分类按钮才固定displayGroup；用户手选的目录/片段筛选仍保留。
      data = {...data, results:view.results, viewInfo:view};
    }
    const retainedIndex = preserveReader && selectedItem
      ? data.results.findIndex(item => sameEvidence(item, selectedItem)) : -1;
    const keepReader = retainedIndex >= 0;
    resultSet = data;
    if (!keepReader) { clearReader(); listPage = 0; }
    else { selectedIndex = retainedIndex; listPage = Math.floor(retainedIndex / pageSize); updateReaderNavigation?.(); }
    const method = data.method === "EXACT_CLAUSE" ? "条款号精确匹配" : "关键词排序";
    const unified = Array.isArray(data.coverage);
    status.textContent = lookup
      ? "已查"+(unified ? " "+data.searched_sources+" 份缓存正文" : "当前缓存正文")+
        " · 找到 "+lookup.primary_indices.length+" 条候选 · 关联候选 "+lookup.related_indices.length+" 条"
      : unified
      ? `检索 ${data.searched_sources} 份正文，找到 ${rawResultSet.results.length} 条。`
      : `${method}，找到 ${rawResultSet.results.length} 条。`;
    if(data.method === "SEMANTIC_ONLY") status.textContent = "按意思找到 "+rawResultSet.results.length+" 条候选，请选择后核对原文。";
    const parallel=parallelSummary(lookup);
    if(parallel)status.textContent=parallel.text;
    renderResultsPage();
    if (!suppressAutoOpen && !keepReader && data.results.length && reader) {
      const allowed=new Set(lookup?.primary_indices.map(index=>rawResultSet.results[index]));
      const restoredIndex=restore ? data.results.findIndex(item=>item.id===restore.id
        && item.source_id===restore.source_id && item.snapshot_id===restore.snapshot_id) : -1;
      const first=restoredIndex>=0 ? restoredIndex : lookup ? data.results.findIndex(item=>allowed.has(item)) : 0;
      if(restoredIndex>=0){listPage=Math.floor(first/pageSize);renderResultsPage();}
      const row=renderedRows.get(first), item=data.results[first];
      const view=restoredIndex>=0 && ["TEXT","OUTLINE","OTHER","ORIGINAL"].includes(restore.view) ? restore.view : "TEXT";
      const page=restoredIndex>=0 && Number.isInteger(restore.page) && restore.page>=item.page_start
        && restore.page<=item.page_end ? restore.page : undefined;
      if(row)openReader(item,row.card,row.read,view==="ORIGINAL",false,page,null,view);
    }
    if(lookup && !keepReader && !selectedItem && reader){
      const note=document.createElement("p");note.className="reader-lookup-gap";
      note.textContent=lookup.notes.join(" ");reader.append(note);
    }
  }

  async function submitSearch({notifyOnline = true, preserveResults = false, reuseGeneration = false, reuseQueryFlow = false, onPrepared, publicTitlesReady, ownerCurrent=()=>true, explicitRead=notifyOnline, retrievalMode="KEYWORD"} = {}) {
    if(!ownerCurrent() || activeDenial() && !explicitRead) return false;
    const query = input.value.trim();
    if (!query) {
      status.textContent = "请先输入问题或关键词。";
      return false;
    }
    if(!reuseGeneration)readingTrail?.clear();
    button.disabled = true;
    const generation = reuseGeneration ? searchGeneration : ++searchGeneration;
    const sourceId = sourceSelect?.value || "gb55031";
    const projectId = currentProject(), requestSerial = ++cacheRequestSerial;
    const ownReadabilityEpoch = readabilityEpoch;
    readContext = {generation, query, sourceId, projectId, retrievalMode};
    // __all__ 是网页上的“全部”选项，不是某一本规范的来源编号。
    const scopedIds=sourceId.startsWith('set:')?globalThis.ArchSpecSourceScope?.decode(sourceId):sourceId==='__all__'?null:[sourceId];
    if(scopedIds===undefined){status.textContent='所选多本范围无法核对，请重新选择；未扩大为全部正文。';button.disabled=false;return false;}
    const unified = sourceId === "__all__" || scopedIds.length>1;
    const isCurrent = () => generation === searchGeneration && sourceId === (sourceSelect?.value || "gb55031") && query === input.value.trim() && projectId === currentProject() && ownerCurrent();
    // 联网任务继续核同一问题；只有最新读取可更新列表、错误和按钮状态。
    const requestCurrent = () => isCurrent() && requestSerial === cacheRequestSerial;
    status.textContent = retrievalMode === "SEMANTIC" ? "正在按意思查找本机正文，首次准备可能稍慢……" : preserveResults ? "新正文已准备，正在更新缓存检索；暂保留上次结果……" : "正在查找缓存正文……";
    if (!preserveResults) { results.replaceChildren(); clearReader(); showLookup(null); }
    let finishLocal, localBody = null;
    const localFinished = new Promise(resolve => { finishLocal = resolve; });
    try {
      const url = unified ? "/api/official-documents/search" : `/api/official-documents/${encodeURIComponent(sourceId)}/search`;
      // 单本也交给同一问题计划识别明确条号/主题；范围仍由原单本路径绑定，不扩大到全库。
      const requestBody = {query, top_k: unified ? 10 : 5, lookup_context: true, lookup_scope: "PLANNED"};
      if (retrievalMode === "SEMANTIC") requestBody.retrieval_mode = "SEMANTIC";
      // 两个入口只传项目身份，由服务器读取已登记地域；原问题保持不变。
      if (projectId) requestBody.project_id = projectId;
      if (unified) requestBody.ranking_version = retrievalMode === "SEMANTIC" ? "V1" : optimizedRanking ? "V2" : "V1";
      if (scopedIds?.length>1) requestBody.source_ids=scopedIds;
      const request = body => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      // 先发本地请求，再独立触发官方补找；无需等官网才能显示缓存结果。
      const initialRequest = request(requestBody);
      if (notifyOnline && document.dispatchEvent) {
        document.dispatchEvent(new CustomEvent("archspec:query-start", {detail: {
          query, sourceId, isCurrent, localFinished,
          onPrepared, reuseQueryFlow, publicTitlesReady,
          refreshCached: () => isCurrent() ? submitSearch({notifyOnline: false, preserveResults: true, reuseGeneration: true, retrievalMode:currentRetrievalMode(query,sourceId,projectId)}) : Promise.resolve(),
        }}));
      }
      let response = await initialRequest;
      let body = await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");
      let downgraded = false;
      // 资料更新后旧成绩可能过期：服务端拒绝 V2 时，仅重查一次原排序。
      if (unified && requestBody.ranking_version === "V2" && response.status === 409 && requestCurrent()) {
        optimizedRanking = false;
        requestBody.ranking_version = "V1";
        response = await request(requestBody);
        body = await globalThis.ArchSpecHTTP.json(response, "条文检索响应暂不可读取");
        downgraded = true;
      }
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "检索没有成功，请检查输入。");
      if (!requestCurrent()) return false;
      if (!body || !Array.isArray(body.results)) throw new Error("检索结果列表无法核对，请重新查询。");
      if (unified) {
        plannedResultSet = body; relatedResultSet = null; relatedLoading = false;
        lastLookupRequest = {...requestBody};
      }
      // 仅明确动作经服务端成功重读可解除旧否决；请求之后出现的新否决不能被迟到响应冲掉。
      if (explicitRead && ownReadabilityEpoch === readabilityEpoch) readabilityDenial = null;
      render(body, preserveResults);
      localBody = body;
      if (downgraded) status.textContent = `搜索检查已过期或未通过，已改用原排序。${status.textContent}`;
      return true;
    } catch (error) {
      if (requestCurrent()) status.textContent = error.message || "无法连接服务，请确认后端已启动。";
      return false;
    } finally {
      if (requestCurrent()) button.disabled = false;
      finishLocal(localBody);
    }
  }
  // 历史只保存任务身份；恢复必须重新读当前清单和正文许可，不能重放旧摘录。
  async function restoreCached({query, sourceId, projectId=null, isCurrent, userAction=false}={}) {
    if(typeof query!=="string" || !query.trim() || query.trim().length>500
      || typeof sourceId!=="string" || typeof isCurrent!=="function") return false;
    const expectedQuery=query.trim(), expectedProject=projectId || "";
    const ownerCurrent=()=>isCurrent() && input.value.trim()===expectedQuery
      && sourceSelect?.value===sourceId
      && (document.querySelector("#search-project")?.value || "")===expectedProject;
    if(!ownerCurrent()) return false;
    const listed=await loadSources({refresh:true,ownerCurrent});
    if(!listed || !ownerCurrent()) return false;
    // 走原检索及阅读入口；不发query-start，不附带onPrepared，不调用问答/模型。
    // 同题重检保留现有查询代次；外层owner继续负责取消，避免重检自行使当前流程失效。
    return submitSearch({notifyOnline:false,ownerCurrent,reuseGeneration:true,explicitRead:userAction,
      retrievalMode:currentRetrievalMode(expectedQuery,sourceId,expectedProject)});
  }
  let readingRestoreTask=null;
  function restoreReadingPosition() {
    // 刷新只取同标签页的位置身份；正文和权限始终重新向服务器读取。
    let saved;
    try { saved=JSON.parse(sessionStorage.getItem("archspec:reading-position") || "null"); }
    catch { return false; }
    if(!["TEXT","ORIGINAL_ONLY"].includes(saved?.kind) || typeof saved.query!=="string"
      || saved.query.length>500 || typeof saved.scope!=="string" || saved.projectId!==currentProject()
      || input.value.trim() && input.value.trim()!==saved.query) return false;
    if(readingRestoreTask) return readingRestoreTask;
    const originalOnly=saved.kind==="ORIGINAL_ONLY";
    const initialGeneration=searchGeneration,projectId=currentProject();
    const unchanged=()=>searchGeneration===initialGeneration && currentProject()===projectId
      && (!input.value.trim() || input.value.trim()===saved.query);
    readingRestoreTask=(async()=>{
      const loaded=await initialInventory;
      if(!unchanged()) return false;
      if(saved.scope.startsWith('set:')&&!globalThis.ArchSpecSourceScope?.ensure(document,saved.scope)){status.textContent='阅读恢复的多本范围尚不可用，未扩大为全部正文。';return true;}
      // 仅原件不会出现在可检索正文清单中；固定原件GET负责重新核权，不以READY清单代替。
      if(!originalOnly && !loaded || saved.scope!=="__all__" && ![...sourceSelect.children].some(option=>option.value===saved.scope)) {
        status.textContent="阅读恢复未完成：当前正文清单暂不可用，请重新查询。";
        return true;
      }
      input.value=saved.query;sourceSelect.value=saved.scope;globalThis.ArchSpecSourceScope?.choose(document,saved.scope);
      // 固定阅读恢复只更新本模块代次，不伪造用户change事件取消同题等待任务。
      // 原身份仍保留；其它入口各自GET恢复，不自动重新执行。
      invalidateSearch();
      const own=searchGeneration;
      const current=()=>own===searchGeneration && currentProject()===projectId
        && input.value.trim()===saved.query && sourceSelect.value===saved.scope;
      try {
        if(originalOnly) {
          // 恢复先于联网开关，只重读原来源与原件pin；无预览的位置用null，不推造页码或正文。
          if(!/^(gb55031|official-[a-f0-9]{32})$/.test(saved.source_id || "")
            || !/^[a-f0-9]{64}$/.test(saved.snapshot_id || "") || saved.view!=="ORIGINAL"
            || !(saved.page===null || Number.isInteger(saved.page) && saved.page>=1 && saved.page<=500)
            || !globalThis.ArchSpecOriginalReader?.open)throw Error("保存的原件身份无法核对");
          const restored=await globalThis.ArchSpecOriginalReader.open(
            {source_id:saved.source_id,snapshot_id:saved.snapshot_id},{isCurrent:current,initialPage:saved.page});
          if(!current())return false;
          if(!restored)throw Error("固定原件当前无法核对");
          status.textContent="已重新核读上次固定原件与阅读位置；未重跑问答、联网或获取原件。";
          return true;
        }
        // 阅读位置可能来自排名外的问答补读；只能按固定片段重取，不能重新排名后换开首条。
        const fixed = fixedChunkIdentity(saved);
        if (!fixed) throw Error("保存的阅读身份无法核对");
        const index = fixed.index;
        const response=await fetch("/api/official-documents/"+encodeURIComponent(saved.source_id)
          +"/snapshots/"+saved.snapshot_id+"/chunks/"+index,{method:"GET",cache:"no-store"});
        const item=await globalThis.ArchSpecHTTP.json(response,"固定出处暂不可读取");
        if(!current()) return false;
        if(!response.ok || item?.id!==fixed.id || item.source_id!==saved.source_id
          || item.snapshot_id!==saved.snapshot_id)throw Error("固定出处当前无法核对");
        // 全书页码必须重核当前固定原件范围；旧位置不能自行扩大条文页码。
        let bookPages=null;
        if(saved.bookPage===true&&saved.view==='ORIGINAL'){
          bookPages=await globalThis.ArchSpecBookPages?.read(item,{isCurrent:current});
          if(!current())return false;if(!bookPages)throw Error('本书原页范围尚不能恢复');
        }
        if(!openFixedCitation(item,saved.page,null,{view:saved.view,userAction:false,bookPages}))throw Error("固定出处当前无法核对");
        status.textContent="已重新核读上次固定出处与页码；未重跑问答、联网或获取原件。";
      } catch(error) {
        if(current()) status.textContent="阅读恢复未完成："+(error.message || "固定出处暂不可读取")+"，未替换为其它条文。";
      }
      return true;
    })();
    return readingRestoreTask;
  }
  // 收藏和题目关联都是主动核读，不是改问。沿原固定阅读门保留原题、条件与范围。
  let bookmarkReadSerial=0;
  async function openBookmark(entry) {
    if(!globalThis.ArchSpecBookmarks?.valid(entry)||entry.projectId!==currentProject())return false;
    const serial=++bookmarkReadSerial,own=searchGeneration,query=input.value.trim(),scope=sourceSelect.value;
    // 新的核读意图、改题/范围/项目或其它阅读均会使迟到结果失效；不取消后台原任务。
    const current=()=>serial===bookmarkReadSerial&&own===searchGeneration&&currentProject()===entry.projectId
      &&input.value.trim()===query&&sourceSelect.value===scope;
    const item=await globalThis.ArchSpecBookmarks.resolve(entry,{fetcher:fetch,isCurrent:current});
    if(!item||!current())return false;
    if(!openFixedCitation(item,entry.page,null,{view:'TEXT',userAction:true}))throw Error('收藏出处当前无法核对，未替换为其它条文。');
    document.querySelector('[data-workbench-view="search"]')?.click();
    reader?.scrollIntoView?.({block:'start',behavior:scrollBehavior()});
    status.textContent='已核读收藏的固定出处；原问题与范围保留，未重新检索、联网或调用模型。';return true;
  }
  async function openTaskPosition(position,{query,projectId,isCurrent,label='本题'}={}) {
    if(typeof isCurrent!=='function'||!isCurrent()||typeof query!=='string'||query!==input.value.trim()||(projectId||'')!==currentProject())return false;
    const fixed=fixedChunkIdentity({...position,id:position.chunk_id});
    if(!fixed)throw Error(label+'保存的固定位置无法核对。');
    const own=searchGeneration,current=()=>own===searchGeneration&&isCurrent()&&query===input.value.trim()&&(projectId||'')===currentProject();
    const response=await fetch('/api/official-documents/'+encodeURIComponent(position.source_id)+'/snapshots/'+position.snapshot_id+'/chunks/'+fixed.index,{method:'GET',cache:'no-store'});
    if(!current())return false;
    if(!response.ok){
      if(selectedItem?.source_id===position.source_id&&selectedItem.snapshot_id===position.snapshot_id){
        invalidateSearch();status.textContent=label+'同一固定出处当前不可读取；已保留位置，未沿用旧正文。';
      }
      throw Error(label+'固定出处当前无法读取，未改用其它版本。');
    }
    const item=await globalThis.ArchSpecHTTP.json(response,label+'固定出处暂不可读取');if(!current())return false;
    if(!fixedCitationReady(item)||item.id!==fixed.id||item.source_id!==position.source_id||item.snapshot_id!==position.snapshot_id
      ||item.snapshot_fingerprint!==position.snapshot_fingerprint||item.page_start!==position.page_start||item.page_end!==position.page_end)throw Error('当前原文与任务位置身份无法核对。');
    const opened=openFixedCitation(item,position.page_start,null,{view:'ORIGINAL',userAction:true});
    if(opened)status.textContent='已重读'+label+'保存的固定出处与原页；未重放旧回答、联网或模型调用。';
    return opened;
  }
  // 书目浏览已由BookIndex逐项GET核对；沿用同一固定阅读器，不虚构问题或改动原任务范围。
  function openLibraryCitation(item,{projectId,isCurrent}={}) {
    if(typeof isCurrent!=='function'||!isCurrent()||(projectId||'')!==currentProject()||!fixedCitationReady(item))return false;
    if(!openFixedCitation(item,item.page_start,null,{view:'TEXT',userAction:true}))return false;
    document.querySelector('[data-workbench-view="search"]')?.click();
    reader?.scrollIntoView?.({block:'start',behavior:scrollBehavior()});
    status.textContent='已打开本书的固定条文；这是主动书目浏览，未按当前问题检索或执行问答。';
    return true;
  }
  globalThis.ArchspecPendingSearch={openLibraryCitation,restoreCached,observeReadability,readingPosition,rememberReadingPosition,restoreReadingPosition,openBookmark,openTaskPosition,openComparisonPosition:(position,options)=>openTaskPosition(position,{...options,label:'对照条文'})};
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(button.disabled)return;discardReadingPosition();
    const query=input.value.trim(),scope=sourceSelect.value,project=currentProject(),own=searchGeneration;
    const current=()=>own===searchGeneration&&query===input.value.trim()&&scope===sourceSelect.value&&project===currentProject();
    button.disabled=true;
    try{
      // 先接通找整本书的原库存入口；带条号或普通问题仍沿原条文检索/联网合同。
      const opened=await globalThis.ArchSpecLibraryInventory?.openBookQuery(query,{isCurrent:current,sourceId:scope,
        readyOnly:document.querySelector('#pending-online-enabled')?.checked===true});
      if(opened||!current())return;
      // 默认 AI 复用既有问答、取件和联网流程，不重复创建关键词请求。
      const aiSearch=globalThis.ArchSpecOfficialQA?.searchDefault;
      const bookQuery=globalThis.ArchSpecLibraryInventory?.isBookQuery?.(query)===true;
      if(!bookQuery && document.querySelector('#official-qa-model-enabled')?.checked===true && aiSearch && await aiSearch())return;
      await submitSearch();
    }finally{if(current())button.disabled=false;}
  });
  // 回答按钮与普通查找共用这一条有限流程，不模拟表单提交，也不清除正在等待的回答。
  document.addEventListener('archspec:qa-query-start', event => {
    const task=event.detail;
    if(!task || typeof task.isCurrent!=='function' || !task.isCurrent()
      || task.query!==input.value.trim() || task.sourceId!==(sourceSelect?.value || 'gb55031')
      || task.projectId!==undefined&&(task.projectId||'')!==currentProject()) return;
    if(task.continueLookup===true){
      const owner=qaLookupOwner;
      if(owner && owner.generation===searchGeneration && owner.query===task.query
        && owner.sourceId===task.sourceId && owner.projectId===currentProject() && owner.taskId===(task.taskId||null)){
        // 保留原查阅/取件与其一次就绪机会，只交接当前条件下的接收者，不再发查阅/发现事件。
        owner.task=task;
      }else{
        status.textContent='本题继续只重读当前缓存；未重复联网。若需新查官方资料，请主动点击“查找条文”。';
      }
      return;
    }
    const owner={query:task.query,sourceId:task.sourceId,projectId:currentProject(),taskId:task.taskId||null,task,generation:null};
    qaLookupOwner=owner;
    const current=()=>qaLookupOwner===owner && owner.generation===searchGeneration && owner.query===input.value.trim()
      && owner.sourceId===(sourceSelect?.value || 'gb55031') && owner.projectId===currentProject();
    discardReadingPosition();
    const searching=submitSearch({reuseQueryFlow:true,publicTitlesReady:task.publicTitlesReady,onPrepared:(...args)=>{
      if(current() && owner.task.isCurrent())return owner.task.onPrepared?.(...args);
    }});
    owner.generation=searchGeneration;
    void searching;
  });

  document.querySelectorAll("[data-pending-query]").forEach(example => {
    example.addEventListener("click", () => {
      input.value = example.dataset.pendingQuery;
      // 程序赋值不会自动触发输入事件；统一使旧请求/历史/进度失效并解锁按钮。
      input.dispatchEvent(new Event("input", {bubbles: true}));
      input.focus();
    });
  });
  const initialInventory=loadSources();
  clearReader();
})();
