/* 同一来源范围合同供缓存、问答、问题清单和固定阅读恢复使用；不以选中状态证明许可。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else{root.ArchSpecSourceScope=api;if(root.document)api.bind(root.document);}})(globalThis,function(){
 const source=v=>typeof v==='string'&&/^(gb55031|official-[a-f0-9]{32})$/.test(v);let bound;
 function encode(ids){if(ids===null||ids===undefined)return '__all__';
  if(!Array.isArray(ids)||!ids.length||ids.length>20||!ids.every(source)||new Set(ids).size!==ids.length)throw Error('请选择1—20份不同的已登记正文。');
  const values=[...ids].sort();return values.length===1?values[0]:'set:'+values.join('|');
 }
 function decode(value){if(value==='__all__')return null;if(source(value))return [value];
  if(typeof value!=='string'||!value.startsWith('set:')||value.length>1000)return undefined;
  const ids=value.slice(4).split('|');try{return ids.length>=2&&encode(ids)===value?ids:undefined;}catch{return undefined;}
 }
 function options(select){return [...(select?.options||select?.children||[])];}
 function ensure(doc,value){const select=doc.querySelector('#pending-source'),ids=decode(value);if(!select||ids===undefined)return false;
  if(options(select).some(option=>option.value===value))return true;
  if(!ids||ids.length<2||ids.some(id=>!options(select).some(option=>option.value===id)))return false;
  const option=doc.createElement('option');option.value=value;
  const titles=ids.slice(0,2).map(id=>options(select).find(row=>row.value===id).textContent.trim().slice(0,35));
  option.textContent='自选'+ids.length+'本 · '+titles.join('、')+(ids.length>2?'等':'');select.append(option);return true;
 }
 function choose(doc,value){if(!ensure(doc,value))return false;const select=doc.querySelector('#pending-source');select.value=value;if(select.value!==value)return false;if(bound?.doc===doc)bound.sync();return true;}
 function bind(doc){if(bound?.doc===doc)return bound;
  const panel=doc.querySelector('#multi-source-scope'),list=doc.querySelector('#multi-source-list'),status=doc.querySelector('#multi-source-status'),apply=doc.querySelector('#multi-source-apply'),select=doc.querySelector('#pending-source');
  if(!panel||!list||!select)return null;
  let sources=[],draft=new Set();
  const node=(tag,text)=>{const e=doc.createElement(tag);e.textContent=text;return e;};
  function render(){list.replaceChildren();const current=decode(select.value),selectedCount=current?.length||0;
   doc.querySelector('#multi-source-summary').textContent=selectedCount>1?'自选规范（当前'+selectedCount+'本）':'自选多本规范';
   const currentLine=doc.querySelector('#multi-source-current');if(currentLine)currentLine.textContent=current===undefined?'当前范围待核':current===null?'当前范围：全部已准备正文':'当前生效：'+current.map(id=>options(select).find(option=>option.value===id)?.textContent||id).join('；');
   for(const row of sources){const label=node('label',''),box=doc.createElement('input');box.type='checkbox';box.checked=draft.has(row.source_id);
    const duplicate=sources.filter(other=>other.title===row.title&&other.standard_code===row.standard_code).length>1;box.value=row.source_id;
    box.setAttribute('aria-label','选入范围：'+row.title+' '+row.standard_code+(duplicate?' · 来源 '+row.source_id:''));
    const title=node('span',row.title+' · '+(row.standard_code||'文号待核'));if(duplicate)title.append(node('small','同名来源标识：'+row.source_id));label.append(box,title);
    box.addEventListener('change',()=>{if(box.checked){if(draft.size>=20){box.checked=false;status.textContent='最多选择20份正文。';return;}draft.add(row.source_id);}else draft.delete(row.source_id);
     apply.disabled=!draft.size;status.textContent='已勾选'+draft.size+'本，点击“使用所选规范”后生效。';});list.append(label);
   }
   apply.disabled=!draft.size||[...draft].some(id=>!sources.some(row=>row.source_id===id));
   if(!sources.length)list.append(node('p','正在等待已准备正文清单；清单不可用时不会自动扩大范围。'));
  }
  function sync(){draft=new Set(decode(select.value)||[]);const online=doc.querySelector('#pending-online-enabled');if(online)online.disabled=select.value!=='__all__';render();}
  apply?.addEventListener('click',()=>{try{if(!draft.size||[...draft].some(id=>!sources.some(row=>row.source_id===id)))throw Error('所选正文清单已变化，请重新核对。');
   const value=encode([...draft]);if(!choose(doc,value))throw Error('所选范围当前无法核对，未改为全部正文。');
   select.dispatchEvent(new Event('change',{bubbles:true}));status.textContent='已限定'+draft.size+'本正文，只查缓存；未自动查询。当前许可、版本与适用性仍需核对。';
  }catch(error){status.textContent=error.message;}});
  doc.querySelector('#multi-source-all')?.addEventListener('click',()=>{if(!choose(doc,'__all__'))return;select.dispatchEvent(new Event('change',{bubbles:true}));status.textContent='已切回全部已准备正文，未自动查询。';});
  select.addEventListener('change',sync);
  globalThis.ArchSpecOfficialInventory?.subscribe(state=>{if(state.status==='READY'){sources=state.sources.filter(row=>source(row.source_id)&&globalThis.ArchSpecOfficialInventory.prepared(row));sync();}else{sources=[];render();}});
  bound={doc,sync};sync();return bound;
 }
 return {source,encode,decode,ensure,choose,bind};
});
