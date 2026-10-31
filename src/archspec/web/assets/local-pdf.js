/* 本机PDF只查选中文件；阅读位置绑定文档和原件SHA，不混用官方证据。 */
(function(){
 'use strict';
 function bind(doc){
  const panel=doc.querySelector('#local-pdf-library');
  if(!panel)return;
  const form=doc.querySelector('#local-pdf-upload'),list=doc.querySelector('#local-pdf-list');
  const status=doc.querySelector('#local-pdf-status'),uploadButton=doc.querySelector('#local-pdf-upload-button');
  const resume=doc.querySelector('#local-pdf-resume'),dialog=doc.querySelector('#local-pdf-dialog');
  const title=doc.querySelector('#local-pdf-title'),notice=doc.querySelector('#local-pdf-reader-status');
  const searchForm=doc.querySelector('#local-pdf-search'),query=doc.querySelector('#local-pdf-query');
  const results=doc.querySelector('#local-pdf-results'),fragment=doc.querySelector('#local-pdf-fragment');
  const original=doc.querySelector('#local-pdf-original'),pageInput=doc.querySelector('#local-pdf-page');
  const total=doc.querySelector('#local-pdf-page-total'),picture=doc.querySelector('#local-pdf-image');
  const previous=doc.querySelector('#local-pdf-previous'),next=doc.querySelector('#local-pdf-next');
  const zoomLabel=doc.querySelector('#local-pdf-zoom-label');
  const base='/api/documents/local-references',storageKey='archspec:local-pdf-reading:v1';
  let generation=0,listSerial=0,searchSerial=0,imageSerial=0,current=null,page=1,pageCount=0,zoom=1,fit=false;
  function valid(value){
   return value && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.id||'')
    && /^[a-f0-9]{64}$/.test(value.sha256||'') && Number.isInteger(value.page) && value.page>=1 && value.page<=50000;
  }
  function stored(){try{const value=JSON.parse(sessionStorage.getItem(storageKey)||'null');return valid(value)?value:null;}catch{return null;}}
  function save(){if(!current)return;try{sessionStorage.setItem(storageKey,JSON.stringify({id:current.id,sha256:current.sha256,page}));}catch{}resume.hidden=false;}
  function prefix(value){if(!valid({...value,page:1}))throw new Error('文件身份无法核对，请重新选择');return base+'/'+value.id+'/'+value.sha256;}
  async function request(url,options){
   const response=await fetch(url,{cache:'no-store',...options});
   let body;try{body=await response.json();}catch{throw new Error('本机接口暂不可读取');}
   if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'文件暂不可读取，请重新选择');
   return body;
  }
  function node(tag,text,className){const item=doc.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
  async function loadList(){
   const serial=++listSerial;
   try{
    const rows=await request(base);
    if(serial!==listSerial)return;
    list.replaceChildren();
    if(!Array.isArray(rows))throw new Error('文件清单格式无法读取');
    for(const item of rows){
     if(!valid({...item,page:1}))continue;
     const row=node('div',undefined,'local-pdf-file');
     const button=node('button',item.original_name,'quiet-button');button.type='button';
     button.addEventListener('click',()=>openReader(item,1));
     row.append(button,node('small',item.has_text?'可查已提取文字，也可翻原页':'尚无可检索文字，可翻原页'));
     list.append(row);
    }
    if(!list.children.length)list.append(node('p','还没有本机PDF。','empty-state'));
   }catch(error){if(serial===listSerial)status.textContent=error.message;}
  }
  function closeReader(){
   generation++;searchSerial++;imageSerial++;current=null;picture.replaceChildren();
   if(dialog.open)dialog.close();
  }
  function imagePage(wanted){
   if(!current||!Number.isInteger(wanted)||wanted<1||wanted>pageCount)return;
   page=wanted;pageInput.value=String(page);previous.disabled=page===1;next.disabled=page===pageCount;
   const token=generation,serial=++imageSerial,owner=current;
   const image=node('img');image.alt=owner.original_name+'，PDF第'+page+'页';
   image.style.width=(zoom*100)+'%';notice.textContent='正在读取原页…';
   image.onload=()=>{if(token===generation&&serial===imageSerial){sizeImage();notice.textContent='本机文件原页；来源、版本和适用性请自行核对。';save();}};
   image.onerror=()=>{if(token===generation&&serial===imageSerial){image.hidden=true;notice.textContent='原页暂不可读取，请重新选择文件核对身份或下载原件。';}};
   image.src=prefix(owner)+'/pages/'+page+'.png';picture.replaceChildren(image);
  }
  async function openReader(item,wanted){
   const token=++generation;searchSerial++;imageSerial++;current=null;results.replaceChildren();fragment.hidden=true;query.value='';
   title.textContent=item.original_name||'本机PDF';notice.textContent='正在核对文件…';picture.replaceChildren();
   original.removeAttribute('href');pageInput.disabled=true;previous.disabled=next.disabled=true;
   if(!dialog.open)dialog.showModal();
   try{
    const info=await request(prefix(item));
    if(token!==generation||!dialog.open)return;
    if(!Number.isInteger(info.page_count)||wanted<1||wanted>info.page_count)throw new Error('保存的页码不在该PDF中，请从文件清单重新打开');
    current=info;pageCount=info.page_count;zoom=1;fit=false;zoomLabel.textContent='100%';
    title.textContent=info.original_name;total.textContent='/ '+pageCount+' 页';pageInput.max=String(pageCount);pageInput.disabled=false;
    original.href=prefix(info)+'/original';imagePage(wanted);
   }catch(error){if(token===generation)notice.textContent=error.message;}
  }
  form.addEventListener('submit',async event=>{
   event.preventDefault();
   const file=doc.querySelector('#local-pdf-file').files?.[0];if(!file)return;
   const token=generation;uploadButton.disabled=true;status.textContent='正在导入本机PDF…';
   try{
    const body=new FormData();body.append('file',file);
    const item=await request(base,{method:'POST',body});
    status.textContent=item.has_text?'已导入，可在此文件内查找。':'已保存原件，暂无可检索文字，可直接翻原页。';
    form.reset();await loadList();
    if(token===generation&&doc.body.dataset.workbenchView==='library'&&panel.open)await openReader(item,1);
   }catch(error){status.textContent=error.message;}
   finally{uploadButton.disabled=false;}
  });
  searchForm.addEventListener('submit',async event=>{
   event.preventDefault();if(!current)return;
   const value=query.value.trim();if(!value)return;
   const owner=current,token=generation,serial=++searchSerial;
   notice.textContent='正在查找文件内已提取的文字…';results.replaceChildren();fragment.hidden=true;
   try{
    const body=await request(prefix(owner)+'/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:value,top_k:8})});
    if(token!==generation||serial!==searchSerial||current!==owner)return;
    for(const item of Array.isArray(body.results)?body.results:[]){
     const button=node('button',undefined,'local-pdf-hit');button.type='button';
     button.append(node('strong',(item.clause_number?'条款 '+item.clause_number:'文件片段')+' · 文件第 '+(item.page_start||'?')+' 页'),
      node('span',typeof item.text==='string'?item.text.slice(0,120):''));
     button.addEventListener('click',()=>{
      if(token!==generation||serial!==searchSerial||current!==owner)return;
      fragment.textContent=item.text;fragment.hidden=false;
      if(Number.isInteger(item.page_start))imagePage(item.page_start);
     });results.append(button);
    }
    notice.textContent=results.children.length?'选择片段，核对对应原页；片段不是设计结论。':'没有找到文字片段；扫描页可直接翻原页。';
   }catch(error){if(token===generation&&serial===searchSerial)notice.textContent=error.message;}
  });
  function sizeImage(){
   const image=picture.querySelector('img');if(!image?.naturalWidth)return;
   // 手机默认保留原页像素，横向滚动阅读；适宽由使用者主动选择。
   const width=fit?picture.clientWidth:Math.max(picture.clientWidth,Math.min(900,image.naturalWidth));
   image.style.width=Math.round(width*zoom)+'px';
  }
  function scale(amount){
   zoom=Math.min(3,Math.max(1,amount));zoomLabel.textContent=Math.round(zoom*100)+'%';sizeImage();
  }
  doc.defaultView?.addEventListener('resize',sizeImage);
  previous.addEventListener('click',()=>imagePage(page-1));next.addEventListener('click',()=>imagePage(page+1));
  pageInput.addEventListener('change',()=>{const wanted=Number(pageInput.value);if(Number.isInteger(wanted)&&wanted>=1&&wanted<=pageCount)imagePage(wanted);else pageInput.value=String(page);});
  doc.querySelector('#local-pdf-zoom-out').addEventListener('click',()=>scale(zoom-.5));
  doc.querySelector('#local-pdf-zoom-in').addEventListener('click',()=>scale(zoom+.5));
  doc.querySelector('#local-pdf-fit').addEventListener('click',()=>{fit=true;scale(1);});
  doc.querySelector('#local-pdf-close').addEventListener('click',closeReader);
  dialog.addEventListener('cancel',event=>{event.preventDefault();closeReader();});
  panel.addEventListener('toggle',()=>{if(panel.open)loadList();});
  doc.addEventListener('archspec:workbench-view',event=>{if(event.detail?.view==='library'&&panel.open)loadList();});
  resume.hidden=!stored();resume.addEventListener('click',()=>{const saved=stored();if(saved)openReader(saved,saved.page);});
 }
 if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>bind(document));else bind(document);}
})();
