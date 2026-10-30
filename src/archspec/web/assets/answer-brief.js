/* 原文条件段的独立核对。任何失败都返回 null，让旧原文与原页仍然可用。 */
(() => {
  // 仅合并显示，不删回答/证据账本；不同原件、页码、核验限制仍各自展示。
  function consolidate(groups, body) {
    const shown=[],seen=new Map(),ledger=body.evidence;
    const readings=[...(Array.isArray(body.page_readings)?body.page_readings:[]),
      ...(Array.isArray(body.table_readings)?body.table_readings:[])];
    for(const group of groups) {
      let key=null;
      if(/^\d+(?:\.\d+){2,3}$/.test(group.clause_number||'')
        && !readings.some(row=>group.evidence_ids.includes(row.evidence_id)
          ||group.evidence_ids.includes(row.anchor_evidence_id))) {
        const parts=group.parts.map(item=>{
          const citation=item.citation,metadata=globalThis.ArchSpecSourceMetadata?.fixed(citation);
          const owner=ledger.find(row=>row.evidence_id===citation.evidence_id);
          const bound=globalThis.ArchSpecSourceMetadata?.fixed(owner);
          if(!metadata||!bound||metadata.metadata_id!==bound.metadata_id
            ||metadata.file_sha256!==bound.file_sha256)return null;
          const fields=['level','region','edition','withdrawal'].map(name=>
            [metadata.fields[name].value,metadata.fields[name].state]);
          return [metadata.file_sha256,citation.standard_code,citation.clause_number,
            citation.page_start,citation.page_end,item.quote,fields];
        });
        if(parts.every(Boolean))key=JSON.stringify([parts,group.segments,group.branch_outline||null]);
      }
      const previous=key&&seen.get(key);
      const fullModes=['FULL_CLAUSE','ORIGINAL_REQUIRED'];
      const compatible=previous&&(previous.mode===group.mode&&previous.reason===group.reason
        ||!previous.branch_outline&&!group.branch_outline&&!group.segments.length
          &&fullModes.includes(previous.mode)&&fullModes.includes(group.mode)&&previous.mode!==group.mode);
      if(compatible&&previous.source_id!==group.source_id
        && !previous.source_copies.some(copy=>copy.source_id===group.source_id)) {
        previous.source_copies.push(group);
        // 识别待核不能被另一个副本的较轻提示冲掉；各来源原状态留在原响应中。
        if(group.mode==='ORIGINAL_REQUIRED') {previous.mode=group.mode;previous.reason=group.reason;}
      }
      else {
        const displayed={...group,source_copies:[]};shown.push(displayed);
        if(key&&!previous)seen.set(key,displayed);
      }
    }
    return shown;
  }

  function validate(body, citationReady) {
    const brief=body.answer_brief,items=body.answer_items,ledger=body.evidence;
    if(!brief || brief.version!=='FIXED_EVIDENCE_BRIEF_V1' || !Array.isArray(items)
      || items.length<1 || items.length>8 || !Array.isArray(ledger)
      || !Array.isArray(brief.groups) || !brief.groups.length || brief.groups.length>8)return null;
    const taken=new Set(),units=new Set(),groups=[];
    for(const group of brief.groups) {
      if(!group || !['CONDITION_EXCERPTS','FULL_CLAUSE','ORIGINAL_REQUIRED'].includes(group.mode)
        || typeof group.reason!=='string' || !group.reason || group.reason.length>400
        || !Array.isArray(group.evidence_ids) || !group.evidence_ids.length
        || group.evidence_ids.length>8 || !Array.isArray(group.segments))return null;
      // 无数字条号的完整性必须来自独立阅读单元，不能让所有 null 共享一个身份。
      const named = group.clause_number == null;
      if(named && (typeof group.reading_unit_key !== 'string'
        || !/^named:(0|[1-9]\d{0,5})$/.test(group.reading_unit_key)
        || typeof group.reading_unit_label !== 'string' || !group.reading_unit_label.trim()
        || group.reading_unit_label.length>400 || /[\r\n]/.test(group.reading_unit_label)))return null;
      const unitIdentity=JSON.stringify([group.source_id,group.snapshot_id,
        named ? group.reading_unit_key : group.clause_number]);
      if(units.has(unitIdentity))return null;
      units.add(unitIdentity);
      const parts=[];
      for(const id of group.evidence_ids) {
        if(taken.has(id))return null;
        const selected=items.filter(item=>item.citation?.evidence_id===id);
        const evidence=ledger.filter(row=>row?.evidence_id===id);
        if(selected.length!==1 || evidence.length!==1)return null;
        const item=selected[0],owner=evidence[0],citation=item.citation;
        if(!citationReady(citation) || !citationReady(owner) || owner.quote_ready!==true
          || item.quote!==owner.text || typeof item.quote!=='string' || item.quote.length>1800
          || ['source_id','snapshot_id','clause_number'].some(key=>citation[key]!==group[key] || owner[key]!==group[key])
          || ['chunk_id','page_start','page_end'].some(key=>citation[key]!==owner[key])
          || ledger.filter(row=>row?.chunk_id===owner.chunk_id).length!==1
          || (named && ['reading_unit_key','reading_unit_label'].some(key=>citation[key]!==group[key] || owner[key]!==group[key])))return null;
        taken.add(id);parts.push(item);
      }
      const ordered=[...parts].sort((a,b)=>Number(a.citation.chunk_id.split(':').at(-1))-Number(b.citation.chunk_id.split(':').at(-1)));
      if(ordered.some((item,index)=>item!==parts[index]))return null;
      // named 编号指向实际首片段；三个对象一致也不能伪造另一个首段身份。
      if(named && group.reading_unit_key!==`named:${parts[0].citation.chunk_id.split(':').at(-1)}`)return null;
      const full=parts.map(item=>item.quote).join('\n'),characters=Array.from(full);
      // Python 区间按 Unicode 字符计算；不能拿 JS 的 UTF-16 下标截含 Emoji 的原文。
      if(group.mode==='CONDITION_EXCERPTS') {
        if(group.segments.length<2 || group.segments.length>16
          || parts.some(item=>item.citation.warnings?.includes('LOW_CONFIDENCE_OCR')))return null;
        let previousEnd=0;
        for(const [index,part] of group.segments.entries()) {
          if(!part || !Number.isInteger(part.start) || !Number.isInteger(part.end)
            || part.start<previousEnd || part.end<=part.start || part.end>characters.length
            || typeof part.text!=='string' || characters.slice(part.start,part.end).join('')!==part.text
            || (index===0 ? part.role!=='SCOPE'||part.start!==0
              : !['CONDITION','RESTRICTION'].includes(part.role)))return null;
          previousEnd=part.end;
        }
        if(previousEnd!==characters.length || !group.segments.some(part=>part.role==='CONDITION'))return null;
      } else if(group.segments.length)return null;
      if(group.branch_outline!==undefined) {
        const outline=group.branch_outline,scope=outline?.scope,rows=outline?.items;
        // 分项须覆盖全部原字，编号一项不少；这只是阅读结构，不挑选工程条件。
        const candidates=[...full.matchAll(/^[ \t]*(\d{1,2})(?:([.．](?!\d)|、)[ \t]*|[ \t]+)(?=\S)/gm)];
        const marks=candidates.filter(mark=>Boolean(mark[2])===Boolean(candidates[0]?.[2]));
        const starts=marks.map(mark=>Array.from(full.slice(0,mark.index)).length);
        if(!['FULL_CLAUSE','ORIGINAL_REQUIRED'].includes(group.mode)||outline?.version!=='NUMBERED_ORIGINAL_V1'
          ||!Array.isArray(rows)||rows.length<2||rows.length>16||marks.length!==rows.length
          ||candidates.some(mark=>Boolean(mark[2])!==Boolean(candidates[0]?.[2])&&Number(mark[1])>=1&&Number(mark[1])<=marks.length+1)
          ||scope?.start!==0||scope.end!==starts[0]||scope.text!==characters.slice(0,scope.end).join('')
          ||!/(?:下列|以下|要求|规定|如下)[^。；:：]{0,16}[:：]$/.test(scope.text.replace(/\s/g,''))
          ||/表\s*\d/.test(full)||parts.some(item=>item.citation.warnings?.some(warning=>
            ['UNRECOGNIZED_IMAGES','OCR_NUMBERED_ITEM_GAP'].includes(warning)
            ||group.mode==='FULL_CLAUSE'&&['LOW_CONFIDENCE_OCR','OCR_GAP_RECOVERED_NEEDS_REVIEW'].includes(warning))))return null;
        for(const [index,row] of rows.entries()) {
          if(!row||row.number!==index+1||Number(marks[index][1])!==row.number
            ||row.start!==starts[index]||row.end!==(starts[index+1]??characters.length)
            ||row.text!==characters.slice(row.start,row.end).join('')||typeof row.title!=='string'
            ||Array.from(row.title).length>180||row.title!==characters.slice(row.start,row.start+Array.from(row.title).length).join('')
            ||row.title&&(!/[:：]$/.test(row.title)||/[。；;]/.test(row.title)))return null;
        }
      }
      groups.push({...group,parts,full});
    }
    return taken.size===items.length ? consolidate(groups,body) : null;
  }
  globalThis.ArchSpecAnswerBrief={validate};
})();
