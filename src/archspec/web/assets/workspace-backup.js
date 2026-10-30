/* 本地查阅备份只包含明确的浏览器业务记录；导入合并不覆盖旧条目，不调用服务器。 */
(function(root,factory){const api=factory(typeof module==='object'&&module.exports?{bookmarks:require('./clause-bookmarks.js'),presets:require('./condition-presets.js'),questions:require('./question-worklist.js')}:{bookmarks:root.ArchSpecBookmarks,presets:root.ArchSpecConditionPresets,questions:root.ArchSpecQuestionWorklist});if(typeof module==='object'&&module.exports)module.exports=api;else{root.ArchSpecWorkspaceBackup=api;if(root.document)api.bind(root.document);}})(globalThis,function(deps){
 const VERSION='ARCHSPEC_BROWSER_READING_BACKUP_V1',LIMIT=2*1024*1024;
 const bookmarkKeys=['version','projectId','source_id','snapshot_id','index','title','code','clause','page','note'];
 const natural={bookmarks:r=>[r.projectId,r.source_id,r.snapshot_id,r.index].join(':'),presets:r=>r.projectId+':'+r.name,questions:r=>JSON.stringify([r.projectId,r.query,r.scope])};
 function groups(fields){const validators={bookmarks:r=>deps.bookmarks.valid(r)&&Object.keys(r).every(key=>bookmarkKeys.includes(key)),
  presets:deps.presets.createStore(null,fields).validEntry,questions:deps.questions.createStore(null).validEntry};
  return Object.entries(deps).map(([name,api])=>({name,api,key:api.storageKey,validate:row=>validators[name](row)&&(name!=='presets'||row.name===row.name.trim())&&(name!=='questions'||(row.query===row.query.trim()&&!/[\r\n]/.test(row.query))),max:name==='presets'?50:100,per:name==='presets'?10:name==='questions'?30:100}));}
 function rows(raw,group){if(raw===null||raw===undefined)return [];if(typeof raw!=='string'||raw.length>LIMIT)throw Error('本机'+group.name+'记录过大或格式无法核对。');
  const data=JSON.parse(raw);if(!Array.isArray(data)||data.length>group.max||!data.every(group.validate)||new Set(data.map(natural[group.name])).size!==data.length
   ||group.name!=='bookmarks'&&new Set(data.map(r=>r.id)).size!==data.length)throw Error('查阅记录格式或身份无法核对，未覆盖现有数据。');return data;}
 function exportData(storage,projectId,fields,live={}){
  if(typeof projectId!=='string'||projectId.length>200)throw Error('当前项目无法核对。');
  const data={version:VERSION,project_id:projectId,created_at:new Date().toISOString()};
  for(const group of groups(fields)){let values;try{if(!storage||live[group.name]&&!live[group.name].persistent())throw Error();values=rows(storage?.getItem(group.key),group).filter(r=>r.projectId===projectId);}
   catch(error){if(!live[group.name])throw error;values=live[group.name].list(projectId);}
   data[group.name]=values;
  }return data;
 }
 function prepareImport(storage,text,projectId,fields){
  if(typeof text!=='string'||text.length>LIMIT)throw Error('请选择2MB以内的查阅备份。');const incoming=JSON.parse(text);
  if(!incoming||Object.keys(incoming).sort().join(',')!=='bookmarks,created_at,presets,project_id,questions,version'||incoming.version!==VERSION
   ||incoming.project_id!==projectId||typeof incoming.created_at!=='string'||!Number.isFinite(Date.parse(incoming.created_at)))throw Error('备份版本或项目不匹配，请选择原项目；未改动本机记录。');
  const specs=groups(fields),before={},arrays={},input={},added={},skipped={};
  for(const group of specs){before[group.key]=storage.getItem(group.key);arrays[group.name]=rows(before[group.key],group);
   input[group.name]=rows(JSON.stringify(incoming[group.name]),group);if(input[group.name].some(r=>r.projectId!==projectId))throw Error('备份条目不属于当前项目。');
   const known=new Set(arrays[group.name].map(natural[group.name]));const fresh=input[group.name].filter(r=>!known.has(natural[group.name](r)));
   added[group.name]=fresh.length;skipped[group.name]=input[group.name].length-fresh.length;input[group.name]=fresh;
   if(arrays[group.name].length+fresh.length>group.max||arrays[group.name].filter(r=>r.projectId===projectId).length+fresh.length>group.per)throw Error('合并后超过'+group.name+'保存上限，请先保留备份并减少导入条目。');
  }
  const values=new Map(Object.entries(before)),memory={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
  for(const group of specs){const store=group.name==='presets'?group.api.createStore(memory,fields):group.api.createStore(memory);
   for(const row of input[group.name]){
    if(group.name==='bookmarks')store.add(row);
    else if(group.name==='presets')store.add(projectId,row.name,row.conditions);
    else {store.add(projectId,row.query,row.scope,row.scopeLabel);const entry=store.list(projectId).find(r=>r.query===row.query&&r.scope===row.scope);if(row.task)store.track(entry.id,{...row.task,project_id:projectId||null,query:row.query,scope:row.scope});
     // 同一题的个人笔记与固定位置随原备份迁移；已存在题目仍保留本机版本。
     if(row.note!==undefined||row.references!==undefined)store.annotate(projectId,entry.id,{...(row.note!==undefined?{note:row.note}:{}),...(row.references!==undefined?{references:row.references}:{})});}
   }
   rows(memory.getItem(group.key),group);
  }
  return {projectId,added,skipped,changes:specs.map(group=>({key:group.key,before:before[group.key],after:memory.getItem(group.key)}))};
 }
 function commit(storage,plan,projectId){
  if(plan?.projectId!==projectId||plan.changes.length!==3||plan.changes.some(row=>!Object.values(deps).some(api=>api.storageKey===row.key)))throw Error('当前项目或导入计划无法核对。');
  if(plan.changes.some(row=>storage.getItem(row.key)!==row.before))throw Error('本机记录已变化，请重新预览后导入。');
  let written=0;try{for(const row of plan.changes)if(row.after!==row.before){storage.setItem(row.key,row.after);written++;}}
  catch{throw Error('导入未全部完成，已处理'+written+'类；旧条目保留。请刷新核对后重新导入。');}return plan.added;
 }
 function bind(doc){const panel=doc.querySelector('#workspace-backup'),status=doc.querySelector('#workspace-backup-status'),preview=doc.querySelector('#workspace-backup-preview'),area=doc.querySelector('#workspace-backup-text'),file=doc.querySelector('#workspace-backup-file'),apply=doc.querySelector('#workspace-backup-apply'),reload=doc.querySelector('#workspace-backup-reload');
  if(!panel||!file||!area)return;
  const fields=[...doc.querySelectorAll('#official-qa-conditions [data-condition-key]')].map(e=>e.getAttribute('data-condition-key'));
  const project=()=>doc.querySelector('#search-project')?.value||'';let plan=null,epoch=0;
  const storage=()=>{const value=globalThis.localStorage;if(!value)throw Error('当前浏览器存储不可用。');return value;};
  const live=()=>Object.fromEntries(Object.entries(deps).map(([name,api])=>[name,api.getStore?.()]));
  function clear(){epoch++;plan=null;apply.disabled=true;preview.hidden=true;area.value='';reload.hidden=true;status.textContent='';file.value='';}
  doc.querySelector('#workspace-backup-export')?.addEventListener('click',()=>{epoch++;plan=null;apply.disabled=true;file.value='';try{const active=live();let durable;try{durable=storage();}catch{}
   const data=exportData(durable,project(),fields,active),text=JSON.stringify(data,null,2);area.value=text;preview.hidden=false;
   const counts='收藏'+data.bookmarks.length+'条、条件方案'+data.presets.length+'个、问题'+data.questions.length+'题';
   try{const url=URL.createObjectURL(new Blob([text],{type:'application/json;charset=utf-8'})),link=doc.createElement('a');link.href=url;link.download='ArchSpec-查阅备份.json';link.hidden=true;panel.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='已生成'+counts+'的本地备份；下方可查看完整JSON。';}
   catch{status.textContent='已生成'+counts+'的备份，下方JSON可手动保存。';}
  }catch(error){status.textContent=error.message||'备份暂不可生成。';}});
  file.addEventListener('change',async()=>{plan=null;apply.disabled=true;preview.hidden=true;area.value='';const chosen=file.files?.[0];if(!chosen)return;const own=++epoch,pid=project();
   try{if(chosen.size>LIMIT)throw Error('请选择2MB以内的查阅备份。');if(Object.values(live()).some(store=>store&&!store.persistent()))throw Error('当前页有未保存内容，请先导出本页备份，再处理导入。');
    const text=await chosen.text();if(own!==epoch||pid!==project())return;plan=prepareImport(storage(),text,pid,fields);area.value=text;preview.hidden=false;const a=plan.added,s=plan.skipped;
    status.textContent='将新增：收藏'+a.bookmarks+'条、条件方案'+a.presets+'个、问题'+a.questions+'题；已有'+(s.bookmarks+s.presets+s.questions)+'项保留本机版本。尚未写入。';apply.disabled=false;
   }catch(error){if(own===epoch)status.textContent=error.message||'备份格式无法读取。';}
  });
  const receiptKey='archspec:workspace-import-result:v1';
  apply.addEventListener('click',()=>{if(!plan)return;const pid=project();let result;
   try{commit(storage(),plan,pid);result={projectId:pid,added:plan.added};status.textContent='已合并保存，正在刷新读取。';}
   catch(error){result={projectId:pid,error:error.message||'导入未完成，请核对。'};status.textContent=result.error;}
   plan=null;apply.disabled=true;
   try{globalThis.sessionStorage?.setItem(receiptKey,JSON.stringify(result));}catch{}
   // 导入后立即GET刷新，避免旧页面的内存记录再次保存而丢掉刚合并的新增项。
   try{globalThis.location.reload();}catch{reload.hidden=false;}
  });
  function restoreReceipt(){try{const raw=globalThis.sessionStorage?.getItem(receiptKey);if(!raw)return;const result=JSON.parse(raw);if(result?.projectId!==project())return;
   globalThis.sessionStorage.removeItem(receiptKey);doc.querySelector('[data-workbench-view="projects"]')?.click();panel.open=true;
   status.textContent=result.error||'已合并读取：收藏新增'+result.added.bookmarks+'条、条件方案新增'+result.added.presets+'个、问题新增'+result.added.questions+'题；已有条目保留。';
  }catch{}}
  reload.addEventListener('click',()=>globalThis.location.reload());doc.addEventListener('archspec:project-change',()=>{clear();restoreReceipt();});clear();restoreReceipt();
 }
 return {exportData,prepareImport,commit,bind};
});
