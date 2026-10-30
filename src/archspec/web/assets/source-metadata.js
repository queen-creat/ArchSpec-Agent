/* 来源档案共用显示：只接受与当前正文相同的固定身份，不推断现行或适用。 */
(() => {
  const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const keys = ['level', 'region', 'publisher', 'announcement_number', 'edition', 'effective_at', 'withdrawal'];
  // 仅与 local_policy.PUBLICATIONS 的精确公告集合同步，不放开整个地方域名。
  const LOCAL_PUBLICATION_URLS = [
    'https://zrzy.yichang.gov.cn/content-42395-986231-1.html',
    'https://zrzy.yichang.gov.cn/content-42395-987164-1.html',
  ];
  const identityKeys = ['source_id', 'snapshot_id', 'metadata_id', 'snapshot_fingerprint', 'file_sha256', 'title', 'standard_code'];
  const validityKeys = ['schema_version', ...identityKeys, 'evaluated_at', 'standard_version_id',
    'artifact_binding_id', 'version_status', 'relations', 'reasons', 'applicability_status'];
  const versionKeys = ['value', 'verification_state', 'event_id', 'checked_as_of', 'valid_until', 'effective_at'];
  const relationKeys = ['relation_id', 'relation_type', 'from_version_id', 'to_version_id', 'effective_at',
    'affected_clauses', 'verification_state', 'event_id', 'checked_as_of', 'valid_until', 'effect_state'];
  const verificationStates = ['UNVERIFIED', 'VERIFIED', 'EXPIRED', 'REJECTED', 'REVOKED', 'MANIFEST_CHANGED', 'CONFLICT'];
  const versionValues = ['UNKNOWN', 'UPCOMING', 'CURRENT', 'SUPERSEDED', 'WITHDRAWN'];
  const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
  const text = (value, limit) => typeof value === 'string' && value.trim().length > 0
    && value.length <= limit && !/[\x00-\x1f\x7f]/.test(value);
  const shape = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  function date(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.slice(0, 4) === '0000') return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  }
  function instant(value) {
    if (typeof value !== 'string') return false;
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(value);
    return Boolean(match && date(match[1]) && Number(match[2]) < 24 && Number(match[3]) < 60
      && Number(match[4]) < 60 && (!match[5] || Number(match[5]) < 24)
      && (!match[6] || Number(match[6]) < 60) && Number.isFinite(Date.parse(value)));
  }
  const instantDate = value => new Date(value).toISOString().slice(0, 10);
  function eventFields(value, evaluated) {
    if (!verificationStates.includes(value.verification_state)) return false;
    if (value.event_id === null) return value.verification_state !== 'VERIFIED'
      && value.checked_as_of === null && value.valid_until === null;
    if (!uuid(value.event_id) || !instant(value.checked_as_of)
      || !(value.valid_until === null || (instant(value.valid_until)
        && Date.parse(value.valid_until) > Date.parse(value.checked_as_of)))) return false;
    if (value.verification_state === 'VERIFIED') return value.valid_until !== null
      && instantDate(value.checked_as_of) <= evaluated && instantDate(value.valid_until) >= evaluated;
    if (value.verification_state === 'EXPIRED') return value.valid_until !== null
      && instantDate(value.valid_until) <= evaluated;
    return true;
  }
  function fixed(item) {
    const data = item?.source_metadata;
    if (!data || data.schema_version !== 'FIXED_SOURCE_V1' || !digest(data.metadata_id)
      || !digest(data.snapshot_id)
      || !digest(data.snapshot_fingerprint) || !digest(data.file_sha256)
      || data.source_id !== item.source_id || data.snapshot_id !== item.snapshot_id
      || data.title !== item.title || data.standard_code !== item.standard_code
      || (item.snapshot_fingerprint && item.snapshot_fingerprint !== data.snapshot_fingerprint)
      || !data.fields || !keys.every(key => {
        const field = data.fields[key];
        return field && (field.value === null || typeof field.value === 'string')
          && ['UNKNOWN', 'CODE_CLUE', 'SNAPSHOT_RECORDED', 'NOTICE_OBSERVED'].includes(field.state)
          && typeof field.basis === 'string' && !(field.state === 'UNKNOWN' && field.value !== null);
      })) return null;
    return data;
  }
  function validity(item) {
    const source = fixed(item), data = item?.source_validity;
    if (!source || !shape(data, validityKeys) || data.schema_version !== 'SOURCE_VALIDITY_V1'
      || !identityKeys.every(key => data[key] === source[key]
        && (key === 'standard_code' && data[key] === '' || text(data[key], 1000)))
      || !date(data.evaluated_at) || data.applicability_status !== 'NOT_REVIEWED'
      || !((data.standard_version_id === null && data.artifact_binding_id === null)
        || (uuid(data.standard_version_id) && uuid(data.artifact_binding_id)))
      || !Array.isArray(data.reasons) || data.reasons.length > 100
      || !data.reasons.every(reason => typeof reason === 'string' && /^[A-Z][A-Z0-9_]{0,99}$/.test(reason))
      || !Array.isArray(data.relations) || data.relations.length > 100) return null;
    const version = data.version_status;
    if (!shape(version, versionKeys) || !versionValues.includes(version.value)
      || !eventFields(version, data.evaluated_at) || !(version.effective_at === null || date(version.effective_at))
      || (version.verification_state !== 'VERIFIED' && version.value !== 'UNKNOWN')
      || (version.verification_state === 'VERIFIED' && data.standard_version_id === null)
      || (data.standard_version_id === null && (version.event_id !== null
        || version.effective_at !== null || data.relations.length > 0))
      || (['CURRENT', 'UPCOMING'].includes(version.value) && version.effective_at === null)
      || (version.value === 'CURRENT' && version.effective_at > data.evaluated_at)) return null;
    const ids = new Set();
    if (!data.relations.every(relation => {
      if (!shape(relation, relationKeys) || !uuid(relation.relation_id) || ids.has(relation.relation_id)
        || !['WHOLE_SUPERSEDES', 'AMENDS', 'PARTIAL_WITHDRAWAL'].includes(relation.relation_type)
        || relation.from_version_id !== data.standard_version_id || !uuid(relation.from_version_id)
        || !(relation.to_version_id === null || uuid(relation.to_version_id))
        || (relation.to_version_id === null && relation.relation_type !== 'PARTIAL_WITHDRAWAL')
        || relation.to_version_id === relation.from_version_id || !date(relation.effective_at)
        || !Array.isArray(relation.affected_clauses) || relation.affected_clauses.length > 1000
        || !relation.affected_clauses.every(clause => text(clause, 200))
        || (relation.relation_type === 'PARTIAL_WITHDRAWAL' && !relation.affected_clauses.length)
        || !eventFields(relation, data.evaluated_at)
        || !['NOT_EFFECTIVE', 'IN_EFFECT', 'UNRESOLVED'].includes(relation.effect_state)
        || (relation.verification_state !== 'VERIFIED' && relation.effect_state !== 'UNRESOLVED')
        || (relation.verification_state === 'VERIFIED' && relation.effect_state === 'UNRESOLVED')
        || (relation.effect_state === 'IN_EFFECT' && relation.effective_at > data.evaluated_at)
        || (relation.effect_state === 'NOT_EFFECTIVE' && relation.effective_at <= data.evaluated_at)) return false;
      ids.add(relation.relation_id); return true;
    })) return null;
    return data;
  }
  function category(item) {
    const level = fixed(item)?.fields.level;
    if (level?.state === 'CODE_CLUE') return ({NATIONAL:'国家编号线索', INDUSTRY:'行业编号线索', LOCAL:'地方编号线索'})[level.value] || '类别待核';
    if (level?.state === 'NOTICE_OBSERVED') return ({NATIONAL:'国家公告线索', INDUSTRY:'行业公告线索', LOCAL:'地方公告线索'})[level.value] || '类别待核';
    return '类别待核';
  }
  // 标签来自当前固定档案，颜色只表达线索类别，不代表法律效力。
  function badge(item) {
    const data=fixed(item),level=data?.fields.level;
    if(!data || !['CODE_CLUE','NOTICE_OBSERVED','SNAPSHOT_RECORDED'].includes(level?.state))
      return {label:'类别待核',tone:'neutral',note:'没有匹配当前快照的可靠类别记录'};
    const names={NATIONAL:['国家','national'],INDUSTRY:['行业','industry'],
      GROUP:['团体/企业','neutral'],ENTERPRISE:['团体/企业','neutral']};
    const value=level.value==='LOCAL'
      ? ['地方·'+(data.fields.region.value || '地域待核'),'local']
      : names[level.value] || ['类别待核','neutral'];
    return {label:value[0],tone:value[1],note:category(item)+'；地域取固定档案声明，适用性仍待核'};
  }
  function badgeElement(item) {
    const data=badge(item),node=element('span',data.label,'norm-badge norm-badge-'+data.tone);
    node.title=data.note;return node;
  }
  function element(tag, text, className) {
    const result = document.createElement(tag); result.textContent = text;
    if (className) result.className = className; return result;
  }
  const verificationNames = {UNVERIFIED:'核验待核', VERIFIED:'已有维护核验记录', EXPIRED:'核验已过期',
    REJECTED:'核验未通过', REVOKED:'核验已撤销', MANIFEST_CHANGED:'核验上下文已变化', CONFLICT:'核验记录存在冲突'};
  function appendValidity(item, wrap, detailed = false) {
    const data = validity(item);
    const row = content => {
      const result = element('p', content, 'source-validity-status'); result.title = content;
      // 关键状态可在窄屏换行，避免阅读头的旧省略样式隐藏待核提醒。
      result.setAttribute('style', 'white-space:normal;overflow-wrap:anywhere'); wrap.append(result);
    };
    if (!data) {row('截至本次核对的状态：待核'); return;}
    row(`截至本次核对的状态 · ${data.evaluated_at}（UTC）`);
    const version = data.version_status;
    const names = {UNKNOWN:'版本状态待核', UPCOMING:'版本核验记录标记待实施', CURRENT:'版本核验记录标记现行',
      SUPERSEDED:'版本记录标记被替代', WITHDRAWN:'版本核验记录标记已废止'};
    row(`${names[version.value]}${version.verification_state === 'VERIFIED' ? '' : ` · ${verificationNames[version.verification_state]}`}`);
    const unresolved = data.reasons.some(reason => reason.includes('RELATION')
      && (reason.endsWith('UNRESOLVED') || reason.endsWith('CONFLICT')))
      || data.relations.some(relation => relation.effect_state === 'UNRESOLVED'
        || (relation.effect_state === 'IN_EFFECT' && relation.relation_type !== 'WHOLE_SUPERSEDES'));
    if (unresolved) row('存在调整，正文状态仍待核');
    if (!detailed) return;
    row('版本记录不等于正文使用许可或工程适用性核准。');
    row(`版本核验状态：${verificationNames[version.verification_state]}`);
    row(`核验事件：${version.event_id || '无可用事件'} · 截至 ${version.checked_as_of || '待核'} · 有效至 ${version.valid_until || '待核'}`);
    row(`核验实施日期：${version.effective_at || '待核'}`);
    if (data.standard_version_id) row(`治理版本：${data.standard_version_id} · 正文绑定：${data.artifact_binding_id}`);
    const relationNames = {WHOLE_SUPERSEDES:'整版替代', AMENDS:'条款修订', PARTIAL_WITHDRAWAL:'部分条款废止'};
    const effectNames = {NOT_EFFECTIVE:'尚未生效', IN_EFFECT:'关系已生效', UNRESOLVED:'调整待核'};
    for (const relation of data.relations) {
      row(`${relationNames[relation.relation_type]}：${effectNames[relation.effect_state]} · 生效日期 ${relation.effective_at} · ${verificationNames[relation.verification_state]}`);
      const range = relation.affected_clauses.length ? relation.affected_clauses.join('、')
        : relation.relation_type === 'WHOLE_SUPERSEDES' ? '整版替代关系' : '未登记具体条款范围';
      row(`原登记范围：${range}`);
      row(`关系 ${relation.relation_id} · 原版本 ${relation.from_version_id} · 目标版本 ${relation.to_version_id || '未登记目标版本'}`);
      row(`关系核验事件：${relation.event_id || '无可用事件'} · 截至 ${relation.checked_as_of || '待核'} · 有效至 ${relation.valid_until || '待核'}`);
    }
    if (data.reasons.length) row(`状态原因：${data.reasons.join('、')}`);
  }
  function value(data, key, unknown) {return data.fields[key].value || unknown;}
  function editionText(data) {
    // 固定登记号和公告题名是出处线索，不能冒充经过核验的正式版本。
    const field = data.fields.edition;
    if (!field.value) return '正式版本待核';
    const label = field.state === 'NOTICE_OBSERVED' ? '登记题名' : '登记标识';
    return `${label}：${field.value} · 正式版本待核`;
  }
  function edition(item) {
    const data = fixed(item); return data ? editionText(data) : "正式版本待核";
  }
  function profile(item) {
    const wrap = element('div', '', 'reader-source-profile'), data = fixed(item);
    if (!data) {wrap.append(element('p', '来源档案待核；尚不能显示可靠的机关和日期。')); return wrap;}
    wrap.append(badgeElement(item));
    const rows = [
      `${category(item)} · ${value(data, 'region', '地域待核')} · 强条待核`,
      `${editionText(data)} · ${data.fields.effective_at.value ? `实施声明 ${data.fields.effective_at.value}` : '实施待核'} · ${value(data, 'withdrawal', '废止待核')}`,
      `${value(data, 'publisher', '发布机关待核')} · ${value(data, 'announcement_number', '文号待核')}`,
    ];
    for (const text of rows) {const row = element('p', text); row.title = text; wrap.append(row);}
    appendValidity(item, wrap);
    // 这一行属于固定阅读头，而不是折叠详情。短编号只供阅读，所有身份比较仍用全值。
    const identity = element('div', '', 'reader-source-identity'), source = safeUrl(data.source_url, item);
    if (source) {
      const link = element('a', `来源 ${new URL(source).hostname.replace(/^www\./, '')}`, 'reader-source-link');
      link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.title = `该固定正文对应的官方来源：${source}`;
      link.setAttribute('aria-label', link.title); identity.append(link);
    } else identity.append(element('span', '来源待核', 'reader-source-link'));
    const snapshot = element('span', `快照 ${data.snapshot_id}`, 'reader-source-snapshot');
    snapshot.title = `固定来源 ${data.source_id}；完整快照 ${data.snapshot_id}`;
    snapshot.setAttribute('data-source-id', data.source_id);
    snapshot.setAttribute('data-snapshot-id', data.snapshot_id);
    snapshot.setAttribute('aria-label', snapshot.title);
    identity.append(snapshot); wrap.append(identity);
    wrap.title = '编号类别与公告声明不是专业核验；完整身份可在来源详情中核对。';
    return wrap;
  }
  function httpsUrl(value) {
    if (typeof value !== 'string' || /[\x00-\x20\\]/.test(value) || value.length > 8192) return null;
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password && !url.hash
        && (!url.port || url.port === '443') ? url : null;
    } catch { return null; }
  }
  function reviewedSource(value, item, url) {
    const data = fixed(item), observation = data?.notice_observation, notice = observation?.notice;
    if (!data || !observation || !notice || !uuid(observation.run_id)
      || !digest(observation.run_sha256) || !digest(notice.text_sha256) || !digest(notice.review_id)
      || !/(?:^|\.)gov\.cn$/.test(url.hostname) || url.search
      || data.source_url !== value || notice.url !== value || notice.observed_url !== value
      || (item.source_url !== undefined && item.source_url !== value)
      || notice.transport_status !== 'HTTPS_OBSERVED' || notice.scope !== 'LOCAL'
      || notice.standard_title !== data.title || notice.standard_code !== data.standard_code) return false;
    const fields = data.fields, region = notice.region_components;
    // document_type 的观察标记由后台精确审核配对产生；这里不把 hash 的形状冒称独立审核。
    if (fields.level.state !== 'NOTICE_OBSERVED' || fields.level.value !== 'LOCAL'
      || fields.region.state !== 'NOTICE_OBSERVED' || !text(fields.region.value, 200)
      || notice.region !== fields.region.value || !region || !text(region.province, 100)
      || !(region.city === null || text(region.city, 100))
      || [region.province, region.city].filter(Boolean).join('·') !== notice.region
      || fields.document_type?.state !== 'NOTICE_OBSERVED'
      || !text(fields.document_type.value, 100) || notice.document_type !== fields.document_type.value
      || fields.edition.state !== 'NOTICE_OBSERVED' || fields.edition.value !== notice.standard_title
      || !text(notice.authority, 500)
      || (fields.publisher.value !== null && (fields.publisher.state !== 'NOTICE_OBSERVED'
        || fields.publisher.value !== notice.authority))) return false;
    const attachment = httpsUrl(data.attachment_url);
    return Boolean(attachment && !attachment.search && /(?:^|\.)gov\.cn$/.test(attachment.hostname)
      && Array.isArray(notice.attachments) && notice.attachments.length <= 10
      && notice.attachments.filter(entry => entry && entry.url === data.attachment_url).length === 1);
  }
  function safeUrl(value, item) {
    const url = httpsUrl(value);
    if (!url) return null;
    // 网页和PDF直发件共用实际原件身份；未知文号不替代完整SHA和固定档案。
    const data = fixed(item), observation = data?.notice_observation, notice = observation?.notice;
    const original = data && /(?:^|\.)gov\.cn$/.test(url.hostname)
      && uuid(observation?.run_id) && digest(observation?.run_sha256)
      && notice?.notice_type === 'OFFICIAL_COPY' && notice.text_sha256 === data.file_sha256
      && data.source_url === value && item.source_url === value && notice.url === value
      && data.attachment_url === value && notice.standard_title === data.title
      && notice.standard_code === data.standard_code && Array.isArray(notice.attachments)
      && notice.attachments.filter(entry => entry?.url === value).length === 1;
    if (original && notice.attachments.some(entry => entry?.url === value
      && (item?.source_format === 'HTML'
        ? entry.source_format === 'HTML' && entry.body_sha256 === data.file_sha256
        : entry.expected_original_sha256 === data.file_sha256))) return value;
    // 政府发布页与PDF附件是两个不同文件；公告正文哈希不能误要求等于PDF哈希。
    // 仍绑定同一固定档案、实读公告、主文件和同站唯一附件；不由域名推断现行性。
    const attachment = httpsUrl(data?.attachment_url);
    const publishedPdf = data && !url.search && /(?:^|\.)gov\.cn$/.test(url.hostname)
      && uuid(observation?.run_id) && digest(observation?.run_sha256) && digest(notice?.text_sha256)
      && notice.notice_type === 'OFFICIAL_COPY' && notice.url === value
      && data.source_url === value && item.source_url === value
      && notice.standard_title === data.title && notice.standard_code === data.standard_code
      && notice.scope === data.fields.level.value
      && (notice.scope !== 'LOCAL' || (data.fields.region.state === 'NOTICE_OBSERVED'
        && text(data.fields.region.value,200) && notice.region === data.fields.region.value))
      && attachment && !attachment.search && attachment.origin === url.origin
      && /\.pdf$/i.test(attachment.pathname) && Array.isArray(notice.attachments)
      && notice.attachments.filter(entry => entry?.url === data.attachment_url).length === 1;
    if (publishedPdf) return value;
    // 无固定档案时沿用原严格名单；新地方来源不能仅靠政府域名或 review_id 获准显示。
    if (['www.mohurd.gov.cn', 'mohurd.gov.cn'].includes(url.hostname)) return value;
    if (value === 'https://zjj.sm.gov.cn/xxgk/fgwj/jsbz/202209/t20220909_1827378.htm') return value;
    if (LOCAL_PUBLICATION_URLS.includes(value)) return value;
    return item && reviewedSource(value, item, url) ? value : null;
  }
  function details(item) {
    const data = fixed(item), wrap = element('div', '', 'fixed-source-details');
    if (!data) {wrap.append(element('p', '来源档案待核；未采用其他版本的来源字段。')); return wrap;}
    const names = {level:'来源类别', region:'地域', publisher:'发布机关', announcement_number:'公告文号',
      edition:'登记标识', effective_at:'实施日期声明', withdrawal:'整本废止状态'};
    for (const key of keys) {
      const displayed = key === 'edition' ? editionText(data) : `${names[key]}：${value(data, key, '待核')}`;
      wrap.append(element('p', `${displayed} · ${data.fields[key].basis}`));
    }
    appendValidity(item, wrap, true);
    wrap.append(element('p', `取得时间：${data.fetched_at || '待核'}`));
    wrap.append(element('p', `固定正文：${data.source_id} / ${data.snapshot_id}`));
    wrap.append(element('p', `正文记录指纹：${data.snapshot_fingerprint}`));
    wrap.append(element('p', `原文件指纹：${data.file_sha256}`));
    wrap.append(element('p', `来源档案编号：${data.metadata_id}`));
    if (data.notice_observation) wrap.append(element('p', `公告运行指纹：${data.notice_observation.run_sha256}`));
    const source = safeUrl(data.source_url, item);
    if (source) {
      const link = element('a', '查看该份正文对应的官方公告', 'source-link');
      link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; wrap.append(link);
    } else wrap.append(element('p', '没有可显示的已登记官方网址。'));
    return wrap;
  }
  globalThis.ArchSpecSourceMetadata = {fixed, validity, category, profile, details, safeUrl, badge, badgeElement, edition};
})();
