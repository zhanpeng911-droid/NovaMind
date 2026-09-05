/* NovaMind 聊天轮次纯状态逻辑（无 DOM 依赖，Node 可直接 require 测试）。

收尾修复 Phase 2：把「处理 SSE 帧 → 更新本轮状态 → 构建最终消息」从
index.html 的 send() 中提取为纯函数，供 send 与 Node 测试共用。

约定：
- createTurnState() 返回一轮的 {tools, chunks, errorText}；
- applyFrame(state, data) 处理 thread/tool/text/limit/error 帧，返回
  是否改变本轮内容（thread 帧只负责会话绑定，不改变轮次内容）；
- setTerminalError(state, aborted, fallbackText) 设置网络/停止类终态文案，
  已有明确服务端错误时不覆盖；
- buildFinalMessage(state) 构建最终消息；纯错误也有 note，不要求存在
  text/tool 才保存；无任何内容时返回 null。
*/
(function (global) {
  'use strict';

  function createTurnState() {
    return { tools: [], chunks: [], errorText: null };
  }

  function applyFrame(state, data) {
    if (!data || typeof data.type !== 'string') return false;
    switch (data.type) {
      case 'thread':
        // 会话 id 绑定由调用方处理（涉及 draft→真实 thread 的 DOM 状态迁移）
        return false;
      case 'tool':
        state.tools.push(data.name);
        return true;
      case 'text':
        state.chunks.push(data.content);
        return true;
      case 'limit':
        if (!state.errorText) state.errorText = '⚠ 达到最大循环次数';
        return true;
      case 'error':
        // 服务端明确错误：优先保留稳定文案，后续连接关闭不覆盖
        state.errorText = '错误: ' + (data.message || '未知错误');
        return true;
      default:
        return false;
    }
  }

  function setTerminalError(state, aborted, fallbackText) {
    if (state.errorText) return false; // 已有明确服务端错误，不覆盖
    state.errorText = aborted
      ? '⏹ 已停止（本轮已取消并回滚）'
      : '连接失败: ' + (fallbackText || '未知网络错误');
    return true;
  }

  function buildFinalMessage(state) {
    const hasAny = state.chunks.length > 0 || state.tools.length > 0
      || !!state.errorText;
    if (!hasAny) return null;
    return {
      role: 'ai',
      content: state.chunks.join(''),
      tools: state.tools.slice(),
      note: state.errorText || null,
    };
  }

  const api = { createTurnState, applyFrame, setTerminalError, buildFinalMessage };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    global.NovaMindChatLogic = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
