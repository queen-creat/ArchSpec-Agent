/* 项目问题清单只由用户主动保存；逐题打开沿原问答/GET恢复，不自动批量执行。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecQuestionWorklist=api;})(globalThis,function(){
 const KEY='archspec:question-worklist:v1',states={CREATED:'已登记',RUNNING:'处理中',WAITING_CONDITIONS:'待补条件',WAITING_SOURCE:'待补正文',COMPLETED:'已整理，适用性待核',INTERRUPTED:'已中断',FAILED:'未完成'};
 let boundStore,boundEnqueue;
 const uuid=v=>typeof v==='string'&&/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
 const scopes=typeof module==='object'&&module.exports?require('./source-scope.js'):globalThis.ArchSpecSourceScope;
 const scope=v=>scopes?scopes.decode(v)!==undefined:typeof v==='string'&&/^(?:__all__|gb55031|official-[a-f0-9]{32})$/.test(v);
 const bookmarks=()=>typeof module==='object'&&module.exports?require('./clause-bookmarks.js'):globalThis.ArchSpecBookmarks;
 const referenceKey=r=>[r.source_id,r.snapshot_id,r.index].join(':');
 const referenceKeys=['version','projectId','source_id','snapshot_id','index','title','code','clause','page'];
 const reference=r=>Object.fromEntries(referenceKeys.map(key=>[key,r[key]]));
 const referencesValid=(rows,pid)=>Array.isArray(rows)&&rows.length<=8&&rows.every(r=>bookmarks()?.valid(r)&&r.projectId===pid&&Object.keys(r).every(key=>referenceKeys.includes(key)))&&new Set(rows.map(referenceKey)).size===rows.length;
 const taskValid=t=>t&&uuid(t.id)&&['LOCAL','MODEL'].includes(t.mode)&&Object.hasOwn(states,t.state)&&Number.isSafeInteger(t.revision)&&t.revision>=0;
 function createStore(storage){
  const copy=r=>({...r,task:r.task?{...r.task}:null,...(r.references?{references:r.references.map(reference)}:{})});let rows=[],persistent=!!storage;
  const valid=r=>r&&r.version===1&&uuid(r.id)&&typeof r.projectId==='string'&&r.projectId.length<=200
   &&typeof r.query==='string'&&!!r.query.trim()&&r.query.length<=500&&scope(r.scope)&&typeof r.scopeLabel==='string'&&r.scopeLabel.length<=300
   &&(r.task===null||taskValid(r.task)&&Object.keys(r.task).sort().join(',')==='id,mode,revision,state')
   &&(r.note===undefined||typeof r.note==='string'&&r.note.length<=2000)
   &&(r.references===undefined||referencesValid(r.references,r.projectId))
   &&Object.keys(r).filter(key=>!['note','references'].includes(key)).sort().join(',')==='id,projectId,query,scope,scopeLabel,task,version';
  try{const raw=storage?.getItem(KEY);if(raw&&raw.length<=2000000){const data=JSON.parse(raw);if(Array.isArray(data)&&data.length<=100&&data.every(valid)&&new Set(data.map(r=>r.id)).size===data.length)rows=data.map(copy);}}catch{persistent=false;}
  function save(){try{if(!storage)throw Error();storage.setItem(KEY,JSON.stringify(rows));persistent=true;}catch{persistent=false;}}
  return {validEntry:valid,list(projectId){return rows.filter(r=>r.projectId===projectId).map(copy);},
   get(projectId,id){const row=rows.find(r=>r.projectId===projectId&&r.id===id);return row?copy(row):null;},
   add(projectId,input,source,scopeLabel){
    if(typeof projectId!=='string'||projectId.length>200||!scope(source)||typeof scopeLabel!=='string'||scopeLabel.length>300)throw Error('当前项目或检索范围无法记录。');
    if(typeof input!=='string'||input.length>6000)throw Error('一次最多加入10题，每题最多500字。');
    const queries=[...new Set(input.split(/\r?\n/).map(s=>s.trim()).filter(Boolean))];
    if(!queries.length||queries.length>10||queries.some(q=>q.length>500))throw Error('一次最多加入10题，每题最多500字，请每行写一题。');
    const additions=queries.filter(q=>!rows.some(r=>r.projectId===projectId&&r.query===q&&r.scope===source));
    if(rows.length+additions.length>100||rows.filter(r=>r.projectId===projectId).length+additions.length>30)throw Error('当前项目最多30题，浏览器合计最多100题；请先移出不再查阅的题目。');
    const prepared=additions.map(query=>({version:1,id:globalThis.crypto.randomUUID(),projectId,query,scope:source,scopeLabel,task:null}));rows.push(...prepared);save();return prepared.length;
   },
   track(id,event){const row=rows.find(r=>r.id===id);
    if(!row||!taskValid(event)||event.project_id!==(row.projectId||null)||event.query!==row.query||event.scope!==row.scope
     ||row.task?.id===event.id&&event.revision<row.task.revision)return false;
    row.task={id:event.id,mode:event.mode,state:event.state,revision:event.revision};save();return true;
   },annotate(projectId,id,patch){
    const row=rows.find(r=>r.projectId===projectId&&r.id===id);
    if(!row||!patch||Object.keys(patch).some(key=>!['note','references'].includes(key))
     ||patch.note!==undefined&&(typeof patch.note!=='string'||patch.note.length>2000)
     ||patch.references!==undefined&&!referencesValid(patch.references,projectId))return false;
    // 用户笔记与固定位置是独立记录，不改任务完成状态，也不保存正文或发送同意。
    if(patch.note!==undefined){if(patch.note.trim())row.note=patch.note;else delete row.note;}
    if(patch.references!==undefined){if(patch.references.length)row.references=patch.references.map(reference);else delete row.references;}
    save();return true;
   },remove(projectId,id){rows=rows.filter(r=>r.projectId!==projectId||r.id!==id);save();},persistent(){return persistent;}};
 }
 function bind(doc,{project,readScope,readTask,openDraft,openTask}){
  const panel=doc.querySelector('#question-worklist'),list=doc.querySelector('#question-worklist-items'),summary=doc.querySelector('#question-worklist-summary'),input=doc.querySelector('#question-worklist-input'),status=doc.querySelector('#question-worklist-status');
  if(!panel||!list||!input||!status)return null;
  let storage;try{storage=globalThis.localStorage;}catch{}
  const store=createStore(storage);boundStore=store;let active=null,opening=null,page=0;
  const previous=doc.querySelector('#question-worklist-previous'),next=doc.querySelector('#question-worklist-next');
  const node=(tag,text,cls)=>{const e=doc.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;};
  function render(){const pid=project(),rows=store.list(pid);page=Math.min(page,Math.max(0,Math.ceil(rows.length/5)-1));list.replaceChildren();
   const remaining=rows.filter(r=>!r.task||r.task.state!=='COMPLETED').length;summary.textContent='问题清单（'+remaining+'待查 / '+rows.length+'题）';
   doc.querySelector('#question-worklist-owner').textContent=pid?'仅列当前项目保存的问题；状态以最后一次整理记录为准。':'未选项目的问题单独保存；状态以最后一次整理记录为准。';
   if(!rows.length)list.append(node('p','把要核对的问题放在这里，再逐题打开查阅。','empty-state'));
   for(const row of rows.slice(page*5,page*5+5)){const card=node('article','','question-worklist-row');if(row.id===active)card.className+=' is-current';
    const open=node('button',row.query,'question-worklist-question');open.type='button';open.setAttribute('aria-label',(row.task?'继续原题：':'打开待查题：')+row.query);
    const meta=node('p',(row.task?'上次状态：'+states[row.task.state]:'尚未整理')+' · '+row.scopeLabel,'question-worklist-meta');
    const hint=node('small',row.task?'打开只重读原任务；模型题仍须重新同意。':'打开只填入问题；查阅和整理仍需主动点击。');
    open.addEventListener('click',async()=>{if(project()!==pid||opening)return;const saved=store.get(pid,row.id);if(!saved)return;opening=row.id;open.disabled=true;
     try{const ok=saved.task?await openTask({id:saved.task.id,project_id:pid||null,query:saved.query,scope:saved.scope}):openDraft(saved);
      if(project()!==pid)return;if(!ok)throw Error('本题未打开，请先核对当前任务状态。');
      active=row.id;render();status.textContent=saved.task?'已重读原题状态；未重新整理或调用模型。':'已填入待查题；请主动查条文或整理，需要时可填入常用条件方案。';
     }catch(error){if(project()===pid)status.textContent=error.message||'本题暂不可打开。';}finally{if(opening===row.id)opening=null;open.disabled=false;}
    });
    const remove=node('button','移出清单','quiet-button');remove.type='button';remove.setAttribute('aria-label','移出清单：'+row.query);
    remove.addEventListener('click',()=>{if(project()!==pid||opening)return;store.remove(pid,row.id);if(active===row.id)active=null;render();status.textContent='已移出清单，历史任务和查阅资料保留。';});
    const dossier=node('details','','question-evidence'),caption=node('summary','');
    const paintCaption=()=>{const saved=store.get(pid,row.id);caption.textContent='核对笔记与关联出处（'+(saved?.references?.length||0)+'）'+(saved?.note?' · 已记笔记':'');};paintCaption();
    const label=node('label','我的核对笔记（非规范原文）'),note=node('textarea','');note.rows=3;note.maxLength=2000;note.value=row.note||'';note.setAttribute('aria-label','核对笔记：'+row.query);label.append(note);
    const saveNote=node('button','保存核对笔记','quiet-button');saveNote.type='button';
    saveNote.addEventListener('click',()=>{if(project()!==pid)return;if(!store.annotate(pid,row.id,{note:note.value})){status.textContent='笔记未保存，请确认仍在原项目且不超过2000字。';return;}paintCaption();status.textContent=store.persistent()?'核对笔记已保存在当前浏览器。':'浏览器无法保存，笔记仅本页有效。';});
    const refList=node('div','','question-evidence-list'),pickLabel=node('label','从当前项目收藏中选择条文'),pick=node('select','');pick.setAttribute('aria-label','选择关联收藏：'+row.query);pickLabel.append(pick);
    const attach=node('button','关联所选条文','quiet-button');attach.type='button';let choices=[];
    function refreshChoices(){choices=(bookmarks()?.getStore?.()?.list(pid)||[]).filter(r=>bookmarks()?.valid(r)&&r.projectId===pid);pick.replaceChildren();const blank=node('option',choices.length?'请选择一条已收藏条文':'请先在阅读区收藏条文');blank.value='';pick.append(blank);pick.value='';
     choices.forEach((r,index)=>{const option=node('option',r.title+' · '+(r.clause||'文件第'+r.page+'页'));option.value=String(index);pick.append(option);});attach.disabled=!choices.length;}
    function paintReferences(){refList.replaceChildren();const saved=store.get(pid,row.id);for(const ref of saved?.references||[]){const line=node('div','','question-evidence-row'),read=node('button',ref.title+' · '+(ref.clause||'文件第'+ref.page+'页'),'question-worklist-question');read.type='button';read.setAttribute('aria-label','打开关联出处：'+ref.title+' '+ref.clause);
      read.addEventListener('click',async()=>{if(project()!==pid||!store.get(pid,row.id)?.references?.some(r=>referenceKey(r)===referenceKey(ref)))return;read.disabled=true;
       try{const ok=await globalThis.ArchspecPendingSearch?.openBookmark(ref);if(project()===pid)status.textContent=ok?'已重新核读关联出处；是否适用于本题仍需核对。':'关联出处未打开，原记录保留。';}
       catch(error){if(project()===pid)status.textContent=error.message||'出处暂不可读取，原关联保留。';}finally{read.disabled=false;}});
      const detach=node('button','移除关联','quiet-button');detach.type='button';detach.setAttribute('aria-label','移除关联：'+ref.title+' '+ref.clause);
      detach.addEventListener('click',()=>{if(project()!==pid)return;const saved=store.get(pid,row.id);if(!saved)return;store.annotate(pid,row.id,{references:(saved.references||[]).filter(r=>referenceKey(r)!==referenceKey(ref))});paintReferences();paintCaption();status.textContent=store.persistent()?'已移除本题关联，收藏条文仍保留。':'关联已移除但浏览器无法保存，仅本页有效。';});
      line.append(read,node('small',(ref.code||'文号待核')+' · 文件第'+ref.page+'页 · 人工关联，适用性待核'),detach);refList.append(line);
     }}
    attach.addEventListener('click',()=>{if(project()!==pid)return;const chosen=choices[Number(pick.value)];if(pick.value===''||!chosen){status.textContent='请先选择一条已收藏条文。';return;}
     const current=(bookmarks()?.getStore?.()?.list(pid)||[]).find(r=>referenceKey(r)===referenceKey(chosen));const saved=store.get(pid,row.id);if(!current||!saved){status.textContent='收藏或问题已变化，请重新展开后选择。';return;}
     const refs=saved.references||[];if(refs.some(r=>referenceKey(r)===referenceKey(current))){status.textContent='这条出处已关联本题。';return;}
     if(!store.annotate(pid,row.id,{references:[...refs,reference(current)]})){status.textContent='每题最多关联8条固定出处。';return;}paintReferences();paintCaption();status.textContent=store.persistent()?'已关联固定出处；未自动查询或判定合规。':'关联仅本页有效，浏览器无法保存。';});
    dossier.addEventListener('toggle',()=>{if(dossier.open&&project()===pid)refreshChoices();});refreshChoices();paintReferences();
    dossier.append(caption,node('small','笔记与关联仅保存在当前浏览器，可随项目查阅备份导出；打开出处时重新核对原文。'),label,saveNote,pickLabel,attach,refList);
    card.append(open,meta,hint,dossier,remove);list.append(card);
   }
   previous.disabled=page===0;next.disabled=(page+1)*5>=rows.length;
   doc.querySelector('#question-worklist-page').textContent=rows.length?'第 '+(page+1)+' / '+Math.ceil(rows.length/5)+' 页':'';
  }
  function add(text,attachCurrent=false){try{const owner=project(),source=readScope(),count=store.add(owner,text,source.scope,source.label);page=0;
   if(attachCurrent){const row=store.list(owner).find(r=>r.query===text.trim()&&r.scope===source.scope);if(row){active=row.id;const task=readTask?.();if(task)store.track(row.id,task);}}
   render();input.value='';
   status.textContent=(count?'已加入'+count+'题。':'相同问题和范围已在清单。')+(store.persistent()?'保存在当前浏览器，未自动查询。':'浏览器无法保存，仅本页有效。');
  }catch(error){status.textContent=error.message;}}
  // 项目总览只追加明确选中的待查问题；继续使用同一store、项目范围与去重规则。
  boundEnqueue=packet=>{
   if(packet?.projectId!==project())throw Error('当前项目已变化，请回项目要求重新选择。');
   if(opening)throw Error('正在打开原题，请稍后再加入问题。');
   const queries=packet.queries;
   if(!Array.isArray(queries)||!queries.length||queries.length>10||queries.some(q=>typeof q!=='string'||!q.trim()||q.length>500||/[\r\n]/.test(q)))
    throw Error('每次请选择1—10条待查要求，每条不超过500字且只含一题。');
   const source=readScope(),count=store.add(project(),queries.join('\n'),source.scope,source.label);
   page=0;render();
   const result={added:count,skipped:queries.length-count,persistent:store.persistent(),scope:source.scope,scopeLabel:source.label};
   status.textContent='已加入'+count+'题。'+(result.skipped?'相同问题和范围跳过'+result.skipped+'题。':'')+(result.persistent?'保存在当前浏览器，未自动查询。':'浏览器无法保存，仅本页有效。');
   return result;
  };
  doc.querySelector('#question-worklist-add')?.addEventListener('click',()=>add(input.value));
  doc.querySelector('#question-worklist-add-current')?.addEventListener('click',()=>add(doc.querySelector('#pending-query')?.value||'',true));
  previous.addEventListener('click',()=>{if(page>0){page--;render();}});next.addEventListener('click',()=>{page++;render();});
  const changed=()=>{if(!opening){active=null;render();}};
  doc.querySelector('#pending-query')?.addEventListener('input',changed);doc.querySelector('#pending-source')?.addEventListener('change',changed);
  doc.addEventListener('archspec:project-change',()=>{active=null;opening=null;page=0;input.value='';status.textContent='';render();});
  doc.addEventListener('archspec:qa-task-updated',event=>{const id=opening||active;if(id&&store.track(id,event.detail)){render();status.textContent='已记录本题状态：'+states[event.detail.state]+'。'+(store.persistent()?'':'浏览器无法保存清单变更，仅本页有效。');}});
  render();return {store};
 }
 return {createStore,bind,storageKey:KEY,getStore:()=>boundStore,enqueue:packet=>{if(!boundEnqueue)throw Error('问题清单尚未就绪，请刷新页面后再试。');return boundEnqueue(packet);}};
});
