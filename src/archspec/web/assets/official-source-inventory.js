// 官方库存只保存来源元数据；准备通知不能成为正文、现行性或许可证明。
(() => {
  let epoch=0, cache=null, pending=null, failure=null, state=null, profiled=false, requireMetadata=false;
  const listeners=new Set(), registered=new Set(), readyHeads=new Map();
  const copy=value=>JSON.parse(JSON.stringify(value));
  const prepared=source=>source?.kind==="FULL_TEXT" && source.acquired===true && !source.cache_error;
  function publish(status,sources=null) {
    if(state?.epoch===epoch && state.status===status) return;
    state={epoch,status,sources};
    for(const listener of [...listeners]) listener(copy(state));
  }
  function invalidate() {
    epoch+=1;cache=null;pending=null;failure=null;profiled=false;
    // 不派发会改变问题、范围或任务的用户“试查”事件。
    publish("LOADING");
  }
  function validate(sources) {
    if(!Array.isArray(sources) || sources.some(source=>!source || typeof source!=="object"
        || typeof source.source_id!=="string" || !source.source_id
        || typeof source.title!=="string" || typeof source.standard_code!=="string"
        || typeof source.kind!=="string" || typeof source.acquired!=="boolean"
        || source.acquired && (typeof source.snapshot_id!=="string" || !/^[a-f0-9]{64}$/.test(source.snapshot_id)))) {
      throw Error("规范清单响应无效，本次资料状态未知。");
    }
    return sources;
  }
  function read({refresh=false,metadata=false}={}) {
    // 本标签页明确选择档案后，其它订阅者也共享该模式；先登记需求，防LOADING通知抢起轻量读取。
    if(metadata)requireMetadata=true;metadata=metadata||requireMetadata;
    if(refresh) invalidate();
    if(metadata && cache!==null && !profiled) invalidate();
    if(cache!==null) return Promise.resolve(copy(cache));
    if(failure) return Promise.reject(failure);
    if(pending?.epoch===epoch){
      // 旧轻量请求先完成再升级；其它消费者仍共享当前同一次读取。
      if(metadata&&!pending.metadata)return pending.task.then(()=>read({metadata:true}));
      return pending.task.then(copy);
    }
    const own=epoch;
    const task=Promise.resolve().then(async()=>{
      const response=await fetch("/api/official-documents"+(metadata?"?include_metadata=true":""));
      const body=await globalThis.ArchSpecHTTP.json(response,"正文清单响应暂不可读取，本次资料状态未知。","array");
      if(!response.ok) throw Error(typeof body.detail==="string"?body.detail:"正文清单暂不可读取，本次资料状态未知。");
      return validate(body);
    }).then(sources=>{
      // 旧读取的每个消费者都转向当前结果，不能画回旧0。
      if(own!==epoch) return read();
      cache=copy(sources);pending=null;profiled=metadata;
      for(const source of sources) registered.add(source.source_id);
      publish("READY",cache);
      return copy(cache);
    }).catch(error=>{
      if(own!==epoch) return read();
      pending=null;cache=null;
      failure=Error(error?.message || "正文清单暂不可读取，本次资料状态未知。");
      publish("UNKNOWN");
      throw failure;
    });
    pending={epoch:own,task,metadata};
    publish("LOADING");
    return task.then(copy);
  }
  function subscribe(listener) {
    if(typeof listener!=="function") throw TypeError("库存订阅者必须是函数");
    listeners.add(listener);
    if(state) listener(copy(state));
    return ()=>listeners.delete(listener);
  }
  function observeBatch(batch) {
    let changed=false;
    for(const item of Array.isArray(batch?.items)?batch.items:[]) {
      if(typeof item?.source_id!=="string" || !/^(gb55031|official-[a-f0-9]{32})$/.test(item.source_id)) continue;
      if(item.state==="READY") {
        if(typeof item.snapshot_id!=="string" || !/^[a-f0-9]{64}$/.test(item.snapshot_id)) continue;
        if(readyHeads.get(item.source_id)!==item.snapshot_id) {
          readyHeads.set(item.source_id,item.snapshot_id);registered.add(item.source_id);changed=true;
        }
      } else if(["QUEUED","DOWNLOADING","PROCESSING","FAILED","INTERRUPTED","NEEDS_CONFIRMATION"].includes(item.state)
          && !registered.has(item.source_id)) {
        // 新来源即使还在处理或已失败也应可见；准备数量继续只读服务端。
        registered.add(item.source_id);changed=true;
      }
    }
    if(changed) invalidate();
    return pending?.task.catch(()=>undefined) || Promise.resolve();
  }
  globalThis.ArchSpecOfficialInventory={read,subscribe,observeBatch,prepared};
})();
