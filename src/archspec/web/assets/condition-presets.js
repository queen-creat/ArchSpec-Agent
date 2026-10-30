/* 条件方案只保存用户明确手填值；主动填入不提交问答，不继承项目资料证据或模型同意。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecConditionPresets=api;})(globalThis,function(){
 const KEY='archspec:manual-condition-presets:v1';let boundStore;
 function createStore(storage,fieldKeys){
  const keys=new Set(fieldKeys),copy=row=>({...row,conditions:{...row.conditions}});
  function conditionsValid(value){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length>0&&Object.keys(value).length<=keys.size
   &&Object.entries(value).every(([k,v])=>keys.has(k)&&typeof v==='string'&&!!v.trim()&&v.length<=100);}
  function valid(row){return row&&row.version===1&&typeof row.id==='string'&&/^[a-f0-9-]{36}$/.test(row.id)
   &&typeof row.projectId==='string'&&row.projectId.length<=200&&typeof row.name==='string'&&!!row.name.trim()&&row.name.length<=40
   &&typeof row.savedAt==='string'&&Number.isFinite(Date.parse(row.savedAt))&&conditionsValid(row.conditions)
   &&Object.keys(row).sort().join(',')==='conditions,id,name,projectId,savedAt,version';}
  let rows=[],persistent=!!storage;
  try{const raw=storage?.getItem(KEY);if(raw&&raw.length<=500000){const data=JSON.parse(raw);if(Array.isArray(data)&&data.length<=50&&data.every(valid)&&new Set(data.map(r=>r.id)).size===data.length)rows=data.map(copy);}}catch{persistent=false;}
  function save(){try{if(!storage)throw Error();storage.setItem(KEY,JSON.stringify(rows));persistent=true;}catch{persistent=false;}}
  return {validEntry:valid,list(projectId){return rows.filter(r=>r.projectId===projectId).map(copy);},
   get(projectId,id){const row=rows.find(r=>r.projectId===projectId&&r.id===id);return row?copy(row):null;},
   add(projectId,name,conditions){
    if(typeof projectId!=='string'||projectId.length>200||typeof name!=='string'||!name.trim()||name.trim().length>40)throw Error('请填写40字以内的方案名。');
    if(!conditionsValid(conditions))throw Error('没有可保存的手填条件，或含未登记字段；项目资料勾选项不保存为手填方案。');
    if(rows.some(r=>r.projectId===projectId&&r.name===name.trim()))throw Error('此方案名已存在，请换一个名称。');
    if(rows.length>=50||rows.filter(r=>r.projectId===projectId).length>=10)throw Error('当前项目最多10个方案，浏览器合计最多50个；请先移除不再使用的方案。');
    const row={version:1,id:globalThis.crypto.randomUUID(),projectId,name:name.trim(),savedAt:new Date().toISOString(),conditions:{...conditions}};
    rows.unshift(row);save();return copy(row);
   },remove(projectId,id){rows=rows.filter(r=>r.projectId!==projectId||r.id!==id);save();},persistent(){return persistent;}};
 }
 function bind(doc,{fields,project,read,apply}){
  const panel=doc.querySelector('#condition-presets'),name=doc.querySelector('#condition-preset-name'),list=doc.querySelector('#condition-preset-list'),status=doc.querySelector('#condition-preset-status'),save=doc.querySelector('#condition-preset-save'),summary=doc.querySelector('#condition-preset-summary');
  if(!panel||!list||!name||!save)return null;
  let storage;try{storage=globalThis.localStorage;}catch{}
  const store=createStore(storage,fields.map(f=>f.key)),labels=new Map(fields.map(f=>[f.key,f.label.textContent]));boundStore=store;
  const node=(tag,text,cls)=>{const e=doc.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e;};
  function render(){list.replaceChildren();const pid=project(),rows=store.list(pid);summary.textContent='常用条件方案（'+rows.length+'）';
   doc.querySelector('#condition-preset-owner').textContent=pid?'只列出当前项目保存的方案。':'当前未选项目，方案单独保存，不属于任何工程。';
   if(!rows.length){list.append(node('p','填好本轮条件后，为它取名保存，下次可主动填入。','empty-state'));return;}
   for(const row of rows){const card=node('article','','condition-preset-card');
    const title=node('strong',row.name),items=node('dl','','condition-preset-values');
    for(const [key,value] of Object.entries(row.conditions)){items.append(node('dt',labels.get(key)),node('dd',value));}
    const use=node('button','填入并替换本轮条件','secondary-button');use.type='button';use.setAttribute('aria-label','填入方案：'+row.name);
    use.addEventListener('click',()=>{if(project()!==pid)return;const current=store.get(pid,row.id);if(!current)return;
     try{apply({...current.conditions});status.textContent='已填入“'+row.name+'”；请核对本题适用条件，再主动整理。';}
     catch(error){status.textContent=error.message||'当前不能填入方案。';}
    });
    const remove=node('button','移除方案','quiet-button');remove.type='button';remove.setAttribute('aria-label','移除方案：'+row.name);
    remove.addEventListener('click',()=>{if(project()!==pid)return;store.remove(pid,row.id);render();status.textContent=store.persistent()?'已移除保存方案，本轮已填条件未改动。':'本页已移除，浏览器未能保存变更。';});
    const actions=node('div','','condition-preset-actions');actions.append(use,remove);card.append(title,items,actions);list.append(card);
   }
  }
  save.addEventListener('click',()=>{try{store.add(project(),name.value,read());name.value='';render();status.textContent=store.persistent()?'已保存于当前浏览器；未上传或自动用于其它问题。':'浏览器无法保存，方案仅本页有效。';}catch(error){status.textContent=error.message;}});
  doc.addEventListener('archspec:project-change',()=>{name.value='';status.textContent='';render();});render();return {store};
 }
 return {createStore,bind,storageKey:KEY,getStore:()=>boundStore};
});
