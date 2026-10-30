/* 只导航当前固定原文里实际出现的公共编号；缺年份不代选版本，不将提及当成依赖证明。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecStandardMentions=api;})(globalThis,function(){
 const digest=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
 function valid(body,item){const text=Array.from(item.text||''),seen=new Set();
  return body?.version==='FIXED_STANDARD_MENTIONS_V1'&&body.source_id===item.source_id&&body.snapshot_id===item.snapshot_id
   &&body.selected_chunk_id===item.id&&digest(body.snapshot_fingerprint)&&(!item.snapshot_fingerprint||body.snapshot_fingerprint===item.snapshot_fingerprint)
   &&body.relation_mode==='BIBLIOGRAPHIC_MENTIONS_ONLY'&&typeof body.truncated==='boolean'&&typeof body.catalog_truncated==='boolean'
   &&Array.isArray(body.mentions)&&body.mentions.length<=6&&body.mentions.every(row=>{
    if(!row||typeof row.code!=='string'||!/^(?:GB|JGJ|CJJ)(?:\/T)? \d{2,6}(?:\.\d{1,2})?(?:-\d{4})?$/.test(row.code)||seen.has(row.code)
     ||typeof row.original!=='string'||row.original.length>100||!Number.isInteger(row.start)||!Number.isInteger(row.end)||row.start<0||row.end<=row.start||row.end>text.length
     ||text.slice(row.start,row.end).join('')!==row.original||row.original.toUpperCase().replace(/\s+/g,'').replace(/([A-Z])(?=\d)/,'$1 ')!==row.code
     ||!['EXPLICIT_IN_TEXT','NOT_SPECIFIED'].includes(row.edition_state)||typeof row.targets_truncated!=='boolean'
     ||!Array.isArray(row.targets)||row.targets.length>5)return false;
    seen.add(row.code);return row.targets.every(target=>target&&/^(gb55031|official-[a-f0-9]{32})$/.test(target.source_id||'')
     &&typeof target.title==='string'&&!!target.title&&target.title.length<=300&&typeof target.standard_code==='string'&&target.standard_code.length<=120
     &&target.kind==='FULL_TEXT'&&typeof target.acquired==='boolean'&&(target.acquired?digest(target.snapshot_id):target.snapshot_id===null)
     &&(target.cache_error===null||typeof target.cache_error==='string'&&target.cache_error.length<=1000));
   });
 }
 function controls(doc,item,{isCurrent,onOpen,onUnavailable}={}){
  const panel=doc.createElement('details');panel.className='reader-standard-mentions';
  const node=(tag,text,cls)=>{const n=doc.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const summary=node('summary','本条提到的其他规范'),note=node('p','只列原文里的编号。缺年份时请核对版本；提及不代表已确认适用或效力关系。','reader-section-note');
  const status=node('p','','reader-section-note');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const list=node('div','','reader-standard-mention-list'),retry=node('button','重新核对规范提及','quiet-button');retry.type='button';
  let epoch=0;const current=()=>typeof isCurrent==='function'&&isCurrent();
  async function load(){if(!current())return;const own=++epoch,active=()=>own===epoch&&current();list.replaceChildren();retry.disabled=true;status.textContent='正在核对固定条文中的规范编号……';
   try{const response=await fetch('/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+'/chunks/'+item.id.split(':').at(-1)+'/standard-mentions',{method:'GET',cache:'no-store'});
    if(!active())return;if(!response.ok){onUnavailable?.();throw Error('本条固定出处当前无法读取。');}
    const body=await globalThis.ArchSpecHTTP.json(response,'规范提及响应暂不可读取');if(!active())return;
    if(!valid(body,item))throw Error('规范编号与当前固定原文无法核对，未打开其它书目。');
    for(const mention of body.mentions){const entry=node('section','','reader-standard-mention');
     entry.append(node('strong',mention.code),node('p',mention.edition_state==='NOT_SPECIFIED'?'原文未写年份，以下仅为登记版本候选。':'原文明确写出此编号版本；现行及适用性仍待核。','reader-section-note'));
     if(!mention.targets.length)entry.append(node('p','当前登记资料未找到对应正文；未联网或获取新资料。','reader-section-note'));
     for(const target of mention.targets){const card=node('div','','reader-mentioned-book');card.append(node('p','《'+target.title+'》 '+target.standard_code));
      if(!target.acquired||target.cache_error){card.append(node('p','这份登记正文尚未准备好，暂不能打开索引。','reader-section-note'));entry.append(card);continue;}
      const index=globalThis.ArchSpecBookIndex?.controls(doc,target,{isCurrent:active,onOpen,summaryText:'查看《'+target.title+'》条款索引',
       onUnavailable:()=>{if(active())index.replaceChildren(node('summary','这份目标正文当前不可读取'),node('p','原条文仍保留；请重新核对后再打开目标书目。','source-warning'));}});
      if(index)card.append(index);entry.append(card);
     }
     if(mention.targets_truncated)entry.append(node('p','候选超过本次5份上限，未全部列出。','reader-section-note'));list.append(entry);
    }
    status.textContent=body.mentions.length?'本片段提到 '+body.mentions.length+' 个其他规范编号；点击目标书目索引后才读取该书。':'本片段未识别到其他公共规范编号；不代表没有跨规范要求。';
    if(body.truncated||body.catalog_truncated)status.textContent+=' 本次读取达到展示或登记数量上限，结果未穷尽。';
   }catch(error){if(active())status.textContent=error.message||'规范提及暂不可读取。';}finally{if(active())retry.disabled=false;}
  }
  panel.addEventListener('toggle',()=>{if(panel.open)void load();else{epoch++;retry.disabled=false;}});retry.addEventListener('click',()=>void load());
  panel.append(summary,note,status,list,retry);return panel;
 }
 return {valid,controls};
});
