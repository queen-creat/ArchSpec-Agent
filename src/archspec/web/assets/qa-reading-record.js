/* 整次查阅只生成本地文本记录；复用固定片段GET、来源档案与原字格式，不重放问答。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecQARecord=api;})(globalThis,function(){
 const digest=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
 function format(packet,items,{metadata,citationCopy,checkedAt}={}){
  if(!citationCopy?.format)throw Error('原文记录组件尚未就绪。');
  const history=packet.kind==='TASK_POSITIONS';
  const lines=[history?'ArchSpec 历史出处核读记录':'ArchSpec 本次查阅记录','生成时间：'+checkedAt,'问题：'+packet.query,
   '项目：'+(packet.projectLabel||'未选项目'),'项目标识：'+(packet.projectId||'无'),
   '检索范围：'+packet.scopeLabel,'范围标识：'+packet.scope,'',
   ...(history?['历史任务：'+packet.task.id,'操作版本：'+packet.task.revision,'']:[]),
   history?'本次核读状态':'本次整理状态',packet.summary,packet.modeLabel,'',
   history?'工程条件恢复范围':'提交时的手填条件',...(packet.conditions.length?packet.conditions:['未提交手填条件；问题中的说明见原问题。']),
   '', history?'项目条件恢复范围':'本次采用的项目条件（不是规范依据）',...(packet.provenance.length?packet.provenance:['无']),
   '', '仍需补充或核对',...packet.questions,...packet.requirements,...packet.gaps,
   '', '固定出处与完整识别原文（'+items.length+'段）'];
  if(!items.length)lines.push('本次没有可保存的正文出处，不能据此给出规范数值或结论。');
  items.forEach((item,index)=>{
   const evidence=packet.evidence[index];
   // 只投影共用模块已经核对的来源字段，不另推版本、地域或现行状态。
   const profile=metadata?.details?.(item);
   const profileLines=profile?Array.from(profile.children).map(row=>row.textContent):[];
   const sourceUrl=metadata?.safeUrl?.(item.source_url,item)||null;
   const role=history?({MATCH:'上次命中位置，本次已核回',CONTINUATION:'上次续文位置，本次已核回',RELATED_CONTEXT:'上次相关位置，本次仅供阅读'}[evidence.role]||'历史固定位置，本次仅供阅读'):evidence.quote_ready===false?'仅供阅读，未具备直接引用条件'
    :({MATCH:'本次命中片段',HIT:'本次命中片段',CONTINUATION:'本次同号续文',RELATED_CONTEXT:'相关片段，不作直接答案'}[evidence.role]||'本次阅读片段');
   lines.push('', '── 出处 '+(index+1)+' · '+role+' ──',citationCopy.format(item,{profileLines,sourceUrl}),
    '固定片段：'+item.id,'正文记录指纹：'+item.snapshot_fingerprint,
    '识别标记：'+(Array.isArray(item.warnings)&&item.warnings.length?item.warnings.join('、'):'无已登记标记；不代表识别无误'));
  });
  lines.push('', '本记录保存生成时的查阅状态，不是工程审查结论。规范版本、现行性、地域适用性、原表行列、前提和例外仍须核对。',
   history?'仅包含上次保存位置中本次已核回的片段；未据此重新评估证据是否齐备或工程适用性。':'仅包含本轮已纳入的片段，不代表整本规范、所有前提或全部相关规范已读齐；请保留固定出处并对照原页。');
  return lines.join('\n');
 }
 function controls(doc,packet,{isCurrent,metadata,citationCopy}={}){
  const node=(tag,text='')=>{const element=doc.createElement(tag);element.textContent=text;return element;};
  const history=packet.kind==='TASK_POSITIONS',prefix=history?'qa-history-record':'qa-record';
  const panel=node('details');panel.className='official-qa-context qa-reading-record';
  const summary=node('summary',history?'核回并保存这些出处':'保存本次查阅记录');
  const note=node('p',history?'按上次保存的位置重新检查原文与权限，再保存完整摘录。上次回答未重放，工程条件未重新确认。':'保存提交时的条件、整理状态、完整原文与出处。先核对固定原文，再下载文本；不重新问答或调用模型。');note.className='reader-section-note';
  const bar=node('div');bar.className='qa-history-toolbar';
  const build=node('button','核对出处并生成'),download=node('button','下载 TXT');
  for(const [button,id] of [[build,prefix+'-build'],[download,prefix+'-download']]){button.type='button';button.className='quiet-button reader-copy-button';button.setAttribute('id',id);}
  download.disabled=true;bar.append(build,download);
  const status=node('p');status.className='reader-section-note';status.setAttribute('id',prefix+'-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const preview=node('label');preview.className='reader-copy-fallback';preview.hidden=true;
  const area=node('textarea');area.readOnly=true;area.rows=12;area.setAttribute('id',prefix+'-text');
  preview.append(node('span',history?'历史出处核读记录（可选择复制或下载）':'本次查阅记录（可选择复制或下载）'),area);panel.append(summary,note,bar,status,preview);
  let content='',loading=false;
  const current=()=>typeof isCurrent==='function'&&isCurrent();
  build.addEventListener('click',async()=>{
   if(!current()||loading)return;loading=true;build.disabled=true;download.disabled=true;content='';area.value='';preview.hidden=true;
   status.textContent='正在核对本次固定出处……';
   try{
    if(!Array.isArray(packet.evidence)||packet.evidence.length>8)throw Error('本次出处范围无法核对，未生成记录。');
    const items=[];
    // 保持原顺序和8片段上限；任何当前读取不通过都不导出一份缺出处的成功记录。
    for(const evidence of packet.evidence){
     if(!/^(gb55031|official-[a-f0-9]{32})$/.test(evidence.source_id||'')||!digest(evidence.snapshot_id)
       ||!digest(evidence.snapshot_fingerprint)||typeof evidence.chunk_id!=='string')throw Error('固定出处身份不完整，未生成记录。');
     const prefix=evidence.source_id+':'+evidence.snapshot_id+':',index=evidence.chunk_id.slice(prefix.length);
     if(!evidence.chunk_id.startsWith(prefix)||!/^(0|[1-9]\d{0,5})$/.test(index))throw Error('固定片段身份无法核对。');
     const response=await fetch('/api/official-documents/'+encodeURIComponent(evidence.source_id)+'/snapshots/'+evidence.snapshot_id+'/chunks/'+index,{method:'GET',cache:'no-store'});
     if(!current())return;if(!response.ok)throw Error('固定原文当前不可读取，未沿用旧原文生成记录。');
     const item=await globalThis.ArchSpecHTTP.json(response,'固定原文暂不可读取');if(!current())return;
     // 历史收据只保存固定身份，不保存旧文字；只允许本组件取得的当前原文进入核读记录。
     const fields=['source_id','snapshot_id','snapshot_fingerprint','title','standard_code','clause_number','page_start','page_end',...(history?[]:['text'])];
     if(!item||item.id!==evidence.chunk_id||typeof item.text!=='string'||!fields.every(key=>item[key]===evidence[key]))
      throw Error('当前固定原文与本轮出处无法逐项核对，未替换版本。');
     items.push(item);
    }
    if(!current())return;content=format(packet,items,{metadata,citationCopy,checkedAt:new Date().toISOString()});
    area.value=content;preview.hidden=false;download.disabled=false;
    status.textContent='已核对 '+items.length+' 段固定原文。记录已生成，可下载或复制。';
   }catch(error){if(current())status.textContent=error.message||'查阅记录暂不可生成。';}
   finally{loading=false;if(current())build.disabled=false;}
  });
  download.addEventListener('click',()=>{
   if(!current()||loading||!content)return;
   try{
    const url=URL.createObjectURL(new Blob([content],{type:'text/plain;charset=utf-8'}));
    const link=node('a');link.href=url;link.download=history?'ArchSpec-历史出处核读记录.txt':'ArchSpec-本次查阅记录.txt';link.hidden=true;panel.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='已生成本地文本下载，下方完整记录仍可复制。';
   }catch{status.textContent='自动下载不可用，请选择下方完整记录手动保存。';}
  });
  return panel;
 }
 return {format,controls};
});
