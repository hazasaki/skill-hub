(() => {
  const root = document.documentElement;
  const preference = (key, values, fallback) => {
    try {
      const value = localStorage.getItem(key);
      return values.includes(value) ? value : fallback;
    } catch {
      return fallback;
    }
  };
  const save = (key, value) => {
    try { localStorage.setItem(key, value); } catch { /* Storage may be unavailable. */ }
  };
  const palette = matchMedia('(prefers-color-scheme: dark)');
  let theme = preference('skillhub.theme', ['system', 'light', 'dark'], 'system');
  root.lang = preference('skillhub.language', ['en', 'vi'], 'en');
  window.skillHubText = (english, vietnamese) => root.lang === 'vi' ? vietnamese : english;

  function applyTheme() {
    const resolved = theme === 'system' ? (palette.matches ? 'dark' : 'light') : theme;
    root.dataset.theme = resolved;
    document.querySelector('meta[name="theme-color"]').content = resolved === 'dark' ? '#090d16' : '#f5f7fb';
  }

  applyTheme();
  palette.addEventListener('change', applyTheme);

  window.addEventListener('DOMContentLoaded', () => {
    const text = window.skillHubText;
    const layout = document.querySelector('.app-layout');
    const sidebar = document.getElementById('dashboard-sidebar');
    const nav = document.getElementById('primary-navigation');
    const toggle = document.getElementById('mobile-menu-toggle');
    const close = document.getElementById('navigation-close');
    const scrim = document.getElementById('navigation-scrim');
    const main = document.querySelector('.main-wrapper');
    const footer = document.querySelector('.app-footer');
    const mobile = matchMedia('(max-width: 860px)');
    const language = document.getElementById('dashboard-language');
    const themePicker = document.getElementById('dashboard-theme');
    const tabs = Array.from(nav.querySelectorAll('[role="tab"]'));
    const localized = window.skillHubLabels || {};
    const labels = [];
    const excluded = '#view-routing, #view-graph, .route-quicklaunch-card, #btn-open-router-studio, #dashboard-language, script';
    const translate = (value, apply) => {
      const vietnamese = Object.hasOwn(localized, value.trim()) ? localized[value.trim()] : null;
      if (vietnamese) labels.push(() => apply(text(value, value.replace(value.trim(), () => vietnamese))));
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.parentElement || node.parentElement.closest(excluded)) continue;
      const initial = node.data;
      let current = initial;
      translate(initial, value => {
        if (node.data === current) node.data = current = value;
      });
    }
    for (const element of document.querySelectorAll('[aria-label], [placeholder], [title], [alt]')) {
      if (element.closest(excluded)) continue;
      for (const attribute of ['aria-label', 'placeholder', 'title', 'alt']) {
        const value = element.getAttribute(attribute);
        if (value) translate(value, translated => element.setAttribute(attribute, translated));
      }
    }
    let expanded = preference('skillhub.sidebar', ['expanded', 'collapsed'], 'expanded') === 'expanded';
    let drawerOpen = false;
    let previousFocus;
    let arrowSelecting = false;

    function updateTitle() {
      const active = nav.querySelector('[aria-selected="true"]');
      if (active) document.getElementById('view-title').textContent = active.querySelector('.nav-label').textContent;
      document.getElementById('view-eyebrow').textContent = text('Dashboard', 'Bảng điều khiển');
    }

    function refreshNavigation() {
      const open = mobile.matches ? drawerOpen : expanded;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? text('Close navigation', 'Đóng điều hướng') : text('Open navigation', 'Mở điều hướng'));
      toggle.title = text('Toggle navigation (Ctrl/⌘+B)', 'Bật/tắt điều hướng (Ctrl/⌘+B)');
      layout.classList.toggle('rail-collapsed', !mobile.matches && !expanded);
      layout.classList.toggle('drawer-open', mobile.matches && drawerOpen);
      sidebar.inert = mobile.matches && !drawerOpen;
      main.inert = mobile.matches && drawerOpen;
      footer.inert = main.inert;
      document.body.classList.toggle('dashboard-drawer-open', mobile.matches && drawerOpen);
      if (mobile.matches && drawerOpen) {
        sidebar.setAttribute('role', 'dialog');
        sidebar.setAttribute('aria-modal', 'true');
      } else {
        sidebar.removeAttribute('role');
        sidebar.removeAttribute('aria-modal');
      }
      sidebar.setAttribute('aria-label', text('Main navigation', 'Điều hướng chính'));
      for (const tab of tabs) {
        const label = tab.querySelector('.nav-label').textContent;
        tab.setAttribute('aria-label', label);
        tab.title = label;
      }
    }

    function localize() {
      labels.forEach(apply => apply());
      document.title = text('Skill Hub · Dashboard', 'Skill Hub · Bảng điều khiển');
      language.value = root.lang;
      refreshNavigation();
      updateTitle();
    }

    function setDrawer(open, restore = true) {
      if (drawerOpen === open) return;
      if (open) previousFocus = document.activeElement;
      drawerOpen = open;
      refreshNavigation();
      if (open) nav.querySelector('[aria-selected="true"]')?.focus();
      else if (restore) (previousFocus instanceof HTMLElement && main.contains(previousFocus) ? previousFocus : toggle).focus();
    }

    function toggleNavigation() {
      if (mobile.matches) {
        setDrawer(!drawerOpen);
      } else {
        expanded = !expanded;
        save('skillhub.sidebar', expanded ? 'expanded' : 'collapsed');
        refreshNavigation();
      }
    }

    function setTabStops() {
      for (const tab of tabs) tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
    }

    toggle.addEventListener('click', toggleNavigation);
    close.addEventListener('click', () => setDrawer(false));
    scrim.addEventListener('click', () => setDrawer(false));
    mobile.addEventListener('change', () => {
      const wasOpen = drawerOpen;
      setDrawer(false, false);
      refreshNavigation();
      if (mobile.matches && sidebar.contains(document.activeElement)) toggle.focus();
      if (wasOpen && !mobile.matches) toggle.focus();
    });
    nav.addEventListener('click', event => {
      if (!event.target.closest('[role="tab"]')) return;
      setTabStops();
      if (drawerOpen && !arrowSelecting) setDrawer(false);
    });
    nav.addEventListener('keydown', event => {
      const index = tabs.indexOf(document.activeElement);
      if (index < 0) return;
      const direction = {ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1}[event.key];
      if (!direction && event.key !== 'Home' && event.key !== 'End') return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + direction + tabs.length) % tabs.length;
      tabs[next].focus();
      arrowSelecting = true;
      tabs[next].click();
      arrowSelecting = false;
    });
    new MutationObserver(setTabStops).observe(nav, {subtree: true, attributes: true, attributeFilter: ['aria-selected']});

    document.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'b') {
        event.preventDefault();
        if (!event.repeat) toggleNavigation();
      }
      if (!drawerOpen) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        setDrawer(false);
      }
      if (event.key === 'Tab') {
        const active = nav.querySelector('[tabindex="0"]');
        if (event.shiftKey && document.activeElement === close) {
          event.preventDefault();
          active?.focus();
        } else if (!event.shiftKey && document.activeElement === active) {
          event.preventDefault();
          close.focus();
        }
      }
    });
    language.addEventListener('change', () => {
      root.lang = language.value === 'vi' ? 'vi' : 'en';
      save('skillhub.language', root.lang);
      localize();
      window.dispatchEvent(new CustomEvent('skillhub:languagechange', {detail: {lang: root.lang}}));
    });
    themePicker.addEventListener('change', () => {
      theme = themePicker.value;
      save('skillhub.theme', theme);
      applyTheme();
    });

    themePicker.value = theme;
    localize();
    setTabStops();
  });
})();
