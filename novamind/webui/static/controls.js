/* Progressive controls: native selects remain the value/event source of truth. */
(function () {
  'use strict';
  let active = null, sequence = 0;
  const upgraded = new WeakMap();
  const chevron = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>';
  function close(restoreFocus = false) {
    if (!active) return;
    const previous = active; active = null;
    previous.close();
    if (restoreFocus && previous.anchor.isConnected) previous.anchor.focus();
  }
  function place(popup, anchor, preferredWidth = 206) {
    const rect = anchor.getBoundingClientRect(), gap = 7, margin = 10;
    const width = Math.min(Math.max(rect.width, preferredWidth), innerWidth - margin * 2);
    popup.style.width = width + 'px';
    const below = innerHeight - rect.bottom - gap - margin, above = rect.top - gap - margin;
    const up = below < Math.min(popup.scrollHeight, 230) && above > below;
    popup.style.maxHeight = Math.min(320, Math.max(80, up ? above : below)) + 'px';
    popup.style.left = Math.max(margin, Math.min(rect.left, innerWidth - width - margin)) + 'px';
    popup.style.top = (up ? Math.max(margin, rect.top - popup.offsetHeight - gap) : rect.bottom + gap) + 'px';
  }
  function enhanceSelect(select) {
    if (upgraded.has(select) || select.multiple || select.size > 1) return;
    const wrap = document.createElement('span'); wrap.className = 'nm-select';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'nm-select-trigger';
    const text = document.createElement('span'); text.className = 'nm-select-value';
    button.append(text); button.insertAdjacentHTML('beforeend', chevron);
    button.setAttribute('role', 'combobox'); button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const popup = document.createElement('div'); popup.className = 'nm-select-popup'; popup.id = 'nm-options-' + (++sequence);
    if (popup.showPopover) popup.setAttribute('popover', 'manual');
    popup.setAttribute('role', 'listbox'); popup.hidden = true; button.setAttribute('aria-controls', popup.id);
    select.before(wrap); wrap.append(select, button);
    select.classList.add('nm-native-select'); select.tabIndex = -1; select.setAttribute('aria-hidden', 'true');
    let focused = -1, options = [], typed = '', typedAt = 0;
    function sync() {
      text.textContent = select.selectedOptions[0]?.textContent || '请选择';
      button.disabled = select.disabled; wrap.hidden = select.hidden;
      button.title = select.title || '';
      const label = select.getAttribute('aria-label') || select.labels?.[0]?.querySelector('span')?.textContent || select.title || text.textContent;
      button.setAttribute('aria-label', label); popup.setAttribute('aria-label', label);
      if (active?.anchor === button) { if (select.hidden || select.disabled || !select.isConnected) close(); else render(); }
    }
    function render() {
      options = Array.from(select.options).filter(option => !option.hidden);
      popup.replaceChildren();
      options.forEach((option, index) => {
        const row = document.createElement('div'); row.className = 'nm-select-option'; row.id = popup.id + '-' + index;
        row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(option.selected));
        row.setAttribute('aria-disabled', String(option.disabled || option.parentElement.disabled));
        const title = document.createElement('span'); title.textContent = option.textContent;
        if(option.dataset.description){
          const copy=document.createElement('span');copy.className='nm-option-copy';
          const description=document.createElement('small');description.textContent=option.dataset.description;
          copy.append(title,description);row.append(copy);
        }else row.append(title);
        if (option.selected) { const check = document.createElement('span'); check.textContent = '✓'; check.setAttribute('aria-hidden', 'true'); row.append(check); }
        row.addEventListener('pointerdown', event => event.preventDefault());
        row.addEventListener('click', () => choose(index));
        row.addEventListener('pointermove', () => highlight(index));
        popup.append(row);
      });
      highlight(Math.max(0, options.findIndex(option => option.selected)));
      place(popup, button);
    }
    function highlight(index) {
      if (!options[index] || options[index].disabled || options[index].parentElement.disabled) return;
      focused = index;
      Array.from(popup.children).forEach((row, i) => row.classList.toggle('is-focused', i === index));
      button.setAttribute('aria-activedescendant', popup.id + '-' + index);
      popup.children[index]?.scrollIntoView({block: 'nearest'});
    }
    function choose(index) {
      const option = options[index];
      if (!option || option.disabled || option.parentElement.disabled) return;
      select.value = option.value;
      select.dispatchEvent(new Event('input', {bubbles: true}));
      select.dispatchEvent(new Event('change', {bubbles: true}));
      sync(); close(true);
    }
    function open() {
      if (select.disabled || select.hidden) return;
      close(); document.body.append(popup); popup.hidden = false;
      if (popup.showPopover) popup.showPopover();
      button.setAttribute('aria-expanded', 'true');
      active = {anchor: button, popup, close() {
        if (popup.hidePopover && popup.matches(':popover-open')) popup.hidePopover();
        popup.remove(); popup.hidden = true; button.setAttribute('aria-expanded', 'false'); button.removeAttribute('aria-activedescendant');
      }};
      render();
    }
    button.addEventListener('click', event => { event.preventDefault(); active?.anchor === button ? close() : open(); });
    button.addEventListener('keydown', event => {
      if (event.key === 'Tab') { close(); return; }
      if (event.key === 'Escape') { if (active?.anchor === button) { event.preventDefault(); event.stopPropagation(); close(true); } return; }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        if (active?.anchor !== button) open();
        const available = options.map((option, i) => (!option.disabled && !option.parentElement.disabled ? i : -1)).filter(i => i >= 0);
        let next = available.indexOf(focused);
        next = event.key === 'Home' ? 0 : event.key === 'End' ? available.length - 1 : Math.max(0, Math.min(available.length - 1, next + (event.key === 'ArrowUp' ? -1 : 1)));
        highlight(available[next]);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault(); active?.anchor === button ? choose(focused) : open();
      } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault(); if (active?.anchor !== button) open();
        typed = Date.now() - typedAt < 700 ? typed + event.key : event.key; typedAt = Date.now();
        const index = options.findIndex(option => !option.disabled && option.textContent.toLocaleLowerCase().startsWith(typed.toLocaleLowerCase()));
        if (index >= 0) highlight(index);
      }
    });
    // Programmatic .value / .selectedIndex assignments must update the trigger too.
    for (const property of ['value', 'selectedIndex']) {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, property);
      Object.defineProperty(select, property, {configurable: true, get() {return descriptor.get.call(this);}, set(value) {descriptor.set.call(this, value); sync();}});
    }
    select.addEventListener('change', sync);
    new MutationObserver(sync).observe(select, {attributes: true, childList: true, subtree: true, characterData: true});
    upgraded.set(select, sync); sync();
  }
  function scan(root = document) {
    if (root.nodeType !== 1 && root !== document) return;
    if (root.matches?.('select')) enhanceSelect(root);
    root.querySelectorAll('select').forEach(enhanceSelect);
  }
  // Disclosures keep low-frequency settings out of the main working surface.
  document.addEventListener('click', event => {
    // Selecting may rebuild/remove the clicked option before the click bubbles.
    // Use the original propagation path, not its now-detached parent chain.
    if (event.composedPath().some(node => node.classList?.contains('nm-select-popup'))) return;
    const details = event.target.closest('details.nm-disclosure');
    document.querySelectorAll('details.nm-disclosure[open]').forEach(item => { if (item !== details) item.open = false; });
    if (event.target.closest('.nm-menu button') && details) details.open = false;
  });
  // The top layer avoids clipping inside a scrolled tool page or a small window,
  // while preserving the DOM ancestry used by existing action delegation.
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (!details.matches?.('details.nm-disclosure')) return;
    const popup = details.querySelector(':scope > .nm-popover');
    if (!popup?.showPopover) return; // Native details remains a working fallback.
    if (details.open) {
      popup.setAttribute('popover', 'manual');
      popup.showPopover();
      place(popup, details.querySelector('summary'), details.classList.contains('composer-options') ? 255 : 235);
    } else if (popup.matches(':popover-open')) popup.hidePopover();
  }, true);
  document.addEventListener('pointerdown', event => { if (active && !active.popup.contains(event.target) && !active.anchor.contains(event.target)) close(); });
  document.addEventListener('focusin', event => { if (active && !active.popup.contains(event.target) && !active.anchor.contains(event.target)) close(); });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || active) return;
    const opened = document.querySelector('details.nm-disclosure[open]');
    if (opened) { event.preventDefault(); event.stopImmediatePropagation(); opened.open = false; opened.querySelector('summary').focus(); }
  });
  function closeDisclosures(event) {
    const target = event?.target;
    document.querySelectorAll('details.nm-disclosure[open]').forEach(details => {
      const popup = details.querySelector('.nm-popover');
      if (!(target instanceof Node) || (!popup?.contains(target) && !active?.popup.contains(target))) details.open = false;
    });
  }
  addEventListener('resize', event => {close(); closeDisclosures(event);});
  document.addEventListener('scroll', event => {
    if (active && !active.popup.contains(event.target)) close();
    closeDisclosures(event);
  }, true);
  new MutationObserver(records => {
    if (active && !active.anchor.isConnected) close();
    for (const record of records) for (const node of record.addedNodes) scan(node);
  }).observe(document.body, {childList: true, subtree: true});
  scan();
  let toastTimer;
  function notify(message) {
    clearTimeout(toastTimer); document.querySelector('.control-toast')?.remove();
    const toast = document.createElement('div'); toast.className = 'control-toast';
    toast.setAttribute('role', 'status'); toast.textContent = message; document.body.append(toast);
    toastTimer = setTimeout(() => toast.remove(), 4500);
  }
  window.NovaMindControls = {enhance: scan, close, notify};
})();
