'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Execute the real diagnostic renderer, without opening a window or making requests.
function render(data) {
  const html = fs.readFileSync(path.join(__dirname, '../../novamind/webui/static/index.html'), 'utf8');
  const start = html.indexOf('function renderDoctor(data) {');
  const end = html.indexOf('let monitorSessions', start);
  const context = {panelEl: {innerHTML: ''}, escapeHtml: text => String(text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'), data};
  vm.runInNewContext(html.slice(start, end) + '\nrenderDoctor(data);', context);
  return context.panelEl.innerHTML;
}

test('diagnostic page shows packaged identity and escapes workspace/version text', () => {
  const html = render({ok: true, counts: {}, findings: [], application: {
    version: '3.0.0 <test>', build_time: '2026-10-23T06:00:00+00:00',
    mode: 'packaged', workspace: 'D:\\学习 & 工作\\<img src=x onerror=boom>',
  }});
  assert.match(html, /3\.0\.0 &lt;test&gt;/);
  assert.match(html, /桌面打包版/);
  assert.match(html, /<time datetime="2026-10-23T06:00:00\.000Z">/);
  assert.match(html, /学习 &amp; 工作/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /HEALTHY/);
});

test('source and missing metadata never manufacture a build time', () => {
  const html = render({ok: false, counts: {error: 1}, findings: [], application: {
    version: '3.0.0', build_time: null, mode: 'source', workspace: 'C:\\workspace',
  }});
  assert.match(html, /源码运行/);
  assert.match(html, /未打包/);
  assert.doesNotMatch(html, /<time/);
  assert.match(html, /DEGRADED/);
  assert.doesNotMatch(render({}), /undefined|Invalid Date/);
});
