/* 历史任务只恢复出处位置；点击后重读当前固定原件，不重放旧回答或收费操作。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecTaskReadings=api;})(globalThis,function(){
 function valid(body,task){return body?.schema_version==='QA_TASK_READING_V1'&&body.task_id===task.id&&body.revision===task.revision&&body.project_id===task.project_id
  &&['AVAILABLE','NOT_RECORDED'].includes(body.state)&&Array.isArray(body.positions)&&body.positions.length<=8
  &&(body.state==='AVAILABLE'?body.positions.length>0:body.positions.length===0)
  &&body.positions.every(row=>/^(gb55031|official-[a-f0-9]{32})$/.test(row?.source_id||'')&&/^[a-f0-9]{64}$/.test(row.snapshot_id||'')
   &&/^[a-f0-9]{64}$/.test(row.snapshot_fingerprint||'')&&typeof row.chunk_id==='string'&&row.chunk_id.startsWith(row.source_id+':'+row.snapshot_id+':')
   &&/^(0|[1-9]\d{0,5})$/.test(row.chunk_id.split(':').at(-1))&&typeof row.title==='string'&&row.title.length<=300
   &&typeof row.standard_code==='string'&&row.standard_code.length<=120&&(row.clause_number===null||typeof row.clause_number==='string'&&row.clause_number.length<=120)
   &&['MATCH','CONTINUATION','RELATED_CONTEXT'].includes(row.role)&&Number.isInteger(row.page_start)&&Number.isInteger(row.page_end)&&row.page_start>=1&&row.page_end>=row.page_start&&row.page_end<=200)
  &&new Set(body.positions.map(row=>row.chunk_id)).size===body.positions.length;}
 function create(doc,container,{openPosition}={}){
  const panel=doc.createElement('details');panel.className='qa-task-readings';panel.hidden=true;
  const summary=doc.createElement('summary');summary.textContent='本题上次整理的出处位置';
  const note=doc.createElement('p');note.className='reader-section-note';note.textContent='这里只保存上次的固定出处位置，不是本次已核回的回答。点击后重新检查当前原件和权限；不会重新检索、联网或调用模型。';
  const status=doc.createElement('p');status.className='reader-section-note';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const record=doc.createElement('div'),list=doc.createElement('div');list.className='qa-task-reading-list';panel.append(summary,note,status,record,list);container.append(panel);
  let owner=null,key='',epoch=0;
  const current=()=>owner&&typeof owner.isCurrent==='function'&&owner.isCurrent();
  async function load(){
   if(!current())return;const own=++epoch,selected=owner,active=()=>own===epoch&&owner===selected&&current();record.replaceChildren();list.replaceChildren();status.textContent='正在读取本题保存的出处位置……';
   try{
    const params=new URLSearchParams();if(selected.task.project_id)params.set('project_id',selected.task.project_id);
    const response=await fetch('/api/official-qa/tasks/'+selected.task.id+'/reading-positions'+(params.size?'?'+params:''),{method:'GET',cache:'no-store'});
    if(!active())return;if(!response.ok)throw Error('本题出处位置暂不可读取，未重放问答。');
    const body=await globalThis.ArchSpecHTTP.json(response,'本题出处位置暂不可读取');if(!active())return;
    if(!valid(body,selected.task))throw Error('出处位置与当前任务版本无法核对。');
    if(body.state==='NOT_RECORDED'){status.textContent='这次操作没有保存可恢复的出处位置；较早任务不会推造出处。需要时可明确在本地重新整理。';return;}
    if(globalThis.ArchSpecQARecord){
     // 历史只重读已提交的位置，不恢复旧回答，也不能从当前已编辑控件冒取上次条件。
     const task={id:selected.task.id,revision:selected.task.revision};
     const states={COMPLETED:'已整理，适用性待核',WAITING_CONDITIONS:'待补条件',WAITING_SOURCE:'待补正文'};
     const packet={kind:'TASK_POSITIONS',task,query:selected.query,projectId:selected.task.project_id||'',projectLabel:selected.projectLabel||selected.task.project_id||'',
      scope:selected.scope||'__all__',scopeLabel:selected.scopeLabel||selected.scope||'原范围以任务记录为准',
      summary:'上次记录状态：'+(states[selected.task.state]||'待核')+'；本次只核读固定出处，上次回答未重放。',
      modeLabel:'本次未重新检索、获取官网资料或调用模型。',
      conditions:['只恢复出处位置，未恢复或重新确认上次工程条件。'],provenance:['本次未重新采用项目条件。'],questions:[],requirements:[],
      gaps:['出处原字、版本、现行性和工程适用性仍须对照原页核验；上次命中位置不自动成为本次回答依据。'],
      evidence:body.positions.map(row=>({...row}))};
     record.append(globalThis.ArchSpecQARecord.controls(doc,packet,{isCurrent:active,metadata:globalThis.ArchSpecSourceMetadata,citationCopy:globalThis.ArchSpecCitationCopy}));
    }
    for(const row of body.positions){
     const open=doc.createElement('button');open.type='button';open.className='book-index-entry';
     const title=doc.createElement('strong');title.textContent=row.title+' · '+(row.standard_code||'编号待核');
     const meta=doc.createElement('small');meta.textContent=({MATCH:'上次命中',CONTINUATION:'上次同号续文',RELATED_CONTEXT:'上次相关片段'}[row.role])+' · '+(row.clause_number||'未识别条号')+' · 保存位置第 '+row.page_start+(row.page_end!==row.page_start?'—'+row.page_end:'')+' 页 · 点击重读';open.append(title,meta);
     open.addEventListener('click',async()=>{if(!active())return;open.disabled=true;status.textContent='正在重新核对本题固定出处……';
      try{const opened=await openPosition(row,{query:selected.query,projectId:selected.task.project_id,isCurrent:active});
       if(active())status.textContent=opened?'已重新读取该处原文，请在阅读区对照；上次结论未重放。':'读取已停止，未替换为其它出处。';
      }catch(error){if(active())status.textContent=error.message||'固定出处当前无法读取。';}finally{if(active())open.disabled=false;}});
     list.append(open);
    }
    status.textContent='本次操作保存了 '+body.positions.length+' 个固定位置；是否当前可读、适用仍需逐处重核。';
   }catch(error){if(active())status.textContent=error.message||'本题出处位置暂不可读取。';}
  }
  panel.addEventListener('toggle',()=>{if(panel.open)void load();else epoch++;});
  return {set(task,options){
    const next=task.id+':'+task.revision;
    // 相同任务对象的重复绘制保留所属对象；新的GET任务对象与旧控件一起换代。
    // 不让旧按钮继续显示为空操作，也不因状态刷新自动重放读取或下载。
    const same=next===key&&owner?.task===task&&owner.query===options.query&&owner.scope===options.scope;
    if(!same){
     epoch++;key=next;panel.open=false;record.replaceChildren();list.replaceChildren();status.textContent='';
     owner={...options,task};
    }else Object.assign(owner,options,{task});
    panel.hidden=!['COMPLETED','WAITING_CONDITIONS','WAITING_SOURCE'].includes(task.state);
   },clear(){owner=null;key='';epoch++;panel.hidden=true;panel.open=false;record.replaceChildren();list.replaceChildren();status.textContent='';}};
 }
 return {valid,create};
});
