/* 只绑定展示；同标签页仅保留少量问题文本，不保存正文或自动恢复请求。 */
(function(root,factory){
 const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;
 else{root.ArchSpecWorkbenchUI=api;if(root.document)api.bind(root.document);}
})(globalThis,function(){
 function createRecentQueries({storage,projectId=''}={}){
  const key='archspec:recent-queries',validProject=value=>typeof value==='string'&&value.length<=200;
  const validQuery=value=>typeof value==='string'&&value.length>0&&value.length<=500&&value.trim()===value;
  projectId=validProject(projectId)?projectId:'';let saved=null,entries=[];
  // 仅接受本模块的最小记录；损坏、旧结构或过大记录均不能进入按钮和当前项目。
  try{
   const raw=storage?.getItem(key);
   if(typeof raw==='string'&&raw.length<=17000){
    const value=JSON.parse(raw);
    if(value&&Object.keys(value).sort().join(',')==='projectId,queries,version'&&value.version===1
     &&validProject(value.projectId)&&Array.isArray(value.queries)&&value.queries.length<=5
     &&value.queries.every(validQuery)&&new Set(value.queries).size===value.queries.length)saved=value;
   }
  }catch{}
  if(saved?.projectId===projectId)entries=[...saved.queries];
  function save(){
   saved={version:1,projectId,queries:[...entries]};
   // 隐私模式、禁用存储或配额不足时继续使用内存，不打断查阅，也不转存到长期存储。
   try{storage?.setItem(key,JSON.stringify(saved));}catch{}
  }
  return{
   add(query){if(typeof query!=='string')return;query=query.trim();if(!validQuery(query))return;
    entries=[query,...entries.filter(value=>value!==query)].slice(0,5);save();},
   values(){return [...entries];},clear(){entries=[];save();},
   selectProject(next){
    if(!validProject(next)||next===projectId)return false;
    projectId=next;
    // 首次异步选择与保存记录一致时恢复；真正切换则清空，旧工程问题不串入新工程。
    entries=saved?.projectId===projectId?[...saved.queries]:[];save();return true;
   }
  };
 }
 // 只汇合现有业务文本，不据摘要推断正文、现行、成功、联网或模型调用。
 function statusSummary(state={}){
  const text=value=>typeof value==='string'?value.trim():'';
  const lookup=state.lookup?.visible?text(state.lookup.text):'';
  const online=state.online?.visible?text(state.online.text):'';
  const search=text(state.search?.text),coverage=text(state.coverage?.text);
  const warning=lookup&&['warning','error'].includes(state.lookup.tone);
  const networkError=online&&state.online.tone==='error';
  // 查询和库存各有时间与归属。出现本轮结果/进度后，不把较早库存当作该题当前正文状态。
  // 这里按状态所有者选择，不能删除地域词或改写旧库存来掩盖冲突；原库存仍在详情保留。
  const currentUnavailable=online&&['RESTRICTED','UNAVAILABLE'].includes(state.online.observationCurrent);
  const sameUnconfirmed=online&&state.online.flowState==='NO_EVIDENCE'&&state.online.observationCurrent==='READABLE'
   &&state.online.historical===false&&state.lookup?.observationSource==='READABLE';
  // 后续固定读取门明确受限/不可用时，以该当前结果为准，不能拼接此前“可读”的查阅观察。
  // 同题未确认只留一句结果；各维度原字仍在各自详情，不按中文词句猜状态。
  const messages=currentUnavailable?[online]:networkError?[online,warning&&state.lookup?.observationSource!=='READABLE'?lookup:null]
   :sameUnconfirmed?['固定正文当前可读 · 回答证据尚未确认；待确认候选可显式阅读']
   :warning?[lookup,online||search]:[online||search||lookup||coverage||'请输入问题或条款号'];
  const network=state.onlineAvailable===false?'当前范围只查缓存':state.onlineEnabled?'联网补充已开启':'联网补充已关闭';
  const model=state.model?.configured===true
   ?state.modelEnabled?'模型已选择 · 按授权范围使用':'模型已配置 · 本轮未启用'
   :text(state.model?.text)||'模型状态待核';
  return {message:[...new Set(messages.filter(Boolean))].join(' · '),text:[...new Set(messages.filter(Boolean)),network,model].join(' · '),
   tone:networkError?'error':currentUnavailable||sameUnconfirmed?'warning':warning?state.lookup.tone:'neutral'};
 }
 function bind(doc){
  const input=doc.querySelector('#pending-query'),form=doc.querySelector('#pending-search-form');
  const toggle=doc.querySelector('#workbench-atmosphere'),guide=doc.querySelector('#workbench-empty-guide');
  const recent=doc.querySelector('#workbench-recent-queries'),section=doc.querySelector('#workbench-recent-section');
  if(!input||!form)return;
  const project=doc.querySelector('#search-project');let storage;
  try{storage=globalThis.sessionStorage;}catch{}
  const history=createRecentQueries({storage,projectId:project?.value||''});
  function fill(value){input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();}
  let atmosphere=true;try{atmosphere=localStorage.getItem('archspec:atmosphere')!=='off';}catch{}
  function paint(){doc.body.dataset.atmosphere=atmosphere?'on':'off';if(toggle){toggle.textContent=atmosphere?'关闭背景氛围':'开启背景氛围';toggle.setAttribute('aria-pressed',String(atmosphere));}}
  paint();toggle?.addEventListener('click',()=>{atmosphere=!atmosphere;paint();try{localStorage.setItem('archspec:atmosphere',atmosphere?'on':'off');}catch{}});
  doc.querySelectorAll('[data-workbench-example]').forEach(button=>button.addEventListener('click',()=>fill(button.dataset.workbenchExample)));
  let recentGeneration=0;
  function renderRecent(){const generation=++recentGeneration;if(!recent)return;recent.replaceChildren();const entries=history.values();if(section)section.hidden=!entries.length;
   entries.forEach(query=>{const button=doc.createElement('button');button.type='button';button.textContent=query;button.title=query;
    // 按展示代次核验回填，项目切换、切回或重新绘制后，旧节点的滞后动作均失效。
    button.addEventListener('click',()=>{if(generation===recentGeneration)fill(query);});recent.append(button);});}
  form.addEventListener('submit',()=>{history.add(input.value);renderRecent();if(guide)guide.hidden=true;});
  doc.addEventListener('archspec:project-change',event=>{
   const next=event?.detail?.projectId??project?.value??'';
   // 刷新核回同一项目不是用户改问；不能取消其它入口正在GET恢复的任务。
   // 同项目任务正由业务层GET恢复时，展示层不能再发input将它取消。
   // 历史仍按项目隔离；只有已明确归属的恢复才保留问题输入。
   const restoringTask=globalThis.ArchSpecQuestionInput?.isRestoringProject?.(next)===true;
   if(history.selectProject(next) && !event?.detail?.restoring && !restoringTask){
    input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));if(guide)guide.hidden=false;
   }
   renderRecent();
  });
  // 刷新只展示历史入口；用户点击后填回问题，再显式提交，绝不自动联网、调用模型或查询。
  renderRecent();
  // 仅GET服务端公开配置布尔值，不发送问题或密钥；原模块处理用户模型同意。
  const model=doc.querySelector('#workbench-model-state');
  const primary=doc.querySelector('#workbench-primary-status'),progress=doc.querySelector('#workbench-progress-toggle');
  const lookup=doc.querySelector('#pending-lookup-status'),search=doc.querySelector('#pending-search-status');
  const online=doc.querySelector('#pending-online-status'),onlineSummary=doc.querySelector('#pending-online-summary');
  const coverage=doc.querySelector('#pending-coverage-hint'),onlineEnabled=doc.querySelector('#pending-online-enabled');
  const scope=doc.querySelector('#pending-source'),modelEnabled=doc.querySelector('#official-qa-model-enabled');
  const results=doc.querySelector('#pending-search-results'),searchButton=doc.querySelector('#pending-search-button');
  const details=doc.querySelector('#pending-reading-info'),onlinePanel=doc.querySelector('#pending-online-panel');
  const tools=doc.querySelector('#search-tools'),toolsSummary=doc.querySelector('#search-tools-summary'),statusBar=doc.querySelector('#workbench-status-bar');
  let configured;
  // Reuse the flow-owned action; it still checks query identity and current access.
  function candidateReview(){
   if(onlineSummary?.hidden!==false || online?.getAttribute('data-flow-state')!=='NO_EVIDENCE'
    ||online?.getAttribute('data-observation-current')!=='READABLE')return null;
   return doc.querySelector('.query-review-candidates');
  }
  function paintStatus(){
   if(primary){const state=statusSummary({lookup:{text:lookup?.textContent,visible:lookup?.hidden===false,tone:lookup?.getAttribute('data-tone'),observationSource:lookup?.getAttribute('data-observation-source')},
    search:{text:search?.textContent},online:{text:online?.textContent,visible:onlineSummary?.hidden===false,tone:online?.getAttribute('data-tone'),flowState:online?.getAttribute('data-flow-state'),observationCurrent:online?.getAttribute('data-observation-current'),historical:online?.getAttribute('data-query-history')==='true'},
    coverage:{text:coverage?.textContent},model:{text:model?.textContent,configured},
    onlineEnabled:onlineEnabled?.checked===true,onlineAvailable:scope?.value==='__all__'&&onlineEnabled?.disabled!==true,
    modelEnabled:modelEnabled?.checked===true});
    primary.textContent=state.message;primary.setAttribute('data-tone',state.tone);
    if(statusBar)statusBar.hidden=!(search?.textContent?.trim()||lookup?.hidden===false||onlineSummary?.hidden===false||searchButton?.disabled===true);}
   if(toolsSummary)toolsSummary.textContent=scope?.value&&scope.value!=='__all__'?'更多选项 · 已限定范围':'更多选项';
   if(progress){
    const review=candidateReview();
    progress.textContent=review?'查看候选原文':'详情';
    progress.disabled=Boolean(review?.disabled);
    progress.setAttribute('aria-controls',review?'pending-search-results':'pending-online-panel pending-reading-info');
    progress.setAttribute('aria-expanded',String(review?false:onlinePanel?.open===true||details?.open===true));
   }
  }
  function paintEmpty(){
   const currentRefusal=lookup?.hidden===false&&['RESTRICTED','UNAVAILABLE'].includes(lookup?.getAttribute('data-observation-source'));
   doc.body.dataset.searchEmpty=String(!results?.querySelector('.pending-result-card')&&!doc.querySelector('#pending-page-reader .reader-heading'));
   if(guide)guide.hidden=Boolean(results?.querySelector('.pending-result-card')||results?.querySelector('[data-readable-unconfirmed-count]'))||currentRefusal||searchButton?.disabled===true;
  }
  progress?.addEventListener('click',()=>{
   const review=candidateReview();
   if(review){if(!review.disabled)review.click();return;}
   if(tools)tools.open=true;
   // 复用原有联网进度入口及其权限/任务守卫，不增加第二套查询或轮询。
   if(onlineSummary?.hidden===false)doc.querySelector('#pending-online-jump')?.click();
   else if(details){details.open=true;details.querySelector?.('summary')?.focus({preventScroll:true});}
   paintStatus();
  });
  [input,scope,onlineEnabled,modelEnabled].forEach(node=>{
   node?.addEventListener('input',()=>{paintStatus();paintEmpty();});
   node?.addEventListener('change',()=>{paintStatus();paintEmpty();});
  });
  if(typeof MutationObserver==='function'){
   // 各模块继续持有原状态节点；仅观察其已提交文本，不抢占业务状态或复制处理循环。
   const observer=new MutationObserver(()=>{paintStatus();paintEmpty();});
   [lookup,search,online,onlineSummary,coverage,model,results,searchButton,doc.querySelector('#pending-online-results')].filter(Boolean).forEach(node=>
    observer.observe(node,{childList:true,characterData:true,subtree:true,attributes:true,attributeFilter:['hidden','data-tone','disabled','data-flow-state','data-observation-current','data-observation-source','data-query-history']}));
   [details,onlinePanel].filter(Boolean).forEach(node=>observer.observe(node,{attributes:true,attributeFilter:['open','hidden']}));
  }
  form.addEventListener('submit',()=>{if(tools)tools.open=false;});
  paintStatus();paintEmpty();
  if(model&&typeof fetch==='function')fetch('/api/official-qa/model-status')
   .then(response=>{if(!response.ok)throw Error('status');return response.json();})
   .then(data=>{configured=typeof data.configured==='boolean'?data.configured:undefined;model.textContent=configured===true?'模型已配置 · 勾选后使用':configured===false?'模型未配置 · 可本地查阅':'模型配置状态暂未取得';paintStatus();})
   .catch(()=>{configured=undefined;model.textContent='模型配置状态暂未取得';paintStatus();});
 }
 return {createRecentQueries,statusSummary,bind};
});
