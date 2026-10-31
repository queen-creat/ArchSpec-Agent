/* 页数来自已核服务器快照；未知旧位置仍限200页，点击读取仍由服务器核权。 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ArchSpecPageBounds=api;})(globalThis,function(){
 const own=(v,k)=>v&&Object.prototype.hasOwnProperty.call(v,k);
 function limit(row,owner){
  if(own(owner,'page_count')&&own(row,'page_count')&&owner.page_count!==row.page_count)return 0;
  const n=own(owner,'page_count')?owner.page_count:own(row,'page_count')?row.page_count:200;
  return Number.isInteger(n)&&n>=1&&n<=500?n:0;
 }
 return {limit};
});
