/* 收藏保存固定出处的位置与独立个人笔记；正文、许可及原页每次仍由原读取门核对。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else {root.ArchSpecBookmarks=api;if(root.document)api.bind(root.document);}})(globalThis,function(){
 const KEY='archspec:clause-bookmarks:v1';
 function valid(v){return v&&v.version===1&&typeof v.projectId==='string'&&v.projectId.length<=200
  &&/^(gb55031|official-[a-f0-9]{32})$/.test(v.source_id||'')&&/^[a-f0-9]{64}$/.test(v.snapshot_id||'')
  &&Number.isInteger(v.index)&&v.index>=0&&v.index<=999999
  &&typeof v.title==='string'&&v.title.length>0&&v.title.length<=300
  &&typeof v.code==='string'&&v.code.length<=120&&typeof v.clause==='string'&&v.clause.length<=120
  &&Number.isInteger(v.page)&&v.page>=1&&v.page<=200&&(v.note===undefined||typeof v.note==='string'&&v.note.length<=2000);}
 const identity=v=>[v.projectId,v.source_id,v.snapshot_id,v.index].join(':');
 function compact(v){return {version:1,projectId:v.projectId,source_id:v.source_id,snapshot_id:v.snapshot_id,index:v.index,title:v.title,code:v.code,clause:v.clause,page:v.page,...(v.note?{note:v.note}:{})};}
 function createStore(storage){
  let rows=[];
  try{const raw=storage?.getItem(KEY);if(raw&&raw.length<=2000000){const data=JSON.parse(raw);if(Array.isArray(data)&&data.length<=100&&data.every(valid))rows=data.map(compact);}}catch{}
  let persistent=true;
  function save(){try{if(!storage)throw Error();storage.setItem(KEY,JSON.stringify(rows));persistent=true;}catch{persistent=false;}return persistent;}
  return {list(projectId){return rows.filter(v=>v.projectId===projectId).map(compact);},has(v){return valid(v)&&rows.some(r=>identity(r)===identity(v));},
   add(v){if(!valid(v))return false;rows=[compact(v),...rows.filter(r=>identity(r)!==identity(v))].slice(0,100);save();return true;},
   setNote(v,note){if(!valid(v)||typeof note!=='string'||note.length>2000)return false;
    const row=rows.find(r=>identity(r)===identity(v));if(!row)return false;
    if(note.trim())row.note=note;else delete row.note;save();return true;},
   remove(v){rows=rows.filter(r=>identity(r)!==identity(v));save();},persistent(){return persistent;}};
 }
 function exportList(rows,projectId){
  if(!Array.isArray(rows)||rows.length>100||typeof projectId!=='string'||rows.some(v=>!valid(v)||v.projectId!==projectId))throw Error('清单必须属于同一当前项目。');
  const heading='ArchSpec 查阅清单\n'+(projectId?'项目标识：'+projectId:'未选项目的收藏')
   +'\n\n本清单仅保存出处位置与已保存的个人笔记，不含规范正文，也未重新核验当前许可、版本及工程适用性。官方来源及原页请在应用中打开固定出处核对。';
  return heading+'\n\n'+rows.map((v,index)=>(index+1)+'. 《'+v.title+'》 '+(v.code||'文号待核')
   +'\n条款：'+(v.clause||'条号待核')+' · 保存的文件页：'+v.page
   +'\n固定来源：'+v.source_id+'\n快照：'+v.snapshot_id+'\n片段序号：'+v.index
   +'\n【个人笔记，非规范原文】\n'+(v.note||'未填写')+'\n【笔记结束】').join('\n\n')+'\n';
 }
 async function resolve(entry,{fetcher,isCurrent}){
  if(!valid(entry)||!isCurrent())return null;
  const response=await fetcher('/api/official-documents/'+encodeURIComponent(entry.source_id)+'/snapshots/'+entry.snapshot_id+'/chunks/'+entry.index,{method:'GET',cache:'no-store'});
  if(!isCurrent())return null;
  if(!response.ok)throw Error('收藏的出处当前不可读取，收藏仍保留。');
  const item=await response.json();if(!isCurrent())return null;
  if(item?.id!==entry.source_id+':'+entry.snapshot_id+':'+entry.index||item.source_id!==entry.source_id||item.snapshot_id!==entry.snapshot_id
   ||!Number.isInteger(item.page_start)||!Number.isInteger(item.page_end)||entry.page<item.page_start||entry.page>item.page_end)throw Error('收藏身份无法核对，未替换为其它条文。');
  return item;
 }
 let binding;
 function bind(doc){
  if(binding)return binding;
  const panel=doc.querySelector('#clause-bookmarks'),list=doc.querySelector('#clause-bookmark-list'),summary=doc.querySelector('#clause-bookmark-summary'),status=doc.querySelector('#clause-bookmark-status');
  if(!panel||!list)return null;
  let storage;try{storage=globalThis.localStorage;}catch{}
  const store=createStore(storage),project=()=>doc.querySelector('#search-project')?.value||'';
  const comparison=globalThis.ArchSpecClauseComparison?.create(doc,{project,valid,resolve});
  const exporter=doc.querySelector('#clause-bookmark-export'),exportPanel=doc.querySelector('#clause-bookmark-export-panel'),exportArea=doc.querySelector('#clause-bookmark-export-text');
  const clearExport=()=>{if(exportPanel)exportPanel.hidden=true;if(exportArea)exportArea.value='';};
  let activeControl=null,activeEntry=null;
  const paintActive=()=>{if(activeControl&&activeEntry){activeControl.textContent=store.has(activeEntry)?'已收藏':'收藏条文';activeControl.setAttribute('aria-pressed',String(store.has(activeEntry)));}};
  const say=text=>{if(status)status.textContent=text;};
  function render(){paintActive();clearExport();list.replaceChildren();const rows=store.list(project());comparison?.sync(rows);if(exporter)exporter.disabled=!rows.length;summary.textContent='收藏条文（'+rows.length+'）';
   if(!rows.length){const p=doc.createElement('p');p.className='empty-state';p.textContent='阅读条文时点击“收藏条文”，下次可从这里直接打开。';list.append(p);return;}
   for(const entry of rows){const row=doc.createElement('div');row.className='clause-bookmark-row';
    const open=doc.createElement('button');open.type='button';open.className='clause-bookmark-open';open.textContent=entry.title+' · '+(entry.clause||'文件第'+entry.page+'页');
    const meta=doc.createElement('small');meta.textContent=(entry.code||'文号待核')+' · 文件第'+entry.page+'页 · 版本与适用性待核';
    const remove=doc.createElement('button');remove.type='button';remove.className='quiet-button';remove.textContent='移除';remove.setAttribute('aria-label','移除收藏 '+open.textContent);
    open.addEventListener('click',async()=>{if(entry.projectId!==project())return;open.disabled=true;say('正在核对固定出处……');
     try{const ok=await globalThis.ArchspecPendingSearch?.openBookmark(entry);if(entry.projectId===project())say(ok?'已打开固定收藏出处。':'未打开收藏；当前查阅已改变。');}
     catch(error){if(entry.projectId===project())say(error.message||'收藏当前无法读取，已保留记录。');}finally{open.disabled=false;}});
    remove.addEventListener('click',()=>{if(entry.projectId!==project())return;store.remove(entry);render();say('已移除此收藏。');});
    const notes=doc.createElement('details');notes.className='clause-bookmark-notes';
    const noteSummary=doc.createElement('summary');noteSummary.textContent=entry.note?'查阅笔记（已填写）':'查阅笔记（个人）';
    const noteLabel=doc.createElement('label');noteLabel.textContent='我的查阅笔记（不是规范原文）';
    const noteInput=doc.createElement('textarea');noteInput.rows=4;noteInput.maxLength=2000;noteInput.value=entry.note||'';noteInput.placeholder='记录待核条件或设计疑问，最多2000字';noteInput.setAttribute('aria-label','我的查阅笔记：'+entry.title+' '+entry.clause);noteLabel.append(noteInput);
    const saveNote=doc.createElement('button');saveNote.type='button';saveNote.className='quiet-button';saveNote.textContent='保存笔记';
    saveNote.addEventListener('click',()=>{if(entry.projectId!==project())return;
     if(!store.setNote(entry,noteInput.value)){say('当前收藏或笔记无法保存，请确认仍在同一项目且未超过2000字。');return;}
     entry.note=noteInput.value.trim()?noteInput.value:undefined;noteSummary.textContent=entry.note?'查阅笔记（已填写）':'查阅笔记（个人）';clearExport();
     say(store.persistent()?'个人笔记已保存在当前浏览器，未修改规范原文或上传云端。':'浏览器无法保存，笔记仅在当前页面有效。');});
    notes.append(noteSummary,noteLabel,saveNote);row.append(open,meta,remove);const choice=comparison?.checkbox(entry);if(choice)row.append(choice);row.append(notes);list.append(row);
   }
  }
  function button(item){const prefix=item.source_id+':'+item.snapshot_id+':',legacy=item.snapshot_id+':';
   const tail=item.id?.startsWith(prefix)?item.id.slice(prefix.length):item.id?.startsWith(legacy)?item.id.slice(legacy.length):'';
   if(!/^(0|[1-9]\d{0,5})$/.test(tail))return null;
   const entry={version:1,projectId:project(),source_id:item.source_id,snapshot_id:item.snapshot_id,index:Number(tail),title:item.title,code:item.standard_code||'',clause:item.clause_number||'',page:item.page_start};
   if(!valid(entry))return null;
   const control=doc.createElement('button');control.type='button';control.className='quiet-button reader-bookmark';
   activeControl=control;activeEntry=entry;const paint=paintActive;paint();
   control.addEventListener('click',()=>{if(project()!==entry.projectId)return;if(store.has(entry))store.remove(entry);else store.add(entry);paint();render();
    say(store.persistent()?'收藏保存在当前浏览器，未上传云端。':'浏览器无法保存，收藏仅在当前页面有效。');});return control;
  }
  exporter?.addEventListener('click',()=>{
   const rows=store.list(project());if(!rows.length)return;
   const text=exportList(rows,project());if(exportArea)exportArea.value=text;if(exportPanel)exportPanel.hidden=false;
   try{const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
    const link=doc.createElement('a');link.href=url;link.download='ArchSpec-查阅清单.txt';link.hidden=true;panel.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);say('已生成已保存的笔记与出处位置清单，下方文本可选择复制。');
   }catch{say('清单已生成，可选择下方文本手动保存。');}
  });
  doc.addEventListener('archspec:project-change',()=>{render();say('');});render();binding={button,store};return binding;
 }
 return {valid,createStore,resolve,exportList,bind,storageKey:KEY,getStore:()=>binding?.store};
});
