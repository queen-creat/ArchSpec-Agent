/* 固定书目的条号索引与已有全文查找；预览只供定位，打开仍重读原固定片段。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecBookIndex=api;})(globalThis,function(){
 const position=value=>Number.isSafeInteger(value)&&value>=0&&value<=1000000;
 function previewBound(row){return ['preview_start','preview_end','match_start','match_end'].every(key=>position(row[key]))
  &&row.preview_start<=row.match_start&&row.match_start<row.preview_end&&row.match_start<row.match_end
  &&row.preview_end-row.preview_start===Array.from(row.preview).length;}
 function matchesPreview(selected,row){const text=Array.from(selected.text||'');return previewBound(row)&&row.preview_end<=text.length&&row.match_end<=text.length
  &&text.slice(row.preview_start,row.preview_end).join('')===row.preview;}
 function valid(body,item,mode='CLAUSE'){const full=mode==='TEXT';return body?.version===(full?'FIXED_BOOK_FIND_V1':'FIXED_BOOK_INDEX_V1')&&body.source_id===item.source_id&&body.snapshot_id===item.snapshot_id
  &&body.title===item.title&&body.standard_code===item.standard_code&&/^[a-f0-9]{64}$/.test(body.snapshot_fingerprint||'')
  &&(!item.snapshot_fingerprint||body.snapshot_fingerprint===item.snapshot_fingerprint)
  &&(!full||body.navigation_only===true&&body.match_mode==='IGNORES_WHITESPACE_CASEFOLD')
  &&Number.isSafeInteger(body.total)&&body.total>=0&&body.total<=1000000&&Number.isSafeInteger(body.offset)&&body.offset>=0
  &&(body.next_offset===null||Number.isSafeInteger(body.next_offset)&&body.next_offset>body.offset)
  &&Array.isArray(body.items)&&body.items.length<=50&&body.items.every(row=>Number.isSafeInteger(row.chunk_index)&&row.chunk_index>=0&&row.chunk_index<=999999
   &&(full&&row.clause_number===null||typeof row.clause_number==='string'&&row.clause_number.length>0&&row.clause_number.length<=120)
   &&typeof row.preview==='string'&&Array.from(row.preview).length<=200&&(!full||previewBound(row))
   &&Number.isInteger(row.page_start)&&Number.isInteger(row.page_end)&&row.page_start>=1&&row.page_end>=row.page_start&&row.page_end<=200)
  &&new Set(body.items.map(row=>row.chunk_index)).size===body.items.length;}
 function controls(doc,item,{isCurrent,onOpen,onUnavailable,summaryText='本书条款索引与全文查找'}={}){
  const node=(tag,text)=>{const n=doc.createElement(tag);n.textContent=text;return n;};
  const panel=doc.createElement('details');panel.className='reader-book-index';
  const summary=node('summary',summaryText),note=node('p','');note.className='reader-section-note';
  const form=doc.createElement('form');form.className='book-index-filter';
  const methodLabel=node('label','查找方式'),method=doc.createElement('select');method.setAttribute('aria-label','查找方式');
  for(const [value,label] of [['CLAUSE','条款索引'],['TEXT','全文查找']]){const option=node('option',label);option.value=value;method.append(option);}method.value='CLAUSE';methodLabel.append(method);
  const label=doc.createElement('label'),caption=node('span','筛选条号或条首文字');
  const input=doc.createElement('input');input.type='search';input.maxLength=80;input.placeholder='例如：3.2 或建筑高度';label.append(caption,input);
  const submit=node('button','筛选索引');submit.type='submit';submit.className='secondary-button';form.append(methodLabel,label,submit);
  const status=node('p','');status.className='reader-section-note';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const list=doc.createElement('div');list.className='book-index-list';
  const pages=doc.createElement('div');pages.className='book-index-pages';
  const previous=node('button','上一页'),next=node('button','下一页');for(const button of [previous,next]){button.type='button';button.className='quiet-button';}pages.append(previous,next);
  let epoch=0,offset=0,nextOffset=null,loading=false;
  const current=()=>typeof isCurrent==='function'&&isCurrent();
  const paint=()=>{submit.disabled=loading;previous.disabled=loading||offset===0;next.disabled=loading||nextOffset===null;};
  function explain(){const full=method.value==='TEXT';caption.textContent=full?'在本书已有全文中查找':'筛选条号或条首文字';submit.textContent=full?'查找全文':'筛选索引';
   note.textContent=full?'查当前快照已取得的文字，忽略空白和大小写；每片段显示首处命中。图片及未识别文字仍须核原页，不代表原版已完整识别。':'仅列当前快照已识别条号，不是原版完整目录；预览用于定位，识别与适用性待核。';}
  function check(response){if(!response.ok){if(current())onUnavailable?.();throw Error('本书固定出处当前无法读取，未切换版本。');}}
  async function load(page=0){
   if(!current())return;const own=++epoch,q=input.value.trim(),mode=method.value,full=mode==='TEXT';list.replaceChildren();
   if(full&&!q){loading=false;nextOffset=null;status.textContent='请输入要在本书查找的文字，再点击“查找全文”。';paint();return;}
   loading=true;paint();status.textContent=full?'正在查找本书已有全文……':'正在读取本书索引……';
   const active=()=>own===epoch&&current()&&panel.open&&q===input.value.trim()&&mode===method.value;
   try{const params=new URLSearchParams({limit:'50',offset:String(page),query:q});
    const response=await fetch('/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+(full?'/find?':'/index?')+params,{method:'GET',cache:'no-store'});
    if(!active())return;check(response);const body=await globalThis.ArchSpecHTTP.json(response,'本书查找响应暂不可读取');if(!active())return;
    if(!valid(body,item,mode)||body.offset!==page||body.query!==q)throw Error('本书查找身份无法核对，未展示其它版本。');
    offset=page;nextOffset=body.next_offset;
    for(const row of body.items){const open=doc.createElement('button');open.type='button';open.className='book-index-entry';
     const title=node('strong',(row.clause_number||'未识别条号')+' · '+(body.source_format==='HTML'?'网页排版位置 ':'文件第 ')+row.page_start+(row.page_end!==row.page_start?'—'+row.page_end:'')+' 页');
     const preview=node('small','');preview.className='book-index-preview';
     if(full){const chars=Array.from(row.preview),start=row.match_start-row.preview_start,end=Math.min(row.match_end,row.preview_end)-row.preview_start;
      preview.append(node('span',chars.slice(0,start).join('')),node('mark',chars.slice(start,end).join('')),node('span',chars.slice(end).join('')));
     }else preview.textContent=row.preview;
     open.append(title,preview);
     open.addEventListener('click',async()=>{if(!active())return;open.disabled=true;status.textContent='正在核对所选固定片段……';
      try{const response=await fetch('/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+'/chunks/'+row.chunk_index,{method:'GET',cache:'no-store'});
       if(!active())return;check(response);const selected=await globalThis.ArchSpecHTTP.json(response,'固定片段暂不可读取');if(!active())return;
       if(selected?.id!==item.source_id+':'+item.snapshot_id+':'+row.chunk_index||selected.source_id!==item.source_id||selected.snapshot_id!==item.snapshot_id
        ||selected.snapshot_fingerprint!==body.snapshot_fingerprint||selected.clause_number!==row.clause_number||selected.page_start!==row.page_start||selected.page_end!==row.page_end
        ||full&&!matchesPreview(selected,row))throw Error('所选片段身份或定位预览无法核对，未改用其它条文。');
       if(!onOpen(selected,row.page_start)&&active())throw Error('所选片段暂不能打开。');
      }catch(error){if(active())status.textContent=error.message||'固定片段暂不可读取。';}finally{if(active())open.disabled=false;}});
     list.append(open);
    }
    status.textContent=body.total?'共 '+body.total+' 个'+(full?'命中片段':'已识别片段位置')+' · 本页 '+(page+1)+'—'+(page+body.items.length)+(full?'；仅查已取得文字。':'；同号续文可能分列。')
     :full?'没有匹配的已有文字；图片或未识别内容请核原页，不代表没有相关要求。':'没有匹配的已识别条号；不代表原书没有相关要求。';
   }catch(error){if(active())status.textContent=error.message||'本书查找暂不可读取。';}finally{if(active()){loading=false;paint();}}
  }
  function edited(){epoch++;loading=false;offset=0;nextOffset=null;list.replaceChildren();explain();status.textContent='查找条件已修改，请点击“'+submit.textContent+'”。';paint();}
  panel.addEventListener('toggle',()=>{if(panel.open)void load();else{epoch++;loading=false;paint();}});form.addEventListener('submit',event=>{event.preventDefault();void load();});
  input.addEventListener('input',edited);method.addEventListener('change',edited);
  previous.addEventListener('click',()=>void load(Math.max(0,offset-50)));next.addEventListener('click',()=>{if(nextOffset!==null)void load(nextOffset);});
  panel.append(summary,note,form,status,list,pages);explain();paint();return panel;
 }
 return {valid,matchesPreview,controls};
});
