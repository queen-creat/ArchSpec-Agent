// 仅原件的阅读入口；不把预览图片或提示文字当成可检索条文。
(() => {
  const reader=document.querySelector("#pending-page-reader");
  if(!reader)return;
  let generation=0;
  const invalidate=()=>{generation+=1;};
  for(const event of ["archspec:query-start","archspec:project-change"])document.addEventListener(event,invalidate);
  document.querySelector("#pending-query")?.addEventListener("input",invalidate);
  document.querySelector("#pending-source")?.addEventListener("change",invalidate);
  const element=(tag,text)=>{const node=document.createElement(tag);if(text)node.textContent=text;return node;};
  async function open(item, {isCurrent=()=>true,initialPage}={}) {
    if(typeof isCurrent!=="function" || !isCurrent())return false;
    const own=++generation;
    const query=document.querySelector("#pending-query")?.value || "";
    const scope=document.querySelector("#pending-source")?.value || "";
    const project=document.querySelector("#search-project")?.value || "";
    // 原件与正文同样绑定本题、项目和范围；恢复回调和旧页按钮都不能复活已离开的阅读。
    const current=()=>own===generation && isCurrent()
      && query===(document.querySelector("#pending-query")?.value || "")
      && scope===(document.querySelector("#pending-source")?.value || "")
      && project===(document.querySelector("#search-project")?.value || "");
    const prefix=`/api/official-documents/${encodeURIComponent(item.source_id)}/originals/${encodeURIComponent(item.snapshot_id)}`;
    const loading=element("p","正在读取官方原件与预览……");reader.replaceChildren(loading);
    try {
      const response=await fetch(prefix,{method:"GET",cache:"no-store"});
      const data=await globalThis.ArchSpecHTTP.json(response,"原件快照暂不可读取");
      if(!current())return false;
      // 描述和预览只取同一固定原件；重新读取仍由服务端核对当前许可，不借新正文替换旧pin。
      if(!response.ok || data?.source_id!==item.source_id || data.snapshot_id!==item.snapshot_id
        || data.body_status!=="ORIGINAL_ONLY" || !Array.isArray(data.chunks) || data.chunks.length
        || !Array.isArray(data.preview_pages) || data.preview_pages.length>3
        || new Set(data.preview_pages).size!==data.preview_pages.length
        || data.preview_pages.some(page=>!Number.isInteger(page) || page<1 || page>3)
        || initialPage!=null && !data.preview_pages.includes(initialPage))
        throw Error("原件快照暂不可读取，请稍后重开");
      reader.replaceChildren();
      const card=element("section");card.className="pending-original-reader";
      card.append(element("h3",data.title || item.title || "官方PDF原件"));
      const web=data.source_format==="HTML";
      const hint=element("p",web ? data.message : "已获取官方 PDF 原件，但系统暂无法自动提取文本，请直接查看原页对照。");
      hint.className="source-warning";card.append(hint);
      const fields=data.source_metadata || {};
      const region=fields.level==="NATIONAL"?"【国家】":fields.level==="INDUSTRY"?"【行业】":
        fields.level==="LOCAL" && fields.region?`【地方·${fields.region.replaceAll("·","")}】`:"地域：待核";
      const badge=element("p",`${region} · 仅原件/待辨认 · 强条：待核`);
      badge.style.color=({NATIONAL:"#2563EB",INDUSTRY:"#0E7490",LOCAL:"#C2410C"})[fields.level] || "#6B7280";
      card.append(badge);
      const metadata=element("dl");
      metadata.style.display="grid";metadata.style.gridTemplateColumns="max-content minmax(0,1fr)";
      metadata.style.gap="4px 10px";metadata.style.overflowWrap="anywhere";
      for(const [key,value]of [["文号",data.standard_code],["发布机关",fields.publisher],["适用地域",fields.region],
        ["版本",null],["实施日期",null],["废止状态",null],["来源",data.page_url],["快照 ID",data.snapshot_id]]) {
        const valueNode=element("dd",value || "待核");valueNode.style.margin="0";
        metadata.append(element("dt",key),valueNode);
      }
      card.append(metadata);
      const link=element("a",web?"下载完整网页原件":"打开或下载完整PDF原件");link.href=prefix+(web?"/file.html":"/file.pdf");link.target="_blank";link.rel="noopener";
      link.className="secondary-button";card.append(link);
      card.append(element("h4","原页对照"));
      card.append(element("p",web ? "网页原始字节已保存；未生成可检索条文。"
        : `原件${data.page_count || "页数待核"}页 · 已提供${data.preview_pages.length}页预览；未自动识别或生成条文。`));
      const controls=element("div"),image=element("img");image.style.maxWidth="100%";image.style.height="auto";
      image.alt="官方PDF原页预览";
      const saved=globalThis.ArchspecPendingSearch?.readingPosition?.();
      const remembered=data.preview_pages.includes(initialPage) ? initialPage
        : saved?.kind==="ORIGINAL_ONLY" && saved.source_id===item.source_id
          && saved.snapshot_id===item.snapshot_id && data.preview_pages.includes(saved.page) ? saved.page : data.preview_pages[0];
      function showPage(page){
        if(!current() || !data.preview_pages.includes(page))return;
        image.src=prefix+`/pages/${page}.png`;
        globalThis.ArchspecPendingSearch?.rememberReadingPosition?.(item,{kind:"ORIGINAL_ONLY",page,view:"ORIGINAL"});
      }
      for(const page of data.preview_pages) {
        const button=element("button",`第${page}页`);button.type="button";button.className="secondary-button";
        button.addEventListener("click",()=>showPage(page));
        controls.append(button);
      }
      card.append(controls);
      if(data.preview_pages.length){showPage(remembered);card.append(image);}
      else {
        // 网页或无法生成预览的原件也保留固定阅读身份；没有预览就不造第1页。
        if(current())globalThis.ArchspecPendingSearch?.rememberReadingPosition?.(item,{kind:"ORIGINAL_ONLY",page:null,view:"ORIGINAL"});
        card.append(element("p",web?"请下载网页原件或打开官方来源查看。":"暂无法生成预览，请打开PDF原件手动查看。"));
      }
      if(!current())return false;
      reader.append(card);reader.scrollIntoView({block:"nearest"});return true;
    } catch {
      if(current())reader.replaceChildren(element("p","原件暂不可读取，请稍后重新打开；已有查询记录保留。"));
      return false;
    }
  }
  globalThis.ArchSpecOriginalReader={open};
})();
