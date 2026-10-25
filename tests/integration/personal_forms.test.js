'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const staticDir = path.join(__dirname, '../../novamind/webui/static');

function element(tag) {
  const listeners = {};
  return {tagName: tag.toUpperCase(), children: [], dataset: {}, attributes: {}, value: '',
    append(...nodes) {this.children.push(...nodes);}, setAttribute(k, v) {this.attributes[k] = v;},
    addEventListener(k, fn) {(listeners[k] ||= []).push(fn);},
    emit(k) {for (const fn of listeners[k] || []) fn({});}, closest() {return null;},
    focus() {}, animate() {}, showModal() {}, close() {}, remove() {},
  };
}
function common() {
  const body = element('body'), stored = new Map(), events = {};
  const context = {window: {}, document: {body, createElement: element}, crypto: {randomUUID: () => 'test'},
    TextDecoder, localStorage: {getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v)},
    addEventListener: (k, fn) => {events[k] = fn;}, matchMedia: () => ({matches: false}),
    requestAnimationFrame: fn => fn()};
  vm.runInNewContext(fs.readFileSync(path.join(staticDir, 'ui_common.js'), 'utf8'), context);
  return {api: context.window.NovaMindUI, body, events};
}
function all(node) {return [node, ...node.children.flatMap(all)];}

test('form preview follows input changes without changing the submitted values', async () => {
  const {api, body} = common();
  const result = api.dialogForm({title: '日程', fields: [{name: 'time', label: '事项时间', value: '15:00'}],
    preview: values => '预计 ' + values.time});
  const form = body.children[0].children[0];
  const hint = all(form).find(node => node.className === 'dialog-preview');
  assert.ok(hint);
  assert.equal(hint.textContent, '预计 15:00');
  const input = all(form).find(node => node.name === 'time');
  input.value = '16:30'; form.emit('input');
  assert.equal(hint.textContent, '预计 16:30');
  await form.onsubmit({preventDefault() {}});
  assert.equal((await result).time, '16:30');
});

test('thinking picker explains auto, provider mappings and safe fallback', () => {
  const {api, events} = common(), host = element('span');
  const select = api.reasoningPicker(host);
  assert.match(select.title, /寒暄/);
  assert.match(select.title, /默认/);
  assert.ok(select.children.every(option => option.dataset.description));
  select.value = 'max'; select.onchange();
  assert.match(select.title, /深入/);
  events.storage({key: 'novamind_reasoning', newValue: 'invalid-level'});
  assert.equal(select.value, 'auto');
});

function scheduleForm() {
  const source = fs.readFileSync(path.join(staticDir, 'personal_ui.js'), 'utf8');
  const start = source.indexOf('function reminderPreview(');
  assert.notEqual(start, -1);
  const end = source.indexOf('async function render(', start);
  const calls = [];
  const context = {NovaMindUI: {dialogForm: options => options}, crypto: {randomUUID: () => 'offline'},
    json: (url, options) => {calls.push({url, ...options}); return {};}};
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return {form: context.editTask(), context, calls};
}

test('schedule uses event time, previews previous-day reminder and keeps API semantics', async () => {
  const {form, calls} = scheduleForm(), options = await form;
  assert.equal(options.fields.find(field => field.name === 'time').label, '事项时间（可选）');
  const values = {description: '会议', date: '2026-10-24', time: '00:15', remind: '30', repeat: ''};
  const preview = options.preview(values);
  assert.match(preview, /2026[\/年-]10[\/月-]23/);
  assert.match(preview, /23:45/);
  await options.onSubmit(values);
  const body = JSON.parse(calls[0].body);
  assert.equal(body.target_time, '2026-10-24 00:15:00');
  assert.equal(body.remind_minutes, 30);
});

test('schedule preview distinguishes todo and rejects normalized invalid dates', async () => {
  const {form} = scheduleForm(), options = await form;
  assert.match(options.preview({date: '2026-10-24', time: '', remind: '0'}), /待办.*不.*定时/);
  assert.match(options.preview({date: '2026-02-30', time: '15:00', remind: '0'}), /有效/);
  assert.match(options.preview({date: '2026-10-24', time: '15:00', remind: '-1'}), /有效/);
});
