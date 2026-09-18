/* Presentation enhancements only. No API calls, model settings or session state. */
(function () {
  'use strict';
  const sidebar = document.getElementById('workspace-sidebar');
  const toggle = document.getElementById('sidebar-toggle');
  const search = document.getElementById('session-search');
  const sessions = document.getElementById('sessions');
  const panel = document.getElementById('panel');
  const input = document.getElementById('input');
  const messages = document.getElementById('messages');
  const footer = document.querySelector('footer');
  const main = document.querySelector('main');
  const home = document.getElementById('home-nav');
  const mobile = matchMedia('(max-width: 760px)');
  home.addEventListener('click', () => document.getElementById('new-chat').click());

  function syncSidebar() {
    const open = mobile.matches ? document.body.classList.contains('sidebar-open')
      : !document.body.classList.contains('sidebar-collapsed');
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? '收起侧栏' : '展开侧栏');
    sidebar.inert = !open;
  }
  toggle.addEventListener('click', () => {
    document.body.classList.toggle(mobile.matches ? 'sidebar-open' : 'sidebar-collapsed');
    syncSidebar();
  });
  mobile.addEventListener('change', syncSidebar);
  syncSidebar();

  function filterSessions() {
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    sessions.querySelectorAll('.session').forEach(row => {
      const title = row.querySelector('.title').textContent;
      row.hidden = !!query && !title.toLocaleLowerCase().includes(query);
      if (!row.hidden) visible++;
    });
    document.getElementById('search-empty').hidden = !query || visible > 0;
  }
  search.addEventListener('input', filterSessions);

  function enhanceControls(root) {
    root.querySelectorAll('.session, .del, .load-more, .load-earlier, .ms-item').forEach(el => {
      el.tabIndex = 0;
      el.setAttribute('role', 'button');
      if (el.classList.contains('del')) {
        el.setAttribute('aria-label', '删除会话：' + el.parentElement.querySelector('.title').textContent);
      }
    });
  }
  // Preserve original click handlers, including deletion's stopPropagation.
  document.addEventListener('keydown', event => {
    const el = event.target;
    if (el.matches('[role="button"]') && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      el.click();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      document.body.classList.remove('sidebar-collapsed');
      document.body.classList.add('sidebar-open');
      syncSidebar();
      search.focus();
    }
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') {
      event.preventDefault();
      document.getElementById('new-chat').click();
    }
    if (event.key === 'Escape' && mobile.matches) {
      document.body.classList.remove('sidebar-open');
      syncSidebar();
      toggle.focus();
    }
  });
  document.addEventListener('click', event => {
    const starter = event.target.closest('.starter');
    if (starter) {
      // Suggestions fill the existing composer; never send on the user's behalf.
      input.value = starter.dataset.prompt;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    }
    if (mobile.matches && !sidebar.contains(event.target) && !toggle.contains(event.target)) {
      document.body.classList.remove('sidebar-open');
      syncSidebar();
    }
    if (event.target.closest('.nav-item, .home-nav, .session, #new-chat')) {
      if (mobile.matches) {
        document.body.classList.remove('sidebar-open');
        syncSidebar();
      }
    }
  });

  function updateHeading() {
    const nav = document.querySelector('.nav-item.active');
    const active = sessions.querySelector('.session.active .title');
    const chatVisible = messages.style.display !== 'none';
    const slot = messages.querySelector('.welcome-composer-slot');
    const atHome = chatVisible && !!slot;
    // Reuse the original composer and its listeners. Rendering may detach it;
    // keep this reference so a conversation repaint never loses the controls.
    if (atHome && footer.parentElement !== slot) slot.appendChild(footer);
    else if (!atHome && footer.parentElement !== document.body) main.after(footer);
    document.getElementById('view-title').textContent = atHome ? '首页'
      : chatVisible ? (active ? active.textContent.replace(/^● /, '') : '聊天')
        : (nav ? nav.querySelector('span').textContent : '工作台');
    document.querySelectorAll('.nav-item, .home-nav').forEach(button => {
      const selected = button === home ? atHome
        : button.dataset.nav === 'chat' ? chatVisible && !atHome
          : !chatVisible && button.classList.contains('active');
      if (button.classList.contains('is-selected') !== selected) button.classList.toggle('is-selected', selected);
      if (selected) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
  }
  new MutationObserver(() => {
    enhanceControls(sessions);
    filterSessions();
    updateHeading();
  }).observe(sessions, { childList: true, subtree: true });
  new MutationObserver(() => enhanceControls(panel)).observe(panel, { childList: true, subtree: true });
  new MutationObserver(updateHeading).observe(document.querySelector('.nav'), {
    attributes: true, attributeFilter: ['class'], subtree: true,
  });
  new MutationObserver(() => {
    enhanceControls(messages);
    updateHeading();
  }).observe(messages, { childList: true, attributes: true, attributeFilter: ['style'] });
  enhanceControls(document);
  updateHeading();
})();
