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
    document.querySelector('meta[name="theme-color"]').content = resolved === 'dark' ? '#0a0a0a' : '#f5f7fb';
  }

  applyTheme();
  palette.addEventListener('change', applyTheme);

  window.addEventListener('DOMContentLoaded', () => {
    const text = window.skillHubText;
    const language = document.getElementById('dashboard-language');
    const themePicker = document.getElementById('dashboard-theme');
    const localized = window.skillHubLabels || {};
    const labels = [];
    const excluded = '#dashboard-sidebar-root, #view-routing, #view-graph, .route-quicklaunch-card, #btn-open-router-studio, #dashboard-language, script';
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
    function updateTitle() {
      const activeLabel = document.querySelector('#primary-navigation [aria-selected="true"] .nav-label');
      if (activeLabel) document.getElementById('view-title').textContent = activeLabel.textContent;
      document.getElementById('view-eyebrow').textContent = text('Dashboard', 'Bảng điều khiển');
    }

    function localize() {
      labels.forEach(apply => apply());
      document.title = text('Skill Hub · Dashboard', 'Skill Hub · Bảng điều khiển');
      language.value = root.lang;
    }

    language.addEventListener('change', () => {
      root.lang = language.value === 'vi' ? 'vi' : 'en';
      save('skillhub.language', root.lang);
      localize();
      window.dispatchEvent(new CustomEvent('skillhub:languagechange', {detail: {lang: root.lang}}));
      updateTitle();
    });
    themePicker.addEventListener('change', () => {
      theme = themePicker.value;
      save('skillhub.theme', theme);
      applyTheme();
    });

    themePicker.value = theme;
    localize();
    updateTitle();
  });
})();
