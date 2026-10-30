// 默认本地问答；明确勾选才走收费模型。出处始终沿用固定快照与原页。
(() => {
  const tableLabel=value=>value.startsWith('附表')?value:'表'+value;
  const queryInput = document.querySelector('#pending-query');
  const sourceSelect = document.querySelector('#pending-source');
  const start = document.querySelector('#official-qa-start');
  const panel = document.querySelector('#official-qa-panel');
  const state = document.querySelector('#official-qa-state');
  const modelOption = document.querySelector('#official-qa-model-enabled');
  const notice = document.querySelector('#official-qa-notice');
  const taskControls = document.querySelector('#official-qa-task-controls');
  const taskState = document.querySelector('#official-qa-task-state');
  const taskResume = document.querySelector('#official-qa-task-resume');
  const taskRefresh = document.querySelector('#official-qa-task-refresh');
  const taskEntry = document.querySelector('#official-qa-task-entry');
  const taskLocalRead = document.querySelector('#official-qa-local-read');
  const answer = document.querySelector('#official-qa-answer');
  const readingRecord = document.querySelector('#official-qa-record');
  const form = document.querySelector('#official-qa-conditions');
  const conditionPanel = document.querySelector('#official-qa-condition-options');
  const retry = document.querySelector('#official-qa-retry');
  const details = document.querySelector('#official-qa-details');
  const detailContent = document.querySelector('#official-qa-detail-content');
  const context = document.querySelector('#official-qa-context');
  const contextSummary = document.querySelector('#official-qa-context-summary');
  const contextItems = document.querySelector('#official-qa-context-items');
  const requirements = document.querySelector('#official-qa-requirements');
  const requirementItems = document.querySelector('#official-qa-requirement-items');
  const contextGroups = document.querySelector('#official-qa-context-groups');
  const contextGroupItems = document.querySelector('#official-qa-context-group-items');
  const projectCandidates = document.querySelector('#official-qa-project-candidates');
  const projectCandidateItems = document.querySelector('#official-qa-project-candidate-items');
  const projectCandidateNote = document.querySelector('#official-qa-project-candidate-note');
  const projectPreview = document.querySelector('#official-qa-project-preview');
  const provenance = document.querySelector('#official-qa-condition-provenance');
  const provenanceItems = document.querySelector('#official-qa-condition-provenance-items');
  if (!queryInput || !sourceSelect || !start || !panel || !state || !answer || !form || !retry || !details || !detailContent || !context || !contextSummary || !contextItems) return;

  const taskReadings=taskControls?globalThis.ArchSpecTaskReadings?.create(document,taskControls,{
    openPosition:(position,owner)=>globalThis.ArchspecPendingSearch?.openTaskPosition(position,owner),
  }):null;

  // 只绑定本页预先登记的有限控件；回答或模型不能新增条件字段。
  // 字段名到标签/容器仅作机械映射，服务器登记与HTML合同负责保持一致。
  const fields = [...document.querySelectorAll('#official-qa-conditions [data-condition-key]')].flatMap(input => {
    const key = input.getAttribute('data-condition-key');
    if (typeof key !== 'string' || !/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/.test(key)
        || !['INPUT', 'SELECT'].includes(input.tagName)) return [];
    const id = `official-qa-${key.replaceAll('_', '-')}`;
    const label = document.querySelector(`#${id}-label`), container = document.querySelector(`#${id}-field`);
    if (input.getAttribute('id') !== id || !label || !container) return [];
    return [{key, input, label, container}];
  }).filter((field, _, rows) => rows.filter(row => row.key === field.key).length === 1);
  // 原因放在对应输入框旁，不增加一整块说明；旧响应没有原因时保持原布局。
  fields.forEach(field => {
    field.reason = node('small', 'official-qa-excerpt-note', ''); field.reason.hidden = true;
    field.container?.append(field.reason);
  });
  // 常用值只来自页面原有输入提示；用户点选才填入，不推定项目事实或提交请求。
  fields.forEach(field => {
    const options = [...(field.input.list?.options || [])].map(row => row.value).filter(Boolean).slice(0, 8);
    if (!options.length) return;
    const wrapper = node('div','official-qa-condition-field','');
    wrapper.hidden = field.container.hidden;
    field.container.replaceWith(wrapper); wrapper.append(field.container); field.container.hidden = false;
    const choices = node('div','official-qa-condition-choices','');
    choices.setAttribute('role','group'); choices.setAttribute('aria-label',field.label.textContent+'常用输入');
    const controls = options.map(value => {
      const control = node('button','quiet-button',value); control.type='button';
      control.addEventListener('click',()=>{
        if (wrapper.hidden || form.hidden || panel.hidden) return;
        field.input.value=value;
        field.input.dispatchEvent(new Event('input',{bubbles:true}));
        field.input.focus({preventScroll:true});
      });
      choices.append(control); return control;
    });
    field.syncChoices=()=>controls.forEach(control=>control.setAttribute('aria-pressed',String(control.textContent===field.input.value.trim())));
    field.input.addEventListener('input',field.syncChoices); field.syncChoices();
    wrapper.append(choices); field.container=wrapper;
  });
  let generation = 0;
  let conditionRevision = 0;
  let projectId = document.querySelector('#search-project')?.value || '';
  // 资料预选尚未执行问答；不能因条件表已展开而把首次请求误作补问。
  let conditionPreview = false;
  const defaultRetryLabel = retry.textContent;
  let candidateLedger = new Map();
  const selectedConditions = new Set();
  const brief = (value, limit = 360) => typeof value === 'string' ? value.length > limit ? `${value.slice(0, limit)}…` : value : '';
  function node(tag, className, value) {
    const element = document.createElement(tag); element.className = className; element.textContent = value; return element;
  }
  // 任务收据只管理等待和操作身份；原文仍必须经过本轮原有出处核验。
  const taskStorageKey = 'archspec.official-qa.task.v1';
  const uuidReady = value => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const taskStates = ['CREATED', 'RUNNING', 'WAITING_CONDITIONS', 'WAITING_SOURCE', 'COMPLETED', 'INTERRUPTED', 'FAILED'];
  let currentTask = null, taskBinding = null, pendingOperation = null, inFlight = null;
  let restoreCandidate = null, sourceRestoreCancel = null, taskGetOwner = null;
  let needsTaskRefresh = false, recoveredModel = false, restoredConditionIds = [];
  const taskTrace = node('div','official-qa-task-trace','');
  function taskStorage(action, value) {
    // 浏览器禁用存储时继续本轮内存操作，不能改用私有问题或条件的其它持久副本。
    try {
      if (action === 'get') return globalThis.localStorage?.getItem(taskStorageKey);
      if (action === 'set') globalThis.localStorage?.setItem(taskStorageKey, JSON.stringify(value));
      else globalThis.localStorage?.removeItem(taskStorageKey);
    } catch (_) { /* 无存储能力保持兼容，不自动重试网络或模型。 */ }
  }
  function dropTask() {
    taskReadings?.clear();
    currentTask = null; taskBinding = null; pendingOperation = null; restoredConditionIds = [];
    restoreCandidate = null;sourceRestoreCancel?.();sourceRestoreCancel=null;taskGetOwner=null;
    needsTaskRefresh = false; recoveredModel = false; taskStorage('remove');
    if (taskControls) taskControls.hidden = true;
    if (taskState) taskState.textContent = '';
    if (taskLocalRead) taskLocalRead.hidden = true;
    if (taskEntry) taskEntry.hidden = true;
    taskTrace.replaceChildren();
  }
  function taskReady(value, expected) {
    const token = text => typeof text === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(text);
    if (!value || value.version !== 'QA_TASK_V1' || !uuidReady(value.id) || value.id !== expected.id
        || value.project_id !== (expected.project || null) || !['LOCAL', 'MODEL'].includes(value.mode)
        || (expected.mode && value.mode !== expected.mode) || !taskStates.includes(value.state)
        || value.condition_mode!==undefined&&!['NONE','OPTIONAL','REQUIRED','UNVERIFIED'].includes(value.condition_mode)
        || !Number.isSafeInteger(value.revision) || value.revision < (expected.revision || 0) || value.revision > 9999999999
        || !Number.isInteger(value.retry_count) || value.retry_count < 0 || value.retry_count > 2 || !token(value.current_step)
        || !Array.isArray(value.question_keys) || value.question_keys.length > 3
        || new Set(value.question_keys).size !== value.question_keys.length
        || value.question_keys.some(key => !fields.some(field => field.key === key))
        || !Array.isArray(value.questions) || value.questions.length !== value.question_keys.length
        || value.questions.some((row, index) => !row || row.key !== value.question_keys[index]
          || typeof row.label !== 'string' || !row.label.trim() || row.label.length > 100
          || (row.reason !== undefined && (typeof row.reason !== 'string' || row.reason.length > 500)))
        || !Array.isArray(value.trace) || value.trace.length > 20
        || value.trace.some(row => !row || !token(row.step) || !token(row.state)
          || (row.error_code !== undefined && !token(row.error_code)))) return null;
    if (expected.operation && (!value.operation || value.operation.id !== expected.operation
        || !taskStates.includes(value.operation.state) || typeof value.operation.replayed !== 'boolean')) return null;
    // 只接有限公共元信息，不接凭据、旧回答、提案或任何资料原说明。
    return {id:value.id, version:value.version, project_id:value.project_id, mode:value.mode, state:value.state,
      revision:value.revision, retry_count:value.retry_count, current_step:value.current_step,
      condition_mode:value.condition_mode||'UNVERIFIED',
      question_keys:[...value.question_keys], questions:value.questions.map(row => ({key:row.key,label:row.label,reason:row.reason})),
      trace:value.trace.map(row => ({step:row.step,state:row.state,error_code:row.error_code})),
      ...(expected.operation ? {operation:{id:value.operation.id,state:value.operation.state,replayed:value.operation.replayed}} : {})};
  }
  const hasRestoreExpectation = value => value?.expected_query!==undefined || value?.expected_scope!==undefined;
  function showTask() {
    if (!taskControls || !taskState) return;
    // 未核回状态只保留身份，不显示可执行的旧操作或失效出处控件。
    if(needsTaskRefresh){
      taskControls.hidden=false;taskReadings?.clear();
      taskState.textContent='本题状态暂未核回，原任务身份已保留。';
      if(taskResume){taskResume.hidden=true;taskResume.disabled=true;}
      if(taskLocalRead)taskLocalRead.hidden=true;
      return;
    }
    if(!currentTask)return;
    if(taskEntry)taskEntry.hidden=true;
    taskControls.hidden = false;
    const readingTask=currentTask,readingQuery=taskBinding?.query,readingScope=taskBinding?.scope;
    if(inFlight)taskReadings?.clear();
    else taskReadings?.set(readingTask,{query:readingQuery,scope:readingScope,scopeLabel:sourceSelect.selectedOptions?.[0]?.textContent||readingScope,
      projectLabel:document.querySelector('#search-project')?.selectedOptions?.[0]?.textContent||readingTask.project_id||'',isCurrent:()=>currentTask===readingTask&&!needsTaskRefresh&&!inFlight
      &&readingQuery===queryInput.value.trim()&&readingScope===(sourceSelect.value||'__all__')
      &&readingTask.project_id===(projectId||null)});
    const messages = {CREATED:'本题已登记，等待处理。', RUNNING:'本题正在处理，请刷新状态查看进度。',
      WAITING_CONDITIONS:currentTask.condition_mode==='OPTIONAL'?'本题已保存，可先阅读条文；需要时再补充项目情况。':'本题已保存，请补充必要条件后继续查找。', WAITING_SOURCE:'本题等待可用正文，可准备后继续。',
      COMPLETED:'本轮整理已完成；工程适用性仍需核验。', INTERRUPTED:'上次处理已中断，可明确继续；不会自动执行。',
      FAILED:'上次处理未完成，可明确继续；不会自动重试。'};
    const exhausted = ['INTERRUPTED','FAILED'].includes(currentTask.state) && currentTask.retry_count >= 2;
    taskState.textContent = exhausted ? '本题已达到继续尝试上限，请核对失败步骤；不会自动重试。' : needsTaskRefresh ? '本题状态已变化，请先刷新状态再继续。'
      : recoveredModel && !modelOption?.checked ? '已恢复模型题；继续前请重新勾选本轮发送同意。' : messages[currentTask.state];
    if (taskResume) {
      // 已有补充入口时避免两个‘继续’按钮并列；模型恢复仍保留原发送同意流程。
      taskResume.hidden = currentTask.state === 'WAITING_CONDITIONS'
        && currentTask.questions.length > 0 && !recoveredModel;
      taskResume.disabled = needsTaskRefresh || ['CREATED','RUNNING'].includes(currentTask.state)
        || (['INTERRUPTED','FAILED'].includes(currentTask.state) && currentTask.retry_count >= 2);
    }
    if (taskRefresh) taskRefresh.disabled = false;
    if (taskLocalRead) {
      taskLocalRead.hidden = !recoveredModel || currentTask.mode !== 'MODEL';
      taskLocalRead.disabled = needsTaskRefresh || ['CREATED','RUNNING'].includes(currentTask.state) || !!inFlight;
    }
    const stepNames = {QA_EXECUTE:'整理本题', MODEL_UNDERSTAND:'理解问题', MODEL_SELECT:'选择证据',
      CONDITIONS:'核对补充条件', QA_CONDITIONS:'补充条件性质', CONDITION_CHECK:'核对条件', RETRIEVE:'检索出处', READ_FIXED:'读取固定原文'};
    const stateNames = {RUNNING:'处理中', DONE:'已完成', REUSED:'沿用已验证理解', FAILED:'未完成', WAITING:'等待补充',OPTIONAL:'选填',REQUIRED:'必填',NONE:'无需补充'};
    taskTrace.replaceChildren();
    for (const step of currentTask.trace) taskTrace.append(node('p','official-qa-step',
      `${stepNames[step.step] || '处理本题'}：${stateNames[step.state] || '状态已记录'}`));
    if (!Array.from(detailContent.children).includes(taskTrace)) detailContent.append(taskTrace);
    if (taskTrace.children.length) details.hidden = false;
  }
  function currentTaskSummary(){return currentTask&&taskBinding?{id:currentTask.id,project_id:currentTask.project_id,
    query:taskBinding.query,scope:taskBinding.scope,mode:currentTask.mode,state:currentTask.state,revision:currentTask.revision}:null;}
  function bindTask(task, query, scope, mode) {
    currentTask = task; taskBinding = {query, scope, mode}; pendingOperation = null; needsTaskRefresh = false;
    taskStorage('set', {id:task.id, project_id:task.project_id}); showTask();
    if(typeof CustomEvent==='function')document.dispatchEvent?.(new CustomEvent('archspec:qa-task-updated',{detail:currentTaskSummary()}));
  }
  function newOperationId() {
    try {
      const id = globalThis.crypto?.randomUUID?.();
      return uuidReady(id) ? id : null;
    } catch (_) {return null;}
  }
  function nextOperation(signature, initialId) {
    if (pendingOperation?.signature === signature) return pendingOperation;
    const id = initialId || newOperationId();
    if (!uuidReady(id)) return null;
    pendingOperation = {id, taskId:currentTask?.id || id, revision:currentTask?.revision || 0, signature};
    taskStorage('set', {id:pendingOperation.taskId, project_id:projectId || null});
    return pendingOperation;
  }
  function restoreRequest(raw, expectedProject) {
    const allowed = ['query','project_id','source_ids','conditions','project_condition_ids'];
    if (!raw || Object.keys(raw).some(key => !allowed.includes(key)) || typeof raw.query !== 'string'
        || !raw.query.trim() || raw.query.length > 500 || (raw.project_id || null) !== (expectedProject || null)
        || !raw.conditions || Array.isArray(raw.conditions) || typeof raw.conditions !== 'object'
        || Object.entries(raw.conditions).some(([key,value]) => !fields.some(field => field.key === key)
          || (value !== null && (typeof value !== 'string' || !value.trim() || value.length > 100)))
        || (raw.source_ids != null && (!Array.isArray(raw.source_ids) || !raw.source_ids.length || raw.source_ids.length>20 || new Set(raw.source_ids).size!==raw.source_ids.length
          || raw.source_ids.some(id=>!/^(gb55031|official-[a-f0-9]{32})$/.test(id))))
        || (raw.project_condition_ids != null && (!expectedProject || !Array.isArray(raw.project_condition_ids)
          || raw.project_condition_ids.length > 60 || new Set(raw.project_condition_ids).size !== raw.project_condition_ids.length
          || raw.project_condition_ids.some(id => typeof id !== 'string' || !/^pc_[a-f0-9]{64}$/.test(id))))) return null;
    return {query:raw.query.trim(), scope:globalThis.ArchSpecSourceScope?.encode(raw.source_ids)||raw.source_ids?.[0]||'__all__', conditions:raw.conditions,
      ids:raw.project_condition_ids || []};
  }
  async function sourceScopeReady(scope, isCurrent) {
    // 单本来源只能来自已加载的本页清单；等待已有DOM补齐，不造选项、不另查来源。
    const ready = () => scope.startsWith('set:')?globalThis.ArchSpecSourceScope?.ensure(document,scope)===true
      :!sourceSelect.options||Array.from(sourceSelect.options).some(option=>option.value===scope);
    if (ready()) return true;
    if (typeof globalThis.MutationObserver !== 'function' || typeof globalThis.setTimeout !== 'function') return false;
    return new Promise(resolve => {
      let done = false, timer;
      const finish = value => {
        if(done) return;done=true;observer.disconnect();globalThis.clearTimeout?.(timer);
        if(sourceRestoreCancel === cancel) sourceRestoreCancel=null;resolve(value);
      };
      const cancel = () => finish(false);
      const observer = new globalThis.MutationObserver(() => {
        if (!isCurrent()) finish(false);else if(ready()) finish(true);
      });
      sourceRestoreCancel=cancel;observer.observe(sourceSelect,{childList:true});
      timer=globalThis.setTimeout(() => finish(false),5000);
      if(ready()) finish(isCurrent());
    });
  }
  async function refreshTask(saved) {
    const id = saved?.id || currentTask?.id, owner = projectId;
    let own = generation, definitive=false, readStatus=null;
    if (!uuidReady(id)) {dropTask();return;}
    if (saved && saved.project_id !== (owner || null)) {
      // 刷新后项目尚未选择时只保留ID；首次选到其原项目才允许GET，其它操作失效。
      if (!owner && saved.project_id) {
        restoreCandidate={id:saved.id,project_id:saved.project_id};
        if(taskControls) taskControls.hidden=false;
        if(taskState) taskState.textContent='上次问答属于项目；选择原项目后可恢复。';
        if(taskResume) taskResume.hidden=true;
        panel.hidden=false;return;
      }
      dropTask();return;
    }
    if(taskGetOwner===own) return;
    taskGetOwner=own;
    const originalQuery=queryInput.value.trim(), originalScope=sourceSelect.value || '__all__';
    // 查询流程也会恢复同一输入；允许同题协作，但迟到的另一历史题不能覆盖已经恢复的题目。
    const sharedInputCurrent=request=>[originalQuery,request.query].includes(queryInput.value.trim())
      && [originalScope,request.scope].includes(sourceSelect.value || '__all__');
    if (taskRefresh) taskRefresh.disabled = true;
    try {
      // GET仅恢复原输入与安全等待状态；无正文、同意、下载或自动POST。
      const url = `/api/official-qa/tasks/${id}${owner ? '?project_id='+encodeURIComponent(owner) : ''}`;
      const response = await fetch(url, {method:'GET', headers:{'Accept':'application/json'}, cache:'no-store'});
      readStatus=response.status;definitive=[403,404].includes(readStatus);
      const body = await globalThis.ArchSpecHTTP.json(response, "出处整理响应暂不可读取");
      if (own !== generation || owner !== projectId) return;
      if(!response.ok)throw Error('本题状态读取暂不可用'+(Number.isInteger(readStatus)?'（HTTP '+readStatus+'）':''));
      definitive=true; // 可读但错绑的响应仍按原身份守卫拒绝，不能采用。
      const task = taskReady(body.qa_task, {id,project:owner,mode:currentTask?.mode,revision:currentTask?.revision || 0});
      const request = restoreRequest(body.request, owner);
      if (!response.ok || !task || !request || (taskBinding && (taskBinding.query !== request.query || taskBinding.scope !== request.scope)))
        throw Error('等待记录暂不可恢复，请重新整理本题。');
      // 导入的清单摘要也必须与服务端原问、原范围一致，不能只凭任务ID恢复。
      if ((saved?.expected_query !== undefined && request.query !== saved.expected_query)
          || (saved?.expected_scope !== undefined && request.scope !== saved.expected_scope))
        throw Error('清单中的问题或范围与原任务不一致，请重新核对。');
      if(!sharedInputCurrent(request)) throw Error('等待记录与当前问题或范围不同，未覆盖当前输入，请重新整理。');
      definitive=false;
      const ready = await sourceScopeReady(request.scope,() => own===generation && owner===projectId);
      if(own!==generation || owner!==projectId) return;
      if(!sharedInputCurrent(request)){definitive=true;throw Error('等待记录与当前问题或范围不同，未覆盖当前输入，请重新整理。');}
      if(!ready) throw Error('原检索范围尚未就绪，请刷新页面后再恢复。');
      restoreCandidate=null;
      own = ++generation; clearOutput(); clearProjectChoices();
      queryInput.value = request.query; sourceSelect.value = request.scope;globalThis.ArchSpecSourceScope?.choose(document,request.scope);
      if (sourceSelect.value !== request.scope) throw Error('原检索范围已变化，请重新选择。');
      fields.forEach(field => {field.input.value = request.conditions[field.key] || '';field.container.hidden = !task.question_keys.includes(field.key);});
      restoredConditionIds = [...request.ids];
      if (modelOption) modelOption.checked = false;
      recoveredModel = task.mode === 'MODEL';
      bindTask(task, request.query, request.scope, task.mode);
      panel.hidden = false; start.setAttribute('aria-expanded','true');
      setConditionsHidden(!task.questions.length,{required:task.state==='WAITING_CONDITIONS'&&task.condition_mode!=='OPTIONAL',count:task.questions.length});
      for (const question of task.questions) {
        const field = fields.find(row => row.key === question.key);field.label.textContent = brief(question.label,80);
        field.reason.textContent = brief(question.reason,140);field.reason.hidden = !field.reason.textContent;field.syncChoices?.();
      }
      // 恢复提示沿服务端条件性质，不能把必填题一概说成可选补充。
      state.textContent = task.condition_mode==='OPTIONAL'
        ? '已恢复上次问题；可先阅读条文，需要时再补充项目情况。'
        : task.state==='WAITING_CONDITIONS' ? '已恢复原问题，请补充必要条件后继续查找。'
        : '已恢复原输入与等待状态；继续后重新核对当前出处。';
    } catch (error) {
      if (own === generation && owner === projectId) {
        clearOutput();setConditionsHidden(true);if(modelOption)modelOption.checked=false;
        if(definitive){dropTask();state.textContent=brief(error.message,180);}
        else{
          const reference={id,project_id:owner||null};
          // 原清单链接尚未核对时只保留本页的匹配约束，不把它写成已确认的恢复身份。
          restoreCandidate={...reference,...(saved?.expected_query!==undefined?{expected_query:saved.expected_query}:{}),...(saved?.expected_scope!==undefined?{expected_scope:saved.expected_scope}:{})};
          if(!hasRestoreExpectation(saved))taskStorage('set',reference);
          needsTaskRefresh=true;panel.hidden=false;start.setAttribute('aria-expanded','true');showTask();
          if(taskEntry)taskEntry.hidden=readStatus!==401;
          state.textContent=readStatus===401?(hasRestoreExpectation(saved)?'请重新登录后从原问题清单再打开本题；未重新执行问答。':'请重新登录后刷新状态；未重新执行问答。'):'请稍后刷新状态；未重新执行问答。';
          detailContent.append(node('p','official-qa-gap',brief(error.message,180)));details.hidden=false;
        }
      }
    } finally {
      if(taskGetOwner===own || taskGetOwner===own-1) taskGetOwner=null;
      if (own===generation && owner===projectId && taskRefresh) taskRefresh.disabled = false;
    }
  }
  function setConditionsHidden(hidden, {required=false, count=0}={}) {
    // 折叠只改变展示，不清空条件，也不改变提交、权限或刷新合同。
    form.hidden = hidden;
    retry.textContent=required?'补充条件后再查':'按这些条件整理';
    if (conditionPanel) {
      conditionPanel.hidden = hidden;
      const summary=document.querySelector('#official-qa-condition-summary');
      if(summary) summary.textContent=required ? `补充${count || 1}项条件后继续整理` : '按项目情况缩小范围（选填）';
      const help=document.querySelector('#official-qa-condition-help');
      if(help) help.textContent=required ? '请选择已确认的情况，也可直接手填；不清楚请说明未知。补充后点击“补充条件后再查”，不会因点选而自动联网或调用模型。下方候选条文仍可先阅读。' : '只查通用条文可直接往下阅读；需要按项目情况核对时再填写。';
      if (hidden) conditionPanel.open = false;
      else if(required) conditionPanel.open = true;
    }
  }
  function clearOutput() {
    if(notice) notice.textContent='本地整理 · 出处待核验';
    fields.forEach(field => {field.reason.textContent = ''; field.reason.hidden = true;});
    answer.replaceChildren(); readingRecord?.replaceChildren(); detailContent.replaceChildren(); contextItems.replaceChildren();
    contextGroupItems?.replaceChildren(); requirementItems?.replaceChildren();
    if (requirements) {requirements.hidden = true; requirements.open = false;}
    if (contextGroups) {contextGroups.hidden = true; contextGroups.open = false;}
    details.hidden = true; details.open = false; context.hidden = true; context.open = false; state.textContent = '';
    provenanceItems?.replaceChildren();
    if (provenance) {provenance.hidden = true; provenance.open = false;}
  }
  function clearProjectChoices() {
    conditionPreview=false;retry.textContent=defaultRetryLabel;
    candidateLedger = new Map(); selectedConditions.clear(); projectCandidateItems?.replaceChildren();
    if (projectCandidates) projectCandidates.hidden = true;
  }
  function preserveOutput() {
    // 自动更新必须整体成功才替换旧答案。保存原节点，恢复时原页按钮仍有原来的事件与快照。
    const containers = [answer, readingRecord, detailContent, contextItems, contextGroupItems, requirementItems, provenanceItems, projectCandidateItems]
      .filter(Boolean).map(element => ({element, children: [...(element.childNodes || element.children)]}));
    const visibility = [form, details, context, contextGroups, requirements, provenance, projectCandidates, ...fields.map(field => field.container)]
      .filter(Boolean).map(element => ({element, hidden: element.hidden, open: element.open}));
    const labels = [notice, contextSummary, projectCandidateNote, ...fields.flatMap(field => [field.label, field.reason])]
      .filter(Boolean).map(element => ({element, text: element.textContent, hidden: element.hidden}));
    const ledger = new Map(candidateLedger), selected = [...selectedConditions];
    return () => {
      containers.forEach(({element, children}) => element.replaceChildren(...children));
      visibility.forEach(({element, hidden, open}) => {element.hidden = hidden; element.open = open;});
      labels.forEach(({element, text, hidden}) => {element.textContent = text; element.hidden = hidden;});
      candidateLedger = ledger; selectedConditions.clear(); selected.forEach(id => selectedConditions.add(id));
    };
  }
  function invalidate({hide = true, resetConditions = true, resetTask = true} = {}) {
    generation += 1; clearOutput();
    if (resetTask) dropTask();
    start.disabled = false; retry.disabled = false;
    if(projectPreview)projectPreview.disabled=false;
    if (hide) {panel.hidden = true; setConditionsHidden(true); start.setAttribute('aria-expanded', 'false');}
    if (resetConditions) {clearProjectChoices(); fields.forEach(field => {if (field.input) field.input.value = '';});}
  }
  function projectConditionReady(row, requestProject) {
    return !!requestProject && row?.project_id === requestProject && row.kind === 'PROJECT_CONDITION'
      && typeof row.id === 'string' && /^pc_[a-f0-9]{64}$/.test(row.id)
      && fields.some(field => field.key === row.field)
      && ['PROJECT_RECORD','PROJECT_MATERIAL'].includes(row.source_kind)
      && typeof row.value === 'string' && !!row.value.trim() && row.value.length <= 100
      && typeof row.label === 'string' && !!row.label.trim()
      && typeof row.source_name === 'string' && typeof row.source_text === 'string';
  }
  function projectExcerpt(row) {
    const fold = node('details', 'official-qa-project-excerpt', ''); fold.open = false;
    fold.append(node('summary', '', `来源：${brief(row.source_name,80)}${Number.isInteger(row.page_start)?` · 文件第${row.page_start}页`:''}`));
    fold.append(node('p', '', brief(row.source_text,240)));
    return fold;
  }
  function renderProjectChoices(packet, query, requestProject, sourceId) {
    if (!projectCandidates || !projectCandidateItems) return;
    projectCandidateItems.replaceChildren(); candidateLedger = new Map();
    const rows = packet?.project_id === requestProject && Array.isArray(packet.candidates) ? packet.candidates.slice(0,60) : [];
    const allowed = rows.filter(row => projectConditionReady(row,requestProject)
      && row.status === 'NEEDS_CONFIRMATION' && rows.filter(item=>item?.id===row.id).length===1);
    for (const id of selectedConditions) if (!allowed.some(row=>row.id===id)) selectedConditions.delete(id);
    for (const row of allowed) {
      const card = node('div', 'official-qa-project-choice', '');
      const label = node('label', 'official-qa-project-choice-label', '');
      const box = node('input','',''); box.type = 'checkbox'; box.checked = selectedConditions.has(row.id);
      box.setAttribute('data-condition-id',row.id);
      candidateLedger.set(row.id,{row,box});
      label.append(box,node('span','',`${brief(row.label,40)}：${row.value}`));
      card.append(label,projectExcerpt(row)); projectCandidateItems.append(card);
      box.addEventListener('change',()=>{
        // 控件身份与项目/问题一起失效；手填修改不丢掉同一问题下的用户勾选。
        if(candidateLedger.get(row.id)?.box!==box || query!==queryInput.value.trim()
          || requestProject!==projectId || sourceId!==(sourceSelect.value || '__all__')) return;
        conditionRevision += 1;
        if(box.checked) {
          for(const [id, entry] of candidateLedger) if(entry.row.field===row.field && id!==row.id) {
            selectedConditions.delete(id);entry.box.checked=false;
          }
          selectedConditions.add(row.id);
        } else selectedConditions.delete(row.id);
        // 这是用户主动修改，不是渲染清理；旧请求不能继续显示旧选择的回答或锁住按钮。
        invalidate({hide:false,resetConditions:false,resetTask:false});
        state.textContent='条件已修改，可重新整理。';
      });
    }
    projectCandidates.hidden = !allowed.length;
    if(projectCandidateNote) projectCandidateNote.textContent = allowed.length && packet.conflicts?.length
      ? '同一条件存在不同值，请核对来源并每项选择一个；手填值优先。'
      : '勾选本次采用的条件；也可以在下方手填，手填值优先。';
  }
  function renderProvenance(body, requestProject) {
    if (!provenance || !provenanceItems) return;
    for(const row of (Array.isArray(body.condition_provenance)?body.condition_provenance:[]).slice(0,60)) {
      if(!projectConditionReady(row,requestProject)) continue;
      const item=node('div','official-qa-project-choice','');
      item.append(node('strong','',`${brief(row.label,40)}：${row.value}`),projectExcerpt(row));
      provenanceItems.append(item);
    }
    provenance.hidden=!provenanceItems.children.length;
  }
  function renderRequirementDetails(body) {
    if (!requirements || !requirementItems || !['REFERENCE_BRIEF', 'READING_ONLY', 'NO_EVIDENCE'].includes(body.status)
        || body.sufficiency?.evidence_state === 'CHECK_FAILED' || body.sufficiency?.condition_state === 'CHECK_FAILED') return [];
    const rows = Array.isArray(body.requirement_details) ? body.requirement_details.slice(0, 64) : [];
    const kinds = ['CORE_BODY', 'TABLE', 'EXPLICIT_REFERENCE', 'CONDITION_EXCEPTION', 'NAMED_RULE'];
    const states = ['UNRESOLVED', 'NOT_RETRIEVED', 'MISSING_SOURCE', 'PARTIAL', 'BUDGET_TRUNCATED',
      'BUDGET_EXCLUDED', 'NOT_LOCATED', 'DENIED', 'ORIGINAL_REQUIRED', 'LIMIT_REACHED'];
    const evidence = (Array.isArray(body.evidence) ? body.evidence : []).slice(0, 8);
    function sourceReady(source) {
      if (!source || typeof source.source_id !== 'string' || !/^(gb55031|official-[a-f0-9]{32})$/.test(source.source_id)
          || typeof source.snapshot_id !== 'string' || !/^[a-f0-9]{64}$/.test(source.snapshot_id)
          || typeof source.title !== 'string' || !source.title.trim() || source.title.length > 300
          || typeof source.standard_code !== 'string' || source.standard_code.length > 100) return false;
      const admitted = evidence.filter(row => citationReady(row) && row.source_id === source.source_id && row.snapshot_id === source.snapshot_id);
      // 缺口来源可能被最终预算整本排除，不能以未入ledger将真实需求隐藏。
      // 若本次已有对应固定证据，公共名称不得与它冲突；此处仍只显示来源文字。
      return !admitted.some(row => row.title !== source.title || (row.standard_code || '') !== source.standard_code);
    }
    const accepted = [];
    for (const row of rows) {
      if (!row || typeof row.requirement_id !== 'string' || !row.requirement_id || row.requirement_id.length > 512
          || rows.filter(value => value?.requirement_id === row.requirement_id).length !== 1
          || !kinds.includes(row.kind) || !['ANSWER_REQUIRED', 'READ_CONTEXT'].includes(row.necessity) || !states.includes(row.state)
          || typeof row.subject !== 'string' || typeof row.label !== 'string' || !row.label.trim()
          || typeof row.message !== 'string' || !row.message.trim() || row.label.length > 1000 || row.message.length > 2000
          || !Array.isArray(row.sources) || row.sources.length > 8 || !row.sources.every(sourceReady)) continue;
      const declared = Array.isArray(body.evidence_requirements) && body.evidence_requirements.length <= 64
        ? body.evidence_requirements.filter(value => value?.requirement_id === row.requirement_id
          && value.kind === row.kind && value.necessity === row.necessity && value.subject === row.subject
          && value.state === row.state) : [];
      // 无可用正文时仅显示原问逐号需求的共同缺口，不放行旧来源缺证或推导引用。
      if (body.status === 'NO_EVIDENCE' && (row.kind !== 'EXPLICIT_REFERENCE' || row.necessity !== 'ANSWER_REQUIRED'
          || declared.length !== 1 || declared[0].trigger !== 'BEFORE_SEARCH'
          || declared[0].completeness !== 'FIXED_EXPLICIT_CLAUSE')) continue;
      const item = node('section', 'official-qa-context-item', '');
      item.setAttribute('data-requirement-id', row.requirement_id);
      item.append(node('strong', '', brief(row.label, 180)), node('p', 'official-qa-gap', brief(row.message, 360)));
      // 锚/目标只取共同需求合同的固定字段，并与本轮同来源、同快照的已纳入锚条交叉核对。
      // 缺字段时只保留缺口，不解析subject或requirement_id拼造关联，也不为未纳入目标造按钮。
      if (body.status !== 'NO_EVIDENCE' && row.kind === 'EXPLICIT_REFERENCE' && declared.length === 1
          && Array.isArray(declared[0].fixed_targets) && declared[0].fixed_targets.length <= 8) {
        const seen = new Set();
        for (const target of declared[0].fixed_targets) {
          if (!target || !['OUTGOING','INCOMING'].includes(target.direction)
              || !/^\d+(?:\.\d+){2,3}$/.test(target.anchor_unit_key || '')
              || !/^\d+(?:\.\d+){2,3}$/.test(target.unit_key || '')
              || target.unit_key !== row.subject || target.unit_key === target.anchor_unit_key
              || !row.sources.some(source => source.source_id === target.source_id && source.snapshot_id === target.snapshot_id)
              || !evidence.some(anchor => citationReady(anchor) && anchor.quote_ready === true && anchor.role === 'MATCH'
                && anchor.source_id === target.source_id && anchor.snapshot_id === target.snapshot_id
                && anchor.clause_number === target.anchor_unit_key)) continue;
          const label = target.direction === 'OUTGOING'
            ? target.anchor_unit_key + ' → ' + target.unit_key : target.unit_key + ' → ' + target.anchor_unit_key;
          const key = target.source_id + ':' + target.snapshot_id + ':' + label;
          if (seen.has(key)) continue; seen.add(key);
          item.append(node('p','official-qa-excerpt-note official-qa-reference-target',label+'（同一固定来源的明确引用，目标尚未完整纳入）'));
        }
      }
      for (const source of row.sources) {
        const line = node('p', 'official-qa-excerpt-note', `${source.standard_code || '编号未登记'} · ${source.title} · 来源 ${source.source_id.slice(-8)} / 快照 ${source.snapshot_id.slice(0, 10)}`);
        line.setAttribute('data-source-id', source.source_id); line.setAttribute('data-snapshot-id', source.snapshot_id); item.append(line);
      }
      // 不为未取得的目标制造引用或原页入口；已有原页控件仍由原证据核验路径提供。
      requirementItems.append(item); accepted.push(row);
    }
    requirements.hidden = !requirementItems.children.length;
    return accepted;
  }
  function checkedSufficiency(body) {
    const value = body.sufficiency;
    if (!value || value.version !== 'ANSWER_SUFFICIENCY_V1' || value.applicability_state !== 'NOT_VERIFIED'
        || !['COMPLETE','INCOMPLETE','NOT_EVALUATED'].includes(value.evidence_state)
        || !['NEEDS_DETAILS','DETAILS_RECORDED','NOT_REQUESTED'].includes(value.condition_state)
        || !Array.isArray(value.unresolved_requirement_ids) || value.unresolved_requirement_ids.length > 64
        || value.unresolved_requirement_ids.some(id => typeof id !== 'string' || !id || id.length > 512)
        || new Set(value.unresolved_requirement_ids).size !== value.unresolved_requirement_ids.length) return null;
    return value;
  }
  function renderSufficiency(body, value = checkedSufficiency(body)) {
    if (!['REFERENCE_BRIEF', 'READING_ONLY'].includes(body.status) || !value) return;
    const evidenceText = {COMPLETE: '本次已登记需求的原文已读齐', INCOMPLETE: '所需原文尚未齐备', NOT_EVALUATED: '原文覆盖尚未评估'};
    const conditionText = {NEEDS_DETAILS: '仍需补充工程条件', DETAILS_RECORDED: '工程条件已记录', NOT_REQUESTED: '本次未要求工程条件'};
    answer.append(node('small', 'official-qa-excerpt-note official-qa-sufficiency',
      evidenceText[value.evidence_state]+'；'+conditionText[value.condition_state]+'。工程适用性尚未核验。'));
  }
  function resultStatus(body, {sufficiency, unmet = [], visibleQuestions = [], compactPreviewCount = null} = {}) {
    if (!['REFERENCE_BRIEF','READING_ONLY'].includes(body.status))
      return brief(body.summary, 240) || (body.status === 'NEEDS_CONDITIONS' ? '请补充本次缺少的条件。' : '没有可用的完整出处，请核对资料范围。');
    const items = Array.isArray(body.answer_items) ? body.answer_items : [];
    const evidence = Array.isArray(body.evidence) ? body.evidence : [];
    const warnings = [...evidence,...items.map(item => item.citation)].flatMap(item => Array.isArray(item?.warnings) ? item.warnings : []);
    const gaps = Array.isArray(body.gaps) ? body.gaps.filter(value => typeof value === 'string') : [];
    const ocr = warnings.includes('OCR_NUMBERED_ITEM_GAP') ? '条目编号不连续，请对照原页中的缺失条目。'
      : warnings.includes('LOW_CONFIDENCE_OCR') ? '识别文字待核对：数字、单位、否定词及适用条件请对照原页。'
      : warnings.includes('OCR_GAP_RECOVERED_NEEDS_REVIEW') ? '局部补识别已完成，文字待核对；可直接打开对应原页。' : '';
    // 充分性、识别风险和补条件共同决定一句状态；后续分支不能再覆盖已经确认的证据缺口。
    let primary;
    if (sufficiency?.evidence_state === 'INCOMPLETE')
      primary = '证据缺口：所需原文尚未齐备'+(unmet.length ? '（'+brief(unmet[0].label,80)+'）' : '');
    else if (sufficiency?.evidence_state === 'NOT_EVALUATED') primary = '原文覆盖尚未评估';
    else if (sufficiency?.evidence_state === 'COMPLETE') primary = '本次已登记需求的原文已读齐';
    else if (body.status === 'READING_ONLY') primary = brief(body.summary,240) || '原文尚未齐备，仅提供阅读出处。';
    else if (ocr) primary = ocr;
    else if (body.answer_mode === 'TABLE_READING_REQUIRED') primary = '需对照原表的行列、等级和地区条件；尚未确定单一系数。';
    else {
      const priority = gaps.find(gap => /低置信度|数字.*核对|否定词/.test(gap)) || gaps[0];
      primary = priority ? '缺口：'+brief(priority,180) : compactPreviewCount !== null
        ? '先显示 '+compactPreviewCount+' 段候选摘录。' : brief(body.summary,240);
    }
    const parts = [primary || '已定位候选出处，仍须核对原文'];
    if (ocr && primary !== ocr) parts.push(ocr);
    if (body.answer_mode === 'TABLE_READING_REQUIRED' && (sufficiency || ocr || body.status === 'READING_ONLY'))
      parts.push('需对照原表，尚未确定单一系数');
    if (visibleQuestions.length || sufficiency?.condition_state === 'NEEDS_DETAILS') parts.push('仍需补充工程条件');
    if (sufficiency?.evidence_state === 'COMPLETE') parts.push('工程适用性尚未核验');
    return parts.join(' · ');
  }
  function citationReady(citation) {
    return citation && typeof citation.source_id === 'string' && /^(gb55031|official-[a-f0-9]{32})$/.test(citation.source_id)
      && typeof citation.snapshot_id === 'string' && /^[a-f0-9]{64}$/.test(citation.snapshot_id)
      && typeof citation.chunk_id === 'string' && citation.chunk_id.startsWith(`${citation.source_id}:${citation.snapshot_id}:`)
      && /^(0|[1-9]\d{0,5})$/.test(citation.chunk_id.slice(citation.source_id.length + citation.snapshot_id.length + 2))
      && Number.isInteger(citation.page_start) && citation.page_start > 0
      && Number.isInteger(citation.page_end) && citation.page_end >= citation.page_start && citation.page_end <= 200;
  }
  function citationLabel(citation) {
    const page = `${citation.page_start}${citation.page_end !== citation.page_start ? `—${citation.page_end}` : ''}`;
    // 空条号保留真实计算单元名称；片段序号不是条号，不能补成第 None 条。
    const unitLabel = !citation.clause_number && typeof citation.reading_unit_label === 'string'
      ? brief(citation.reading_unit_label.trim(), 180) : '';
    return `${citation.standard_code || citation.title || '候选来源'}${citation.clause_number ? ` · 条款 ${citation.clause_number}` : unitLabel ? ` · ${unitLabel}` : ''} · 文件第 ${page} 页`;
  }
  function sourceItem(citation,text) {
    return {
        id: citation.chunk_id, source_id: citation.source_id, snapshot_id: citation.snapshot_id,
        title: citation.title, standard_code: citation.standard_code, text,
        clause_number: citation.clause_number, page_start: citation.page_start, page_end: citation.page_end,
        // 命名单元仍用同一固定片段开原页，真实名称随出处传递，不编造数字条号。
        ...(typeof citation.reading_unit_key === 'string' ? {reading_unit_key:citation.reading_unit_key} : {}),
        ...(typeof citation.reading_unit_label === 'string' ? {reading_unit_label:citation.reading_unit_label} : {}),
        source_url: citation.source_url, warnings: Array.isArray(citation.warnings) ? citation.warnings : [], status: 'PENDING_REVIEW',
        // 只传同一固定正文的档案；缺失或跨快照档案不影响原有旧数据入口。
        ...(globalThis.ArchSpecSourceMetadata?.fixed(citation) ? {source_metadata:citation.source_metadata} : {}),
        // 当前核验视图由同一模块核全身份；错配时仍保留已核的原阅读出处。
        ...(globalThis.ArchSpecSourceMetadata?.validity?.(citation)
          ? {source_validity:citation.source_validity} : {}),
      ...(typeof citation.snapshot_fingerprint==='string'?{snapshot_fingerprint:citation.snapshot_fingerprint}:{})};
  }
  function figureControls(citation,text,isCurrent,query,requestProject) {
    const item=sourceItem(citation,text);
    return globalThis.ArchSpecBookPages?.controls?.(document,item,{isCurrent,onOpen:(initialPage,bookPages)=>{
      if(!isCurrent())return;
      document.dispatchEvent(new CustomEvent('archspec:qa-evidence-open',{detail:{query,projectId:requestProject,item,initialPage,bookPages}}));
    }});
  }
  function sourceControl(citation, text, label, isCurrent, query, requestProject, initialPage, tableReading) {
    // 保留有限的服务端行列证明；阅读区还会与当前固定页布局重新核对。
    const serialized = tableReading ? JSON.stringify(tableReading) : '';
    const fixedReading = serialized && serialized.length <= 16000 ? JSON.parse(serialized) : null;
    const source = node('button', 'official-qa-source quiet-button', label); source.type = 'button';
    source.addEventListener('click', () => {
      if (!isCurrent()) return;
      document.dispatchEvent(new CustomEvent('archspec:qa-evidence-open', {detail: {query, projectId: requestProject,
        // 打开哪一页与原片段覆盖哪些页是两件事，不改写固定出处的页范围。
        ...(initialPage !== undefined ? {initialPage} : {}), item: sourceItem(citation,text), ...(fixedReading ? {tableReading:fixedReading} : {})}}));
    });
    return source;
  }
  function resolveContextGroup(group, evidence, admitted) {
    // 编号只在同一次有限证据账本内解析；条号相同、仅同源或同页都不能替代固定出处。
    if (!group || group.kind !== 'MEASUREMENT_CONTEXT' || group.coverage !== 'READ_CONTEXT_ONLY'
      || typeof group.title !== 'string' || !group.title.trim()
      || typeof group.standard_code !== 'string' || !group.standard_code.trim()
      || typeof group.heading_evidence_id !== 'string' || !group.heading_evidence_id
      || !Array.isArray(group.evidence_ids) || !group.evidence_ids.length || group.evidence_ids.length > 8
      || new Set(group.evidence_ids).size !== group.evidence_ids.length
      || group.evidence_ids.includes(group.heading_evidence_id)) return null;
    const fixed = item => citationReady(item) && admitted.has(item)
      // 同一固定片段有别名或冲突原文时不能挑一个编号掩盖歧义。
      && evidence.filter(value => value?.chunk_id === item.chunk_id).length === 1
      && item.source_id === group.source_id && item.snapshot_id === group.snapshot_id && item.standard_code === group.standard_code
      && typeof item.text === 'string' && !!item.text.trim() && ['MATCH', 'CONTINUATION', 'RELATED_CONTEXT'].includes(item.role);
    const headingMatches = evidence.filter(value => value?.evidence_id === group.heading_evidence_id);
    if (headingMatches.length !== 1) return null;
    const heading = headingMatches[0];
    // 标题单独核对，不冒充正文成员；标题原文仍只在原来的上下文入口可读。
    if (!fixed(heading) || heading.text.trim() !== group.title.trim() || /[\r\n]|\.{2,}|…|·{2,}/.test(heading.text.trim())) return null;
    const actualHeading = heading.text.trim().match(/^(\d+(?:\.\d+)?)\s+([^\r\n]+)$/);
    // 原生PDF标题可能没有条号字段：只从实际单行标题读取，不补写原文；已有矛盾编号整组拒绝。
    if (!actualHeading || (heading.clause_number != null && heading.clause_number !== actualHeading[1])
      || !actualHeading[2].includes('建筑高度') || actualHeading[2].includes('目录') || /\d\s*$/.test(actualHeading[2])) return null;
    const members = [], chunks = new Set([heading.chunk_id]);
    for (const id of group.evidence_ids) {
      if (typeof id !== 'string' || !id) return null;
      const matching = evidence.filter(value => value?.evidence_id === id);
      if (matching.length !== 1) return null;
      const item = matching[0];
      // 必须是标题的真实点分节：3.20不是3.2，未知条号留在独立上下文而不冒充组成员。
      if (!fixed(item) || chunks.has(item.chunk_id) || typeof item.clause_number !== 'string'
        || !/^\d+(?:\.\d+){2,3}$/.test(item.clause_number) || !item.clause_number.startsWith(`${actualHeading[1]}.`)) return null;
      chunks.add(item.chunk_id); members.push(item);
    }
    if (!members.some(item => item.role === 'MATCH')) return null;
    return {group, members};
  }
  function resolveTableGroup(group, evidence, admitted) {
    // 原表只从同次、同书、同快照的有限账本读取；表号本身不代表可信出处。
    if (!group || group.kind !== 'TABLE_CONTEXT' || group.coverage !== 'READ_CONTEXT_ONLY'
      || typeof group.table_number !== 'string' || !/^(?:\d+(?:\.\d+){2,3}|\d+-\d+|附表(?:[一二三四五六七八九十百千万零〇]+|\d{1,3}))$/.test(group.table_number)
      || !Array.isArray(group.table_pages) || !group.table_pages.length || group.table_pages.length > 8
      || !Array.isArray(group.related_evidence_ids) || group.related_evidence_ids.length > 8
      || new Set(group.related_evidence_ids).size !== group.related_evidence_ids.length) return null;
    const fixed = id => {
      const rows = evidence.filter(row => row?.evidence_id === id);
      if (rows.length !== 1) return null;
      const row = rows[0];
      return citationReady(row) && admitted.has(row) && evidence.filter(item=>item?.chunk_id===row.chunk_id).length===1
        && row.source_id===group.source_id && row.snapshot_id===group.snapshot_id && row.standard_code===group.standard_code
        && typeof row.text==='string' && ['MATCH','CONTINUATION','RELATED_CONTEXT'].includes(row.role) ? row : null;
    };
    const anchor=fixed(group.anchor_evidence_id);
    if (!anchor || anchor.role!=='MATCH' || !/^(?:\d+(?:\.\d+){2,3}|第[一二三四五六七八九十百千万零〇0-9]+条)$/.test(anchor.clause_number || '')) return null;
    const normalize=text=>text.replace(/\s/g,'');
    if(group.proof_basis||Object.hasOwn(group,'proof')) {
      const proof=globalThis.ArchSpecRelationProof?.verify(group.proof,group.proof_basis,evidence);
      if(!proof||proof.kind!=='TABLE_REFERENCE'||proof.target_unit_key!=='table:'+group.table_number
        ||proof.source_id!==group.source_id||proof.snapshot_id!==group.snapshot_id
        ||group.proof_basis.selected_chunk_id!==anchor.chunk_id)return null;
    } else {
      // 无证明的历史响应只核指定表号字面；新响应必须通过完整证明。
      if(group.table_number.startsWith('附表'))return null;
      const literal=new RegExp('表'+group.table_number.replaceAll('.','\\.')+'(?![\\d.])','u');
      if(!evidence.some(row=>row?.clause_number===anchor.clause_number
        &&fixed(row.evidence_id)&&literal.test(normalize(row.text))))return null;
    }
    const members=[], entryKeys=new Set();
    for(const entry of group.table_pages) {
      const row=fixed(entry?.evidence_id),page=entry?.pdf_page;
      if(!row || !Number.isInteger(page) || page<row.page_start || page>row.page_end
        || typeof entry.caption!=='string' || typeof entry.continuation!=='boolean') return null;
      const caption=normalize(entry.caption),match=caption.match(/^(续)?(?:表(?=\d)|(?=附表))((?:\d+(?:\.\d+){2,3}|\d+-\d+|附表(?:[一二三四五六七八九十百千万零〇]+|\d{1,3})))(?![\d.\-])(.+)$/);
      if(!match || match[2]!==group.table_number || !!match[1]!==entry.continuation
        || /[.…⋯·]{2,}/.test(caption) || /^(的|规定|所示|中|应|要求|内)/.test(match[3])
        || !(group.proof ? row.text.includes(entry.caption)
          : row.text.split(/\r?\n/).some(line=>normalize(line)===caption))) return null;
      const key=`${row.evidence_id}:${page}`;
      if(entryKeys.has(key)) return null;
      entryKeys.add(key);
      members.push({row,page,label:`${entry.continuation?'续表':'原表'} ${group.table_number} · 文件第 ${page} 页`});
    }
    const section=anchor.clause_number.slice(0,anchor.clause_number.lastIndexOf('.')+1);
    for(const id of group.related_evidence_ids) {
      const row=fixed(id);
      if(!row || row.role!=='RELATED_CONTEXT' || !/^\d+(?:\.\d+){2,3}$/.test(row.clause_number || '')
        || !row.clause_number.startsWith(section) || members.some(item=>item.row.evidence_id===id)) return null;
      members.push({row,label:`相关片段 · ${row.clause_number} · 文件第 ${row.page_start}${row.page_end!==row.page_start?`—${row.page_end}`:''} 页`});
    }
    const reading=[];
    // 旧响应仍可阅读原表；新文字块逐字对照实际有限证据，不依靠表号猜归属。
    if(Array.isArray(group.reading_parts)&&group.reading_parts.length<=16) {
      const seen=new Set();
      for(const part of group.reading_parts) {
        const row=fixed(part?.evidence_id),page=part?.pdf_page;
        if(!row||!['TABLE_TEXT','TABLE_NOTE'].includes(part.kind)||typeof part.text!=='string'
          ||!part.text||part.text.length>16000||!row.text.includes(part.text)
          ||!Number.isInteger(page)||page<row.page_start||page>row.page_end
          ||!group.table_pages.some(entry=>entry.pdf_page===page)
          ||(part.kind==='TABLE_NOTE'&&!/^注(?:\d+)?[:：]/.test(normalize(part.text))))continue;
        const key=JSON.stringify([row.evidence_id,page,part.kind,part.text]);
        if(seen.has(key))continue;seen.add(key);reading.push({row,page,part});
      }
    }
    return {group,members,reading};
  }
  function resolveClauseGroup(group,evidence,admitted) {
    // 关系不是额外事实：每个按钮必须对应当前有限账本和同书明确引用。
    if(group.coverage!=='READ_CONTEXT_ONLY' || !Array.isArray(group.links)
      || !group.links.length || group.links.length>6) return null;
    const fixed=id=>{
      const matches=evidence.filter(row=>row?.evidence_id===id);
      if(matches.length!==1)return null;
      const row=matches[0];
      return admitted.has(row)&&citationReady(row)&&typeof row.text==='string'
        &&['MATCH','CONTINUATION','RELATED_CONTEXT'].includes(row.role)
        &&row.source_id===group.source_id&&row.snapshot_id===group.snapshot_id
        &&row.standard_code===group.standard_code
        &&evidence.filter(item=>item?.chunk_id===row.chunk_id).length===1?row:null;
    };
    const anchor=fixed(group.anchor_evidence_id);
    if(!anchor||anchor.role!=='MATCH'||!/^(?:\d+(?:\.\d+){2,3}|第[一二三四五六七八九十百千万零〇0-9]+条)$/.test(anchor.clause_number||''))return null;
    const anchors=evidence.filter(row=>fixed(row?.evidence_id)&&row.clause_number===anchor.clause_number);
    const members=[],relations=new Set();
    for(const link of group.links) {
      if(!['INCOMING','OUTGOING'].includes(link?.direction)||typeof link.complete!=='boolean'
        ||!/^(?:\d+(?:\.\d+){2,3}|第[一二三四五六七八九十百千万零〇0-9]+条)$/.test(link.clause_number||'')||link.clause_number===anchor.clause_number
        ||!Array.isArray(link.evidence_ids)||!link.evidence_ids.length||link.evidence_ids.length>8
        ||new Set(link.evidence_ids).size!==link.evidence_ids.length)return null;
      const key=`${link.direction}:${link.clause_number}`;
      if(relations.has(key))return null;relations.add(key);
      const rows=link.evidence_ids.map(fixed);
      if(rows.some(row=>!row||row.clause_number!==link.clause_number))return null;
      if(group.proof_basis||Object.hasOwn(link,'proof')) {
        const proof=globalThis.ArchSpecRelationProof?.verify(link.proof,group.proof_basis,evidence);
        if(!proof||proof.kind!=='CLAUSE_REFERENCE'||proof.direction!==link.direction
          ||proof.related_unit_key!==link.clause_number||proof.anchor_unit_key!==anchor.clause_number
          ||proof.source_id!==group.source_id||proof.snapshot_id!==group.snapshot_id
          ||group.proof_basis.selected_chunk_id!==anchor.chunk_id
          ||link.complete!==(proof.status==='LOCATED')
          ||JSON.stringify(proof.admitted_evidence_ids)!==JSON.stringify(link.evidence_ids))return null;
      } else {
        // 兼容没有证明的旧响应，仅核指定第一项原字；不重新发现并列关系。
        const referring=link.direction==='OUTGOING'?anchors:rows;
        const number=link.direction==='OUTGOING'?link.clause_number:anchor.clause_number;
        if(!referring.some(row=>!/[.…⋯·]{2,}\\s*\\d/u.test(row.text)
          &&['本规范','本标准'].some(seed=>row.text.replace(/\\s+/gu,'').includes(seed+'第'+number+'条'))))return null;
      }
      for(const row of rows)members.push({row,complete:link.complete,
        label:`${link.direction==='OUTGOING'?'本条引用':'引用本条'} · ${link.clause_number} · 文件第 ${row.page_start}${row.page_end!==row.page_start?`—${row.page_end}`:''} 页`});
    }
    return {anchor,members};
  }
  function renderContextGroups(body, isCurrent, query, requestProject) {
    if (!contextGroups || !contextGroupItems) return;
    const evidence = Array.isArray(body.evidence) ? body.evidence : [];
    const admitted = new Set(); let length = 0;
    for (const item of evidence.slice(0, 8)) {
      if (typeof item?.text !== 'string') continue;
      length += item.text.length;
      if (length > 16000) break;
      admitted.add(item);
    }
    const groups=(Array.isArray(body.context_groups)?body.context_groups:[]).slice(0,8);
    const clauseGroups=new Map();
    for(const group of groups)if(group?.kind==='CLAUSE_REFERENCE_CONTEXT') {
      const linked=resolveClauseGroup(group,evidence,admitted);if(linked)clauseGroups.set(group,linked);
    }
    const explicitIds=new Set([...clauseGroups.values()].flatMap(group=>group.members.map(item=>item.row.evidence_id)));
    // 明确引用比普通邻接更有可检查的联系，优先显示；只去重按钮，不删原文或原表页。
    groups.sort((a,b)=>(clauseGroups.has(a)?0:1)-(clauseGroups.has(b)?0:1));
    for (const group of groups) {
      if(group?.kind==='CLAUSE_REFERENCE_CONTEXT') {
        const linked=clauseGroups.get(group);
        if(!linked)continue;
        const section=node('section','official-qa-context-group','');
        section.append(node('h4','official-qa-context-group-title',brief(`${group.standard_code} · ${linked.anchor.clause_number}的明确关联`,180)));
        for(const {row,label} of linked.members) {
          const control=sourceControl(row,row.text,label,isCurrent,query,requestProject);
          control.setAttribute('data-evidence-id',row.evidence_id);section.append(control);
        }
        if(linked.members.some(row=>!row.complete))section.append(node('small','official-qa-excerpt-note','关联条文未读齐，可从原页继续阅读。'));
        contextGroupItems.append(section);continue;
      }
      if(group?.kind==='TABLE_CONTEXT') {
        const table=resolveTableGroup(group,evidence,admitted);
        if(!table) continue;
        const section=node('section','official-qa-context-group','');
        section.append(node('h4','official-qa-context-group-title',brief(`${group.standard_code} · ${tableLabel(group.table_number)}`,180)));
        for(const {row,page,label} of table.members) {
          if(page===undefined&&explicitIds.has(row.evidence_id))continue;
          const control=sourceControl(row,row.text,label,isCurrent,query,requestProject,page);
          control.setAttribute('data-evidence-id',row.evidence_id);section.append(control);
        }
        for(const {row,page,part} of table.reading) {
          const note=part.kind==='TABLE_NOTE',fold=node('details','official-qa-table-part','');fold.open=false;
          fold.append(node('summary','',`${note?'表注':'表内文字'} · 文件第 ${page} 页`));
          // 用textContent原样显示，保留换行；表内文字不强行重排成数字表格。
          fold.append(node('p','official-qa-table-text',part.text));
          const control=sourceControl(row,row.text,`${note?'表注':'表内文字'}原页 · 文件第 ${page} 页`,isCurrent,query,requestProject,page);
          control.setAttribute('data-evidence-id',row.evidence_id);fold.append(control);section.append(fold);
        }
        if(group.table_status==='BUDGET_OMITTED') section.append(node('small','official-qa-excerpt-note','部分原表、表内文字或表注未纳入，请从固定原页继续核对。'));
        contextGroupItems.append(section);continue;
      }
      const resolved = resolveContextGroup(group, evidence, admitted);
      if (!resolved) continue;
      // 先核对整组再创建控件，坏成员不能留下半组看似有效的按钮；原上下文入口不受影响。
      const section = node('section', 'official-qa-context-group', '');
      section.append(node('h4', 'official-qa-context-group-title', brief(`${group.standard_code} · ${group.title}`, 180)));
      for (const item of resolved.members) {
        const role = item.role === 'MATCH' ? '命中条文' : item.role === 'CONTINUATION' ? '同号续文' : '相关片段';
        const page = `${item.page_start}${item.page_end !== item.page_start ? `—${item.page_end}` : ''}`;
        const label = `${role}${item.clause_number ? ` · ${item.clause_number}` : ''} · 文件第 ${page} 页`;
        const control = sourceControl(item, item.text, brief(label, 180), isCurrent, query, requestProject);
        control.setAttribute('data-evidence-id', item.evidence_id);
        section.append(control);
      }
      contextGroupItems.append(section);
    }
    contextGroups.hidden = !contextGroupItems.children.length;
    const summary=document.querySelector('#official-qa-context-group-summary');
    if(summary) summary.textContent=body.answer_mode === 'HEIGHT_MEASUREMENT_READING'
      ? '高度计量 · 相关条款与原页' : '集中阅读出处（待核验）';
  }
  function render(body, isCurrent, query, requestProject, submitted) {
    conditionPreview=false;retry.textContent=defaultRetryLabel;
    const status = body.status;
    let compactPreviewCount = null;
    const supported = ['NEEDS_CONDITIONS', 'REFERENCE_BRIEF', 'READING_ONLY', 'MISSING_SOURCE', 'NO_EVIDENCE', 'EVIDENCE_CHECK_FAILED', 'MODEL_ERROR'];
    if (!supported.includes(status)) throw Error('回答未完成，请重新整理出处。');
    if(notice && body.model) {
      const model=body.model;
      const label=model.real_provider===true ? '真实模型' : '测试假模型';
      // 理解是否通过校验与本轮是否调用分开；恢复可沿用已校验提案而不重复调用。
      // 只有旧响应未提供状态时才兼容布尔调用标记，显式未校验状态不能被它覆盖。
      const understandingReady = Object.hasOwn(model,'understanding_state')
        ? model.understanding_state === 'VALIDATED' : model.understanding_called === true;
      const understanding = model.understanding_state === 'VALIDATED' && model.understanding_reused === true
        ? '已沿用经校验的理解' : '已理解问法';
      notice.textContent=status==='MODEL_ERROR' || status==='EVIDENCE_CHECK_FAILED' ? `${label}流程未通过 · ${brief(body.summary,180)}`
        : !understandingReady ? `${label}未完成理解 · ${brief(body.summary,180)}`
        : status==='NEEDS_CONDITIONS' ? `${label}${understanding} · 等待补充条件`
        : `${label}${understanding} · ${model.selection_called ? '模型已选择证据' : '正文仍在本机整理'} · 出处待核验`;
      const budget=model.budget;
      if(budget && Number.isFinite(budget.reserved_cny) && Number.isFinite(budget.limit_cny)) {
        detailContent.append(node('p','official-qa-step',`开发累计预留 ${budget.reserved_cny.toFixed(2)} / ${budget.limit_cny.toFixed(2)} 元（不是实际扣费）。`));
      }
    }
    // 原文已经找到后仍可能缺少车库形式等条件；保留阅读卡，不用追问取代整份答案。
    const conditionQuestions = (status === 'NEEDS_CONDITIONS' ? body.questions
      : ['REFERENCE_BRIEF','READING_ONLY'].includes(status) ? body.condition_followups : []) || [];
    // 错误更新不能被当成“没有缺项”成功替换旧出处；上层会恢复上次整份阅读结果。
    if(!Array.isArray(conditionQuestions) || conditionQuestions.some(question =>
      !question || typeof question.key !== 'string')) throw Error('补充条件格式无效，请重新整理。');
    const visibleQuestions = (Array.isArray(conditionQuestions) ? conditionQuestions : [])
      .filter(question => fields.some(field => field.key === question?.key))
      .filter((question, index, rows) => rows.findIndex(row => row.key === question.key) === index).slice(0, 3);
    setConditionsHidden(status !== 'NEEDS_CONDITIONS' && !visibleQuestions.length,{required:status==='NEEDS_CONDITIONS',count:visibleQuestions.length});
    if (!form.hidden) {
      if(status === 'NEEDS_CONDITIONS') {
        renderProjectChoices(body.project_conditions,query,requestProject,sourceSelect.value || '__all__');
      }
      // 已在问题中明确的条件不重复追问；重试仍保留用户已填写的值。
      const requested = new Set(visibleQuestions.map(question => question.key));
      fields.forEach(field => {if (field.container) field.container.hidden = !requested.has(field.key);});
      for (const question of visibleQuestions) {
        const field = fields.find(value => value.key === question.key);
        if (field?.label && typeof question.label === 'string') field.label.textContent = brief(question.label, 80);
        field?.syncChoices?.();
        if (field && typeof question.reason === 'string' && question.reason.trim()) {
          field.reason.textContent = brief(question.reason, 140); field.reason.hidden = false;
        }
      }
    }
    if (status === 'REFERENCE_BRIEF') {
      const items = Array.isArray(body.answer_items) ? body.answer_items : [];
      if (!items.length || items.some(item => typeof item.quote !== 'string' || !item.quote || !citationReady(item.citation))) {
        throw Error('出处信息不完整，请重新整理。');
      }
      answer.append(node('p', 'official-qa-summary', brief(body.summary, 400)));
      const publicScope=body.answer_brief?.conditions?.public_fire_scope;
      // 明确排除范围直接放在短答上方，不让用户从折叠日志猜为什么没有数值卡。
      if(body.public_fire_condition_state==='SCOPE_EXCLUDED'
        && ['木结构建筑','有特殊要求的建筑','附建于民用建筑中的汽车库'].includes(publicScope)) {
        answer.append(node('p','official-qa-excerpt-note',`${publicScope}不套用本条一般公共建筑限值；另查对应要求，原文仍可对照。`));
      } else if(body.public_fire_condition_state==='SCOPE_UNLOCATED'
        && ['木结构建筑','有特殊要求的建筑','附建于民用建筑中的汽车库'].includes(publicScope)) {
        answer.append(node('p','official-qa-excerpt-note',`已说明${publicScope}；当前摘录尚未定位对应范围，请查对应原文。`));
      }
      // 原表读数和项目最终系数分开：只呈现本次账本内的行列、字面值和固定原页。
      const ledger = Array.isArray(body.evidence) ? body.evidence.slice(0,8) : [];
      const compact = text => text.replace(/\s/g,'');
      const readingIds = new Set();
      for(const reading of (Array.isArray(body.table_readings)?body.table_readings:[]).slice(0,4)) {
        if(!reading || reading.status!=='ORIGINAL_TABLE_READING' || reading.scope!=='BASE_TABLE_ONLY'
          || reading.is_project_value!==false || !/^tr_[a-f0-9]{64}$/.test(reading.id || '')
          || readingIds.has(reading.id) || !/^\d+(?:\.\d+){2,3}$/.test(reading.table_number || '')
          || typeof reading.caption!=='string' || typeof reading.row_label!=='string'
          || typeof reading.column_label!=='string' || !/^\d+(?:\.\d+)?$/.test(reading.value || ''))continue;
        const matches=ledger.filter(row=>row?.evidence_id===reading.evidence_id);
        const anchors=items.filter(item=>item.citation.evidence_id===reading.anchor_evidence_id);
        if(matches.length!==1 || anchors.length!==1)continue;
        const owner=matches[0],anchor=anchors[0].citation;
        if(!citationReady(owner) || owner.chunk_id!==reading.chunk_id || typeof owner.text!=='string'
          || owner.source_id!==reading.source_id || owner.snapshot_id!==reading.snapshot_id
          || anchor.source_id!==reading.source_id || anchor.snapshot_id!==reading.snapshot_id
          || !Number.isInteger(reading.pdf_page) || reading.pdf_page<owner.page_start || reading.pdf_page>owner.page_end
          || !owner.text.split(/\r?\n/).some(line=>compact(line)===compact(reading.caption))
          || compact(reading.caption).match(/^表(\d+(?:\.\d+){2,3})(?![\d.])(.+)$/)?.[1]!==reading.table_number
          || !compact(owner.text).includes(compact(reading.row_label))
          || !owner.text.split(/\r?\n/).some(line=>line.trim()===reading.value))continue;
        const headers=reading.column_label.split(' / ');
        // 实际OCR按行读整页，另一列文字可能穿插其中；按已有单元格里的原始行核对，不能要求混排全文连成一句。
        const splitHeaders = Array.isArray(reading.header_cells) && reading.header_cells.length===2
          && reading.header_cells.every((cell,index)=>Array.isArray(cell?.lines)&&cell.lines.length>0&&cell.lines.length<=10
            && cell.lines.every(line=>typeof line?.text==='string'&&!!line.text.trim()
              && owner.text.split(/\r?\n/).some(raw=>compact(raw)===compact(line.text)))
            && compact(cell.lines.map(line=>line.text.trim()).join(''))===compact(headers[index]));
        const garage=reading.reading_kind==='GARAGE_AREA_BASELINE' && reading.unit==='m²'
          && /汽车库防火分区.*最大允许建筑面积\(m[²2]\)/.test(compact(reading.caption));
        const validHeaders=garage ? headers[0]==='耐火等级' && headers[1]?.split(/[、,，]/)
          .every(label=>['单层汽车库','多层汽车库','半地下汽车库','地下汽车库','高层汽车库'].includes(label))
          : ['侧面采光','顶部采光'].includes(headers[0]) && headers[1]?.includes('采光系数') && headers[1]?.includes('标准值(%)');
        if(headers.length!==2 || !validHeaders
          || (reading.header_cells!==undefined ? !splitHeaders
            : headers.some(label=>!compact(owner.text).includes(compact(label)))))continue;
        readingIds.add(reading.id);
        const card=node('article','official-qa-table-reading official-qa-item','');
        // 面积和采光共用固定原页入口；不能给车库面积套百分号或“房间行”。
        card.append(node('strong','',garage ? `原表基础面积 ${reading.value} m²（不是项目最终面积）`
            : `原表读数 ${reading.value}%（不是项目最终系数）`),
          node('p','',`${garage?'耐火等级行':'房间行'}：${reading.row_label}`),node('p','',`表头：${reading.column_label}`),
          node('small','official-qa-citation',`${owner.standard_code || '候选来源'} · 表${reading.table_number} · 文件第 ${reading.pdf_page} 页`));
        card.append(sourceControl(owner,owner.text,'对照原表',isCurrent,query,requestProject,reading.pdf_page,reading));
        answer.append(card);
      }
      // 主区只列本次条件对应的固定出处；全文继续在单条阅读区展开。
      // 不能借另一个快照、同号条文或不完整邻接，让一张“找到”卡片看似可信。
      const ruleSection=node('section','official-qa-garage-rules official-qa-item','');
      const ruleRows=[];
      for(const check of (Array.isArray(body.garage_rule_checks)?body.garage_rule_checks:[]).slice(0,3)) {
        if(!check || !['FORM','STORAGE','SUPPRESSION'].includes(check.kind)
          || !['LOCATED','NOT_LOCATED','DETAILS_NEEDED'].includes(check.state)
          || typeof check.label!=='string' || typeof check.message!=='string'
          || !Array.isArray(check.references) || check.references.length>8)continue;
        const linked=[];
        let invalid=false;
        for(const reference of check.references) {
          if(!reference || !Array.isArray(reference.evidence_ids) || !reference.evidence_ids.length
            || reference.evidence_ids.length>8 || new Set(reference.evidence_ids).size!==reference.evidence_ids.length) {invalid=true;break;}
          const members=reference.evidence_ids.map(id=>{
            const rows=ledger.filter(row=>row?.evidence_id===id);
            return rows.length===1?rows[0]:null;
          });
          if(members.some(row=>!row || !citationReady(row) || row.quote_ready!==true
            || !['MATCH','CONTINUATION'].includes(row.role) || typeof row.text!=='string'
            || row.source_id!==reference.source_id || row.snapshot_id!==reference.snapshot_id
            || row.clause_number!==reference.clause_number)) {invalid=true;break;}
          // 同条已经进入账本的完整片段必须一并关联，不能只显示开头。
          const same=ledger.filter(row=>row?.source_id===reference.source_id&&row.snapshot_id===reference.snapshot_id
            &&row.clause_number===reference.clause_number&&row.quote_ready===true&&row.role!=='RELATED_CONTEXT');
          if(same.length!==members.length || same.some(row=>!members.includes(row))) {invalid=true;break;}
          linked.push(...members);
        }
        if(invalid || (check.state==='LOCATED'&&!linked.length))continue;
        const row=node('div','official-qa-garage-rule','');
        row.append(node('strong','',brief(check.label,80)),node('small','official-qa-excerpt-note',brief(check.message,180)));
        const seen=new Set();
        for(const member of linked) {
          if(seen.has(member.chunk_id))continue;seen.add(member.chunk_id);
          row.append(sourceControl(member,member.text,
            `${member.standard_code || '候选正文'} · ${member.clause_number} · 文件第 ${member.page_start}${member.page_end!==member.page_start?'—'+member.page_end:''} 页`,
            isCurrent,query,requestProject));
        }
        ruleRows.push(row);
      }
      if(ruleRows.length) {
        ruleSection.append(node('strong','','本次条件相关出处'),...ruleRows);answer.append(ruleSection);
      }
      // 可选短答先独立核对，失败不隐藏旧原文。固定书/快照/条款只合并一次，
      // 完整续文折叠保留，每个原页入口仍使用未裁选的证据。
      const briefGroups=globalThis.ArchSpecAnswerBrief?.validate(body,citationReady);
      const pageReadings=globalThis.ArchSpecPageReadings?.validate(body,citationReady) || [];
      if(briefGroups) {
        answer.append(node('p','official-qa-excerpt-note','先看相关条件，完整条文可展开；这些摘录不是单一设计结论。'));
        for(const group of briefGroups) {
          const reading=pageReadings.find(row=>group.evidence_ids.length===1&&row.evidence_id===group.evidence_ids[0]);
          const card=node('article','official-qa-item official-qa-brief-card'+(reading?' official-qa-page-reading':''),'');
          card.append(node('strong','',reading ? `原页基准读数 ${reading.baseline_value} ${reading.unit}（未计算灭火修正）`
            : group.branch_outline ? (group.mode==='ORIGINAL_REQUIRED'?'按识别文字分项阅读 · 原页待核':'按原文分项阅读') : group.mode==='CONDITION_EXCERPTS' ? '与你问法相关的原文条件' : '已定位出处，先对照原页'));
          card.append(node('small','official-qa-citation',citationLabel(group.parts[0].citation)));
          card.append(node('p','official-qa-excerpt-note',reading ? 'AI 原页核读，待人工确认。旧识别文字没有被改写。' : group.reason));
          const segments=reading ? [{text:reading.scope_text,role:'SCOPE'},
            {text:reading.condition_text,role:'CONDITION'},{text:reading.modifier_text,role:'RESTRICTION'}] : group.segments;
          for(const part of segments) {
            const segment=node('section','official-qa-brief-segment','');
            segment.append(node('small','official-qa-excerpt-note',
              {SCOPE:'适用范围',CONDITION:'整段条件',RESTRICTION:'同时保留的限制或修正'}[part.role]));
            segment.append(node('blockquote','official-qa-quote',part.text));card.append(segment);
          }
          if(group.branch_outline) {
            card.append(node('blockquote','official-qa-quote',group.branch_outline.scope.text));
            const branches=node('div','official-qa-branches','');
            for(const branch of group.branch_outline.items){
              const entry=node('details','official-qa-branch','');entry.open=false;
              entry.append(node('summary','',branch.title||`第 ${branch.number} 项 · 阅读整段原文`),
                node('blockquote','official-qa-quote',branch.text));branches.append(entry);
            }
            card.append(branches);
          }
          const pages=node('div','official-qa-brief-pages','');
          for(const item of group.parts)pages.append(sourceControl(item.citation,item.quote,
            group.parts.length===1 ? '对照原页' : `原页 ${item.citation.page_start}—${item.citation.page_end}`,
            isCurrent,query,requestProject));
          card.append(pages);
          if(group.source_copies?.length) {
            const copies=node('details','official-qa-original-copies','');copies.open=false;
            copies.append(node('summary','',`同一原件的其他出处（${group.source_copies.length}）`));
            for(const copy of group.source_copies)for(const item of copy.parts) {
              copies.append(node('small','official-qa-excerpt-note',copy.reason));
              let host='另一登记来源';try{host=new URL(item.citation.source_url).hostname;}catch{ /* 固定原页仍可核读。 */ }
              copies.append(sourceControl(item.citation,item.quote,`对照 ${host} 的同一原件`,isCurrent,query,requestProject));
              const metadata=globalThis.ArchSpecSourceMetadata?.details(item.citation);
              if(metadata)copies.append(metadata);
            }
            card.append(copies);
          }
          for(const item of group.parts){
            const figures=figureControls(item.citation,item.quote,isCurrent,query,requestProject);
            if(figures)card.append(figures);
          }
          if(reading) {
            if(reading.extinguishing_state==='PRESENT_SCOPE_UNCONFIRMED')card.append(node('p','official-qa-excerpt-note',
              '你填写了有自动灭火系统；还需确认是否整个防火分区全部设置，不能据此直接增加面积。'));
            const transcript=node('details','official-qa-page-transcription','');transcript.open=false;
            transcript.append(node('summary','','展开整条原页核读（含其他等级）'),
              node('blockquote','official-qa-quote',reading.transcription));card.append(transcript);
          }
          const fold=node('details','official-qa-full-clause','');fold.open=false;
          fold.append(node('summary','',reading ? '展开原始识别文字（保持原样）' : '展开完整条文（含其他条件与续文）'));
          fold.append(node('blockquote','official-qa-quote',group.full));card.append(fold);answer.append(card);
        }
      }
      // 老响应保持兼容，不把被截断的旧预览称为完整条件。
      if (!briefGroups && items.length > 5) answer.append(node('p', 'official-qa-excerpt-note',
        '预览只展示前5段，其余续文请在阅读出处中查看。'));
      for (const item of briefGroups ? [] : items.slice(0, 5)) {
        if (typeof item.quote !== 'string' || !item.quote) continue;
        const card = node('article', 'official-qa-item', '');
        // 表格文字可能混排行列：原文保留在折叠区，主区不把数字串冒充已确定答案。
        const excerpt = body.answer_mode === 'TABLE_READING_REQUIRED' ? node('details', '', '') : card;
        if(excerpt !== card) {
          excerpt.open=false;excerpt.append(node('summary','','查看识别原文（表格请对照原页）'));card.append(excerpt);
        }
        excerpt.append(node('blockquote', 'official-qa-quote', brief(item.quote, 180)));
        if (item.quote.length > 180) excerpt.append(node('small', 'official-qa-excerpt-note', '预览未显示全部条件，全文见出处。'));
        const citation = item.citation;
        card.append(node('small', 'official-qa-citation', citationLabel(citation)));
        const evidence = (Array.isArray(body.evidence) ? body.evidence : []).find(value =>
          value?.chunk_id === citation.chunk_id && value.source_id === citation.source_id && value.snapshot_id === citation.snapshot_id);
        card.append(sourceControl(citation, typeof evidence?.text === 'string' ? evidence.text : item.quote, '查看出处', isCurrent, query, requestProject));
        const figures=figureControls(citation,typeof evidence?.text==='string'?evidence.text:item.quote,isCurrent,query,requestProject);
        if(figures)card.append(figures);
        answer.append(card);
      }
      if (!briefGroups && items.length > 5) compactPreviewCount = Math.min(items.length, 5);
    } else {
      if (status === 'MISSING_SOURCE') {
        // 导航不是证据：只显示已审核的固定入口，不把摘要或版本线索写成回答。
        for (const hint of (Array.isArray(body.source_hints) ? body.source_hints : []).slice(0, 3)) {
          if (hint?.kind !== 'OFFICIAL_ENTRY' || hint.body_status !== 'NOT_ACQUIRED'
            || hint.url !== 'https://zrzy.yichang.gov.cn/' || hint.target_title !== '宜昌市城乡规划管理技术规定') continue;
          const card = node('div', 'official-qa-item', '');
          card.append(node('strong', '', '去哪里找'));
          card.append(node('p', '', `可能文件：《${hint.target_title}》；准确版本与正文仍需取得。`));
          const link = node('a', 'source-link', '打开宜昌官方政策法规入口');
          link.href = hint.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          card.append(link); answer.append(card);
        }
      }
    }
    if(status !== 'NEEDS_CONDITIONS') clearProjectChoices();
    renderProvenance(body,requestProject);
    if (status === 'REFERENCE_BRIEF' || status === 'READING_ONLY') {
      // 只读回退复用同一阅读区，只放紧凑的原页按钮，不把未齐的片段堆成答案。
      const contextEvidence = (Array.isArray(body.evidence) ? body.evidence : []).filter(value => citationReady(value) && typeof value.text === 'string').slice(0, 8);
      for (const evidence of contextEvidence) {
        const role = evidence.quote_ready === false ? '仅阅读' : evidence.role === 'RELATED_CONTEXT' ? '相邻片段' : evidence.role === 'CONTINUATION' ? '同号续文' : '命中片段';
        const row = node('p', 'official-qa-context-item', '');
        row.append(sourceControl(evidence, evidence.text, `${role} · ${citationLabel(evidence)}`, isCurrent, query, requestProject));
        contextItems.append(row);
      }
      contextSummary.textContent = `${status === 'READING_ONLY' ? '阅读出处' : '相关上下文'}（${contextEvidence.length} 段）`;
      context.hidden = !contextEvidence.length;
      renderContextGroups(body, isCurrent, query, requestProject);
      // 原表和高度量法需要对照完整条件，直接露出紧凑入口，不堆叠跨页全文。
      if(['TABLE_READING_REQUIRED','HEIGHT_MEASUREMENT_READING'].includes(body.answer_mode)
          && contextGroups && !contextGroups.hidden) contextGroups.open=true;
    }
    const gaps = Array.isArray(body.gaps) ? body.gaps.filter(value => typeof value === 'string') : [];
    for (const gap of gaps) detailContent.append(node('p', 'official-qa-gap', gap));
    for (const step of Array.isArray(body.trace) ? body.trace : []) {
      if (typeof step.message === 'string') detailContent.append(node('p', 'official-qa-step', step.message));
    }
    details.hidden = !detailContent.children.length;
    const unmet = renderRequirementDetails(body), sufficiency = checkedSufficiency(body);
    renderSufficiency(body, sufficiency);
    state.textContent = resultStatus(body, {sufficiency, unmet, visibleQuestions, compactPreviewCount});
    if(readingRecord && globalThis.ArchSpecQARecord && ['REFERENCE_BRIEF','READING_ONLY','NO_EVIDENCE','MISSING_SOURCE','NEEDS_CONDITIONS'].includes(status)) {
      // 记录锁定本轮实际提交内容，不能在导出时从已编辑的控件反读条件。
      const packet={query,projectId:requestProject||'',projectLabel:document.querySelector('#search-project')?.selectedOptions?.[0]?.textContent||requestProject||'',
        scope:sourceSelect.value||'__all__',scopeLabel:sourceSelect.selectedOptions?.[0]?.textContent||sourceSelect.value||'全部已准备正文',
        summary:state.textContent,modeLabel:notice?.textContent||'本地整理 · 出处待核验',
        conditions:fields.flatMap(field=>typeof submitted?.conditions?.[field.key]==='string'?[field.label.textContent+'：'+submitted.conditions[field.key]]:[]),
        provenance:(Array.isArray(body.condition_provenance)?body.condition_provenance:[]).filter(row=>projectConditionReady(row,requestProject)).map(row=>row.label+'：'+row.value+'（'+row.source_name+'）'),
        questions:visibleQuestions.map(row=>row.label+(row.reason?'；'+row.reason:'')),
        requirements:unmet.map(row=>row.label+'：'+row.message),gaps,
        evidence:['REFERENCE_BRIEF','READING_ONLY'].includes(status)?(Array.isArray(body.evidence)?body.evidence:[]):[]};
      readingRecord.append(globalThis.ArchSpecQARecord.controls(document,packet,{
        isCurrent:()=>isCurrent()&&!inFlight&&!needsTaskRefresh&&!panel.hidden,
        metadata:globalThis.ArchSpecSourceMetadata,citationCopy:globalThis.ArchSpecCitationCopy}));
    }
  }
  function requestLocalReading() {
    // 只有GET已核回且不在执行中的模型题可显式另开本地题；不取消或重放旧模型操作。
    if (!recoveredModel || !currentTask || currentTask.mode !== 'MODEL' || needsTaskRefresh
      || ['CREATED','RUNNING'].includes(currentTask.state) || inFlight) {showTask();return;}
    if (taskBinding?.query !== queryInput.value.trim() || taskBinding?.scope !== (sourceSelect.value || '__all__')
      || currentTask.project_id !== (projectId || null)) return;
    const id = newOperationId();
    if (!id || id === currentTask.id) {
      if (taskState) taskState.textContent = '浏览器无法登记新的本地题，请刷新状态后再试；原模型题未改变。';
      return;
    }
    // 只沿用受信恢复的输入与已确认编号；新LOCAL任务重新核权和取证，不复制旧答案或模型同意。
    const confirmedIds = [...restoredConditionIds];
    invalidate({hide:false,resetConditions:false});
    restoredConditionIds = confirmedIds;
    if (modelOption) modelOption.checked = false;
    return requestAnswer({online:false,operationId:id});
  }
  async function requestAnswer(options = {}) {
    const query = queryInput.value.trim();
    const sourceId = sourceSelect.value || '__all__';
    const requestProject = projectId;
    const useModel = modelOption?.checked === true;
    const retryingConditions = !form.hidden && !conditionPreview;
    if(needsTaskRefresh||taskGetOwner!==null){
      panel.hidden=false;start.setAttribute('aria-expanded','true');showTask();
      state.textContent='请先核回本题状态，再继续整理；不会另开问答。';return;
    }
    if(restoreCandidate) dropTask();
    const mode = useModel ? 'MODEL' : 'LOCAL';
    if (currentTask && (taskBinding?.query !== query || taskBinding?.scope !== sourceId
        || currentTask.project_id !== (requestProject || null)
        || (mode !== currentTask.mode && !(recoveredModel && currentTask.mode === 'MODEL' && !useModel)))) dropTask();
    if (currentTask && (needsTaskRefresh || ['CREATED','RUNNING'].includes(currentTask.state))) {showTask();return;}
    if (currentTask?.mode === 'MODEL' && !useModel) {showTask();return;}
    if (currentTask && ['INTERRUPTED','FAILED'].includes(currentTask.state) && currentTask.retry_count >= 2) {showTask();return;}
    // 同一在途点击共用既有请求；条件/归属改变后旧请求仍受原守卫隔离。
    if (inFlight?.isCurrent()) return;
    const automatic=options.automatic===true;
    const restoreOutput = automatic ? preserveOutput() : null;
    // 自动刷新仍属于同一问题；不令进度控件或原回答的出处按钮无故失效。
    let own = automatic ? options.ownerGeneration : currentTask ? generation : ++generation;
    const conditionOwner=conditionRevision;
    const manualSignature=()=>JSON.stringify(fields.map(field=>field.input?.value.trim() || ''));
    const originalManual=manualSignature();
    const isCurrent = () => own === generation && query === queryInput.value.trim()
      && sourceId === (sourceSelect.value || '__all__') && requestProject === projectId
      && useModel === (modelOption?.checked === true) && conditionOwner===conditionRevision
      && originalManual===manualSignature();
    const preserveReceipt = !!currentTask;
    const restoreReceipt = preserveReceipt ? preserveOutput() : null;
    if(!automatic && !preserveReceipt) clearOutput();
    panel.hidden = false; setConditionsHidden(true); start.setAttribute('aria-expanded', 'true');
    if (!query) {start.disabled = false; retry.disabled = false; state.textContent = '请先输入问题或条款号。'; return;}
    const conditions = {};
    fields.forEach(({key, input}) => {const value = input?.value.trim(); if (value) conditions[key] = value;});
    const identifiers=[...new Set([...restoredConditionIds, ...[...selectedConditions].filter(id=>candidateLedger.has(id))])];
    // 保存本轮明确选择的条件；界面渲染清空候选控件不能偷偷删掉本次已确认的编号。
    const scopedIds=sourceId.startsWith('set:')?globalThis.ArchSpecSourceScope?.decode(sourceId):sourceId==='__all__'?null:[sourceId];
    if(scopedIds===undefined){state.textContent='多本范围无法核对，请重新选择，未扩大查阅范围。';return;}
    const payload = options.payload || {query, conditions, ...(requestProject ? {project_id: requestProject} : {}), ...(scopedIds ? {source_ids:scopedIds} : {}),
      ...(requestProject && identifiers.length ? {project_condition_ids:identifiers} : {}),
      ...(useModel ? {model_sharing_confirmed:true} : {})};
    const signature = JSON.stringify([payload,mode,requestProject,sourceId]);
    // 同题补条件、继续或核回同一待确认操作，不重新开启官网查询；首次问题仍走原自动流程。
    const continueLookup = !!currentTask || retryingConditions || pendingOperation?.signature===signature;
    const operation = nextOperation(signature, options.operationId);
    if(currentTask && !operation) {
      if(restoreReceipt) restoreReceipt();
      if(taskState) taskState.textContent='浏览器无法登记本轮操作，请检查浏览器后刷新状态；不会自动继续。';
      return;
    }
    const headers = {'Content-Type':'application/json', ...(operation ? {'X-Archspec-Operation-Id':operation.id,
      'X-Archspec-Task-Id':operation.taskId, 'X-Archspec-Expected-Revision':String(operation.revision)} : {})};
    inFlight = {own,isCurrent};
    start.disabled = true; retry.disabled = true;
    if(taskResume) taskResume.disabled = true;
    if(taskLocalRead) taskLocalRead.disabled = true;
    state.textContent = automatic ? '新正文已就绪，正在更新同一问题的回答……'
      : useModel ? '模型正在理解问题并核对出处……' : '正在整理本机缓存出处……';
    let pending=true, prepared=false, updated=false, successful=false, revoked=false;
    let publicTitleHints=[], finishPublicTitles;
    const publicTitlesReady=new Promise(resolve=>{finishPublicTitles=resolve;});
    let preparationCurrent=()=>true, waitingSourceOnly=false, acceptedSourceTask=null;
    async function onPrepared(current, policy) {
      // 正文可能先于初次回答完成；等待期间取消联网后，不能沿用已经失效的收费刷新机会。
      if(typeof current==='function') preparationCurrent=current;
      // 新协调流程只能续答本轮明确等待来源的同一收据；刷新恢复不获得此闭包。
      if(policy?.waitingSourceOnly===true) waitingSourceOnly=true;
      prepared=true;
      if(pending || updated || !successful || !isCurrent() || !preparationCurrent()) return;
      if(waitingSourceOnly && (!acceptedSourceTask || automatic || needsTaskRefresh || recoveredModel
          || currentTask!==acceptedSourceTask || currentTask.state!=='WAITING_SOURCE'
          || taskBinding?.query!==query || taskBinding?.scope!==sourceId || taskBinding?.mode!==mode
          || (useModel && payload.model_sharing_confirmed!==true))) return;
      updated=true;
      await requestAnswer({automatic:true,ownerGeneration:own,payload,online:false});
    }
    try {
      if(options.online!==false) document.dispatchEvent(new CustomEvent('archspec:qa-query-start',
        {detail:{query,sourceId,isCurrent,onPrepared,projectId:requestProject||null,taskId:operation?.taskId||null,continueLookup,publicTitlesReady}}));
      const response = await fetch(useModel ? '/api/official-qa/model' : '/api/official-qa', {method: 'POST', headers, body: JSON.stringify(payload)});
      if (!isCurrent()) return;
      const body = await globalThis.ArchSpecHTTP.json(response, "出处整理响应暂不可读取");
      if (!isCurrent()) return;
      if (!response.ok) {
        if(response.status===409) {clearProjectChoices();pendingOperation=null;
          if(currentTask) {needsTaskRefresh=true;showTask();}}
        else if(response.status >= 400 && response.status < 500) pendingOperation=null;
        if([403,404].includes(response.status)) {revoked=true;own=++generation;dropTask();clearProjectChoices();}
        throw Error(typeof body.detail === 'string' ? brief(body.detail, 240) : '出处整理暂时不可用，请重试。');
      }
      const task = operation ? taskReady(body.qa_task, {id:operation.taskId,project:requestProject,mode,
        revision:operation.revision,operation:operation.id}) : null;
      if (body.status === 'TASK_RECEIPT') {
        if (!task) throw Error('等待收据归属不一致，请刷新状态。');
        // 收据没有原文，既不绘制旧引用也不将它当异题清掉本轮已核原页。
        if (restoreReceipt) restoreReceipt();
        bindTask(task,query,sourceId,mode);state.textContent = brief(body.summary,180) || '本次操作已登记，请查看等待状态。';
        return;
      }
      if (body.query !== query || body.project_id !== (requestProject || null)) throw Error('回答归属不一致，请重新整理。');
      if(automatic && ['MODEL_ERROR','EVIDENCE_CHECK_FAILED'].includes(body.status)) {
        throw Error(brief(body.summary,180) || '新正文回答未通过检查');
      }
      if(preserveReceipt && !automatic) {own=++generation;if(inFlight) inFlight.own=own;}
      clearOutput(); state.textContent = ''; render(body, isCurrent, query, requestProject, payload);
      successful=['REFERENCE_BRIEF','READING_ONLY','MISSING_SOURCE','NO_EVIDENCE'].includes(body.status);
      if(task) {
        bindTask(task,query,sourceId,mode);
        // 初次新执行须已接受该操作且只前进一版；旧操作重放或未知结果不能触发新收费动作。
        if(!automatic && ['MISSING_SOURCE','NO_EVIDENCE'].includes(body.status)
            && task.state==='WAITING_SOURCE' && task.revision===operation.revision+1
            && task.operation.state==='WAITING_SOURCE' && task.operation.replayed===false) {
          acceptedSourceTask=task;
          // 只交接本轮明确授权、已接受的新理解；历史/重放/失败不产生联网机会。
          const titles=body.suggested_source_gaps ?? body.model?.public_search_titles;
          if(useModel && payload.model_sharing_confirmed===true && body.model?.understanding_state==='VALIDATED'
              && Array.isArray(titles) && titles.length<=3
              && titles.every(title=>typeof title==='string' && title.length>=2 && title.length<=100))
            publicTitleHints=[...new Set(titles)];
        }
      } else dropTask();
    } catch (error) {
      if (isCurrent()) {
        if(revoked) {clearOutput();setConditionsHidden(true);}
        else if(automatic) restoreOutput();
        else if(restoreReceipt && !needsTaskRefresh) restoreReceipt();
        else {clearOutput(); setConditionsHidden(!retryingConditions);}
        state.textContent = automatic ? `新正文回答更新未完成：${brief(error.message,180)}；保留上次出处。`
          : brief(error.message, 240) || '无法读取出处，请重试。';
      }
    } finally {
      pending=false;
      finishPublicTitles(isCurrent() && acceptedSourceTask ? publicTitleHints : []);
      if(inFlight?.own===own) inFlight=null;
      if (isCurrent()) {start.disabled = false; retry.disabled = false;showTask();}
      if(prepared && !automatic) await onPrepared();
    }
  }
  async function previewProjectConditions(){
    const query=queryInput.value.trim(),owner=projectId,scope=sourceSelect.value||'__all__';
    panel.hidden=false;start.setAttribute('aria-expanded','true');
    if(!query||!owner){state.textContent='请先选择项目并输入本次问题，再查看资料条件。';return;}
    if(currentTask||inFlight||pendingOperation||needsTaskRefresh){state.textContent='本题已有任务，请沿本题补充条件或先刷新状态。';return;}
    clearOutput();clearProjectChoices();setConditionsHidden(true);
    const own=++generation,current=()=>own===generation&&owner===projectId&&query===queryInput.value.trim()&&scope===(sourceSelect.value||'__all__');
    projectPreview.disabled=true;state.textContent='正在读取本项目已登记资料的条件候选……';
    try{
      const response=await fetch('/api/official-qa/project-conditions?project_id='+encodeURIComponent(owner),{method:'GET',cache:'no-store'});
      const packet=await globalThis.ArchSpecHTTP.json(response,'项目条件暂不可读取');if(!current())return;
      if(!response.ok||packet?.version!=='PROJECT_CONDITION_PREVIEW_V1'||packet.project_id!==owner
        ||!Array.isArray(packet.candidates)||packet.candidates.length>60)throw Error('本项目条件候选暂不能核对，请稍后重试或手填。');
      renderProjectChoices(packet,query,owner,scope);
      const hasChoices=candidateLedger.size>0;conditionPreview=hasChoices;
      fields.forEach(field=>{field.container.hidden=true;});setConditionsHidden(!hasChoices);
      if(hasChoices){
        if(conditionPanel)conditionPanel.open=true;retry.textContent='按所选条件整理';
        if(projectCandidateNote)projectCandidateNote.textContent='勾选本次采用项，展开来源核对；已有手填值优先。';
        const summary=document.querySelector('#official-qa-condition-summary');if(summary)summary.textContent='从项目资料选择本次条件';
        const help=document.querySelector('#official-qa-condition-help');if(help)help.textContent='默认不采用任何候选；核对来源并勾选后，再点击“按所选条件整理”。项目条件不是规范依据。';
      }
      state.textContent=hasChoices?'已读取项目条件候选；请勾选本次采用项后再整理，尚未执行问答。'
        :'当前项目资料没有可采用的明确条件；可直接整理问题，再按需要手动补充。';
      if(Array.isArray(packet.gaps)&&projectCandidateNote)projectCandidateNote.textContent+=' '+packet.gaps.filter(v=>typeof v==='string').slice(0,5).map(v=>brief(v,160)).join(' ');
    }catch(error){if(current()){clearProjectChoices();state.textContent=error.message||'项目条件暂不可读取，可手动补充。';}}
    finally{if(current())projectPreview.disabled=false;}
  }
  if(projectPreview){projectPreview.hidden=!projectId;projectPreview.addEventListener('click',previewProjectConditions);}
  start.addEventListener('click', requestAnswer);
  form.addEventListener('submit', event => {event.preventDefault(); return requestAnswer();});
  document.querySelector('#official-qa-close')?.addEventListener('click', () => {
    // 收起未核回的题目只改变可见性，不能丢失原身份后误建另一收费题。
    if(needsTaskRefresh){panel.hidden=true;start.setAttribute('aria-expanded','false');}
    else invalidate();start.focus();
  });
  queryInput.addEventListener('input', () => invalidate());
  sourceSelect.addEventListener('change', () => invalidate());
  modelOption?.addEventListener('change', () => {
    if(needsTaskRefresh||taskGetOwner!==null){modelOption.checked=false;panel.hidden=false;showTask();state.textContent='请先核回原任务状态，再确认本轮模型发送；未调用模型。';return;}
    // 刷新后为原MODEL题重新勾选同意，不把原任务改成本地模式；其它模式变更开始新题。
    const confirming = recoveredModel && currentTask?.mode === 'MODEL' && modelOption.checked === true;
    invalidate({hide:!confirming,resetConditions:false,resetTask:!confirming});if(confirming) showTask();
  });
  document.querySelector('#pending-search-form')?.addEventListener('submit', () => invalidate());
  document.querySelectorAll('[data-pending-query]').forEach(example => example.addEventListener('click', () => invalidate()));
  fields.forEach(({input}) => input?.addEventListener('input', () => {
    const visible = !panel.hidden; invalidate({hide: false, resetConditions: false, resetTask:false});
    if (visible) state.textContent = '条件已修改，可重新整理。';
  }));
  document.addEventListener('archspec:project-change', event => {
    const pending=restoreCandidate;projectId=event.detail?.projectId || '';invalidate();
    if(projectPreview)projectPreview.hidden=!projectId;
    if(pending?.project_id===projectId) {taskStorage('set',pending);void refreshTask(pending);}
  });
  document.addEventListener('archspec:official-source-ready', () => invalidate());
  taskResume?.addEventListener('click', () => {if(currentTask) return requestAnswer();});
  taskRefresh?.addEventListener('click', () => refreshTask(restoreCandidate));
  async function openHistoryTask(saved) {
    if(!uuidReady(saved?.id)||saved.project_id!==(projectId||null))return;
    // 在途或结果未知的操作必须先核原任务，不能切历史时丢失待核操作并误触发重放。
    if(inFlight||pendingOperation||needsTaskRefresh){
      panel.hidden=false;state.textContent='当前问题仍在处理中或等待核对，请先刷新本题状态，再打开历史问题。';return;
    }
    queryInput.value='';queryInput.dispatchEvent(new Event('input',{bubbles:true}));
    sourceSelect.value='__all__';sourceSelect.dispatchEvent(new Event('change',{bubbles:true}));
    if(modelOption)modelOption.checked=false;
    const online=document.querySelector('#pending-online-enabled');
    if(online){online.checked=false;online.dispatchEvent(new Event('change',{bubbles:true}));}
    await refreshTask({id:saved.id,project_id:saved.project_id,expected_query:saved.query,expected_scope:saved.scope});
    if(currentTask?.id===saved.id&&currentTask.project_id===(projectId||null)){
      document.dispatchEvent(new CustomEvent('archspec:qa-history-restored',{detail:{query:taskBinding.query,sourceId:taskBinding.scope,projectId}}));
      panel.scrollIntoView?.({block:'start',behavior:'auto'});
      const title=document.querySelector('#official-qa-title');title?.setAttribute('tabindex','-1');title?.focus({preventScroll:true});
    }
    return currentTask?.id===saved.id&&currentTask.project_id===(projectId||null);
  }
  document.addEventListener('archspec:qa-history-open',event=>void openHistoryTask(event.detail));
  // 项目专题、资料要求与问题清单复用同一填题入口；只切输入，不执行检索或收费。
  function stageQuestion(row) {
    if(typeof row?.query!=='string'||!row.query.trim()||row.query.length>500||/[\r\n]/.test(row.query))throw Error('请将待查内容整理为500字以内的一条问题。');
    if(row.projectId!==projectId)throw Error('当前项目已变化，请重新打开清单。');
    if(inFlight||pendingOperation||needsTaskRefresh||taskGetOwner!==null||currentTask?.state==='RUNNING')throw Error('当前题仍在处理或等待核对，请先刷新状态。');
    const scope=row.scope===undefined?(sourceSelect.value||'__all__'):row.scope;
    if(typeof scope!=='string')throw Error('检索范围无法核对。');
    if(!(scope.startsWith('set:')?globalThis.ArchSpecSourceScope?.ensure(document,scope):[...(sourceSelect.options||[])].some(option=>option.value===scope)))throw Error('原检索范围当前不可用，请先在规范库核对。');
    queryInput.value=row.query;queryInput.dispatchEvent(new Event('input',{bubbles:true}));
    sourceSelect.value=scope;sourceSelect.dispatchEvent(new Event('change',{bubbles:true}));
    if(modelOption){modelOption.checked=false;modelOption.dispatchEvent(new Event('change',{bubbles:true}));}
    const online=document.querySelector('#pending-online-enabled');if(online){online.checked=false;online.dispatchEvent(new Event('change',{bubbles:true}));}
    panel.hidden=false;start.setAttribute('aria-expanded','true');state.textContent='已填入待查题，请主动查条文或按出处整理。';return true;
  }
  // 仅公布恢复归属的布尔状态；展示层不读取任务正文，也不另管恢复流程。
  function isRestoringProject(target) {
    if(typeof target!=='string'||!target)return false;
    return restoreCandidate?.project_id===target || (taskGetOwner!==null&&projectId===target);
  }
  globalThis.ArchSpecQuestionInput={stage:stageQuestion,isRestoringProject};
  globalThis.ArchSpecQuestionWorklist?.bind(document,{
    project:()=>projectId,readTask:currentTaskSummary,
    readScope:()=>({scope:sourceSelect.value||'__all__',label:sourceSelect.selectedOptions?.[0]?.textContent?.trim()||sourceSelect.value||'全部已准备正文'}),
    openTask:openHistoryTask,
    openDraft:stageQuestion,
  });
  // 方案是用户手填输入，不能沿用项目资料出处编号，也不会触发提交或模型许可。
  globalThis.ArchSpecConditionPresets?.bind(document,{
    fields,project:()=>projectId,
    read:()=>Object.fromEntries(fields.filter(field=>field.input.value.trim()).map(field=>[field.key,field.input.value.trim()])),
    apply:conditions=>{
      if(inFlight||pendingOperation||needsTaskRefresh||currentTask?.state==='RUNNING')throw Error('本题仍在处理中或等待核对，请先刷新状态。');
      if(taskGetOwner!==null)throw Error('正在恢复任务，请等读取完成后再填入方案。');
      // 原生选择框只接受本页已登记的选项，不能静默丢失旧方案值或自动改用默认值。
      for(const field of fields){const value=conditions[field.key];
        if(value&&field.input.tagName==='SELECT'&&![...(field.input.options||[])].some(option=>option.value===value))throw Error('方案中的选项已变化，请手动核对后重新保存。');
      }
      conditionRevision++;invalidate({hide:false,resetConditions:false,resetTask:false});
      clearProjectChoices();restoredConditionIds=[];
      for(const field of fields){field.input.value=conditions[field.key]||'';if(conditions[field.key])field.container.hidden=false;field.syncChoices?.();}
      panel.hidden=false;start.setAttribute('aria-expanded','true');setConditionsHidden(false);
      if(conditionPanel)conditionPanel.open=true;
      state.textContent='已填入保存的手填条件，请核对后点击“按出处整理回答”或“补充条件后再查”。';
    },
  });
  taskLocalRead?.addEventListener('click', requestLocalReading);
  try {
    const saved = JSON.parse(taskStorage('get') || 'null');
    if (saved && Object.keys(saved).length === 2 && uuidReady(saved.id)
        && (saved.project_id === null || (typeof saved.project_id === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(saved.project_id))))
      void refreshTask(saved);
    else if(saved) dropTask();
  } catch (_) {dropTask();}
})();
