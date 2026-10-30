/* 已保存公告供人工核对；独立于正文证据和维护核验，不据此声明现行或整本废止。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecNoticeChanges=api;})(globalThis,function(){
 function valid(body,item){
  const digest=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
  return body?.schema_version==='SAVED_NOTICE_CHANGES_V1'&&body.source_id===item.source_id&&body.snapshot_id===item.snapshot_id
   &&body.standard_code===item.standard_code&&digest(body.snapshot_fingerprint)&&(!item.snapshot_fingerprint||body.snapshot_fingerprint===item.snapshot_fingerprint)
   &&body.scope==='LOCAL_SAVED_METADATA_ONLY'&&body.version_status==='NOT_VERIFIED'
   &&Array.isArray(body.items)&&body.items.length<=10&&body.items.every(row=>digest(row.notice_metadata_id)&&digest(row.notice_sha256)
    &&/^(gb55031|official-[a-f0-9]{32})$/.test(row.observed_source_id||'')&&digest(row.observed_snapshot_id)
    &&typeof row.title==='string'&&row.title.length<=1000&&typeof row.url==='string'&&row.url.length<=8192
    &&typeof row.evidence==='string'&&row.evidence.length>0&&row.evidence.length<=16000
    &&typeof row.target_code==='string'&&row.target_code.length<=120&&['PARTIAL_CLAUSES','FULL_STANDARD'].includes(row.change_scope)
    &&typeof row.excerpt_truncated==='boolean');
 }
 function controls(doc,item,{isCurrent,onUnavailable}={}){
  const panel=doc.createElement('details');panel.className='reader-notice-changes';
  const summary=doc.createElement('summary');summary.textContent='已保存公告中的调整线索';
  const note=doc.createElement('p');note.className='reader-section-note';note.textContent='只核本机已存公告中提到当前规范完整编号的线索；不是全网最新变更查询。部分条款调整不等于整本废止，也不证明当前条文已废止。';
  const status=doc.createElement('p');status.className='reader-section-note';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const list=doc.createElement('div');list.className='reader-notice-list';
  let epoch=0;
  const current=()=>typeof isCurrent==='function'&&isCurrent();
  async function load(){
   if(!current())return;const own=++epoch,active=()=>own===epoch&&current();list.replaceChildren();status.textContent='正在核对已保存公告……';
   try{
    const response=await fetch('/api/official-documents/'+encodeURIComponent(item.source_id)+'/snapshots/'+item.snapshot_id+'/notice-changes',{method:'GET',cache:'no-store'});
    if(!active())return;if(!response.ok){onUnavailable?.();throw Error('当前固定出处的公告线索暂不可读取。');}
    const body=await globalThis.ArchSpecHTTP.json(response,'公告线索响应暂不可读取');if(!active())return;
    if(!valid(body,item))throw Error('公告线索与当前固定身份无法核对。');
    summary.textContent=body.items.length?'已保存公告中的调整线索（'+body.items.length+'）':'已保存公告中的调整线索';
    for(const row of body.items){
     const entry=doc.createElement('details');entry.className='reader-notice-entry';const title=doc.createElement('summary');
     title.textContent=(row.change_scope==='PARTIAL_CLAUSES'?'部分条款调整线索':'规范调整线索')+' · '+row.title;
     const info=doc.createElement('p');info.className='reader-section-note';info.textContent='涉及编号：'+row.target_code+'。以下是已保存公告摘录，不是规范正文或维护核验结论。'+(row.excerpt_truncated?' 摘录未保存完整，必须核原公告。':' 摘录可能不含全部上下文，请核原公告。');
     const evidence=doc.createElement('blockquote');evidence.className='reader-notice-text';evidence.textContent=row.evidence;
     entry.append(title,info,evidence);
     const safe=globalThis.ArchSpecSourceMetadata?.safeUrl(row.url);
     if(safe){const link=doc.createElement('a');link.className='secondary-button';link.textContent='对照原官方公告';link.href=safe;link.target='_blank';link.rel='noopener noreferrer';entry.append(link);}
     else{const url=doc.createElement('p');url.className='reader-section-note';url.textContent='归档网址（未作为可点击官方入口授信）：'+row.url;entry.append(url);}
     const identity=doc.createElement('p');identity.className='reader-section-note';identity.textContent='归档公告指纹：'+row.notice_sha256+'；对应来源档案：'+row.notice_metadata_id;entry.append(identity);list.append(entry);
    }
    status.textContent=(body.items.length?'找到 '+body.items.length+' 条已保存调整线索，请核公告范围与后续版本。':'本次已保存档案中没有匹配线索；不代表当前规范没有调整。')
     +(body.limited||body.skipped?' 资料扫描有上限或部分档案不可用，结果不完整。':'')+' 当前版本与工程适用性仍待核验。';
   }catch(error){if(active())status.textContent=error.message||'公告线索暂不可读取。';}
  }
  panel.addEventListener('toggle',()=>{if(panel.open)void load();else epoch++;});panel.append(summary,note,status,list);return panel;
 }
 return {valid,controls};
});
