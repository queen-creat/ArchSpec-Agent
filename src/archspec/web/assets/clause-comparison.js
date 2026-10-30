/* 条文对照只在本页保存选择；每次显式打开均重读各自固定出处，不拼接工程结论。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecClauseComparison=api;})(globalThis,function(){
 const key=e=>[e.projectId,e.source_id,e.snapshot_id,e.index].join(':');
 async function load(entries,{projectId,valid,read,isCurrent}){
  if(!Array.isArray(entries)||entries.length<2||entries.length>4||entries.some(e=>!valid(e)||e.projectId!==projectId)
   ||new Set(entries.map(key)).size!==entries.length)throw Error('请选择当前项目的2—4条不同收藏。');
  if(!isCurrent())return null;
  const results=await Promise.allSettled(entries.map(async entry=>{
   const item=await read(entry);if(!isCurrent())return null;
   if(!item||typeof item.text!=='string'||!Array.isArray(item.warnings)||!/^[a-f0-9]{64}$/.test(item.snapshot_fingerprint||''))throw Error('当前正文或固定身份不完整，请重新打开出处核对。');
   return item;
  }));
  if(!isCurrent())return null;
  return results.map((result,i)=>result.status==='fulfilled'?{entry:entries[i],item:result.value}:{entry:entries[i],error:result.reason?.message||'当前出处无法读取。'});
 }
 function create(doc,{project,valid,resolve}){
  const trigger=doc.querySelector('#clause-compare-open'),panel=doc.querySelector('#clause-comparison'),cards=doc.querySelector('#clause-compare-cards'),status=doc.querySelector('#clause-compare-status');
  if(!trigger||!panel||!cards)return null;
  let selected=new Map(),generation=0;
  const element=(tag,text,cls)=>{const n=doc.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  function paint(){trigger.textContent='对照所选条文（'+selected.size+'/4）';trigger.disabled=selected.size<2;}
  function clear(){generation++;cards.replaceChildren();panel.hidden=true;status.textContent='';paint();}
  function checkbox(entry){
   const label=element('label','','clause-compare-choice'),input=doc.createElement('input');input.type='checkbox';input.checked=selected.has(key(entry));
   input.setAttribute('aria-label','加入对照：'+entry.title+' '+entry.clause);
   input.addEventListener('change',()=>{if(project()!==entry.projectId){input.checked=false;return;}
    if(input.checked&&selected.size>=4){input.checked=false;const note=doc.querySelector('#clause-bookmark-status');if(note)note.textContent='最多对照4条，请先取消一条。';return;}
    if(input.checked)selected.set(key(entry),entry);else selected.delete(key(entry));clear();
   });label.append(input,element('span','加入对照'));return label;
  }
  function sync(rows){const available=new Set(rows.map(key));for(const k of selected.keys())if(!available.has(k))selected.delete(k);clear();}
  trigger.addEventListener('click',async()=>{
   clear();panel.hidden=false;const own=generation,pid=project(),entries=[...selected.values()];
   const current=()=>own===generation&&pid===project();trigger.disabled=true;status.textContent='正在重读所选条文……';
   try{
    const rows=await load(entries,{projectId:pid,valid,read:entry=>resolve(entry,{fetcher:fetch,isCurrent:current}),isCurrent:current});
    if(!rows||!current())return;
    let readable=0;
    for(const row of rows){const card=element('article','','clause-compare-card'),entry=row.entry;
     card.append(element('h4',entry.title),element('p',(entry.code||'文号待核')+' · '+(entry.clause||'条号待核'),'clause-compare-meta'));
     if(!row.item){card.append(element('p',row.error,'source-warning'));cards.append(card);continue;}
     readable++;const item=row.item,metadata=globalThis.ArchSpecSourceMetadata;
     card.querySelector('h4').textContent=item.title;card.querySelector('.clause-compare-meta').textContent=(item.standard_code||'文号待核')+' · '+(item.clause_number||'条号待核');
     card.append(element('p',(item.source_format==='HTML'?'网页排版位置 ':'文件页 ')+item.page_start+(item.page_end!==item.page_start?'—'+item.page_end:''),'clause-compare-meta'));
     if(metadata?.profile)card.append(metadata.profile(item));
     if(item.warnings.length)card.append(element('p','识别或图片存在待核提示，请核原页中的数字、单位、前提与例外。','source-warning'));
     card.append(element('p',item.text,'clause-compare-text'));
     const open=element('button','打开此条原页','secondary-button');open.type='button';open.setAttribute('aria-label','打开原页：'+entry.title+' '+entry.clause);
     open.addEventListener('click',async()=>{if(!current())return;open.disabled=true;
      try{const ok=await globalThis.ArchspecPendingSearch?.openComparisonPosition({...item,chunk_id:item.id},{query:doc.querySelector('#pending-query')?.value.trim()||'',projectId:pid||null,isCurrent:current});
       if(current())status.textContent=ok?'原页已在上方阅读区打开，对照条文仍保留。':'本次读取已停止，请重新打开对照。';
      }catch(error){if(current()){
       // 原出处重新读取失败，立即撤下同一快照的旧对照文字，保留其它独立可读条目。
       for(const old of rows)if(old.item?.source_id===item.source_id&&old.item.snapshot_id===item.snapshot_id){const target=cards.children[rows.indexOf(old)];target?.replaceChildren(element('h4',old.entry.title),element('p','此固定出处当前无法重新读取，请重新打开对照核对。','source-warning'));}
       status.textContent=error.message||'原页无法读取。';
      }}finally{if(current())open.disabled=false;}
     });card.append(open);
     const details=doc.createElement('details');details.append(element('summary','完整出处与核验记录'));
     if(metadata?.details)details.append(metadata.details(item));else details.append(element('p','来源 '+item.source_id+'\n快照 '+item.snapshot_id));
     card.append(details);cards.append(card);
    }
    status.textContent='已重读'+readable+'条，共'+rows.length+'条；保留各自原文，未判断效力优先或合并为工程结论。';
    panel.scrollIntoView({block:'start',behavior:'instant'});
   }catch(error){if(current())status.textContent=error.message||'对照暂不可读取。';}
   finally{if(current())paint();}
  });
  doc.querySelector('#clause-compare-close')?.addEventListener('click',()=>{clear();trigger.focus();});
  doc.querySelector('#pending-query')?.addEventListener('input',clear);
  doc.querySelector('#pending-source')?.addEventListener('change',clear);
  doc.addEventListener('archspec:query-start',clear);
  doc.addEventListener('archspec:project-change',()=>{selected.clear();clear();});paint();
  return {checkbox,sync};
 }
 return {load,create};
});
