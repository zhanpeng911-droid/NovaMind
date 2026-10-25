'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const markdown = require('../../novamind/webui/static/markdown.js');
const staticDir = path.join(__dirname, '../../novamind/webui/static');

test('Chinese answers render bold headings, numbered lists and nested bullets', () => {
  const html = markdown.render('# 两个项目\n\n1. **Notebook 智能知识工作台**\n   - *分析*与~~旧结论~~\n2. **NovaMind**');
  assert.match(html, /<h1>两个项目<\/h1>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<strong>Notebook 智能知识工作台<\/strong>/);
  assert.match(html, /<ul>[\s\S]*<em>分析<\/em>与<s>旧结论<\/s>/);
  assert.doesNotMatch(html, /\*\*Notebook/);
});

test('tables, quotes, line breaks and code keep their intended meaning', () => {
  const html = markdown.render('> 引用\n\n| 项目 | 状态 |\n| --- | --- |\n| GUI | 完成 |\n\n第一行\n第二行\n\n`**字面量**`\n\n```js\nconst s = "**保留**";\n  next();\n```');
  assert.match(html, /<blockquote>/);
  assert.match(html, /<table>[\s\S]*<td>完成<\/td>/);
  assert.match(html, /第一行<br>\n第二行/);
  assert.match(html, /<code>\*\*字面量\*\*<\/code>/);
  assert.match(html, /<pre><code class="language-js">const s = &quot;\*\*保留\*\*&quot;;\n  next\(\);/);
});

test('untrusted HTML and dangerous links cannot produce executable markup', () => {
  const html = markdown.render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[bad](javascript:alert(1))\n[x](data:text/html,boom)\n[x](file:///C:/private.txt)\n[正常](https://example.com/?q=%22)');
  assert.doesNotMatch(html, /<(script|img|iframe|svg)\b/i);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /href="(?:javascript|data|file):/i);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
});

test('unfinished streamed fences stay code and complete emphasis rerenders correctly', () => {
  const partial = markdown.render('```html\n<img src=x onerror=alert(1)>');
  assert.match(partial, /<pre><code class="language-html">&lt;img/);
  assert.doesNotMatch(partial, /<img\b/);
  assert.match(markdown.render('**两'), /\*\*两/);
  assert.match(markdown.render('**两个项目**'), /<strong>两个项目<\/strong>/);
});

// Exercise the actual quick-panel entry points without network, models or a browser.
function quickFixture({storedThread = null, messages = [], chunks = [], bridge = false, historyError = null} = {}) {
  function element() {
    const classes = new Set();
    return {
      value: '', dataset: {}, children: [], innerHTML: '', textContent: '', hidden: false,
      classList: {add: name => classes.add(name), toggle() {}},
      append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } },
      replaceChildren() { this.children = []; },
      querySelector() { return null; },
      querySelectorAll(selector) {
        const turn = /data-live-turn="([^"]+)"/.exec(selector)?.[1];
        return turn ? this.children.filter(node => node.dataset.liveTurn === turn) : [];
      },
      remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(node => node !== this); },
      focus() {},
    };
  }
  const ids = new Map();
  const by = id => { if (!ids.has(id)) ids.set(id, element()); return ids.get(id); };
  by('action').value = 'chat';
  by('memory-mode').value = 'normal';
  by('scope').value = 'global';
  const stored = new Map(storedThread ? [['novamind_quick_thread', storedThread]] : []);
  const storage = {getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
  let live, copied;
  const listeners = {};
  const context = {
    NovaMindMarkdown: markdown,
    NovaMindUI: {
      json: async () => { if (historyError) throw new Error(historyError); return {messages}; },
      frames: async (_, callback) => {
        callback({type: 'thread', thread_id: 'test-thread'});
        for (const content of chunks) callback({type: 'text', content});
        callback({type: 'done'});
      },
      renderImages() {}, contextCards() {}, icon() { return ''; },
      Images: class { constructor() { this.items = []; this.button = element(); } clear() {} },
      reasoningPicker: () => ({value: 'auto'}),
      liveConversations: callback => { live = callback; },
    },
    document: {getElementById: by, createElement: element, querySelectorAll: () => [], addEventListener() {}},
    window: {addEventListener(name, callback) { listeners[name] = callback; }}, localStorage: storage, sessionStorage: storage,
    fetch: async () => ({}), AbortController,
    navigator: {clipboard: {writeText: async text => { copied = text; }}},
  };
  let source;
  if (bridge) {
    context.EventSource = class {constructor() {source = this;} close() {}};
    context.crypto = {randomUUID: () => 'quick-client'};
    context.addEventListener = () => {};
    vm.runInNewContext(fs.readFileSync(path.join(staticDir, 'ui_common.js'), 'utf8'), context);
    context.NovaMindUI.liveConversations = context.window.NovaMindUI.liveConversations;
  }
  vm.runInNewContext(fs.readFileSync(path.join(staticDir, 'quick.js'), 'utf8'), context);
  return {by, live: event => live(event), copied: () => copied, focus: () => listeners.focus(), snapshot: turns => source.onmessage({data: JSON.stringify({kind: 'snapshot', turns})})};
}

test('quick-panel history renders existing answers and leaves user text literal', async () => {
  const fixture = quickFixture({storedThread: 'history-thread', messages: [
    {role: 'user', content: '**不要改我的输入**'},
    {role: 'ai', content: '**两个项目**\n\n1. Notebook\n2. NovaMind'},
  ]});
  await new Promise(resolve => setImmediate(resolve));
  const nodes = fixture.by('messages').children;
  assert.equal(nodes[0].children[0].textContent, '**不要改我的输入**');
  assert.match(nodes[1].children[0].innerHTML, /<strong>两个项目<\/strong>/);
  assert.match(nodes[1].children[0].innerHTML, /<ol>/);
});

test('local streamed answers render from complete raw text and copy keeps Markdown', async () => {
  const fixture = quickFixture({chunks: ['**两个', '项目**\n\n1. A\n', '2. B']});
  fixture.by('input').value = '帮我整理';
  await fixture.by('send').onclick();
  const answer = fixture.by('messages').children[1].children[0].innerHTML;
  assert.match(answer, /<strong>两个项目<\/strong>/);
  assert.match(answer, /<ol>[\s\S]*<li>A<\/li>[\s\S]*<li>B<\/li>/);
  await fixture.by('copy').onclick();
  assert.equal(fixture.copied(), '**两个项目**\n\n1. A\n2. B');
});

test('workspace-synchronized chunks do not lose delimiters between frames', () => {
  const fixture = quickFixture();
  fixture.live({kind: 'start', turn_id: 'turn', thread_id: 'shared', message: '问题'});
  fixture.live({kind: 'frame', turn_id: 'turn', frame: {type: 'text', content: '**两'}});
  fixture.live({kind: 'frame', turn_id: 'turn', frame: {type: 'text', content: '个项目**'}});
  const answer = fixture.by('messages').children[1].children[0].innerHTML;
  assert.match(answer, /<strong>两个项目<\/strong>/);
  assert.doesNotMatch(answer, /\*\*/);
});

test('remote error survives done, finish, repeated snapshot and history redraw', async () => {
  const fixture = quickFixture();
  const begin = {kind: 'start', turn_id: 'failed', thread_id: 'shared', message: '问题', frames: []};
  const failure = {kind: 'frame', turn_id: 'failed', frame: {type: 'error', message: '离线错误 <img src=x>'}};
  for (let replay = 0; replay < 2; replay++) {
    fixture.live(begin);
    fixture.live(failure);
    fixture.live({kind: 'frame', turn_id: 'failed', frame: {type: 'done'}});
    fixture.live({kind: 'finish', turn_id: 'failed', error: true});
  }
  assert.match(fixture.by('state').textContent, /离线错误/);
  assert.notEqual(fixture.by('state').textContent, '已完成');
  assert.equal(fixture.by('messages').children.length, 2);
  let answer = fixture.by('messages').children[1].children[0];
  assert.equal(answer.children.at(-1).textContent, '离线错误 <img src=x>');
  fixture.focus();
  await new Promise(resolve => setImmediate(resolve));
  answer = fixture.by('messages').children[1].children[0];
  assert.equal(answer.children.at(-1).textContent, '离线错误 <img src=x>');
  fixture.live(begin);
  fixture.live(failure);
  fixture.live({kind: 'finish', turn_id: 'failed', error: true});
  assert.equal(fixture.by('messages').children.length, 2);
  assert.equal(fixture.by('send').disabled, false);
});

test('cancelled truncated replay keeps partial text and a clear incomplete note', async () => {
  const fixture = quickFixture();
  fixture.live({kind: 'start', turn_id: 'cancelled', thread_id: 'shared', message: '问题', frames: [], truncated: true});
  fixture.live({kind: 'frame', turn_id: 'cancelled', frame: {type: 'text', content: '**保留的尾部**'}});
  fixture.live({kind: 'finish', turn_id: 'cancelled', cancelled: true});
  assert.match(fixture.by('state').textContent, /已停止/);
  const answer = fixture.by('messages').children[1].children[0];
  assert.match(answer.innerHTML, /<strong>保留的尾部<\/strong>/);
  assert.match(answer.children.at(-1).textContent, /不完整/);
  fixture.focus();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.by('messages').children.length, 2);
});

test('successful truncated replay is replaced by complete history once finished', async () => {
  const fixture = quickFixture({messages: [{role: 'user', content: '问题'}, {role: 'ai', content: '完整历史回答'}]});
  fixture.live({kind: 'start', turn_id: 'complete', thread_id: 'shared', message: '问题', frames: [], truncated: true});
  fixture.live({kind: 'frame', turn_id: 'complete', frame: {type: 'text', content: '尾部'}});
  fixture.live({kind: 'finish', turn_id: 'complete', error: false});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.by('state').textContent, '已完成');
  assert.equal(fixture.by('messages').children.length, 2);
  assert.match(fixture.by('messages').children[1].children[0].innerHTML, /完整历史回答/);
});

test('success at queue overflow finishes and corrects from history through the real sync bridge', async () => {
  const fixture = quickFixture({bridge: true, messages: [{role: 'user', content: '问题'}, {role: 'ai', content: '完整历史'}]});
  fixture.snapshot([{kind: 'start', turn_id: 'complete', thread_id: 'shared', message: '问题', truncated: true,
    frames: [{type: 'text', content: '部分尾部'}], finish: {error: false, cancelled: false}}]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.by('state').textContent, '已完成');
  assert.equal(fixture.by('send').disabled, false);
  assert.equal(fixture.by('messages').children.length, 2);
  assert.match(fixture.by('messages').children[1].children[0].innerHTML, /完整历史/);
});

test('failed history correction retains the incomplete answer and reports the fetch failure', async () => {
  const fixture = quickFixture({bridge: true, historyError: '离线历史读取失败'});
  fixture.snapshot([{kind: 'start', turn_id: 'complete', thread_id: 'shared', message: '问题', truncated: true,
    frames: [{type: 'text', content: '部分尾部'}], finish: {error: false, cancelled: false}}]);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(fixture.by('state').textContent, /历史读取失败/);
  const answer = fixture.by('messages').children[1].children[0];
  assert.match(answer.innerHTML, /部分尾部/);
  assert.match(answer.children.at(-1).textContent, /不完整/);
});

test('workspace inline scripts remain syntactically valid', () => {
  const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
  for (const [, script] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    if (script.trim()) new vm.Script(script);
  }
});
