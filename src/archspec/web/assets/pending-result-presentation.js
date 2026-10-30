// 仅整理本次已检索到的候选；不改变服务端排序、正文、核验状态或原题库成绩。
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ArchSpecResultPresentation = api;
})(globalThis, function () {
  const topics = [
    {id:'HEIGHT', label:'高度与层数', pattern:/建筑高度|高层|多层|层数|屋顶|屋面|地坪/},
    {id:'EVACUATION', label:'疏散与出口', pattern:/疏散|安全出口|避难|逃生/},
    {id:'FIRE_AREA', label:'防火分区', pattern:/防火分区|防火分隔/},
    {id:'PARKING', label:'车位与通道', pattern:/车位|停车位|车道|停车泊位|回转/},
    {id:'GARAGE', label:'车库与停车场', pattern:/车库|停车场|地下停车/},
    {id:'SUNLIGHT', label:'日照与间距', pattern:/日照|间距|采光/},
  ];
  const groups = [{id:'CLAUSE', label:'条文'}, {id:'NAVIGATION', label:'章节 / 目录'},
    {id:'OTHER', label:'待辨认片段'}, {id:'ALL', label:'全部'}];
  const completeClause = /^(?:\d+(?:\.\d+){2,3}|第[一二三四五六七八九十百千万零〇0-9]+条)$/;
  const action = /不应|不得|不宜|必须|应|宜|不小于|不大于|大于|小于|超过|至少|最大|最小|≤|≥/;
  const quantity = /\d+(?:\.\d+)?\s*(?:mm|cm|km|m(?:\s*[²³23])?|㎡|平方米|毫米|米|%|％|辆|层)(?![A-Za-z])/i;
  function describe(item) {
    const text = String(item.text || '').trim();
    // 只为匹配提示去掉空白；展示始终用 item.text，不猜改OCR原文。
    const compact = text.replace(/\s+/g, '');
    const body = text.replace(/^\s*\d+(?:\.\d+){1,3}\s*/, '');
    // 括号编号可能直接带计算规则，不能仅因没有完整条号或句号就放进目录。
    const paragraphRule = /^（[一二三四五六七八九十百]+）.*(?:按照|按.+计算|当|若)/.test(compact);
    const substantive = action.test(body) || quantity.test(body) || paragraphRule;
    const toc = /(?:^|\n)\s*目\s*录\s*(?:\n|$)/.test(text)
      || /[.…·]{3,}\s*\d+\s*(?:\n|$)/.test(text);
    const shortHeading = /^(?:第[一二三四五六七八九十百\d]+[章节篇]|\d+(?:\.\d+)?)\s+[^。；！？\n]{1,30}$/.test(text)
      || /^(?:[一二三四五六七八九十百]+、|（[一二三四五六七八九十百]+）)\s*[^。；！？\n]{1,30}$/.test(text);
    let kind = 'OTHER';
    if ((toc || shortHeading) && !substantive) kind = 'NAVIGATION';
    else if (!toc && completeClause.test(item.clause_number || '')
      && (substantive || /[。；！？]/.test(body))) kind = 'CLAUSE';
    return {kind, topics:topics.filter(topic => topic.pattern.test(compact)).map(topic => topic.id),
      numericCue:quantity.test(body) && action.test(body), verified:false};
  }
  function present(items, query, options = {}) {
    const rows = items.map((item, index) => ({item, index, description:describe(item)}));
    const counts = Object.fromEntries(groups.map(group => [group.id,
      group.id === 'ALL' ? rows.length : rows.filter(row => row.description.kind === group.id).length]));
    const group = options.group && options.group !== 'AUTO' ? options.group
      : counts.CLAUSE ? 'CLAUSE' : counts.OTHER ? 'OTHER' : 'NAVIGATION';
    const topic = options.topic || 'ALL', sort = options.sort || 'DESIGN';
    const requested = topics.filter(entry => entry.pattern.test(String(query).replace(/\s+/g,''))).map(entry => entry.id);
    const exact = completeClause.test(String(query).trim());
    let chosen = rows.filter(row => (group === 'ALL' || row.description.kind === group)
      && (topic === 'ALL' || row.description.topics.includes(topic)));
    function priority(row) {
      // 相关主题优先，无数字的定义/计量条文也保留；不相关的大数字不能挤走相关正文。
      const related = row.description.topics.some(id => requested.includes(id));
      if (requested.length && !related) return 0;
      return (related ? 4 : 0) + (row.description.numericCue ? 2 : 0);
    }
    if (sort === 'DESIGN' && !exact) chosen = [...chosen].sort((a,b) => {
      const groupOrder = {CLAUSE:0, OTHER:1, NAVIGATION:2};
      return (group === 'ALL' ? groupOrder[a.description.kind] - groupOrder[b.description.kind] : 0)
        || priority(b) - priority(a) || a.index - b.index;
    });
    return {group, topic, sort, results:chosen.map(row => row.item),
      groups:groups.map(entry => ({...entry, count:counts[entry.id]})),
      topics:topics.map(entry => ({id:entry.id, label:entry.label,
        count:rows.filter(row => (group === 'ALL' || row.description.kind === group)
          && row.description.topics.includes(entry.id)).length})),
      total:rows.length};
  }
  // 同一展示校验供当前检索和历史流程使用；观察维度不能替代正文许可或回答证据门。
  function lookupObservations(value, lookup = null) {
    const shape = (item, keys) => item && typeof item === 'object' && !Array.isArray(item)
      && Object.keys(item).length === keys.length && keys.every(key => Object.hasOwn(item, key));
    const count = (number, maximum) => Number.isSafeInteger(number) && number >= 0 && number <= maximum;
    const text = (string, maximum) => typeof string === 'string' && string.trim().length > 0 && string.length <= maximum && !/[\x00-\x1f\x7f]/.test(string);
    const digest = string => typeof string === 'string' && /^[a-f0-9]{64}$/.test(string);
    if (!shape(value, ['version','source','candidates','understanding','conditions','evidence','validity','recognition'])
      || value.version !== 'LOOKUP_OBSERVATIONS_V1'
      || !shape(value.source, ['state','scope','bindings'])
      || !['READABLE','MISSING','UNVERIFIED','RESTRICTED'].includes(value.source.state)
      || !['REGION','EXPLICIT_SOURCE','CACHE'].includes(value.source.scope)
      || !Array.isArray(value.source.bindings) || value.source.bindings.length > 20
      || !value.source.bindings.every(binding => shape(binding, ['source_id','snapshot_id','metadata_id'])
        && text(binding.source_id, 100) && digest(binding.snapshot_id) && (binding.metadata_id === null || digest(binding.metadata_id)))
      || new Set(value.source.bindings.map(binding => binding.source_id+':'+binding.snapshot_id)).size !== value.source.bindings.length
      || Boolean(value.source.bindings.length) !== (value.source.state === 'READABLE')
      || !shape(value.candidates, ['state','matched_count','unconfirmed_count'])
      || !count(value.candidates.matched_count, 40) || !count(value.candidates.unconfirmed_count, 40)
      || value.candidates.state !== (value.candidates.matched_count ? 'MATCHED' : value.candidates.unconfirmed_count ? 'UNCONFIRMED' : 'NONE')
      || !shape(value.understanding, ['state','topic','operation'])
      || !['CLEAR','UNCERTAIN'].includes(value.understanding.state) || !text(value.understanding.topic, 80) || !text(value.understanding.operation, 80)
      || !shape(value.conditions, ['state','missing_fields']) || !['NEEDS_DETAILS','NOT_REQUESTED'].includes(value.conditions.state)
      || !Array.isArray(value.conditions.missing_fields) || value.conditions.missing_fields.length > 30
      || !value.conditions.missing_fields.every(field => text(field, 80))
      || (value.conditions.state === 'NEEDS_DETAILS') !== Boolean(value.conditions.missing_fields.length)
      || !shape(value.evidence, ['state','applicability_state']) || value.evidence.state !== 'NOT_EVALUATED'
      || value.evidence.applicability_state !== 'NOT_VERIFIED'
      || !shape(value.validity, ['state','verified_count','unverified_count'])
      || !count(value.validity.verified_count, 20) || !count(value.validity.unverified_count, 20)
      || value.validity.verified_count + value.validity.unverified_count !== value.source.bindings.length
      || value.validity.state !== (value.validity.verified_count ? value.validity.unverified_count ? 'MIXED' : 'VERIFIED' : 'UNVERIFIED')
      || !shape(value.recognition, ['state','low_count'])
      || !['NO_RECORDED_DOUBTS','HAS_DOUBTS','UNVERIFIED'].includes(value.recognition.state)
      || (value.recognition.state === 'UNVERIFIED' ? value.recognition.low_count !== null
        : !count(value.recognition.low_count, 4000000) || (value.recognition.state === 'HAS_DOUBTS') !== (value.recognition.low_count > 0))) return null;
    if (lookup && (value.candidates.matched_count !== lookup.primary_indices.length
      || value.candidates.unconfirmed_count !== lookup.related_indices.length)) return null;
    return value;
  }
  function observationSummary(value) {
    const data = lookupObservations(value); if (!data) return '正文与候选观察尚不能核对，未提升为回答依据。';
    const source = {READABLE:'目标范围正文可读',MISSING:'本次目标范围未取得可读正文',
      UNVERIFIED:'目标正文可读性待核',RESTRICTED:'目标正文当前使用受限'}[data.source.state];
    const candidates = data.candidates.matched_count ? '找到 '+data.candidates.matched_count+' 条候选，'+data.candidates.unconfirmed_count+' 条待确认'
      : data.candidates.unconfirmed_count ? data.candidates.unconfirmed_count+' 条候选待确认' : '本次未召回候选';
    return source+' · '+candidates+' · 回答证据尚未评估';
  }
  function observationDetails(value) {
    const data = lookupObservations(value); if (!data) return ['正文与候选观察尚不能核对。'];
    const rows = [observationSummary(data),
      data.understanding.state === 'UNCERTAIN' ? '本次问题对应尚待确认；不能把召回候选直接当作答案。' : '问题对应已有分类记录；仍须核对证据范围。',
      data.conditions.state === 'NEEDS_DETAILS' ? '工程条件仍待补充：'+data.conditions.missing_fields.join('、') : '本次未提出额外工程条件；不等于工程适用性已核准。',
      '回答证据尚未评估 · 工程适用性未核验',
      '固定来源核验记录：'+data.validity.verified_count+' 个来源/快照已核验，'+data.validity.unverified_count+' 个待核；不等于版本现行或工程适用。'];
    rows.push(data.recognition.state === 'UNVERIFIED' ? '本次候选片段识别疑点记录待核。'
      : data.recognition.state === 'HAS_DOUBTS' ? '本次候选片段累计记录 '+data.recognition.low_count+' 处识别疑点，非整页或整本统计。'
      : '本次候选片段没有已登记的识别疑点，不代表整页或整本识别无误。');
    return rows;
  }
  return {describe, present, lookupObservations, observationSummary, observationDetails};
});
