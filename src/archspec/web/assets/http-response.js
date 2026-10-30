// 共用HTTP解码边界：只消费已有响应，不发请求、不重试，也不决定业务权限或任务状态。
(() => {
  async function json(response, message = '服务响应暂不可读取', shape = 'object') {
    const status = Number.isInteger(response.status) && response.status >= 100 && response.status <= 599
      ? response.status : null;
    const failure = () => {
      const error = new Error(message + (status === null ? '' : `（HTTP ${status}）`));
      if (status !== null) error.status = status;
      return error;
    };
    let body;
    try { body = await response.json(); }
    catch {
      // 错误HTTP的状态仍是确定事实；错误页不能让403/409绕开原模块的拒绝分支。
      if (!response.ok) return {detail: failure().message};
      throw failure();
    }
    const object = body !== null && typeof body === 'object' && !Array.isArray(body);
    if (!response.ok) return object ? body : {detail: failure().message};
    // 各入口声明自己的基本形状；来源清单合法返回数组，项目最新记录也可返回null。
    const valid = shape === 'array' ? Array.isArray(body)
      : shape === 'any' ? body === null || object || Array.isArray(body) : object;
    if (!valid) throw failure();
    return body;
  }
  globalThis.ArchSpecHTTP = {json};
})();
