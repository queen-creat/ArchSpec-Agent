/* 本标签页内存中的有限阅读路径；不保存正文、问题或项目条件，刷新仍走原固定位置恢复。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecReadingTrail=api;})(globalThis,function(){
 const bounds=globalThis.ArchSpecPageBounds||(typeof require==='function'?require('./page-bounds.js'):null);
 function position(value){
  if(!value||!/^(gb55031|official-[a-f0-9]{32})$/.test(value.source_id||'')||!/^[a-f0-9]{64}$/.test(value.snapshot_id||''))return null;
  const prefix=value.source_id+':'+value.snapshot_id+':';
  if(typeof value.id!=='string'||!value.id.startsWith(prefix)||!/^(0|[1-9]\d{0,5})$/.test(value.id.slice(prefix.length))
   ||!Number.isInteger(value.page)||value.page<1||value.page>(bounds?bounds.limit(value):200)||!['TEXT','OUTLINE','OTHER','ORIGINAL'].includes(value.view))return null;
  return Object.freeze({source_id:value.source_id,snapshot_id:value.snapshot_id,id:value.id,page:value.page,view:value.view,...(value.page_count!==undefined?{page_count:value.page_count}:{}),
   ...(value.bookPage===true&&value.view==='ORIGINAL'?{bookPage:true}:{}),
   title:typeof value.title==='string'?value.title.slice(0,300):'',clause:typeof value.clause==='string'?value.clause.slice(0,120):'',
   fingerprint:/^[a-f0-9]{64}$/.test(value.fingerprint||'')?value.fingerprint:null});
 }
 function create(){
  let entries=[];
  const same=(a,b)=>a&&b&&a.id===b.id&&a.page===b.page&&a.view===b.view;
  return {push(value){const next=position(value);if(!next)return false;if(!same(entries.at(-1),next))entries=[...entries.slice(-19),next];return true;},
   peek(){return entries.at(-1)||null;},pop(expected){if(entries.at(-1)!==expected)return false;entries.pop();return true;},clear(){entries=[];}};
 }
 return {create,position};
});
