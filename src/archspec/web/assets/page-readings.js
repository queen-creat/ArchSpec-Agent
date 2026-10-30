/* 核读是独立待确认记录，绝不能替换旧识别文字或变成专业审核。 */
(() => {
  const value='([1-9]\\d{0,5}(?:\\.\\d{1,2})?)m²';
  const pattern=new RegExp('^(\\d+(?:\\.\\d+){2}除有特殊要求的建筑、木结构建筑和附建于民用建筑中的汽车库外，其他公共建筑中每个防火分区的最大允许建筑面积应符合下列规定：)'+
    '1对于高层建筑，不应大于'+value+'。2(对于一、二级耐火等级的单、多层建筑，不应大于'+value+'；)'+
    '(对于三级耐火等级的单、多层建筑，不应大于'+value+'；)(对于四级耐火等级的单、多层建筑，不应大于'+value+'。)'+
    '3对于地下设备房，不应大于'+value+'；对于地下其他区域，不应大于'+value+'。4'+
    '(当防火分区全部设置自动灭火系统时，上述面积可以增加1\\.0倍；当局部设置自动灭火系统时，可按该局部区域建筑面积的1/2计入所在防火分区的总建筑面积。)$');
  const hex=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
  function validate(body,citationReady){
    if(body.status!=='REFERENCE_BRIEF'||!Array.isArray(body.page_readings)||body.page_readings.length>4
      ||!Array.isArray(body.answer_items)||!Array.isArray(body.evidence))return [];
    const ids=new Set(),anchors=new Set(),accepted=[];
    for(const row of body.page_readings){
      if(!row||ids.has(row.transcription_id)||anchors.has(row.chunk_id))return [];
      ids.add(row.transcription_id);anchors.add(row.chunk_id);
      if(row.version!=='FIXED_PAGE_TRANSCRIPTION_V1'||row.method!=='AI_VISUAL_TRANSCRIPTION'
        ||row.status!=='PENDING_HUMAN_CONFIRMATION'||row.reviewer_role!=='AI_DEVELOPER'
        ||row.is_project_value!==false||row.reading_kind!=='PUBLIC_MULTISTOREY_BASELINE'
        ||row.adjusted_value!==undefined||row.unit!=='m²'
        ||!['ABSENT','PRESENT_SCOPE_UNCONFIRMED'].includes(row.extinguishing_state)
        ||!['transcription_id','snapshot_id','snapshot_fingerprint','file_sha256','raw_text_sha256','page_png_sha256'].every(key=>hex(row[key]))
        ||typeof row.transcription!=='string'||row.transcription.length>6000||typeof row.original_quote!=='string'
        ||!Number.isInteger(row.page_start)||row.page_start<1||row.page_start!==row.page_end)continue;
      const items=body.answer_items.filter(item=>item?.citation?.evidence_id===row.evidence_id);
      const ledger=body.evidence.filter(item=>item?.evidence_id===row.evidence_id);
      if(items.length!==1||ledger.length!==1)continue;
      const item=items[0],owner=ledger[0];
      if(!citationReady(item.citation)||!citationReady(owner)||owner.quote_ready!==true
        ||item.quote!==owner.text||item.quote!==row.original_quote
        ||['source_id','snapshot_id','snapshot_fingerprint','chunk_id','clause_number','page_start','page_end'].some(key=>
          item.citation[key]!==row[key]||owner[key]!==row[key])
        ||item.citation.source_metadata?.file_sha256!==row.file_sha256)continue;
      const conditions=row.conditions,base=body.answer_brief?.conditions;
      // 常见用途后缀不影响定位；排除范围绝不能套一般公共建筑基准值。
      if(!conditions||!base||['building_use','fire_resistance','fire_context','public_fire_scope','suppression_scope'].some(key=>conditions[key]!==base[key])
        ||!/^(办公|学校|医疗|文化|旅馆)(建筑)?$/.test(conditions.building_use||'')
        ||['木结构建筑','有特殊要求的建筑','附建于民用建筑中的汽车库'].includes(conditions.public_fire_scope)
        ||typeof conditions.fire_resistance!=='string'
        ||!/(单层|多层)/.test(body.query||'')||/(高层|地下|不是|未知|不确定)/.test(body.query||'')
        ||typeof conditions.fire_context!=='string'||!conditions.fire_context.includes('地上')||conditions.fire_context.includes('地下'))continue;
      const grade=conditions.fire_resistance.replace('（待人工核对）','').replace('(待人工核对)','').trim()
        .match(/^(?:耐火等级\s*(?:为|是)?\s*)?([一二三四1234])级$/)?.[1];
      const index={'一':3,'二':3,'1':3,'2':3,'三':5,'3':5,'四':7,'4':7}[grade];
      const full=row.transcription.replace(/\s/g,''),match=full.match(pattern);
      if(!index||!match||!full.startsWith(row.clause_number+'除')
        ||row.baseline_value!==match[index+1]||row.scope_text!==match[1]
        ||row.condition_text!==match[index]||row.modifier_text!==match[11])continue;
      accepted.push({...row,item});
    }
    return accepted;
  }
  globalThis.ArchSpecPageReadings={validate};
})();
