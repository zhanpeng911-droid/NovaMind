// Presentation-layer regression tests; no browser, server or model calls.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
  const observers = [];
  const make = (classes = []) => {
    const values = new Set(classes);
    return {
      dataset: {}, style: {}, attrs: {}, handlers: {}, textContent: '', value: '',
      classList: {
        contains: name => values.has(name),
        add: name => values.add(name), remove: name => values.delete(name),
        toggle(name, force = !values.has(name)) { force ? values.add(name) : values.delete(name); },
      },
      setAttribute(name, value) { this.attrs[name] = value; },
      removeAttribute(name) { delete this.attrs[name]; },
      addEventListener(name, fn) { this.handlers[name] = fn; },
      querySelector: () => null, querySelectorAll: () => [],
      appendChild(el) { el.parentElement = this; },
      click() { this.handlers.click?.(); },
    };
  };
  const ids = Object.fromEntries(['workspace-sidebar', 'sidebar-toggle', 'session-search',
    'sessions', 'panel', 'input', 'messages', 'home-nav', 'new-chat', 'view-title', 'search-empty']
    .map(id => [id, make()]));
  const body = make();
  const footer = make();
  footer.parentElement = body;
  const main = make();
  main.after = el => { el.parentElement = body; };
  const navRoot = make();
  const nav = ['chat', 'skills', 'monitor', 'doctor'].map(name => {
    const el = make(['nav-item', ...(name === 'chat' ? ['active'] : [])]);
    el.dataset.nav = name;
    el.querySelector = () => ({ textContent: name });
    return el;
  });
  const home = ids['home-nav'];
  let slot = make();
  ids.messages.querySelector = () => slot;
  const document = {
    body, getElementById: id => ids[id], addEventListener() {},
    querySelector(selector) {
      if (selector === 'footer') return footer;
      if (selector === 'main') return main;
      if (selector === '.nav') return navRoot;
      if (selector === '.nav-item.active') return nav.find(el => el.classList.contains('active'));
      return null;
    },
    querySelectorAll(selector) { return selector === '.nav-item, .home-nav' ? [...nav, home] : []; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../novamind/webui/static/workspace.js'), 'utf8'), {
    document, matchMedia: () => ({ matches: false, addEventListener() {} }),
    MutationObserver: class {
      constructor(fn) { this.fn = fn; }
      observe(target) { observers.push({ target, fn: this.fn }); }
    },
  });
  return { ids, nav, body, footer, home, slot,
    repaint(nextSlot) { slot = nextSlot; observers.filter(o => o.target === ids.messages).forEach(o => o.fn()); },
  };
}

test('home reuses original composer; chat repaint restores the same node', () => {
  const ui = fixture();
  assert.equal(ui.footer.parentElement, ui.slot);
  assert.equal(ui.home.attrs['aria-current'], 'page');
  assert.equal(ui.nav[0].attrs['aria-current'], undefined);
  ui.repaint(null);
  assert.equal(ui.footer.parentElement, ui.body);
  assert.equal(ui.home.attrs['aria-current'], undefined);
  assert.equal(ui.nav[0].attrs['aria-current'], 'page');
  ui.repaint(ui.slot);
  assert.equal(ui.footer.parentElement, ui.slot);
});

test('opening a tool panel restores composer and preserves original active classes', () => {
  const ui = fixture();
  ui.ids.messages.style.display = 'none';
  ui.nav[0].classList.remove('active');
  ui.nav[1].classList.add('active');
  ui.repaint(ui.slot);
  assert.equal(ui.footer.parentElement, ui.body);
  assert.equal(ui.nav[1].attrs['aria-current'], 'page');
  assert.equal(ui.nav[1].classList.contains('active'), true);
  assert.equal(ui.home.attrs['aria-current'], undefined);
  assert.equal(ui.ids['view-title'].textContent, 'skills');
});

test('home entry delegates to original new-chat action exactly once', () => {
  const ui = fixture();
  let clicks = 0;
  ui.ids['new-chat'].handlers.click = () => clicks++;
  ui.home.click();
  assert.equal(clicks, 1);
});
