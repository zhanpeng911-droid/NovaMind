// chat_logic.js 行为测试（Node 内置测试运行器，node --test）
// 收尾修复 Phase 2：验证 SSE 终态状态机的确定性行为。
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { createTurnState, applyFrame, setTerminalError, buildFinalMessage } =
  require('../../novamind/webui/static/chat_logic.js');

test('thread → error → done: final message has note, no text/tool', () => {
  const s = createTurnState();
  applyFrame(s, { type: 'thread', thread_id: 't1' });
  assert.strictEqual(s.chunks.length, 0);
  applyFrame(s, { type: 'error', message: '服务器处理失败，请稍后重试', request_id: 'r1' });
  const final = buildFinalMessage(s);
  assert.ok(final, '纯错误也必须构建最终消息');
  assert.strictEqual(final.content, '');
  assert.strictEqual(final.tools.length, 0);
  assert.match(final.note, /服务器处理失败/);
});

test('text → error → done: keeps partial text and error, one message only', () => {
  const s = createTurnState();
  applyFrame(s, { type: 'text', content: '部分' });
  applyFrame(s, { type: 'text', content: '结果' });
  applyFrame(s, { type: 'error', message: '中断' });
  const final = buildFinalMessage(s);
  assert.strictEqual(final.content, '部分结果');
  assert.match(final.note, /中断/);
  // 重复调用返回等值对象（每次新建）：字段一致，且前端 finally 只 push 一次
  assert.deepStrictEqual(buildFinalMessage(s), final);
});

test('normal turn has no note; empty turn returns null', () => {
  const s = createTurnState();
  applyFrame(s, { type: 'text', content: 'ok' });
  const final = buildFinalMessage(s);
  assert.strictEqual(final.note, null);
  assert.strictEqual(final.content, 'ok');

  const empty = createTurnState();
  assert.strictEqual(buildFinalMessage(empty), null);
});

test('setTerminalError does not override a server error', () => {
  const s = createTurnState();
  applyFrame(s, { type: 'error', message: '服务端明确错误' });
  const changed = setTerminalError(s, false, '连接失败: timeout');
  assert.strictEqual(changed, false, '已有服务端错误时不覆盖');
  assert.match(s.errorText, /服务端明确错误/);
  assert.ok(!/timeout/.test(s.errorText));
});

test('setTerminalError sets stop/network text when no prior error', () => {
  const aborted = createTurnState();
  assert.strictEqual(setTerminalError(aborted, true, 'AbortError'), true);
  assert.match(aborted.errorText, /已停止/);

  const net = createTurnState();
  setTerminalError(net, false, 'fetch failed');
  assert.match(net.errorText, /连接失败: fetch failed/);
});

test('tool/limit frames update state', () => {
  const s = createTurnState();
  assert.strictEqual(applyFrame(s, { type: 'tool', name: 'search' }), true);
  assert.strictEqual(s.tools[0], 'search');
  assert.strictEqual(applyFrame(s, { type: 'limit' }), true);
  assert.match(s.errorText, /达到最大循环次数/);
  assert.strictEqual(applyFrame(s, { type: 'thread', thread_id: 'x' }), false,
    'thread 帧不改变轮次内容');
  assert.strictEqual(applyFrame(s, { type: 'unknown' }), false);
});
