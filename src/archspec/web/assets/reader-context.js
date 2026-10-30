/* 普通阅读的有限补读；原字与关系证明沿用服务端已有结果，不推断适用结论。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecReaderContext=api;})(globalThis,function(){
 function valid(body,item){
  const prefix=item.source_id+':'+item.snapshot_id+':',index=String(item.id||'').split(':').at(-1);
  return body?.source_id===item.source_id&&body.snapshot_id===item.snapshot_id&&body.selected_chunk_id===prefix+index
   &&body.title===item.title&&body.standard_code===item.standard_code&&/^[a-f0-9]{64}$/.test(body.snapshot_fingerprint||'')
   &&(!item.snapshot_fingerprint||body.snapshot_fingerprint===item.snapshot_fingerprint)
   &&body.is_citable===false&&body.evidence_mode==='CANDIDATE_ONLY'&&typeof body.complete==='boolean'
   &&Array.isArray(body.gaps)&&body.gaps.length<=100&&body.gaps.every(gap=>typeof gap==='string'&&gap.length<=2000)
   &&Array.isArray(body.fragments)&&body.fragments.length>0&&body.fragments.length<=8
   &&body.fragments.every(row=>typeof row.id==='string'&&row.id.startsWith(prefix)&&/^(0|[1-9]\d{0,5})$/.test(row.id.slice(prefix.length))
    &&typeof row.text==='string'&&(row.clause_number==null||typeof row.clause_number==='string'&&row.clause_number.length<=120)
    &&['MATCH','CONTINUATION','RELATED_CONTEXT'].includes(row.role)&&Number.isInteger(row.page_start)&&Number.isInteger(row.page_end)
    &&row.page_start>=1&&row.page_end>=row.page_start&&row.page_end<=200)
   &&new Set(body.fragments.map(row=>row.id)).size===body.fragments.length
   &&body.fragments.reduce((sum,row)=>sum+Array.from(row.text).length,0)<=16000
   &&body.fragments.some(row=>row.id===body.selected_chunk_id&&row.role==='MATCH'&&row.text===item.text);
 }
 function labels(body){
  // 只有现有关系证明完整通过，才称为引用；其余一律保留为普通关联片段。
  const evidence=body.fragments.map(row=>({...body,...row,chunk_id:row.id,evidence_id:row.id})),result=new Map();
  for(const link of body.clause_references||[]){
   const proof=globalThis.ArchSpecRelationProof?.verify(link.proof,body,evidence);
   if(!proof||proof.kind!=='CLAUSE_REFERENCE')continue;
   for(const id of proof.admitted_chunk_ids)result.set(id,proof.direction==='OUTGOING'?'本条明确引用':'含有对本条的引用');
  }
  return result;
 }
 function tables(body){
  // 表号、表题、续表及片段归属都交给原有独立证明核验；不把任意同号当作原表。
  const evidence=body.fragments.map(row=>({...body,...row,chunk_id:row.id,evidence_id:row.id})),result=[];
  for(const reference of (Array.isArray(body.table_references)?body.table_references:[]).slice(0,3)){
   const proof=globalThis.ArchSpecRelationProof?.verify(reference.proof,body,evidence);
   if(!proof||proof.kind!=='TABLE_REFERENCE'||proof.target_unit_key!=='table:'+reference.table_number)continue;
   const pages=[],seen=new Set();
   for(const entry of (reference.pages||[]).slice(0,8)){
    const row=body.fragments.find(row=>row.id===entry.chunk_id),page=entry.pdf_page,key=entry.chunk_id+':'+page;
    if(!row||!proof.admitted_chunk_ids.includes(row.id)||seen.has(key)
     ||!Number.isInteger(page)||page<row.page_start||page>row.page_end)continue;
    seen.add(key);pages.push({row,page,caption:entry.caption,continuation:entry.continuation});
   }
   result.push({number:reference.table_number,status:proof.status,pages});
  }
  return result;
 }
 function controls(doc,item,{isCurrent,onOpen,onUnavailable}={}){
  const panel=doc.createElement('details');panel.className='reader-context';
  const summary=doc.createElement('summary');summary.textContent='同号续文与相关片段';
  const note=doc.createElement('p');note.className='reader-section-note';note.textContent='按当前固定版本补读，最多8个片段、16000字；相邻片段不代表例外或适用结论。识别疑点仍须核原页。';
  const status=doc.createElement('p');status.className='reader-section-note';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const list=doc.createElement('div');list.className='reader-context-list';
  const retry=doc.createElement('button');retry.type='button';retry.className='quiet-button';retry.textContent='重新读取上下文';
  let epoch=0;
  const current=()=>typeof isCurrent==='function'&&isCurrent();
  async function load(){
   if(!current())return;const own=++epoch,active=()=>own===epoch&&current();retry.disabled=true;list.replaceChildren();status.textContent='正在读取同一固定版本的上下文……';
   const base='/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+'/chunks/';
   try{
    const response=await fetch(base+String(item.id).split(':').at(-1)+'/context',{method:'GET',cache:'no-store'});
    if(!active())return;if(!response.ok){onUnavailable?.();throw Error('当前固定上下文无法读取，未切换版本。');}
    const body=await globalThis.ArchSpecHTTP.json(response,'固定上下文响应暂不可读取');if(!active())return;
    if(!valid(body,item))throw Error('固定上下文身份或范围无法核对，未展示其它版本。');
    const relations=labels(body);
    async function openRow(row,page,view,button){
     if(!active())return;button.disabled=true;status.textContent='正在重核所选固定片段……';
     try{const result=await fetch(base+row.id.split(':').at(-1),{method:'GET',cache:'no-store'});if(!active())return;
      if(!result.ok){onUnavailable?.();throw Error('所选固定片段当前无法读取。');}
      const selected=await globalThis.ArchSpecHTTP.json(result,'固定片段暂不可读取');if(!active())return;
      if(selected?.id!==row.id||selected.source_id!==body.source_id||selected.snapshot_id!==body.snapshot_id||selected.snapshot_fingerprint!==body.snapshot_fingerprint
       ||selected.text!==row.text||selected.page_start!==row.page_start||selected.page_end!==row.page_end||selected.clause_number!==row.clause_number)throw Error('片段身份无法核对，未改用其它原文。');
      if(!onOpen(selected,page,{view})&&active())throw Error('所选片段暂不能打开。');
     }catch(error){if(active())status.textContent=error.message||'固定片段暂不可读取。';}finally{if(active())button.disabled=false;}
    }
    const references=tables(body);
    if(references.length){
     const section=doc.createElement('section');section.className='reader-context-tables';
     const title=doc.createElement('strong');title.textContent='本条引用的原表';section.append(title);
     const hint=doc.createElement('p');hint.className='reader-section-note';hint.textContent='打开同一快照的原表页；原表放大仍以实际页面定位为准，表注和适用条件请一并核对。';section.append(hint);
     for(const table of references){
      if(!table.pages.length||table.status!=='LOCATED'){
       const gap=doc.createElement('p');gap.className='reader-section-note';gap.textContent='表'+table.number+'：'+(table.pages.length?'只定位到部分原表页，未证明完整。':'本次未定位到可读原表页，请直接核原件。');section.append(gap);
      }
      for(const entry of table.pages){
       const open=doc.createElement('button');open.type='button';open.className='book-index-entry';
       const title=doc.createElement('strong');title.textContent=(entry.continuation?'续表':'原表')+' '+table.number+(body.source_format==='HTML'?' · 网页排版第 ':' · 文件第 ')+entry.page+' 页';
       const caption=doc.createElement('small');caption.textContent=entry.caption;open.append(title,caption);
       open.addEventListener('click',()=>openRow(entry.row,entry.page,'ORIGINAL',open));section.append(open);
      }
     }
     list.append(section);
    }
    for(const row of body.fragments){
     const entry=doc.createElement('details');entry.className='reader-context-entry';const heading=doc.createElement('summary');
     const role=row.role==='MATCH'?'当前片段':row.role==='CONTINUATION'?'同号续文':relations.get(row.id)||'相邻或关联片段';
     heading.textContent=role+' · '+(row.clause_number||'未识别条号')+(body.source_format==='HTML'?' · 网页排版第 ':' · 文件第 ')+row.page_start+(row.page_end!==row.page_start?'—'+row.page_end:'')+' 页';
     const text=doc.createElement('p');text.className='reader-context-text';text.textContent=row.text;
     const open=doc.createElement('button');open.type='button';open.className='secondary-button';open.textContent='打开此片段与原页';
     open.addEventListener('click',()=>openRow(row,row.page_start,'TEXT',open));
     entry.append(heading);
     if(Array.isArray(row.warnings)&&row.warnings.length){const warning=doc.createElement('p');warning.className='source-warning';warning.textContent='本片段有 '+row.warnings.length+' 处识别提示，须打开原页核对。';entry.append(warning);}
     entry.append(text,open);list.append(entry);
    }
    const coverage=body.selected_clause_coverage;
    status.textContent='已读取 '+body.fragments.length+' 个固定片段。'+(coverage?.complete===true?'已包含本次识别到的同号片段；这不证明原版识别完整。':'同号片段未证明完整，请核原页。')
     +(body.complete?'':' 补读存在缺口。')+(body.gaps.length?' '+body.gaps.join('；'):'');
   }catch(error){if(active())status.textContent=error.message||'上下文暂不可读取，请稍后主动重试。';}finally{if(active())retry.disabled=false;}
  }
  panel.addEventListener('toggle',()=>{if(panel.open)void load();else{epoch++;retry.disabled=false;}});
  retry.addEventListener('click',()=>void load());panel.append(summary,note,status,list,retry);return panel;
 }
 return {valid,labels,tables,controls};
});
