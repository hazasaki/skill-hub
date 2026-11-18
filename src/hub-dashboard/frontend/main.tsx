import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { AnimatedSidebar } from './components/ui/animated-sidebar';

declare global {
  interface Window {
    switchView?(view: string): void;
    skillHubSidebar?: { setView(view: string): void };
  }
}

const anchor = document.getElementById('dashboard-sidebar-root');
if (!anchor) throw new Error('Missing Dashboard sidebar root');

const root = createRoot(anchor);
let activeView = 'overview';
const onSelect = (view: string) => window.switchView?.(view);

function renderSidebar() {
  const lang = document.documentElement.lang === 'vi' ? 'vi' : 'en';
  // The existing Dashboard reads navigation labels immediately after updating the view.
  flushSync(() => {
    root.render(<AnimatedSidebar lang={lang} activeView={activeView} onSelect={onSelect} />);
  });
}

window.skillHubSidebar = {
  setView(view) {
    if (activeView === view) return;
    activeView = view;
    renderSidebar();
  },
};

renderSidebar();
window.addEventListener('skillhub:languagechange', renderSidebar);
