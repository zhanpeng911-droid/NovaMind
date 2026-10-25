'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(fetchJson) {
  const html = fs.readFileSync(path.join(__dirname, '../../novamind/webui/static/index.html'), 'utf8');
  const nodes = new Map();
  const by = id => {if (!nodes.has(id)) nodes.set(id, {value: '', dataset: {}, isConnected: true, hidden: true}); return nodes.get(id);};
  by('memory-document-scope').value = 'global';
  const context = {document: {getElementById: by}, fetchJson, loadMemories: async () => {},
    NovaMindUI: {dialogForm: async () => true}};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf("let memoryDocumentScope='global';"), html.indexOf('let memoryCache = []')), context);
  return {by, context};
}
function doc(content = '文件中的版本', extra = {}) {
  return {content, revision: 'revision', path: 'C:/offline/MEMORY.md', scope: 'global', ...extra};
}

test('memory conflict displays both versions as text without overwriting the editable draft', async () => {
  const raw = '<img src=x onerror=boom> 已保存的治理文本';
  const ui = fixture(async () => doc('文件中的版本', {conflict: true, server_content: raw}));
  await ui.context.mountMemoryDocument();
  assert.equal(ui.by('memory-document-text').value, '文件中的版本');
  assert.equal(ui.by('memory-document-compare').hidden, false);
  assert.equal(ui.by('memory-document-server').value, raw);
  assert.equal(ui.by('memory-document-server').readOnly, true);
});

test('failed or stale saves keep draft and revision until the user explicitly reloads', async () => {
  const calls = [], ui = fixture(async (url, options) => {
    calls.push({url, options});
    if (options?.method === 'PUT') throw Object.assign(new Error('记忆已更新'), {status: 409});
    return doc();
  });
  await ui.context.mountMemoryDocument();
  ui.by('memory-document-text').value = '尚未合并的草稿';
  await ui.by('memory-document-save').onclick();
  assert.equal(ui.by('memory-document-text').value, '尚未合并的草稿');
  assert.equal(ui.by('memory-document-text').dataset.revision, 'revision');
  assert.equal(ui.by('memory-document-save').disabled, false);
  assert.match(ui.by('memory-document-state').textContent, /草稿/);
  assert.equal(calls.filter(call => call.options?.method === 'PUT').length, 1);
});

test('late scope loads cannot replace the currently selected scope or revision', async () => {
  let first;
  const ui = fixture(url => url.endsWith('global') ? new Promise(resolve => {first = resolve;})
    : Promise.resolve(doc('项目版本', {scope: 'project', revision: 'project-rev'})));
  const loading = ui.context.mountMemoryDocument();
  ui.by('memory-document-scope').value = 'project';
  await ui.by('memory-document-scope').onchange({target: ui.by('memory-document-scope')});
  first(doc('过期全局版本'));
  await loading;
  assert.equal(ui.by('memory-document-text').value, '项目版本');
  assert.equal(ui.by('memory-document-text').dataset.revision, 'project-rev');
});
