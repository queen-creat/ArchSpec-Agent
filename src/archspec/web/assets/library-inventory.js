// 共享规范库存只读取服务端登记清单；切换页面不会下载正文、识别图片或调用模型。
(() => {
  const panel = document.querySelector("#library-inventory-panel");
  if (!panel) return;
  const status = document.querySelector("#library-inventory-status");
  const list = document.querySelector("#library-inventory-list");
  const summary = document.querySelector("#library-inventory-summary");
  const refresh = document.querySelector("#library-inventory-refresh");
  const filter = document.querySelector("#library-filter");
  const clearFilter = document.querySelector("#library-filter-clear");
  const filterCount = document.querySelector("#library-filter-count");
  const filterEmpty = document.querySelector("#library-filter-empty");
  const metadataLoad=document.querySelector('#library-metadata-load'),levelFilter=document.querySelector('#library-level'),regionFilter=document.querySelector('#library-region');
  let wantMetadata=false,metadataLoaded=false;
  const scopeApply=document.querySelector('#library-scope-apply'),scopeNote=document.querySelector('#library-scope-note');

  let filterRows = [], catalogHint = null;
  function clearCatalogHint() {
    if (!catalogHint) return;
    catalogHint.node.hidden = true;
    catalogHint.node.remove?.();
    catalogHint = null;
    if (status) status.textContent = '目录线索已收起，可继续筛选本站正文。';
  }
  // 只比较已登记题名/编号；格式归一不生成新题名、版本、正文或来源身份。
  const filterKey = value => String(value || "").normalize("NFKC").toLowerCase().replace(/[\s\-—－/·]+/g, "");
  function applyFilter() {
    const key = filterKey(filter?.value),level=metadataLoaded?levelFilter?.value||'':'',region=metadataLoaded?filterKey(regionFilter?.value):'';
    // 目录线索独立于正文行；筛选改变即移除，不能借它拼出可检索source。
    if (catalogHint && (catalogHint.key !== key || level || region)) clearCatalogHint();
    let shown = 0;
    for (const row of filterRows) {
      row.node.hidden = !!key&&!row.key.includes(key)||!!region&&!row.region.includes(region)
        ||!!level&&(level==='UNKNOWN'?['NATIONAL','INDUSTRY','LOCAL'].includes(row.level):row.level!==level);
      if (!row.node.hidden) shown++;
    }
    if (clearFilter) clearFilter.hidden = !(filter?.value||levelFilter?.value||regionFilter?.value);
    if (filterCount) filterCount.textContent = filterRows.length ? `显示 ${shown} / ${filterRows.length} 份资料` : "";
    if (filterCount && catalogHint) filterCount.textContent = `本站正文清单无匹配 · 目录线索 ${catalogHint.count} 条`;
    if(scopeApply){
      const eligible=scopeRows();scopeApply.disabled=loading||!globalThis.ArchSpecSourceScope||!eligible.length||eligible.length>20;
      scopeApply.textContent='使用筛选范围（'+eligible.length+'本）';
      if(scopeNote)scopeNote.textContent=eligible.length>20?'当前超过20份完整正文，请继续缩小筛选。'
        :eligible.length?'只选择这'+eligible.length+'份已准备完整正文，不提交问题；许可、版本与适用性仍由后续查询核对。'
        :'当前筛选没有可选完整正文；未取得资料和局部附件不纳入范围。';
    }
    if (filterEmpty) {
      filterEmpty.textContent = "当前清单中没有匹配的名称或编号。可更换关键词或清除筛选；这不代表没有相关规范要求。";
      filterEmpty.hidden = Boolean(catalogHint) || !filterRows.length || shown > 0;
    }
  }
  function scopeRows(){return filterRows.filter(row=>!row.node.hidden&&canTrial(row.source)
    &&globalThis.ArchSpecSourceScope?.source(row.source.source_id));}
  function clearRows() {
    clearCatalogHint();
    filterRows = [];
    list.replaceChildren();
    applyFilter();
  }
  filter?.addEventListener("input", applyFilter);
  levelFilter?.addEventListener('change',applyFilter);regionFilter?.addEventListener('input',applyFilter);
  scopeApply?.addEventListener('click',()=>{
    if(!visible()||loading||!loaded)return;
    try{
      const selected=scopeRows();
      if(!selected.length||selected.length>20)throw Error('请先筛选出1—20份已准备完整正文。');
      const scope=globalThis.ArchSpecSourceScope.encode(selected.map(row=>row.source.source_id));
      if(!globalThis.ArchSpecSourceScope.choose(document,scope))throw Error('所选正文范围尚不能核对，请刷新清单后重试。');
      // 明确用户动作才发送change；沿原任务/阅读/联网代次守卫，不模拟试查或自动提交。
      document.querySelector('#pending-source').dispatchEvent(new Event('change',{bubbles:true}));
      if(scopeNote)scopeNote.textContent='已选择'+selected.length+'本正文，原问题和项目保留，尚未执行查询。';
      document.querySelector('[data-workbench-view="search"]')?.click();
      document.querySelector('#pending-search-panel')?.scrollIntoView?.({behavior:'auto',block:'start'});
      document.querySelector('#pending-query')?.focus?.({preventScroll:true});
    }catch(error){if(scopeNote)scopeNote.textContent=error.message||'范围暂不能选择，请重新核对。';}
  });

  clearFilter?.addEventListener("click", () => {
    if (filter) { filter.value = ""; filter.focus?.(); }
    if(levelFilter)levelFilter.value='';if(regionFilter)regionFilter.value='';
    applyFilter();
  });
  const inventory=globalThis.ArchSpecOfficialInventory;
  let generation = 0;
  let loaded = false;
  let loading = false;
  const visible = () => document.body.dataset.workbenchView === "library";

  function text(tag, content, className = "") {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }
  function canTrial(source) {
    // 这里只提供尝试入口；真正查阅仍由后端检查该来源的使用条件。
    return inventory ? inventory.prepared(source)
      : source.kind === "FULL_TEXT" && source.acquired === true && !source.cache_error;
  }
  function render(sources) {
    clearRows();
    metadataLoaded=sources.length>0&&sources.every(source=>Object.hasOwn(source,'source_metadata'));
    if(levelFilter)levelFilter.disabled=!metadataLoaded;if(regionFilter)regionFilter.disabled=!metadataLoaded;
    if(metadataLoad)metadataLoad.textContent=metadataLoaded?'重新核对地域与版本档案':'读取地域与版本档案';
    const prepared = sources.filter(canTrial).length;
    summary.textContent = `共登记 ${sources.length} 份资料 · 已准备的完整正文 ${prepared} 份（试查受使用条件限制，仍待核验）`;
    if (!sources.length) {
      status.textContent = "尚未登记官方资料。这不代表没有相关规范要求，可在下方查找官方来源。";
      return;
    }
    const labels = {FULL_TEXT: "完整正文", REVISION: "局部修订附件（不是完整正文）", WITHDRAWAL: "废止条文清单（不是完整正文）", UNCONFIRMED: "附件类型待确认"};
    for (const source of sources) {
      const row = document.createElement("article");
      row.className = "library-inventory-row";
      const name = text("h3", source.title || "未识别资料", "library-inventory-title");
      // 库存登记用page_url，来源展示合同用source_url；只映射已登记值，链接仍由原模块与固定档案核对。
      const sourceView={...source,source_url:source.page_url};
      const profile=globalThis.ArchSpecSourceMetadata?.fixed(sourceView);
      if(metadataLoaded){const badge=globalThis.ArchSpecSourceMetadata?.badgeElement?.(sourceView);if(badge)name.append(badge);}

      // 标准编号尾部的年份不是现行性证明，也不是发布时间。
      const year = source.standard_code.match(/[-—－](\d{4})\s*$/)?.[1];
      row.append(name, text("p", `${source.standard_code || "编号未登记"}${year ? ` · 编号年份 ${year}` : " · 编号年份未登记"}`, "library-inventory-code"));
      row.append(text("p", labels[source.kind] || "附件类型待确认", "library-inventory-kind"));
      const state = source.cache_error
        ? `缓存读取失败：${source.cache_error}`
        : source.acquired === true
          ? "已准备 · 待核验"
          : "未取得正文 · 待核验（不代表没有规范要求）";
      row.append(text("p", state, "library-inventory-state"));
      if(metadataLoaded){
        const region=profile?.fields.region.value;
        row.append(text('p',(region?'档案地域：'+region:'档案地域待核')+' · '+(globalThis.ArchSpecSourceMetadata?.edition?.(sourceView)||'正式版本待核'),'library-inventory-profile-summary'));
      }

      if (source.acquired === true && !source.cache_error) {
        const records = document.createElement("details");
        records.className = "library-inventory-details";
        records.append(text("summary", "来源与处理记录"));
        const pages = Number.isInteger(source.page_count) && source.page_count > 0 ? ` · ${source.page_count} 页` : "";
        records.append(text("p", `本机已保存${pages}${source.processed_at ? ` · 处理时间 ${source.processed_at}` : ""}`, "library-inventory-meta"));
        if(canTrial(source)&&/^[a-f0-9]{64}$/.test(source.snapshot_id||'')){
          // 只链接这一行已登记的固定原件；点击时仍由服务端复核原件和当前使用条件。
          const download=text('a',source.source_format==='HTML'?'下载网页原件':'下载完整PDF原件','secondary-button library-original-download');
          download.href='/api/official-documents/'+encodeURIComponent(source.source_id)+'/snapshots/'+source.snapshot_id+'/original';
          download.target='_blank';download.rel='noopener';row.append(download);
        }

        if (source.reason) records.append(text("p", source.reason, "library-inventory-reason"));
        if(metadataLoaded){
          const view=globalThis.ArchSpecSourceMetadata?.profile?.(sourceView);if(view)records.append(view);
          if(source.source_metadata_gap)records.append(text('p',source.source_metadata_gap,'library-inventory-reason'));
        }
        row.append(records);
      } else if (source.reason) {
        // 未取得资料的登记说明仍保留；登记原因不能当作下载失败原因。
        row.append(text("p", source.reason, "library-inventory-reason"));
      }
      if (canTrial(source)) {
        const trial = text("button", "试查正文", "secondary-button library-inventory-trial");
        trial.type = "button";
        trial.addEventListener("click", () => {
          document.dispatchEvent(new CustomEvent("archspec:official-source-ready", {detail: {
            source_id: source.source_id, title: source.title, standard_code: source.standard_code, query: "",
          }}));
        });
        row.append(trial);
        // 直接复用固定书索引：只在展开时GET，预览和实际片段仍由原模块核对。
        const owner=generation,projectId=document.querySelector('#search-project')?.value||'';
        const current=()=>owner===generation&&visible()&&!row.hidden
          &&projectId===(document.querySelector('#search-project')?.value||'');
        const browse=globalThis.ArchSpecBookIndex?.controls(document,source,{
          summaryText:'浏览本书',isCurrent:current,
          onOpen:item=>globalThis.ArchspecPendingSearch?.openLibraryCitation(item,{projectId,isCurrent:current})===true,
          onUnavailable:()=>{if(current())status.textContent='这份固定正文当前无法读取，请刷新清单后重试；未改用其它版本。';},
        });
        if(browse){browse.className+=' library-book-browser';row.append(browse);}

      }
      filterRows.push({node:row,key:filterKey(`${source.title} ${source.standard_code}`),level:profile?.fields.level.value||'UNKNOWN',region:filterKey(profile?.fields.region.value),source});
      list.append(row);
    }
    applyFilter();
    status.textContent = "已读取本机登记资料；准备状态不代表现行性、项目适用性或使用许可已核验。试查时仍检查使用条件。";
    if(metadataLoaded)status.textContent+=' 已读取固定档案；类别只是编号或公告线索，地域只按档案原值筛选，不证明本工程适用。';
  }

  async function load(force=false) {
    const current = ++generation;
    loading = true;
    loaded = false;
    refresh.disabled = true;if(metadataLoad)metadataLoad.disabled=true;
    // 刷新期间移除旧行，避免失败之后仍能点击旧的成功资料。
    clearRows();
    summary.textContent = "";
    status.textContent = "正在读取共享规范库清单……";
    try {
      let sources;
      if(inventory) sources=await inventory.read({refresh:force,metadata:wantMetadata});
      else {
        // 实际工作台始终使用共同读取器，不在新错误后回退历史成功。
        const response=await fetch("/api/official-documents"+(wantMetadata?"?include_metadata=true":""));
        sources=await globalThis.ArchSpecHTTP.json(response,"规范清单响应暂时无法读取","array");
        if(!response.ok) throw Error(typeof sources?.detail==="string"?sources.detail:"规范清单暂时无法读取");
      }
      if (!Array.isArray(sources) || sources.some(source => !source || typeof source !== "object"
          || typeof source.source_id !== "string" || !source.source_id
          || typeof source.title !== "string" || typeof source.standard_code !== "string"
          || typeof source.acquired !== "boolean" || typeof source.kind !== "string")) {
        throw new Error("规范清单响应无效，无法确认资料状态");
      }
      if (current !== generation) return;
      render(sources);
      loaded = true;
    } catch (error) {
      if (current !== generation) return;
      clearRows();
      summary.textContent = "";
      status.textContent = `${error.message || "规范清单无法读取"}；本次资料状态未知，请重试。`;
    } finally {
      if (current === generation) {
        loading = false;
        refresh.disabled = false;if(metadataLoad)metadataLoad.disabled=false;
        applyFilter();
      }
    }
  }
  refresh.addEventListener("click",()=>load(true));
  metadataLoad?.addEventListener('click',()=>{wantMetadata=true;void load(true);});
  inventory?.subscribe(snapshot=>{
    // 共同变化不更改当前问题、项目、已核引用或任务。
    generation+=1;loaded=false;loading=false;refresh.disabled=false;if(metadataLoad)metadataLoad.disabled=false;
    if(!visible()) return;
    if(snapshot.status==="READY") {render(snapshot.sources);loaded=true;}
    else {
      clearRows();summary.textContent="";
      status.textContent=snapshot.status==="UNKNOWN"
        ?"正文清单暂不可读取；本次资料状态未知，请重试。":"正在读取共享规范库清单……";
      if(snapshot.status==="LOADING") void load();
    }
  });
  document.addEventListener("archspec:workbench-view", event => {
    if (event.detail?.view === "library") {
      if (!loaded && !loading) load();
    } else if (loading) {
      // 离开页面后忽略迟到结果，返回时重新读取，不让旧状态覆盖新资料。
      generation += 1;
      loading = false;
      loaded = false;
      refresh.disabled = false;if(metadataLoad)metadataLoad.disabled=false;
    }
  });
  document.addEventListener("archspec:official-source-ready", () => {
    generation += 1;
    loaded = false;
    loading = false;
    refresh.disabled = false;if(metadataLoad)metadataLoad.disabled=false;
    // 等同一个事件的页面切换完成：点击“试查”后不额外读取隐藏库存。
    Promise.resolve().then(() => { if (visible()) load(); });
  });
  document.addEventListener('archspec:project-change',()=>{
    // 项目来回切换也使旧展开控件失效；下一次进入用新归属重建。
    generation++;loaded=false;loading=false;refresh.disabled=false;if(metadataLoad)metadataLoad.disabled=false;
    if(visible())void load();
  });
  // 完整书名/编号直接查书目；只消费同一库存，不把书目匹配当正文证据。
  globalThis.ArchSpecLibraryInventory={openBookQuery:async(query,{isCurrent}={})=>{
    if(typeof query!=='string'||typeof isCurrent!=='function'||!isCurrent())return false;
    const term=query.normalize('NFKC').trim().replace(/^《([^》]+)》$/,'$1');
    const code=value=>String(value||'').normalize('NFKC').toUpperCase().replace(/\s+/g,'').replace(/[—－]/g,'-');
    const wanted=code(term),isCode=/^(?:GB|JGJ|CJJ)(?:\/T)?\d+(?:\.\d+)?(?:-\d{4})?$/.test(wanted);
    let sources;
    try{
      if(inventory)sources=await inventory.read();
      else if(loaded)sources=filterRows.map(row=>row.source);
      else{const response=await fetch('/api/official-documents');sources=await globalThis.ArchSpecHTTP.json(response,'规范清单暂不可读取','array');if(!response.ok)return false;}
    }catch{return false;}
    if(!isCurrent()||!Array.isArray(sources))return false;
    const matched=sources.filter(source=>isCode
      ?code(source.standard_code)===wanted||!/-\d{4}$/.test(wanted)&&code(source.standard_code).replace(/-\d{4}$/,'')===wanted
      :filterKey(source.title)===filterKey(term));
    let clues = [];
    if (!matched.length) {
      // 只为明确找书读取本机既有目录；普通问题及书号加条号仍沿原检索流程。
      if (!isCode && !/(?:规范|标准|规程|规定)$/.test(term)) return false;
      const clueGeneration = generation;
      try {
        const response = await fetch('/api/standard-catalog/search?query='+encodeURIComponent(term)+'&limit=5', {cache:'no-store'});
        const entries = await globalThis.ArchSpecHTTP.json(response,'规范目录暂不可读取','array');
        if (!response.ok || !isCurrent() || clueGeneration !== generation) return false;
        clues = entries.filter(entry => typeof entry?.title === 'string' && typeof entry.standard_code === 'string'
          && (isCode ? code(entry.standard_code) === wanted
            || !/-\d{4}$/.test(wanted) && code(entry.standard_code).replace(/-\d{4}$/,'') === wanted
            : filterKey(entry.title) === filterKey(term)));
      } catch { return false; }
      if (!clues.length) return false;
    }
    if (!isCurrent()) return false;
    // 这是用户明确找书的独立展示动作，不更改原项目、问题或条文检索范围。
    generation++;loading=false;refresh.disabled=false;
    if(filter)filter.value=term;if(levelFilter)levelFilter.value='';if(regionFilter)regionFilter.value='';
    render(sources);loaded=true;
    if (clues.length) {
      const group = document.createElement('section'); group.className = 'library-catalog-clues';
      for (const entry of clues) {
        const row = document.createElement('article'); row.className = 'library-inventory-row library-catalog-clue';
        row.append(text('h3',entry.title,'library-inventory-title'),text('p',entry.standard_code,'library-inventory-code'),
          text('p','目录线索 · 本次未找到可读官方正文，版本与适用性待核。','library-inventory-state'));
        // 目录URL只是查询入口，不绑定正文/快照；不借普通上传资料的full_text_available生成官方阅读动作。
        try {
          const url = new URL(entry.official_url);
          if (url.protocol === 'https:' && !url.username && !url.password && !url.port && !url.search && !url.hash
              && ['openstd.samr.gov.cn','std.samr.gov.cn','www.mohurd.gov.cn','mohurd.gov.cn'].includes(url.hostname)) {
            const link = text('a','到官方平台查询（输入上方编号）'); link.href = url.href;
            link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link);
          }
        } catch { /* 无有效入口仍保留目录书名，不生成猜测网址。 */ }
        group.append(row);
      }
      list.append(group); catalogHint = {node:group,key:filterKey(term),count:clues.length}; applyFilter();
    }
    document.querySelector('[data-workbench-view="library"]')?.click();
    panel.scrollIntoView?.({block:'start',behavior:'auto'});filter?.focus?.({preventScroll:true});
    status.textContent=clues.length ? '找到目录登记，可到官方平台按编号查询；此处尚不能阅读或引用正文。'
      : '已找到对应规范。展开“浏览本书”读条文，或打开已保存原件；版本与适用性仍待核。';
    return true;
  }};
  if (visible()) load();
})();
