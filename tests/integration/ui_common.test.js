'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function common(fetch, extra = {}) {
  const context = {window: {}, crypto: {randomUUID: () => 'test-client'}, TextDecoder, fetch, ...extra};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../novamind/webui/static/ui_common.js'), 'utf8'), context);
  return context.window.NovaMindUI;
}

function response(chunks) {
  const encoded = chunks.map(chunk => typeof chunk === 'string' ? new TextEncoder().encode(chunk) : chunk);
  const calls = {cancel: 0, release: 0};
  return {
    calls, ok: true,
    body: {getReader: () => ({
      read: async () => encoded.length ? {value: encoded.shift(), done: false} : {done: true},
      cancel: async () => {calls.cancel++;}, releaseLock: () => {calls.release++;},
    })},
  };
}

test('SSE preserves split Chinese UTF-8, CRLF frames and the last unterminated frame', async () => {
  const bytes = new TextEncoder().encode('data: {"type":"text","content":"你好"}\r\n\r\ndata: {"type":"done"}');
  const stream = response(Array.from(bytes, byte => Uint8Array.of(byte)));
  const received = [];
  await common().frames(stream, frame => received.push(frame));
  assert.equal(received[0].content, '你好');
  assert.equal(received[1].type, 'done');
  assert.deepEqual(stream.calls, {cancel: 0, release: 1});
});

test('malformed SSE cancels the connection and reports readable error text', async () => {
  const stream = response(['data: not-json\n\n']);
  await assert.rejects(common().frames(stream, () => {}), /回答数据格式异常/);
  assert.deepEqual(stream.calls, {cancel: 1, release: 1});
});

test('consumer failures also cancel the reader while preserving the original error', async () => {
  const stream = response(['data: {"type":"text"}\n\n']);
  await assert.rejects(common().frames(stream, () => {throw new Error('render failed');}), /render failed/);
  assert.deepEqual(stream.calls, {cancel: 1, release: 1});
});

test('missing stream bodies have a readable error', async () => {
  await assert.rejects(common().frames({ok: true, body: null}, () => {}), /连接未返回回答内容/);
});

test('null HTTP errors and structured validation errors retain useful status', async () => {
  for (const data of [null, {detail: [{msg: 'invalid'}]}]) {
    await assert.rejects(common(async () => ({ok: false, status: 422, json: async () => data})).json('/test'), /HTTP 422/);
  }
});

test('live reconnect replays the bounded suffix and terminal failure only once', () => {
  let source;
  const api = common(undefined, {
    EventSource: class { constructor() { source = this; } close() {} },
    addEventListener() {},
  });
  const events = [];
  api.liveConversations(event => events.push(event));
  const turn = {kind: 'start', turn_id: 'failed', thread_id: 'offline', truncated: true,
    frames: [{type: 'text', content: '末尾'}, {type: 'error', message: '离线失败'}, {type: 'done'}],
    finish: {error: true, cancelled: false}};
  source.onmessage({data: JSON.stringify({kind: 'snapshot', turns: [turn]})});
  source.onmessage({data: JSON.stringify({kind: 'snapshot', turns: [turn]})});
  source.onmessage({data: JSON.stringify({kind: 'finish', turn_id: 'failed', error: true})});
  assert.equal(events.filter(event => event.kind === 'start').length, 1);
  assert.equal(events.filter(event => event.kind === 'finish').length, 1);
  assert.equal(events.find(event => event.kind === 'start').truncated, true);
  assert.equal(events.filter(event => event.kind === 'frame' && event.frame.type === 'text').map(event => event.frame.content).join(''), '末尾');
});

test('active resync resets a turn before replay, without appending old chunks twice', () => {
  let source, answer = '';
  const api = common(undefined, {EventSource: class {constructor() {source = this;} close() {}}, addEventListener() {}});
  api.liveConversations(event => {
    if (event.kind === 'start') answer = '';
    if (event.kind === 'frame' && event.frame.type === 'text') answer += event.frame.content;
  });
  const snapshot = {kind: 'snapshot', turns: [{kind: 'start', turn_id: 'active', frames: [{type: 'text', content: '保留'}], truncated: true}]};
  source.onmessage({data: JSON.stringify(snapshot)});
  source.onmessage({data: JSON.stringify(snapshot)});
  source.onmessage({data: JSON.stringify({kind: 'frame', turn_id: 'active', frame: {type: 'text', content: '后续'}})});
  assert.equal(answer, '保留后续');
});
