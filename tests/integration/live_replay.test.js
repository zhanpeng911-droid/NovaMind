'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Run the actual workbench sync adapter against in-memory session/history doubles.
function workbench(messages = []) {
  const html = fs.readFileSync(path.join(__dirname, '../../novamind/webui/static/index.html'), 'utf8');
  const begin = html.indexOf('  const mirroredTurns=new Map();');
  const end = html.indexOf('  loadSessions().finally(', begin);
  const states = new Map();
  let receive;
  const context = {
    NovaMindUI: {liveConversations: callback => {receive = callback;}},
    currentId: 'offline', currentView: 'chat', sessions: [],
    input: {value: ''}, imageComposer: {items: []},
    sessionState: id => {
      if (!states.has(id)) states.set(id, {messages: [], running: false, historyLoaded: true});
      return states.get(id);
    },
    fetchJson: async () => ({messages}),
    renderMessages() {}, refreshRunUI() {}, renderSessions() {},
    loadSessions: async () => {}, switchTo() {}, flashStatus() {},
  };
  vm.runInNewContext(html.slice(begin, end) + '\nconnectConversations();', context);
  return {live: event => receive({thread_id: 'offline', ...event}), state: () => states.get('offline'), reload: () => context.reloadMirrored('offline')};
}

test('workbench repeated replay and redraw keep error text and the incomplete state', async () => {
  const ui = workbench();
  for (let replay = 0; replay < 2; replay++) {
    ui.live({kind: 'start', turn_id: 'failed', message: '问题', frames: [], truncated: true});
    ui.live({kind: 'frame', turn_id: 'failed', frame: {type: 'text', content: '保留尾部'}});
    ui.live({kind: 'frame', turn_id: 'failed', frame: {type: 'error', message: '离线失败'}});
    ui.live({kind: 'frame', turn_id: 'failed', frame: {type: 'done'}});
    ui.live({kind: 'finish', turn_id: 'failed', error: true});
  }
  await ui.reload();
  const state = ui.state();
  assert.equal(state.running, false);
  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[1].content, '保留尾部');
  assert.match(state.messages[1].note, /离线失败.*不完整/);
  assert.equal(state.messages[1].live, false);
});

test('workbench completed replay corrects the suffix from complete stored history', async () => {
  const ui = workbench([{role: 'user', content: '问题'}, {role: 'ai', content: '完整历史'}]);
  ui.live({kind: 'start', turn_id: 'complete', message: '问题', frames: [], truncated: true});
  ui.live({kind: 'frame', turn_id: 'complete', frame: {type: 'text', content: '尾部'}});
  ui.live({kind: 'finish', turn_id: 'complete', error: false});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.state().running, false);
  assert.equal(ui.state().messages.length, 2);
  assert.equal(ui.state().messages[1].content, '完整历史');
});

test('workbench cancellation retains its terminal note across history redraw', async () => {
  const ui = workbench();
  ui.live({kind: 'start', turn_id: 'cancelled', message: '问题', frames: []});
  ui.live({kind: 'finish', turn_id: 'cancelled', cancelled: true});
  await ui.reload();
  assert.equal(ui.state().messages[1].note, '已停止');
  assert.equal(ui.state().messages[1].live, false);
});
