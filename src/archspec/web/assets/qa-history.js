/* 历史列表仅展示当前项目摘要；显式打开交给原问答GET恢复，不复制旧回答。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else{root.ArchSpecQAHistory=api;if(root.document)api.bind(root.document);}})(globalThis,function(){
 const states={CREATED:'已登记',RUNNING:'处理中',WAITING_CONDITIONS:'待补条件',WAITING_SOURCE:'待补正文',COMPLETED:'已整理',INTERRUPTED:'已中断',FAILED:'未完成'};
 const uuid=v=>typeof v==='string'&&/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
 function valid(body,project){return body&&body.project_id===project&&typeof body.has_more==='boolean'&&Array.isArray(body.items)&&body.items.length<=20
  &&body.items.every(row=>uuid(row.id)&&row.project_id===project&&typeof row.query==='string'&&row.query.trim()&&row.query.length<=500
   &&['LOCAL','MODEL'].includes(row.mode)&&Object.hasOwn(states,row.state)&&Number.isSafeInteger(row.revision)&&row.revision>=0
   &&typeof row.updated_at==='string'&&Number.isFinite(Date.parse(row.updated_at)))&&new Set(body.items.map(v=>v.id)).size===body.items.length;}
 function bind(doc){
  const panel=doc.querySelector('#qa-history'),list=doc.querySelector('#qa-history-list'),status=doc.querySelector('#qa-history-status');
  const refresh=doc.querySelector('#qa-history-refresh'),previous=doc.querySelector('#qa-history-previous'),next=doc.querySelector('#qa-history-next');
  if(!panel||!list||!status)return;
  let epoch=0,offset=0,more=false,loading=false;
  const project=()=>doc.querySelector('#search-project')?.value||null;
  function controls(){if(refresh)refresh.disabled=loading;if(previous)previous.disabled=loading||offset===0;if(next)next.disabled=loading||!more||offset>=10000;}
  async function load(page=0){
   const own=++epoch,owner=project();loading=true;more=false;controls();list.replaceChildren();status.textContent='正在读取历史问题……';
   const current=()=>own===epoch&&owner===project();
   try{
    const params=new URLSearchParams({limit:'20',offset:String(page)});if(owner)params.set('project_id',owner);
    const response=await fetch('/api/official-qa/tasks?'+params.toString(),{method:'GET',cache:'no-store'});
    const body=await globalThis.ArchSpecHTTP.json(response,'历史问题响应暂不可读取');
    if(!current())return;
    if(!response.ok||!valid(body,owner))throw Error('历史问题暂不可读取，请刷新列表。');
    offset=page;more=body.has_more;
    for(const task of body.items){const row=doc.createElement('div');row.className='qa-history-row';
     const open=doc.createElement('button');open.type='button';open.className='qa-history-open';open.textContent=task.query;
     const meta=doc.createElement('small');meta.textContent=states[task.state]+' · '+(task.mode==='MODEL'?'模型题，恢复后需重新同意':'本地整理')+' · '+new Date(task.updated_at).toLocaleString('zh-CN',{hour12:false});
     open.addEventListener('click',()=>{if(!current())return;doc.dispatchEvent(new CustomEvent('archspec:qa-history-open',{detail:{id:task.id,project_id:task.project_id}}));});
     row.append(open,meta);list.append(row);
    }
    status.textContent=body.items.length?'第 '+(Math.floor(offset/20)+1)+' 页 · 按最近更新排序；打开只恢复输入和状态。':'当前项目暂无历史问题；使用“按出处整理回答”后会保存。';
   }catch(error){if(current())status.textContent=error.message||'历史问题暂不可读取。';}
   finally{if(current()){loading=false;controls();}}
  }
  panel.addEventListener('toggle',()=>{if(panel.open)void load();});
  refresh?.addEventListener('click',()=>void load());previous?.addEventListener('click',()=>void load(Math.max(0,offset-20)));next?.addEventListener('click',()=>{if(more)void load(offset+20);});
  doc.addEventListener('archspec:project-change',()=>{epoch++;offset=0;more=false;loading=false;list.replaceChildren();status.textContent='';controls();if(panel.open)void load();});
  doc.addEventListener('archspec:qa-task-updated',event=>{if(panel.open&&event.detail?.project_id===project())void load();});controls();
 }
 return {valid,bind};
});
