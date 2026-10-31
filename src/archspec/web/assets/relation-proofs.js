// 独立核验指定原字token和固定账本；不扫描原书发现关系。
(() => {
  const bounds=globalThis.ArchSpecPageBounds;
  const number=/^(?:\d+(?:\.\d+){2,3}|第[一二三四五六七八九十百千万零〇0-9]+条)$/;
  // 表号保持原字；独立核验器不将连字符表号当数字条号。
  const tableNumber=/^(?:\d+(?:\.\d+){2,3}|\d+-\d+|附表(?:[一二三四五六七八九十百千万零〇]+|\d{1,3}))$/;
  const tableLabel=value=>value.startsWith('附表')?value:'表'+value;
  const fields=['version','kind','direction','source_id','snapshot_id','anchor_unit_key',
    'origin_unit_key','target_unit_key','related_unit_key','origin_chunk_ids','related_chunk_ids',
    'expected_fragment_count','admitted_chunk_ids','status','coverage','witness'];
  const compact=text=>text.replace(/\s+/gu,'');
  const points=text=>Array.from(text);
  const unit=row=>row.reading_unit_key||row.clause_number;
  const index=id=>Number(id.slice(id.lastIndexOf(':')+1));
  const same=(a,b)=>a.length===b.length&&a.every((value,i)=>value===b[i]);
  const connectors=new Set(['、','，',',','及','和','以及','与','或']);
  function localScope(stream,start,table) {
    const raw=points(stream),before=compact(raw.slice(0,start).join(''));
    if(before.lastIndexOf('《')>before.lastIndexOf('》'))return false;
    const prefix=raw.slice(0,start).join(''),line=prefix.slice(prefix.lastIndexOf('\n')+1)
      +raw.slice(start).join('').split('\n',1)[0];
    if(/[.…⋯·]{2,}\s*\d/u.test(line))return false;
    const sentence=before.split(/[。；;！？!?]/u).at(-1);
    return !table||!sentence.includes('《')
      ||Math.max(sentence.lastIndexOf('本规范'),sentence.lastIndexOf('本标准'))>=sentence.lastIndexOf('》');
  }
  function check(proof,basis,evidence) {
    if(!proof||!basis||Object.keys(proof).length!==fields.length
      ||!fields.every(key=>Object.hasOwn(proof,key))||proof.version!=='EVIDENCE_RELATION_V1'
      ||proof.coverage!=='READ_CONTEXT_ONLY'||!['CLAUSE_REFERENCE','TABLE_REFERENCE'].includes(proof.kind)
      ||!['OUTGOING','INCOMING'].includes(proof.direction)
      ||!['LOCATED','NOT_LOCATED','PARTIAL','BUDGET_EXCLUDED','LIMIT_REACHED'].includes(proof.status)
      ||!Number.isInteger(proof.expected_fragment_count)||proof.expected_fragment_count<0
      ||!Array.isArray(evidence)||evidence.length>8
      ||evidence.some(row=>!row||typeof row.text!=='string')
      ||evidence.reduce((total,row)=>total+points(row.text).length,0)>16000
      ||new Set(evidence.map(row=>row.evidence_id)).size!==evidence.length
      ||new Set(evidence.map(row=>row.chunk_id)).size!==evidence.length)return null;
    const source=proof.source_id,snapshot=proof.snapshot_id;
    if(typeof source!=='string'||!/^(gb55031|official-[a-f0-9]{32})$/.test(source)
      ||typeof snapshot!=='string'||!/^[a-f0-9]{64}$/.test(snapshot)
      ||basis.source_id!==source||basis.snapshot_id!==snapshot
      ||unit(basis.selected_clause_coverage||{})!==proof.anchor_unit_key)return null;
    const prefix=source+':'+snapshot+':';
    const identities=values=>Array.isArray(values)&&new Set(values).size===values.length
      &&values.every(id=>typeof id==='string'&&id.startsWith(prefix)&&/^(0|[1-9]\d{0,5})$/.test(id.slice(prefix.length)));
    if(!['origin_chunk_ids','related_chunk_ids','admitted_chunk_ids'].every(key=>identities(proof[key]))
      ||!proof.origin_chunk_ids.length||proof.related_chunk_ids.length!==proof.expected_fragment_count
      ||!proof.admitted_chunk_ids.every(id=>proof.related_chunk_ids.includes(id)))return null;
    const ledger=new Map();
    for(const row of evidence) {
      if(row.source_id!==source||row.snapshot_id!==snapshot||!identities([row.chunk_id])
        ||!['MATCH','CONTINUATION','RELATED_CONTEXT'].includes(row.role)
        ||!Number.isInteger(row.page_start)||!Number.isInteger(row.page_end)
        ||row.page_start<1||row.page_start>row.page_end||row.page_end>(bounds?bounds.limit(row,basis):200)
        ||(typeof row.standard_code!=='string'||row.standard_code!==basis.standard_code)
        ||!['snapshot_fingerprint','title','source_url'].every(key=>row[key]&&row[key]===basis[key]))continue;
      if(Array.isArray(basis.fragments)) {
        const fragment=basis.fragments.find(item=>item.id===row.chunk_id);
        if(!fragment||!['text','clause_number','page_start','page_end','reading_unit_key']
          .every(key=>fragment[key]===row[key]))continue;
      }
      ledger.set(row.chunk_id,row);
    }
    const anchor=ledger.get(basis.selected_chunk_id);
    if(!anchor||anchor.role!=='MATCH'||!proof.origin_chunk_ids.every(id=>ledger.has(id)))return null;
    const origin=proof.origin_chunk_ids,target=proof.target_unit_key,related=proof.related_unit_key;
    if(!same([...origin].sort((a,b)=>index(a)-index(b)),origin)
      ||origin.some(id=>unit(ledger.get(id))!==proof.origin_unit_key))return null;
    let ref;
    if(proof.kind==='CLAUSE_REFERENCE') {
      if(!number.test(target)||!number.test(proof.origin_unit_key)
        ||proof.anchor_unit_key!==(proof.direction==='OUTGOING'?proof.origin_unit_key:target)
        ||related!==(proof.direction==='OUTGOING'?target:proof.origin_unit_key))return null;
      ref=(basis.clause_references||[]).find(item=>item.direction===proof.direction&&item.clause_number===related);
      if(!ref||ref.expected_fragment_count!==proof.expected_fragment_count
        ||!(ref.chunk_ids||[]).every(id=>proof.related_chunk_ids.includes(id))
        ||!same((ref.chunk_ids||[]).filter(id=>ledger.has(id)),proof.admitted_chunk_ids)
        ||proof.admitted_chunk_ids.some(id=>ledger.has(id)&&unit(ledger.get(id))!==related))return null;
    } else {
      if(proof.direction!=='OUTGOING'||proof.anchor_unit_key!==proof.origin_unit_key
        ||related!==target||typeof target!=='string'||!target.startsWith('table:')||!tableNumber.test(target.slice(6)))return null;
      ref=(basis.table_references||[]).find(item=>item.table_number===target.slice(6));
      if(!ref)return null;
      const ids=[];
      for(const page of ref.pages||[]) {
        const row=ledger.get(page.chunk_id);
        if(!row)continue;
        if(!Number.isInteger(page.pdf_page)||page.pdf_page<row.page_start||page.pdf_page>row.page_end
          ||typeof page.caption!=='string'||typeof page.continuation!=='boolean')return null;
        ids.push(page.chunk_id);
        for(const part of page.reading_parts||[]) {
          const owner=ledger.get(part.chunk_id);
          if(!owner)continue;
          if(typeof part.text!=='string'||!part.text||!owner.text.includes(part.text)
            ||page.pdf_page<owner.page_start||page.pdf_page>owner.page_end)return null;
          ids.push(part.chunk_id);
        }
      }
      if(!same([...new Set(ids)],proof.admitted_chunk_ids))return null;
    }
    const actual=proof.related_chunk_ids.filter(id=>ledger.has(id));
    if(!same(actual,proof.admitted_chunk_ids)
      ||proof.status==='LOCATED'&&(!actual.length||actual.length!==proof.expected_fragment_count)
      ||proof.status==='NOT_LOCATED'&&(actual.length||proof.expected_fragment_count!==0)
      ||proof.status==='BUDGET_EXCLUDED'&&actual.length===proof.expected_fragment_count
      ||proof.status==='LIMIT_REACHED')return null;
    const coverage=basis.selected_clause_coverage;
    const coreComplete=coverage.complete===true
      &&coverage.included_fragment_count===coverage.expected_fragment_count
      &&[...ledger.values()].filter(row=>unit(row)===proof.anchor_unit_key
        &&['MATCH','CONTINUATION'].includes(row.role)).length===coverage.expected_fragment_count;
    if(proof.status==='PARTIAL'&&coreComplete&&actual.length===proof.expected_fragment_count)return null;
    const offsets=new Map();let stream='',cursor=0;
    for(const id of origin){offsets.set(id,cursor);stream+=ledger.get(id).text;cursor=points(stream).length;}
    if(!Array.isArray(proof.witness)||!proof.witness.length)return null;
    const tokens=[];let previousEnd=null,firstStart=null;
    for(const witness of proof.witness) {
      if(!witness||Object.keys(witness).some(key=>!['purpose','number','spans'].includes(key))
        ||!Array.isArray(witness.spans)||!witness.spans.length)return null;
      let raw='',first=null,end=null,previousPart=null;
      for(const span of witness.spans) {
        if(!span||!same(Object.keys(span).sort(),['chunk_id','start','end','text'].sort()))return null;
        const row=ledger.get(span.chunk_id),start=span.start,stop=span.end,chars=row?points(row.text):[];
        if(!row||!Number.isInteger(start)||!Number.isInteger(stop)||start<0||start>=stop||stop>chars.length
          ||chars.slice(start,stop).join('')!==span.text)return null;
        if(witness.purpose!=='TABLE_CAPTION') {
          if(!offsets.has(span.chunk_id))return null;
          const absolute=offsets.get(span.chunk_id)+start,last=offsets.get(span.chunk_id)+stop;
          if(previousPart!==null&&(absolute<previousPart||compact(points(stream).slice(previousPart,absolute).join(''))))return null;
          if(first===null)first=absolute;end=last;previousPart=last;
        }
        raw+=span.text;
      }
      if(witness.purpose!=='TABLE_CAPTION') {
        if(previousEnd!==null&&(first<previousEnd||compact(points(stream).slice(previousEnd,first).join(''))))return null;
        if(firstStart===null)firstStart=first;previousEnd=end;
      }
      tokens.push([witness.purpose,compact(raw),witness.number]);
    }
    if(proof.kind==='CLAUSE_REFERENCE') {
      if(tokens.length<2||tokens.length%2||tokens[0][0]!=='SAME_BOOK_SEED'
        ||!['本规范','本标准','本规定','按'].includes(tokens[0][1])||tokens[0][2]!==undefined
        ||!localScope(stream,firstStart,tokens[0][1]==='按'))return null;
      for(let i=1;i<tokens.length;i++) {
        const [purpose,literal,value]=tokens[i];
        if(i%2) {
          if(purpose!=='REFERENCE_ITEM'||typeof value!=='string'||!number.test(value)||!(value.startsWith('第')?[value]:['第'+value+'条',value+'条']).includes(literal))return null;
        } else if(purpose!=='LIST_CONNECTOR'||!connectors.has(literal)||value!==undefined)return null;
      }
      if(tokens.at(-1)[2]!==target)return null;
    } else {
      const value=target.slice(6);
      if(!same(tokens[0],['TABLE_REFERENCE',tableLabel(value),value])||!localScope(stream,firstStart,true)
        ||proof.status==='LOCATED'&&tokens.length===1)return null;
      const expectedCaptions=new Set((ref.pages||[]).filter(page=>proof.admitted_chunk_ids.includes(page.chunk_id))
        .map(page=>JSON.stringify([page.chunk_id,compact(page.caption),page.continuation])));
      const tokenCaptions=new Set();
      for(let i=1;i<tokens.length;i++) {
        const [purpose,literal,tokenNumber]=tokens[i],spans=proof.witness[i].spans;
        if(purpose!=='TABLE_CAPTION'||tokenNumber!==value||spans.length!==1)return null;
        const key=JSON.stringify([spans[0].chunk_id,literal,literal.startsWith('续')]);
        const caption=new RegExp('^(?:续)?'+tableLabel(value).replaceAll('.','\\.')+'(?![\\d.\\-])(.+)$','u').exec(literal);
        if(!expectedCaptions.has(key)||!caption||/^(的|规定|所示|中|应|要求|内)/u.test(caption[1])
          ||/[.…⋯·]{2,}/u.test(literal))return null;
        tokenCaptions.add(key);
      }
      if(tokenCaptions.size!==expectedCaptions.size
        ||[...expectedCaptions].some(key=>!tokenCaptions.has(key)))return null;
    }
    const used=new Set(proof.witness.filter(item=>item.purpose!=='TABLE_CAPTION')
      .flatMap(item=>item.spans.map(span=>span.chunk_id)));
    if(used.size>1) {
      const expected=proof.origin_unit_key===proof.anchor_unit_key?coverage.expected_fragment_count:proof.expected_fragment_count;
      if(origin.length!==expected||!coreComplete||origin.slice(1).some((id,i)=>index(id)!==index(origin[i])+1))return null;
    }
    return {...proof,admitted_evidence_ids:proof.admitted_chunk_ids.map(id=>ledger.get(id).evidence_id)};
  }
  function verify(proof,basis,evidence) {try{return check(proof,basis,evidence);}catch{return null;}}
  globalThis.ArchSpecRelationProof=Object.freeze({verify});
})();
