import { Activity, Book, ChartNoAxesColumn, CircleCheck, Layers, LayoutGrid, Server, Settings, Share2, Users } from 'lucide-react';
import { motion, useReducedMotion } from 'motion/react';
import { useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

const groups = [
  {
    label: 'Workspace',
    items: [
      { id: 'overview', label: 'Overview', icon: LayoutGrid, pill: 'pill-overview-status', initial: 'Live' },
      { id: 'routing', label: 'Router Studio', icon: Activity, pill: 'pill-routing-badge', initial: 'Studio' },
    ],
  },
  {
    label: 'Catalog',
    items: [
      { id: 'skills', label: 'Skills', icon: Layers, pill: 'pill-count-skills', initial: '—' },
      { id: 'library', label: 'Prompt Library', icon: Book, pill: 'pill-count-prompts', initial: '—' },
      { id: 'graph', label: 'Capability Graph', icon: Share2, pill: 'pill-count-graph', initial: '5 types' },
    ],
  },
  {
    label: 'Runtime',
    items: [
      { id: 'agents', label: 'Agents', icon: Users, pill: 'pill-count-agents', initial: '—' },
      { id: 'integrations', label: 'Integrations', icon: Server, pill: 'pill-count-mcp', initial: '—' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { id: 'telemetry', label: 'Telemetry', icon: ChartNoAxesColumn, pill: 'pill-count-telemetry', initial: 'Log' },
      { id: 'quality', label: 'Catalog Quality', icon: CircleCheck, pill: 'pill-quality-badge', initial: 'Audit' },
      { id: 'config', label: 'Settings & Quarantine', icon: Settings, pill: 'pill-count-quarantine', initial: '0' },
    ],
  },
];

const railPreference = 'skillhub.sidebar';

function readExpanded() {
  try {
    return localStorage.getItem(railPreference) !== 'collapsed';
  } catch {
    return true;
  }
}

function SidebarSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="nav-group" role="presentation">
      <div className="nav-group-label" aria-hidden="true">{title}</div>
      <div className="nav-group-items" role="presentation">{children}</div>
    </div>
  );
}

type SidebarProps = {
  lang: 'en' | 'vi';
  activeView: string;
  onSelect: (view: string) => void;
};

export function AnimatedSidebar({ lang, activeView, onSelect }: SidebarProps) {
  const [mobileQuery] = useState(() => matchMedia('(max-width: 860px)'));
  const [mobile, setMobile] = useState(mobileQuery.matches);
  const [expanded, setExpanded] = useState(readExpanded);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const csrfBadge = useRef<HTMLSpanElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const wasDrawerOpen = useRef(false);
  const reduceMotion = useReducedMotion();
  const drawerVisible = mobile && drawerOpen;
  const open = mobile ? drawerVisible : expanded;
  const labels = (window as Window & { skillHubLabels?: Readonly<Record<string, string>> }).skillHubLabels;
  const t = (label: string) => lang === 'vi' ? labels?.[label] ?? label : label;

  useLayoutEffect(() => {
    function resize() {
      setDrawerOpen(false);
      setMobile(mobileQuery.matches);
    }
    mobileQuery.addEventListener('change', resize);
    return () => mobileQuery.removeEventListener('change', resize);
  }, [mobileQuery]);

  useLayoutEffect(() => {
    try { localStorage.setItem(railPreference, expanded ? 'expanded' : 'collapsed'); }
    catch { /* The current session works with storage disabled. */ }
  }, [expanded]);

  useLayoutEffect(() => {
    const toggle = document.getElementById('mobile-menu-toggle');
    function toggleNavigation() {
      if (mobile) setDrawerOpen(value => !value);
      else setExpanded(value => !value);
    }
    function shortcut(event: globalThis.KeyboardEvent) {
      if (
        !event.defaultPrevented && (event.ctrlKey || event.metaKey) &&
        !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'b'
      ) {
        event.preventDefault();
        if (!event.repeat) toggleNavigation();
      }
    }
    toggle?.addEventListener('click', toggleNavigation);
    document.addEventListener('keydown', shortcut);
    return () => {
      toggle?.removeEventListener('click', toggleNavigation);
      document.removeEventListener('keydown', shortcut);
    };
  }, [mobile]);

  useLayoutEffect(() => {
    const toggle = document.getElementById('mobile-menu-toggle');
    toggle?.setAttribute('aria-expanded', String(open));
    toggle?.setAttribute('aria-label', lang === 'vi'
      ? open ? 'Đóng điều hướng' : 'Mở điều hướng'
      : open ? 'Close navigation' : 'Open navigation');
    toggle?.setAttribute('aria-keyshortcuts', 'Control+b Meta+b');
    toggle?.setAttribute('title', lang === 'vi' ? 'Bật/tắt điều hướng (Ctrl/⌘+B)' : 'Toggle navigation (Ctrl/⌘+B)');
  }, [lang, open]);

  useLayoutEffect(() => {
    const layout = sidebar.current?.closest('.app-layout');
    const main = layout?.querySelector<HTMLElement>('.main-wrapper');
    const footer = document.querySelector<HTMLElement>('.app-footer');
    const opener = document.getElementById('mobile-menu-toggle');
    const focus = document.activeElement;
    const sidebarFocused = sidebar.current?.contains(focus);
    const justOpened = drawerVisible && !wasDrawerOpen.current;

    if (justOpened) previousFocus.current = focus instanceof HTMLElement && main?.contains(focus) ? focus : opener;
    if (sidebar.current) sidebar.current.inert = mobile && !drawerVisible;
    if (main) main.inert = drawerVisible;
    if (footer) footer.inert = drawerVisible;
    layout?.classList.toggle('rail-collapsed', !mobile && !expanded);
    layout?.classList.toggle('drawer-open', drawerVisible);
    document.body.classList.toggle('dashboard-drawer-open', drawerVisible);

    if (justOpened) {
      sidebar.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus();
    } else if (!drawerVisible && wasDrawerOpen.current) {
      (previousFocus.current?.isConnected ? previousFocus.current : opener)?.focus();
    } else if (mobile && !drawerVisible && sidebarFocused) {
      opener?.focus();
    }
    wasDrawerOpen.current = drawerVisible;
  }, [drawerVisible, expanded, mobile]);

  const loadingLabel = t('CSRF: Loading…');
  useLayoutEffect(() => {
    // The existing Dashboard owns the badge once the request has completed.
    if (csrfBadge.current?.classList.contains('badge-muted')) csrfBadge.current.textContent = loadingLabel;
  }, [loadingLabel]);

  function moveSelection(event: KeyboardEvent<HTMLElement>) {
    const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    const focused = tabs.findIndex(tab => tab === event.target);
    if (focused < 0) return;

    const direction = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
    if (!direction && event.key !== 'Home' && event.key !== 'End') return;

    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End'
      ? tabs.length - 1 : (focused + (direction ?? 0) + tabs.length) % tabs.length;
    const tab = tabs[next];
    const view = tab.dataset.view;
    tab.focus();
    if (view) onSelect(view);
  }

  function handleDrawerKeys(event: KeyboardEvent<HTMLElement>) {
    if (!drawerVisible) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setDrawerOpen(false);
    } else if (event.key === 'Tab') {
      const selected = sidebar.current?.querySelector<HTMLElement>('[tabindex="0"]');
      if (event.shiftKey && event.target === closeButton.current) {
        event.preventDefault();
        selected?.focus();
      } else if (!event.shiftKey && event.target === selected) {
        event.preventDefault();
        closeButton.current?.focus();
      }
    }
  }

  return (
    <>
      <aside
        ref={sidebar}
        id="dashboard-sidebar"
        className="sidebar"
        data-component="animated-sidebar"
        data-state={open ? 'expanded' : 'collapsed'}
        role={drawerVisible ? 'dialog' : undefined}
        aria-modal={drawerVisible || undefined}
        aria-label={t('Main navigation')}
        onKeyDown={handleDrawerKeys}
      >
        <div className="brand">
          <img src="skill-hub.png" width={42} height={42} alt="" />
          <div className="brand-text">
            <h1 className="brand-title">Skill Hub</h1>
            <span className="brand-subtitle">{t('Dashboard')}</span>
          </div>
          <button ref={closeButton} id="navigation-close" type="button" className="navigation-close btn btn-icon btn-subtle" aria-label={t('Close navigation')} onClick={() => setDrawerOpen(false)}>×</button>
        </div>
        <nav id="primary-navigation" className="nav-menu" role="tablist" aria-orientation="vertical" aria-label={t('Dashboard views')} onKeyDown={moveSelection}>
          {groups.map(group => (
            <SidebarSection key={group.label} title={t(group.label)}>
              {group.items.map(({ id, label, icon: Icon, pill, initial }) => {
                const title = t(label);
                const selected = id === activeView;
                return (
                  <button
                    key={id}
                    type="button"
                    className={selected ? 'nav-item active' : 'nav-item'}
                    role="tab"
                    id={`tab-${id}`}
                    aria-selected={selected}
                    aria-controls={`view-${id}`}
                    aria-label={title}
                    title={title}
                    data-view={id}
                    tabIndex={selected ? 0 : -1}
                    onClick={() => {
                      onSelect(id);
                      if (drawerVisible) setDrawerOpen(false);
                    }}
                  >
                    <motion.span aria-hidden="true" className="tw:inline-flex tw:shrink-0" initial={false} animate={{ scale: !reduceMotion && selected ? 1.1 : 1 }} transition={{ duration: reduceMotion ? 0 : 0.16 }}>
                      <Icon className="nav-icon" />
                    </motion.span>
                    <span className="nav-label">{title}</span>
                    <span className={id === 'routing' ? 'nav-pill nav-pill-highlight' : 'nav-pill'} id={pill}>{t(initial)}</span>
                  </button>
                );
              })}
            </SidebarSection>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="safety-mode">
            <span className="safety-dot" aria-hidden="true" />
            <div>
              <strong>{t('Protected workspace')}</strong>
              <small>{t('Read access and quarantine')}</small>
            </div>
          </div>
          <div className="csrf-indicator">
            <span ref={csrfBadge} className="badge badge-muted" id="csrf-badge" />
          </div>
        </div>
      </aside>
      <div id="navigation-scrim" className="navigation-scrim" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
    </>
  );
}
