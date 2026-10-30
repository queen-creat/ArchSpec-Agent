/* 复制当前已读取原文及固定出处；不联网、不改写原字，也不将摘录升级为工程结论。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecCitationCopy=api;})(globalThis,function(){
 function format(item,{profileLines=[],sourceUrl=null}={}){
  if(typeof item?.text!=='string'||typeof item.title!=='string'||!/^[a-f0-9]{64}$/.test(item.snapshot_id||'')
   ||!/^(gb55031|official-[a-f0-9]{32})$/.test(item.source_id||'')||!Number.isInteger(item.page_start)||!Number.isInteger(item.page_end)
   ||item.page_start<1||item.page_end<item.page_start)throw Error('当前条文身份不完整，不能复制出处。');
  const pages=item.page_start===item.page_end?String(item.page_start):item.page_start+'—'+item.page_end;
  const heading='《'+item.title+'》'+(item.standard_code?' '+item.standard_code:'（文号待核）')+'\n'+(item.clause_number?'条款 '+item.clause_number+' · ':'')+(item.source_format==='HTML'?'网页排版位置（非官方PDF页码） ':'文件页码 ')+pages;
  const lines=profileLines.filter(v=>typeof v==='string'&&v.trim()).map(v=>v.trim());
  // 网址仅接收共用来源档案模块已核过的官方来源；不从正文或用户输入猜链接。
  return heading+'\n\n'+item.text+'\n\n出处与核验状态\n'+(lines.length?lines.join('\n'):'来源档案待核')
   +'\n官方来源：'+(sourceUrl||'待核')+'\n固定来源：'+item.source_id+'\n快照：'+item.snapshot_id
   +'\n\n查阅摘录：识别文字、规范版本、现行性和工程适用性仍须核验；请对照原页、前提及例外，不能直接作为工程结论。';
 }
 function controls(doc,item,{profileLines,sourceUrl,isCurrent,clipboard}={}){
  const wrap=doc.createElement('div');wrap.className='reader-copy-controls';
  const button=doc.createElement('button');button.type='button';button.className='quiet-button reader-copy-button';button.textContent='复制条文与出处';
  const preview=doc.createElement('button');preview.type='button';preview.className='quiet-button reader-copy-button';preview.textContent='查看完整摘录';preview.setAttribute('aria-expanded','false');
  const status=doc.createElement('p');status.className='reader-copy-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const fallback=doc.createElement('label');fallback.className='reader-copy-fallback';fallback.hidden=true;
  const caption=doc.createElement('span');caption.textContent='条文与完整出处（可选择复制）';
  const area=doc.createElement('textarea');area.readOnly=true;area.rows=10;fallback.append(caption,area);
  preview.addEventListener('click',()=>{if(typeof isCurrent!=='function'||!isCurrent())return;try{area.value=format(item,{profileLines,sourceUrl});fallback.hidden=!fallback.hidden;preview.setAttribute('aria-expanded',String(!fallback.hidden));if(!fallback.hidden)area.focus();}catch(error){status.textContent=error.message;}});
  button.addEventListener('click',async()=>{
   if(typeof isCurrent!=='function'||!isCurrent())return;
   let text;try{text=format(item,{profileLines,sourceUrl});}catch(error){status.textContent=error.message;return;}
   button.disabled=true;status.textContent='';
   try{if(!clipboard?.writeText)throw Error();await clipboard.writeText(text);if(isCurrent()){status.textContent='已复制条文与完整出处。';}}
   catch{if(isCurrent()){area.value=text;fallback.hidden=false;preview.setAttribute('aria-expanded','true');status.textContent='自动复制不可用，请选中下方内容手动复制。';area.focus();area.select();}}
   finally{if(isCurrent())button.disabled=false;}
  });wrap.append(button,preview,status,fallback);return wrap;
 }
 return {format,controls};
});
