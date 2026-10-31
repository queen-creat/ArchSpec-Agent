/* 本书原页只认固定快照范围；图题导航不产生图示读数、条文关系或现行结论。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecBookPages=api;})(globalThis,function(){
 const bounds=globalThis.ArchSpecPageBounds||(typeof require==='function'?require('./page-bounds.js'):null);
 function valid(body,item){return body?.version==='FIXED_BOOK_PAGES_V1'&&body.navigation_only===true
  &&body.source_id===item.source_id&&body.snapshot_id===item.snapshot_id&&body.title===item.title&&body.standard_code===item.standard_code
  &&/^[a-f0-9]{64}$/.test(body.snapshot_fingerprint||'')&&(!item.snapshot_fingerprint||body.snapshot_fingerprint===item.snapshot_fingerprint)
  &&['PDF','HTML'].includes(body.source_format)&&Number.isInteger(body.page_count)&&body.page_count>=item.page_end&&body.page_count<=(bounds?bounds.limit(body):200);}
 function chunkIdentity(item){
  const id=item.id||item.chunk_id,prefix=item.source_id+':'+item.snapshot_id+':',legacy=item.snapshot_id+':';
  if(typeof id!=='string')return null;const suffix=id.startsWith(prefix)?id.slice(prefix.length):id.startsWith(legacy)?id.slice(legacy.length):'';
  return /^(?:0|[1-9]\d{0,5})$/.test(suffix)?{id:prefix+suffix,index:suffix}:null;
 }
 function validReferences(body,item){
  const fixed=chunkIdentity(item),characters=Array.from(item.text||'');
  if(!fixed||!valid(body,item)||body.selected_chunk_id!==fixed.id||body.reference_mode!=='FIGURE_CAPTION_NAVIGATION_ONLY'
   ||typeof body.references_truncated!=='boolean'||!Array.isArray(body.figure_references)||body.figure_references.length>8)return false;
  const labels=new Set();
  for(const row of body.figure_references){
   if(!row||labels.has(row.label)||!Number.isInteger(row.start)||!Number.isInteger(row.end)||row.start<0||row.end<=row.start||row.end>characters.length
    ||characters.slice(row.start,row.end).join('')!==row.original||row.original.replace(/\s/g,'')!==row.label
    ||!/^(?:附?图)(?:[A-Z](?:[-.]\d+)*|\d+(?:[-.]\d+)*)$/.test(row.label)
    ||typeof row.targets_truncated!=='boolean'||!Array.isArray(row.targets)||row.targets.length>8)return false;
   labels.add(row.label);const pages=new Set();
   for(const target of row.targets){
    const head=typeof target.caption==='string'?target.caption.split(/\r?\n/)[0].match(/^((?:附?图)\s*(?:[A-Z](?:[-.]\d+)*|\d+(?:[-.]\d+)*))(?:\s+|$)/):null;
    if(!Number.isInteger(target.pdf_page)||target.pdf_page<1||target.pdf_page>body.page_count||pages.has(target.pdf_page)
     ||!head||head[1].replace(/\s/g,'')!==row.label||target.caption.length>250||/[.…⋯·]{2,}/.test(target.caption))return false;
    pages.add(target.pdf_page);
   }
  }
  return true;
 }
 async function read(item,{isCurrent,fetcher=fetch,references=false}={}){
  if(typeof isCurrent!=='function'||!isCurrent())return null;
  const fixed=references?chunkIdentity(item):null;if(references&&!fixed)return null;
  const response=await fetcher('/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+'/book-pages'+(references?'?chunk_index='+fixed.index:''),{method:'GET',cache:'no-store'});
  const body=await globalThis.ArchSpecHTTP.json(response,'本书原页范围暂不可读取');if(!isCurrent())return null;
  if(!response.ok||!valid(body,item)||references&&!validReferences(body,item))throw Error('本书固定原页范围或提图位置无法核对，未切换版本。');return body;
 }
 function controls(doc,item,{isCurrent,onOpen}={}){
  if(typeof isCurrent!=='function'||typeof onOpen!=='function'||!chunkIdentity(item)||!/(?:附?图)\s*(?:[A-Z]|\d)/.test(item.text||''))return null;
  const panel=doc.createElement('div');panel.className='reader-figure-links';panel.hidden=true;
  const node=(tag,text)=>{const element=doc.createElement(tag);element.textContent=text;return element;};
  void (async()=>{
   try{
    const body=await read(item,{isCurrent,references:true});if(!body||!isCurrent())return;
    let count=0;
    for(const row of body.figure_references)for(const target of row.targets){
     if(count++>=8)continue;
     const button=node('button',row.label+' · 原页'+target.pdf_page);button.type='button';button.className='quiet-button';
     button.addEventListener('click',async()=>{
      if(!isCurrent()||button.disabled)return;button.disabled=true;
      try{
       // 点击时再次核当前许可与同一图题页；后台旧响应不能替新问题开图。
       const current=await read(item,{isCurrent,references:true});if(!current||!isCurrent())return;
       if(!current.figure_references.some(value=>value.label===row.label&&value.targets.some(page=>page.pdf_page===target.pdf_page&&page.caption===target.caption)))throw Error('附图位置已变化，请重新核对。');
       onOpen(target.pdf_page,current);
      }catch{if(isCurrent())note.textContent='附图当前无法打开，请重新核对原件。';}
      finally{if(isCurrent())button.disabled=false;}
     });panel.append(button);
    }
    const note=node('small',count?'图题定位；图示及续页仍须核对。':'附图尚未定位，请浏览原件核对。');panel.append(note);panel.hidden=body.figure_references.length===0;
   }catch{if(isCurrent()){panel.append(node('small','附图定位暂不可读取，请浏览原件核对。'));panel.hidden=false;}}
  })();return panel;
 }
 return {valid,read,validReferences,controls};
});
