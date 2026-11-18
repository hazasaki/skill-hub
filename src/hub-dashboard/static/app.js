/**
 * Skill Hub Management Console - app.js
 * Browser-native JS: 10 views, safe text rendering, interactive SVG graph,
 * quarantine intake (preview & confirmed apply), strict CSP compliance,
 * zero injected inline styles, full keyboard accessibility & focus management.
 */

'use strict';

// ============================================================================
// State Management
// ============================================================================

const state = {
  currentView: null,
  csrfToken: '',
  capabilities: [],
  owned: null,
  autoRefresh: true,
  refreshTimer: null,
  isRefreshing: false,

  // Active focus trackers for WCAG focus restoration
  activeModalOpener: null,
  activeDrawerOpener: null,

  // View states
  skills: { mode: 'fts', page: 1, limit: 25, q: '', domain: '', total: 0, items: [] },
  library: { kind: 'prompt', page: 1, limit: 25, q: '', domain: '', total: 0, items: [] },
  agents: { q: '', total: 0, items: [] },
  routing: { source: 'all', items: [] },
  routeStudio: {
    mode: 'tempr',
    includeGraph: false,
    taskId: '',
    stateText: '',
    isRunning: false,
    lastScores: [],
    scoresFilter: '',
    scoresKindFilter: 'all'
  },
  telemetry: { source: 'all', kind: 'all', items: [] },
  graph: {
    zoom: 1.0,
    panX: 0,
    panY: 0,
    typeFilter: 'all',
    searchQuery: '',
    depth: 1,
    focusedNodeId: null,
    selectedNodeId: null,
    selectedEdgeId: null,
    nodes: [],
    edges: [],
    nodeMap: new Map(),
    isDragging: false,
    startX: 0,
    startY: 0
  },

  // Modal state
  modal: {
    planId: null,
    planExpiry: 0,
    timerId: null,
    step: 1
  }
};

// ============================================================================
// Utilities: HTML Escaping, Enums & Safe Text Rendering
// ============================================================================

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function truncateStr(str, maxLen = 40) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

function formatIsoTime(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return escapeHtml(isoStr);
    return d.toLocaleString('vi-VN', {
      timeZone: 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    }) + ' UTC';
  } catch (e) {
    return escapeHtml(isoStr);
  }
}

/**
 * Maps actual router producer outcome enums to user-facing badges.
 * Valid abstention (ABSTAIN_LOW_CONFIDENCE, NO_SKILL) is NOT an error.
 */
function formatRouteOutcome(outcome) {
  if (!outcome) {
    return { text: 'Unknown', label: 'Chưa ghi nhận', badgeClass: 'badge-subtle', isError: false };
  }
  const upper = String(outcome).toUpperCase();
  switch (upper) {
    case 'SELECTED':
      return { text: 'SELECTED', label: 'Đã chọn', badgeClass: 'badge-alive', isError: false };
    case 'OK':
      return { text: 'OK', label: 'Hoàn tất', badgeClass: 'badge-alive', isError: false };
    case 'ABSTAIN_LOW_CONFIDENCE':
      return { text: 'ABSTAIN_LOW_CONFIDENCE', label: 'Từ chối (độ tin cậy thấp)', badgeClass: 'badge-warning', isError: false };
    case 'NO_SKILL':
      return { text: 'NO_SKILL', label: 'Không cần skill', badgeClass: 'badge-subtle', isError: false };
    case 'POLICY_DENIED':
      return { text: 'POLICY_DENIED', label: 'Chính sách từ chối', badgeClass: 'badge-warning', isError: false };
    case 'FALLBACK':
      return { text: 'FALLBACK', label: 'Dự phòng', badgeClass: 'badge-warning', isError: false };
    case 'ROUTER_ERROR':
    case 'ERROR':
      return { text: 'ROUTER_ERROR', label: 'Lỗi Router', badgeClass: 'badge-blocked', isError: true };
    default:
      return { text: outcome, label: outcome, badgeClass: 'badge-subtle', isError: false };
  }
}

function renderSafeMarkdownText(text) {
  if (!text) return '<p class="text-muted">Không có nội dung</p>';
  const lines = text.split('\n');
  let html = '';
  let inCodeBlock = false;
  let codeBuffer = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      if (inCodeBlock) {
        html += `<pre class="code-block-sm"><code>${escapeHtml(codeBuffer.join('\n'))}</code></pre>`;
        codeBuffer = [];
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeBuffer.push(line);
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    } else if (trimmed.startsWith('# ')) {
      html += `<h3>${escapeHtml(trimmed.slice(2))}</h3>`;
    } else if (trimmed.startsWith('## ')) {
      html += `<h4>${escapeHtml(trimmed.slice(3))}</h4>`;
    } else if (trimmed.startsWith('### ')) {
      html += `<h5>${escapeHtml(trimmed.slice(4))}</h5>`;
    } else if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      html += `<li>${escapeHtml(trimmed.slice(2))}</li>`;
    } else {
      html += `<p>${escapeHtml(trimmed)}</p>`;
    }
  }

  if (inCodeBlock && codeBuffer.length > 0) {
    html += `<pre class="code-block-sm"><code>${escapeHtml(codeBuffer.join('\n'))}</code></pre>`;
  }

  return html || '<p class="text-muted">Không có nội dung</p>';
}

// ============================================================================
// API Client
// ============================================================================

async function ensureCsrfToken(force = false) {
  if (state.csrfToken && !force) return state.csrfToken;
  try {
    const configResp = await fetch('/api/v1/config', {
      headers: { 'Accept': 'application/json' }
    });
    if (configResp.ok) {
      const json = await configResp.json();
      if (json && json.data && json.data.csrf_token) {
        state.csrfToken = json.data.csrf_token;
        const badge = document.getElementById('csrf-badge');
        if (badge) {
          badge.textContent = 'CSRF: Sẵn sàng';
          badge.className = 'badge badge-alive';
        }
      }
    }
  } catch (e) {
    console.warn('Failed to ensure CSRF token:', e);
  }
  return state.csrfToken;
}

async function apiFetch(endpoint, options = {}) {
  const url = endpoint.startsWith('/') ? endpoint : `/api/v1/${endpoint}`;
  const isPost = options.method === 'POST';

  if (isPost && !state.csrfToken) {
    await ensureCsrfToken();
  }

  const headers = Object.assign(
    {
      'Accept': 'application/json'
    },
    options.headers || {}
  );

  if (isPost) {
    headers['Content-Type'] = 'application/json';
    if (state.csrfToken) {
      headers['X-CSRF-Token'] = state.csrfToken;
    }
  }

  const reqConfig = {
    method: options.method || 'GET',
    headers: headers
  };

  if (options.body) {
    reqConfig.body = JSON.stringify(options.body);
  }

  const resp = await fetch(url, reqConfig);
  if (!resp.ok) {
    let errMsg = `HTTP ${resp.status} ${resp.statusText}`;
    try {
      const errJson = await resp.json();
      if (errJson?.schema === 'skill-hub.dashboard.v1' && errJson.data &&
          (endpoint === 'route_task' || endpoint.startsWith('semantic?'))) return errJson;
      if (errJson) {
        errMsg = errJson.error || errJson.data?.reason || errJson.data?.error || errJson.message || errMsg;
        if (errMsg === 'INVALID_TASK_STATE' && typeof errJson.data?.detail === 'string') {
          errMsg += ': ' + errJson.data.detail;
        }
      }
    } catch (e) {
      // ignore json parse error
    }

    if (resp.status === 403 && isPost && !options._isCsrfRetry && String(errMsg).toLowerCase().includes('csrf')) {
      state.csrfToken = '';
      const refreshed = await ensureCsrfToken(true);
      if (refreshed) {
        const retryOpts = Object.assign({}, options, { _isCsrfRetry: true });
        return await apiFetch(endpoint, retryOpts);
      }
    }

    throw new Error(errMsg);
  }

  const result = await resp.json();
  document.getElementById('connection-text').textContent = 'Đã kết nối dashboard';
  document.getElementById('connection-dot').className = 'status-indicator status-alive';
  return result;
}

function showGlobalAlert(message) {
  const alertEl = document.getElementById('global-alert');
  const alertText = document.getElementById('global-alert-text');
  if (alertEl && alertText) {
    alertText.textContent = message;
    alertEl.hidden = false;
  }
}

function hideGlobalAlert() {
  const alertEl = document.getElementById('global-alert');
  if (alertEl) {
    alertEl.hidden = true;
  }
}

// ============================================================================
// Navigation Management
// ============================================================================

function setupNavigation() {
  const navLinks = document.querySelectorAll('.nav-item');
  navLinks.forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const view = link.getAttribute('data-view');
      switchView(view);
    });
  });
}

function switchView(viewName) {
  if (state.currentView === viewName && document.getElementById(`view-${viewName}`) && !document.getElementById(`view-${viewName}`).hidden) {
    return;
  }

  state.currentView = viewName;
  const tabs = [...document.querySelectorAll('.nav-item')];
  const selectedTab = tabs.find(link => link.getAttribute('data-view') === viewName);
  if (selectedTab) {
    const labelEl = selectedTab.querySelector('.nav-label');
    const titleText = labelEl ? labelEl.textContent.trim() : selectedTab.textContent.replace(/^\s*\d+\s*/, '').trim();
    document.getElementById('view-title').textContent = titleText;
    document.getElementById('view-eyebrow').textContent = 'PHÂN HỆ ' + String(tabs.indexOf(selectedTab) + 1).padStart(2, '0');
  }

  // Update nav tabs aria-selected
  document.querySelectorAll('.nav-item').forEach(link => {
    const isTarget = link.getAttribute('data-view') === viewName;
    link.classList.toggle('active', isTarget);
    link.setAttribute('aria-selected', isTarget ? 'true' : 'false');
  });

  // Switch view panels
  document.querySelectorAll('.view-panel').forEach(panel => {
    const isTarget = panel.id === `view-${viewName}`;
    panel.hidden = !isTarget;
  });

  // Load view data
  loadViewData(viewName);
}

async function loadViewData(viewName) {
  hideGlobalAlert();
  const refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) refreshBtn.querySelector('.spin-icon').classList.add('spinning');
  state.isRefreshing = true;

  try {
    switch (viewName) {
      case 'overview':
        await loadOverviewView();
        break;
      case 'skills':
        await loadSkillsView();
        break;
      case 'graph':
        await loadGraphView();
        break;
      case 'routing':
        await loadRoutingView();
        break;
      case 'agents':
        await loadAgentsView();
        break;
      case 'integrations':
        await loadIntegrationsView();
        break;
      case 'library':
        await loadLibraryView();
        break;
      case 'telemetry':
        await loadTelemetryView();
        break;
      case 'quality':
        await loadQualityView();
        break;
      case 'config':
        await loadConfigView();
        break;
    }
  } catch (err) {
    document.getElementById('connection-text').textContent = 'Không tải được phân hệ';
    document.getElementById('connection-dot').className = 'status-indicator status-blocked';
    showGlobalAlert(`Lỗi tải dữ liệu phân hệ ${viewName}: ${err.message}`);
  } finally {
    state.isRefreshing = false;
    if (refreshBtn) refreshBtn.querySelector('.spin-icon').classList.remove('spinning');
  }
}

// ----------------------------------------------------------------------------
// 01: Overview View
// ----------------------------------------------------------------------------

async function loadOverviewView() {
  const resp = await apiFetch('overview');
  const data = resp.data || {};
  const counts = data.counts || {};

  // Metrics numbers - render dynamically when available, otherwise truthful indicator
  document.getElementById('metric-skills').textContent = counts.skills !== null && counts.skills !== undefined ?
    Number(counts.skills).toLocaleString('vi-VN') : '—';
  document.getElementById('metric-skills-sub').textContent = counts.skills !== null && counts.skills !== undefined ?
    'Đã nạp vào catalog SQLite' : 'Không có dữ liệu';

  document.getElementById('metric-prompts').textContent = counts.prompts !== null && counts.prompts !== undefined ?
    Number(counts.prompts).toLocaleString('vi-VN') : '—';
  document.getElementById('metric-prompts-sub').textContent = counts.prompts !== null && counts.prompts !== undefined ?
    'Catalog prompts & templates' : 'Không có dữ liệu';

  document.getElementById('metric-agents').textContent = counts.agents !== null && counts.agents !== undefined ?
    Number(counts.agents).toLocaleString('vi-VN') : 'Chưa nạp';
  document.getElementById('metric-agents-sub').textContent = counts.agents !== null && counts.agents !== undefined ?
    'Profiles OpenBot & Hub' : 'Adapter danh bạ chưa kết nối';

  document.getElementById('metric-mcp').textContent = counts.mcp !== null && counts.mcp !== undefined ?
    Number(counts.mcp).toLocaleString('vi-VN') : 'Snapshot';
  document.getElementById('metric-mcp-sub').textContent = counts.mcp !== null && counts.mcp !== undefined ?
    'Servers đã cấu hình' : 'Bản chụp danh mục công cụ';

  document.getElementById('metric-plugins').textContent = counts.plugins !== null && counts.plugins !== undefined ?
    Number(counts.plugins).toLocaleString('vi-VN') : 'Chưa nạp';
  document.getElementById('metric-plugins-sub').textContent = counts.plugins !== null && counts.plugins !== undefined ?
    'Plugins Claude đã cài' : 'Chưa ghi nhận bản ghi';

  // Update live nav pills
  const pillSkills = document.getElementById('pill-count-skills');
  if (pillSkills && counts.skills !== null && counts.skills !== undefined) {
    pillSkills.textContent = Number(counts.skills).toLocaleString('vi-VN');
  }
  const pillPrompts = document.getElementById('pill-count-prompts');
  if (pillPrompts && counts.prompts !== null && counts.prompts !== undefined) {
    pillPrompts.textContent = Number(counts.prompts).toLocaleString('vi-VN');
  }
  const pillAgents = document.getElementById('pill-count-agents');
  if (pillAgents) {
    pillAgents.textContent = counts.agents !== null && counts.agents !== undefined ?
      Number(counts.agents).toLocaleString('vi-VN') : '—';
  }
  const pillMcp = document.getElementById('pill-count-mcp');
  if (pillMcp) {
    pillMcp.textContent = counts.mcp !== null && counts.mcp !== undefined ?
      Number(counts.mcp).toLocaleString('vi-VN') : '—';
  }

  // Sources health
  const sourcesWrap = document.getElementById('overview-sources');
  sourcesWrap.innerHTML = '';
  const sources = resp.sources || [];
  document.getElementById('overview-observed-at').textContent = 'Thời điểm: ' + formatIsoTime(resp.observed_at);
  sources.forEach(src => {
    const item = document.createElement('div');
    item.className = 'source-item';
    const statusClass = src.status === 'readable' ? 'badge-alive' : 'badge-blocked';
    const freshnessClass = src.freshness === 'recent' ? 'badge-alive' : (src.freshness === 'stale' ? 'badge-warning' : 'badge-subtle');

    item.innerHTML = `
      <div class="source-header">
        <span class="source-name">${escapeHtml(src.name)}</span>
        <span class="badge ${statusClass}">${escapeHtml(src.status || 'unknown')}</span>
      </div>
      <div class="source-meta">
        <div>Loại: <code>${escapeHtml(src.type || 'sqlite')}</code></div>
        <div>Độ tươi mới: <span class="badge ${freshnessClass}">${escapeHtml(src.freshness || 'unknown')}</span></div>
        <div>Cập nhật lần cuối: ${formatIsoTime(src.modified_at)}</div>
      </div>
    `;
    sourcesWrap.appendChild(item);
  });

  // Recent activity table
  const tbody = document.getElementById('overview-activity-rows');
  tbody.innerHTML = '';
  const activities = data.recent_activity || [];
  if (activities.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">Không có sự kiện telemetry gần đây</td></tr>';
  } else {
    activities.forEach(ev => {
      const tr = document.createElement('tr');
      const rawOutcome = (ev.fields && ev.fields.outcome) ? ev.fields.outcome : (ev.kind || 'event');
      const { text, badgeClass } = formatRouteOutcome(rawOutcome);
      tr.innerHTML = `
        <td class="font-mono text-xs">${formatIsoTime(ev.emitted_at)}</td>
        <td><span class="badge badge-subtle">${escapeHtml(ev.source || 'native')}</span></td>
        <td><code>${escapeHtml(ev.kind || '—')}</code></td>
        <td><span class="badge ${badgeClass}">${escapeHtml(text)}</span></td>
      `;
      tbody.appendChild(tr);
    });
  }

  // Usage Chart SVG
  renderOverviewChart(data.usage || []);
}

function renderOverviewChart(usageData) {
  const chartSvg = document.getElementById('overview-chart');
  const legendWrap = document.getElementById('chart-legend');
  if (!chartSvg || !legendWrap) return;

  chartSvg.innerHTML = '';
  // Zero inline styles for CSP compliance
  legendWrap.innerHTML = `
    <div class="legend-item"><span class="legend-color legend-color-native"></span><span>Native telemetry</span></div>
    <div class="legend-item"><span class="legend-color legend-color-docker"></span><span>Docker telemetry</span></div>
  `;

  if (!usageData || usageData.length === 0) {
    chartSvg.innerHTML = `<text x="300" y="120" text-anchor="middle" fill="#9baec4" font-size="12">Không có dữ liệu biểu đồ sự kiện</text>`;
    return;
  }

  // Group by date
  const dates = Array.from(new Set(usageData.map(d => d.date))).sort();
  const maxEvents = Math.max(...usageData.map(d => d.events), 1);

  const svgW = 600;
  const svgH = 240;
  const padL = 40;
  const padR = 20;
  const padB = 40;
  const padT = 20;
  const chartW = svgW - padL - padR;
  const chartH = svgH - padT - padB;

  // Render Y axis guide lines
  for (let step = 0; step <= 4; step++) {
    const val = Math.round((maxEvents / 4) * step);
    const y = padT + chartH - (chartH / 4) * step;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', padL);
    line.setAttribute('x2', svgW - padR);
    line.setAttribute('y1', y);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', 'rgba(255,255,255,0.06)');
    line.setAttribute('stroke-dasharray', '3 3');
    chartSvg.appendChild(line);

    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', padL - 8);
    txt.setAttribute('y', y + 4);
    txt.setAttribute('text-anchor', 'end');
    txt.setAttribute('fill', '#64748b');
    txt.setAttribute('font-size', '10');
    txt.textContent = val;
    chartSvg.appendChild(txt);
  }

  // Bar rendering
  const slotW = chartW / dates.length;
  const barW = Math.max(Math.min(slotW * 0.35, 20), 6);

  dates.forEach((date, i) => {
    const xCenter = padL + i * slotW + slotW / 2;

    const nativeItem = usageData.find(d => d.date === date && d.source === 'native');
    const dockerItem = usageData.find(d => d.date === date && d.source === 'docker');

    const nativeEvents = nativeItem ? nativeItem.events : 0;
    const dockerEvents = dockerItem ? dockerItem.events : 0;

    const nativeH = (nativeEvents / maxEvents) * chartH;
    const dockerH = (dockerEvents / maxEvents) * chartH;

    // Native bar
    if (nativeEvents > 0) {
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', xCenter - barW - 2);
      rect.setAttribute('y', padT + chartH - nativeH);
      rect.setAttribute('width', barW);
      rect.setAttribute('height', nativeH);
      rect.setAttribute('fill', '#689bd6');
      rect.setAttribute('rx', '2');
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `Native: ${nativeEvents} sự kiện (${date})`;
      rect.appendChild(title);
      chartSvg.appendChild(rect);
    }

    // Docker bar
    if (dockerEvents > 0) {
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', xCenter + 2);
      rect.setAttribute('y', padT + chartH - dockerH);
      rect.setAttribute('width', barW);
      rect.setAttribute('height', dockerH);
      rect.setAttribute('fill', '#4ec9b0');
      rect.setAttribute('rx', '2');
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `Docker: ${dockerEvents} sự kiện (${date})`;
      rect.appendChild(title);
      chartSvg.appendChild(rect);
    }

    // Date Label
    const dateTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    dateTxt.setAttribute('x', xCenter);
    dateTxt.setAttribute('y', svgH - 12);
    dateTxt.setAttribute('text-anchor', 'middle');
    dateTxt.setAttribute('fill', '#9baec4');
    dateTxt.setAttribute('font-size', '10');
    dateTxt.textContent = date.slice(5);
    chartSvg.appendChild(dateTxt);
  });
}

// ----------------------------------------------------------------------------
// 02: Skill Registry View
// ----------------------------------------------------------------------------

async function loadSkillsView() {
  const modeSelect = document.getElementById('skill-search-mode');
  const searchMode = modeSelect ? modeSelect.value : (state.skills.mode || 'fts');
  state.skills.mode = searchMode;

  const { page, limit, q, domain } = state.skills;
  const statusLabel = document.getElementById('skills-search-status-text') || document.getElementById('skills-search-mode');
  const totalCountEl = document.getElementById('skills-total-count');
  const tbody = document.getElementById('skills-table-body');

  // Semantic search mode
  if (searchMode === 'semantic' && q) {
    try {
      const resp = await apiFetch(`semantic?q=${encodeURIComponent(q)}&limit=${limit}`);
      const data = resp.data || resp || {};
      const hits = data.hits || [];
      state.skills.total = hits.length;
      state.skills.items = hits;

      if (statusLabel) {
        statusLabel.textContent = `Chế độ tìm kiếm: Tìm ngữ nghĩa (dGPU BGE-M3, ${data.elapsed_ms || 0} ms)`;
      }
      if (totalCountEl) {
        totalCountEl.textContent = `Tổng: ${hits.length} kết quả`;
      }

      tbody.innerHTML = '';
      if (hits.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Không tìm thấy kết quả ngữ nghĩa nào phù hợp.</td></tr>';
      } else {
        hits.forEach((hit, idx) => {
          const tr = document.createElement('tr');
          const scoreVal = hit.raw_score !== undefined ? hit.raw_score : 0;
          const scoreClass = scoreMeterClass(scoreVal);
          const scoreBadge = `<div class="flex-align-center gap-xs">
            <progress class="score-meter ${scoreClass}" max="100" value="${Math.round(scoreVal * 100)}"></progress>
            <span class="font-mono text-xs">${scoreVal.toFixed(3)}</span>
          </div>`;
          const resId = hit.id || `hit-${idx + 1}`;
          const kind = resId.startsWith('prompt:') ? 'prompt' : 'skill';
          const cleanName = resId.replace(/^(skill|prompt|document):/, '');

          tr.innerHTML = `
            <td>
              <strong>${escapeHtml(cleanName)}</strong>
              <div class="text-xs text-muted">${escapeHtml(hit.evidence?.summary || hit.evidence?.chunk || 'Kết quả tương đồng ngữ nghĩa BGE-M3')}</div>
            </td>
            <td><code>semantic-vector</code></td>
            <td class="font-mono text-xs" title="Mô hình BGE-M3 dGPU">bge-m3:latest</td>
            <td>${scoreBadge}</td>
            <td>
              <div class="pagination-controls">
                <button class="btn btn-subtle btn-inspect-skill" data-id="${escapeHtml(cleanName)}" data-kind="${escapeHtml(kind)}">Chi tiết</button>
              </div>
            </td>
          `;
          tbody.appendChild(tr);
        });
      }

      tbody.querySelectorAll('.btn-inspect-skill').forEach(b => {
        b.addEventListener('click', () => {
          openResourceInspector(b.getAttribute('data-id'), b.getAttribute('data-kind') || 'skill');
        });
      });

      const start = hits.length > 0 ? 1 : 0;
      const end = hits.length;
      document.getElementById('skills-pagination-info').textContent = `Hiển thị ${start} - ${end} của ${hits.length}`;
      document.getElementById('skills-page-indicator').textContent = 'Trang 1';
      document.getElementById('skills-prev-page').disabled = true;
      document.getElementById('skills-next-page').disabled = true;
      return;
    } catch (e) {
      if (statusLabel) {
        statusLabel.textContent = `Chế độ: Lỗi tìm kiếm ngữ nghĩa (${e.message})`;
      }
    }
  }

  // FTS mode (default)
  const offset = (page - 1) * limit;
  let queryUrl = `resources?kind=skill&offset=${offset}&limit=${limit}`;
  if (q) queryUrl += `&q=${encodeURIComponent(q)}`;
  if (domain) queryUrl += `&domain=${encodeURIComponent(domain)}`;

  const resp = await apiFetch(queryUrl);
  const data = resp.data || {};
  state.skills.total = data.total || 0;
  state.skills.items = data.items || [];

  if (statusLabel) {
    if (searchMode === 'semantic') {
      statusLabel.textContent = 'Chế độ tìm kiếm: Ngữ nghĩa (Nhập từ khóa để tìm vector BGE-M3; đang hiển thị catalog đầy đủ)';
    } else {
      statusLabel.textContent = 'Chế độ tìm kiếm: ' + (data.search_mode || 'Đọc catalog FTS');
    }
  }
  if (totalCountEl) {
    totalCountEl.textContent = `Tổng: ${state.skills.total.toLocaleString('vi-VN')} kỹ năng`;
  }

  // Populate domains dropdown if empty
  const domainSelect = document.getElementById('skills-domain-select');
  if (domainSelect && domainSelect.options.length <= 1) {
    const domains = new Set();
    state.skills.items.forEach(it => { if (it.domain) domains.add(it.domain); });
    domains.forEach(dom => {
      const opt = document.createElement('option');
      opt.value = dom;
      opt.textContent = dom;
      domainSelect.appendChild(opt);
    });
  }

  // Table body
  tbody.innerHTML = '';
  if (state.skills.items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Không tìm thấy kỹ năng nào phù hợp.</td></tr>';
  } else {
    state.skills.items.forEach(skill => {
      const tr = document.createElement('tr');
      const activeBadge = skill.active ? '<span class="badge badge-alive">Hoạt động</span>' : '<span class="badge badge-muted">Tắt</span>';
      const versionShort = skill.version ? skill.version.slice(0, 12) + '…' : '—';

      tr.innerHTML = `
        <td>
          <strong>${escapeHtml(skill.name)}</strong>
          <div class="text-xs text-muted">${escapeHtml(skill.description || 'Không có mô tả')}</div>
        </td>
        <td><code>${escapeHtml(skill.domain || 'general')}</code></td>
        <td class="font-mono text-xs" title="${escapeHtml(skill.version || '')}">${escapeHtml(versionShort)}</td>
        <td>${activeBadge}</td>
        <td>
          <div class="pagination-controls">
            <button class="btn btn-subtle btn-inspect-skill" data-id="${escapeHtml(skill.id)}" data-kind="skill">Chi tiết</button>
            <button class="btn btn-secondary btn-revise-catalog" data-id="${escapeHtml(skill.id)}" data-kind="skill">Sửa đổi</button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  // Wire inspect & revision buttons
  tbody.querySelectorAll('.btn-inspect-skill').forEach(b => {
    b.addEventListener('click', () => {
      openResourceInspector(b.getAttribute('data-id'), b.getAttribute('data-kind'));
    });
  });

  tbody.querySelectorAll('.btn-revise-catalog').forEach(b => {
    b.addEventListener('click', () => {
      initiateCatalogRevision(b.getAttribute('data-id'), b.getAttribute('data-kind'));
    });
  });

  // Pagination update
  const start = state.skills.total > 0 ? offset + 1 : 0;
  const end = Math.min(offset + limit, state.skills.total);
  document.getElementById('skills-pagination-info').textContent = `Hiển thị ${start} - ${end} của ${state.skills.total.toLocaleString('vi-VN')}`;
  document.getElementById('skills-page-indicator').textContent = `Trang ${page}`;
  document.getElementById('skills-prev-page').disabled = page <= 1;
  document.getElementById('skills-next-page').disabled = end >= state.skills.total;
}

// ----------------------------------------------------------------------------
// 03: Capability Graph Explorer View
// ----------------------------------------------------------------------------

async function loadGraphView() {
  const type = state.graph.typeFilter || 'all';
  const q = state.graph.searchQuery || '';
  const depth = state.graph.depth !== undefined ? state.graph.depth : 1;
  const limit = state.owned?.settings?.graph_limit || 100;

  let queryUrl = `graph?type=${encodeURIComponent(type)}&limit=${limit}`;
  if (q) queryUrl += `&q=${encodeURIComponent(q)}`;
  if (state.graph.focusedNodeId) {
    queryUrl += `&node_id=${encodeURIComponent(state.graph.focusedNodeId)}&depth=${encodeURIComponent(depth)}`;
  }

  const resp = await apiFetch(queryUrl);
  const data = resp.data || {};
  state.graph.nodes = data.nodes || [];
  state.graph.edges = data.edges || [];

  const focusNotice = state.graph.focusedNodeId ? ` [Tập trung: ${state.graph.focusedNodeId}]` : '';
  const truncNotice = data.truncated ? ' (Đã giới hạn hiển thị)' : '';
  const statsEl = document.getElementById('graph-stats');
  if (statsEl) {
    statsEl.textContent = `Đã nạp ${state.graph.nodes.length} nodes, ${state.graph.edges.length} liên kết phụ thuộc (Tổng khớp: ${data.total || 0})${focusNotice}${truncNotice}`;
  }

  const focusBtn = document.getElementById('graph-focus-btn');
  if (focusBtn) {
    focusBtn.disabled = !state.graph.selectedNodeId;
  }
  const resetBtn = document.getElementById('graph-reset-focus-btn');
  if (resetBtn) {
    resetBtn.hidden = !state.graph.focusedNodeId;
  }

  renderCapabilityGraph();
}

function renderCapabilityGraph() {
  const nodesGroup = document.getElementById('graph-nodes-group');
  const edgesGroup = document.getElementById('graph-edges-group');
  if (!nodesGroup || !edgesGroup) return;

  nodesGroup.innerHTML = '';
  edgesGroup.innerHTML = '';

  state.graph.nodeMap.clear();
  const cols = 5;
  const cellW = 300;
  const cellH = 130;
  const padX = 80;
  const padY = 60;

  state.graph.nodes.forEach((node, idx) => {
    const col = idx % cols;
    const row = Math.floor(idx / cols);
    const x = padX + col * cellW;
    const y = padY + row * cellH;
    state.graph.nodeMap.set(node.id, { node, x, y });
  });

  // Render Edges with keyboard accessibility & inspection
  state.graph.edges.forEach(edge => {
    const sPos = state.graph.nodeMap.get(edge.source);
    const tPos = state.graph.nodeMap.get(edge.target);
    if (!sPos || !tPos) return;

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const x1 = sPos.x + 110;
    const y1 = sPos.y + 35;
    const x2 = tPos.x + 110;
    const y2 = tPos.y + 35;
    const dx = x2 - x1;
    const dy = y2 - y1;
    const cx = x1 + dx / 2;
    const cy = y1 + dy / 2 - 24;

    path.setAttribute('d', `M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`);
    path.setAttribute('class', 'graph-edge');
    path.setAttribute('marker-end', 'url(#arrow)');
    path.setAttribute('tabindex', '0');
    path.setAttribute('role', 'button');
    path.setAttribute('aria-label', `Liên kết từ ${edge.source} đến ${edge.target} (${edge.type || 'alias_of'})`);

    const selectEdge = () => {
      document.querySelectorAll('.graph-edge.selected, .graph-node.selected').forEach(el => el.classList.remove('selected'));
      path.classList.add('selected');
      state.graph.selectedEdgeId = edge.id;
      openEdgeInspector(edge);
    };

    path.addEventListener('click', selectEdge);
    path.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectEdge();
      }
    });

    edgesGroup.appendChild(path);
  });

  // Render Nodes with keyboard accessibility & zero inline styles
  state.graph.nodeMap.forEach(({ node, x, y }) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'graph-node');
    g.setAttribute('transform', `translate(${x}, ${y})`);
    g.setAttribute('tabindex', '0');
    g.setAttribute('role', 'button');
    g.setAttribute('aria-label', `Node ${node.label} loại ${node.type}`);

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('width', '240');
    rect.setAttribute('height', '72');
    rect.setAttribute('class', `graph-node-rect type-${node.type}`);

    const title = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    title.setAttribute('x', '14');
    title.setAttribute('y', '30');
    title.setAttribute('class', 'graph-node-text');
    title.textContent = truncateStr(node.label, 24);

    const sub = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    sub.setAttribute('x', '14');
    sub.setAttribute('y', '52');
    sub.setAttribute('class', 'graph-node-sub');
    const ver = (node.metadata && node.metadata.version) ? node.metadata.version.slice(0, 10) : '';
    sub.textContent = `${node.type.toUpperCase()} · ${ver || node.status || ''}`;

    g.appendChild(rect);
    g.appendChild(title);
    g.appendChild(sub);

    const selectNode = () => {
      document.querySelectorAll('.graph-node.selected, .graph-edge.selected').forEach(el => el.classList.remove('selected'));
      g.classList.add('selected');
      state.graph.selectedNodeId = node.id;
      const focusBtn = document.getElementById('graph-focus-btn');
      if (focusBtn) focusBtn.disabled = false;
      openGraphNodeInspector(node);
    };

    g.addEventListener('click', selectNode);
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectNode();
      }
    });

    nodesGroup.appendChild(g);
  });

  updateGraphTransform();
}

function updateGraphTransform() {
  const svg = document.getElementById('graph-svg');
  if (!svg) return;
  const nodesG = document.getElementById('graph-nodes-group');
  const edgesG = document.getElementById('graph-edges-group');
  const transformVal = `translate(${state.graph.panX}, ${state.graph.panY}) scale(${state.graph.zoom})`;
  if (nodesG) nodesG.setAttribute('transform', transformVal);
  if (edgesG) edgesG.setAttribute('transform', transformVal);
  const zoomBadge = document.getElementById('graph-zoom-level');
  if (zoomBadge) zoomBadge.textContent = Math.round(state.graph.zoom * 100) + '%';
}

function fitGraphToExtent() {
  const viewport = document.getElementById('graph-viewport');
  if (!viewport || state.graph.nodeMap.size === 0) return;

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;

  state.graph.nodeMap.forEach(({ x, y }) => {
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x + 240);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y + 72);
  });

  const bboxW = Math.max(maxX - minX, 100);
  const bboxH = Math.max(maxY - minY, 100);
  const pad = 60;
  const viewW = Math.max(viewport.clientWidth, 300);
  const viewH = Math.max(viewport.clientHeight, 300);

  const scaleX = (viewW - pad * 2) / bboxW;
  const scaleY = (viewH - pad * 2) / bboxH;
  const zoom = Math.min(Math.max(Math.min(scaleX, scaleY), 0.25), 1.8);

  state.graph.zoom = zoom;
  state.graph.panX = (viewW - bboxW * zoom) / 2 - minX * zoom;
  state.graph.panY = (viewH - bboxH * zoom) / 2 - minY * zoom;
  updateGraphTransform();
}

function setupGraphControls() {
  const viewport = document.getElementById('graph-viewport');
  if (!viewport) return;

  viewport.addEventListener('mousedown', (e) => {
    if (e.target.closest('.graph-node') || e.target.closest('.graph-edge')) return;
    state.graph.isDragging = true;
    state.graph.startX = e.clientX - state.graph.panX;
    state.graph.startY = e.clientY - state.graph.panY;
    viewport.classList.add('dragging');
  });

  window.addEventListener('mousemove', (e) => {
    if (!state.graph.isDragging) return;
    state.graph.panX = e.clientX - state.graph.startX;
    state.graph.panY = e.clientY - state.graph.startY;
    updateGraphTransform();
  });

  window.addEventListener('mouseup', () => {
    state.graph.isDragging = false;
    viewport.classList.remove('dragging');
  });

  document.getElementById('graph-zoom-in')?.addEventListener('click', () => {
    state.graph.zoom = Math.min(state.graph.zoom + 0.15, 2.5);
    updateGraphTransform();
  });

  document.getElementById('graph-zoom-out')?.addEventListener('click', () => {
    state.graph.zoom = Math.max(state.graph.zoom - 0.15, 0.25);
    updateGraphTransform();
  });

  document.getElementById('graph-zoom-reset')?.addEventListener('click', () => {
    state.graph.zoom = 1.0;
    state.graph.panX = 0;
    state.graph.panY = 0;
    updateGraphTransform();
  });

  document.getElementById('graph-zoom-fit')?.addEventListener('click', fitGraphToExtent);

  document.getElementById('graph-type-filter')?.addEventListener('change', (e) => {
    state.graph.typeFilter = e.target.value;
    loadGraphView();
  });

  let filterDebounce = null;
  document.getElementById('graph-filter-input')?.addEventListener('input', (e) => {
    clearTimeout(filterDebounce);
    filterDebounce = setTimeout(() => {
      state.graph.searchQuery = e.target.value.trim();
      loadGraphView();
    }, 300);
  });

  document.getElementById('graph-depth-filter')?.addEventListener('change', (e) => {
    state.graph.depth = parseInt(e.target.value, 10);
    if (state.graph.focusedNodeId) {
      loadGraphView();
    }
  });

  document.getElementById('graph-focus-btn')?.addEventListener('click', () => {
    if (!state.graph.selectedNodeId) return;
    state.graph.focusedNodeId = state.graph.selectedNodeId;
    loadGraphView();
  });

  document.getElementById('graph-reset-focus-btn')?.addEventListener('click', () => {
    state.graph.focusedNodeId = null;
    loadGraphView();
  });
}

function openGraphNodeInspector(target) {
  let node = null;
  if (typeof target === 'object' && target !== null && target.id) {
    node = target;
  } else if (typeof target === 'string') {
    node = state.graph.nodes.find(n => n.id === target) || null;
    if (!node) {
      const idStr = target;
      let type = 'skill';
      if (idStr.startsWith('agent:')) type = 'agent';
      else if (idStr.startsWith('mcp:')) type = 'mcp';
      else if (idStr.startsWith('prompt:')) type = 'prompt';
      else if (idStr.startsWith('document:')) type = 'document';
      else if (idStr.startsWith('project:')) type = 'project';
      node = { id: idStr, type: type, label: idStr, status: 'projected', metadata: {} };
    }
  }

  if (!node) return;

  if (node.type === 'agent') {
    const agentId = node.id.startsWith('agent:') ? node.id.slice(6) : node.id;
    openAgentInspector({
      id: agentId,
      name: node.label,
      model: node.metadata?.model || null,
      runtime: node.metadata?.runtime || 'openbot',
      status: node.status || 'projected',
      skills: null, // unknown from graph node
      mcp: null,    // unknown from graph node
      permissions: null // unknown from graph node
    });
  } else if (node.type === 'mcp') {
    const mcpId = node.id.startsWith('mcp:') ? node.id.slice(4) : node.id;
    openMcpInspector({
      id: mcpId,
      name: node.label,
      status: node.status || 'projected',
      enabled: node.metadata?.enabled,
      transport: node.metadata?.transport || 'stdio/http',
      tools: []
    });
  } else if (node.type === 'document') {
    openResourceInspector(node.id, 'document');
  } else if (node.type === 'skill') {
    const skillName = node.label || (node.id.startsWith('skill:') ? node.id.slice(6) : node.id);
    openResourceInspector(skillName, 'skill');
  } else if (node.type === 'prompt') {
    const promptName = node.label || (node.id.startsWith('prompt:') ? node.id.slice(7) : node.id);
    openResourceInspector(promptName, 'prompt');
  } else {
    openGenericNodeInspector(node);
  }
}

function openMcpInspector(mcp) {
  openInspectorDrawer();
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const nameEl = document.getElementById('inspector-name');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = 'CHI TIẾT MÁY CHỦ MCP';
  nameEl.textContent = mcp.name || mcp.id;

  const enabledBadge = mcp.enabled === true ? '<span class="badge badge-alive">Bật</span>' :
    (mcp.enabled === false ? '<span class="badge badge-muted">Tắt</span>' : '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>');

  const toolsHtml = (mcp.tools || []).map(t => `<span class="tool-tag">${escapeHtml(t.name || t)}</span>`).join(' ') ||
    '<span class="text-muted text-xs">Không có công cụ cụ thể hoặc chưa probe</span>';

  bodyEl.innerHTML = `
    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Máy chủ ID</span>
      <div class="inspector-prop-val"><code>${escapeHtml(mcp.id)}</code></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Trạng thái kích hoạt</span>
      <div class="inspector-prop-val">${enabledBadge}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Giao thức truyền dẫn (Transport)</span>
      <div class="inspector-prop-val"><code>${escapeHtml(mcp.transport || 'stdio/http')}</code></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Trạng thái kết nối</span>
      <div class="inspector-prop-val"><span class="badge badge-subtle">${escapeHtml(mcp.status || 'catalogued')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Danh sách công cụ khai báo (${(mcp.tools || []).length})</span>
      <div class="mcp-tools-list mt-xs">${toolsHtml}</div>
    </div>

    <div class="inspector-prop-row mt-sm">
      <span class="badge badge-subtle text-xs">Cấu hình máy chủ MCP là tài nguyên bảo vệ của hệ thống host; dashboard chỉ hiển thị dưới dạng bản chiếu chỉ đọc.</span>
    </div>
  `;
}

function openGenericNodeInspector(node) {
  openInspectorDrawer();
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const nameEl = document.getElementById('inspector-name');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = `CHI TIẾT NODE (${(node.type || 'UNKNOWN').toUpperCase()})`;
  nameEl.textContent = node.label || node.id;

  const metaStr = node.metadata ? JSON.stringify(node.metadata, null, 2) : '—';

  bodyEl.innerHTML = `
    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Định danh Node (ID)</span>
      <div class="inspector-prop-val"><code>${escapeHtml(node.id)}</code></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Loại Node</span>
      <div class="inspector-prop-val"><span class="badge badge-alive">${escapeHtml(node.type)}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Trạng thái chiếu (Status)</span>
      <div class="inspector-prop-val"><span class="badge badge-subtle">${escapeHtml(node.status || 'projected')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Dữ liệu đặc tả (Metadata)</span>
      <div class="inspector-prop-val"><pre class="code-block-sm"><code>${escapeHtml(metaStr)}</code></pre></div>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// 04: Routing Monitor & Interactive route_task Studio View
// ----------------------------------------------------------------------------

function routeText(en, vi) {
  return document.documentElement?.lang?.toLowerCase().split('-')[0] === 'vi' ? vi : en;
}
const routerEnglish = new WeakMap();
const routeContextFields = ['current_step','desired_output','artifact_types','available_tools','constraints','loaded_skill_ids_and_versions','explicit_skill_ids'];
let routeStudioRequest = 0;
let routeStudioResult = null;
let routeStudioLoadingMode = null;
let routeStudioError = null;

function localizeRouterStudio() {
  const fields = {'data-router-vi': 'textContent','data-router-vi-title': 'title',
    'data-router-vi-placeholder': 'placeholder','data-router-vi-aria-label': 'aria-label','data-router-vi-task': 'data-task'};
  document.querySelectorAll(Object.keys(fields).map(key => '[' + key + ']').join(',')).forEach(el => {
    if (!routerEnglish.has(el)) routerEnglish.set(el, Object.keys(fields).filter(key => el.hasAttribute(key)).map(key => {
      const target = fields[key];
      return [key, target, target === 'textContent' ? el.textContent : el.getAttribute(target)];
    }));
    for (const [key, target, en] of routerEnglish.get(el)) {
      const value = routeText(en, el.getAttribute(key));
      if (target === 'textContent') el.textContent = value;
      else el.setAttribute(target, value);
    }
  });
  updateRouterControls();
  if (routeStudioLoadingMode) updateRouterLoading(routeStudioLoadingMode);
  if (routeStudioError) showRouterError(...routeStudioError);
  if (routeStudioResult?.mode === 'semantic') renderSemanticStudioResults(routeStudioResult.data, routeStudioResult.query);
  else if (routeStudioResult) renderRouteTaskStudioResults(routeStudioResult.data, routeStudioResult);
}

function scoreMeterClass(score) {
  if (score >= 0.8) return 'score-high';
  if (score >= 0.6) return 'score-medium';
  return 'score-low';
}
function formatStudioOutcome(outcome, pool) {
  const labels = {
    SELECTED: [pool ? routeText('Menu ready', 'Đã có danh sách đề xuất') : routeText('Hub returned a result', 'Hub đã trả kết quả'), 'badge-alive'],
    OK: [routeText('Result received', 'Đã nhận kết quả'), 'badge-alive'],
    NO_SKILL: [routeText('No resources offered', 'Không có tài nguyên được đề xuất'), 'badge-subtle'],
    ABSTAIN_LOW_CONFIDENCE: [routeText('Abstained · low confidence', 'Không đề xuất · độ tin cậy thấp'), 'badge-warning'],
    ROUTER_ERROR: [routeText('Routing error', 'Lỗi định tuyến'), 'badge-blocked'],
    ROUTER_BUSY: [routeText('Router busy', 'Bộ định tuyến đang bận'), 'badge-blocked']
  };
  const [text, badgeClass] = (Object.hasOwn(labels, outcome) ? labels[outcome] : null) || [outcome || routeText('Unknown', 'Chưa biết'), 'badge-subtle'];
  return {text, badgeClass};
}

function triggerOverviewQuickRoute(taskText) {
  const text = (taskText || document.getElementById('overview-route-input')?.value || '').trim();
  if (!text) {
    document.getElementById('overview-route-input')?.focus();
    return;
  }
  switchView('routing');
  const input = document.getElementById('route-task-input');
  if (input) {
    input.value = text;
    invalidateRouterStudio();
    input.focus();
  }
}
function readRouteContext() {
  const result = {};
  for (const key of routeContextFields) {
    const value = document.getElementById('route-context-' + key)?.value || '';
    if (!value.trim()) continue;
    if (key === 'loaded_skill_ids_and_versions') {
      const parsed = JSON.parse(value);
      if (parsed !== null && !Array.isArray(parsed)) throw new Error('INVALID_LOADED_IDENTITIES');
      result[key] = parsed;
    } else if (['current_step','desired_output'].includes(key)) result[key] = value;
    else result[key] = value.split(/\r?\n/).filter(line => line.trim());
  }
  return Object.keys(result).length ? result : null;
}
function routerMode() {
  return document.getElementById('route-studio-mode')?.value || 'metadata';
}
function routerModeLabel(mode) {
  if (mode === 'metadata') return routeText('Metadata ranking', 'Xếp hạng metadata');
  if (mode === 'semantic') return routeText('Semantic search', 'Tìm kiếm ngữ nghĩa');
  return mode === 'tempr' ? 'TEMPR' : (mode || routeText('Unknown', 'Chưa biết'));
}
function routerPut(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
  return el;
}
function routerMetric(value, suffix = '') {
  return Number.isFinite(value) ? String(value) + suffix : routeText('Unknown', 'Chưa biết');
}
function routerHide(ids, hidden) {
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el) el.hidden = hidden;
  }
}
function updateRouterControls() {
  const semantic = routerMode() === 'semantic';
  routerHide(['route-model-warning'], semantic);
  const run = document.getElementById('btn-run-route-task');
  if (run) {
    const label = semantic ? routeText('Search', 'Tìm kiếm') : routeText('Run routing', 'Chạy định tuyến');
    (run.querySelector('span') || run).textContent = label;
    run.title = label;
  }
  for (const id of ['route-task-id-input','route-include-graph',...routeContextFields.map(key => 'route-context-' + key)]) {
    const field = document.getElementById(id);
    if (field) field.disabled = semantic;
  }
}
function updateRouterLoading(mode) {
  routerPut('route-studio-loading-text', mode === 'semantic' ?
    routeText('Searching by meaning…', 'Đang tìm kiếm ngữ nghĩa…') :
    routeText('Routing and scoring resources…', 'Đang định tuyến và chấm điểm tài nguyên…'));
}
function showRouterError(type, reason) {
  routeStudioError = [type, reason];
  const text = type === 'missing' ? routeText('Enter a task or query.', 'Nhập mô tả tác vụ hoặc truy vấn.') :
    type === 'context' ? routeText('Loaded identities must be a JSON array or null.', 'Danh tính đã nạp phải là mảng JSON hoặc null.') :
      routeText('Request failed', 'Yêu cầu thất bại') + (reason ? ' · ' + reason : '');
  const box = routerPut('route-studio-error', text);
  if (box) box.hidden = false;
}
function invalidateRouterStudio() {
  ++routeStudioRequest;
  routeStudioResult = routeStudioLoadingMode = routeStudioError = null;
  routerHide(['route-studio-error','route-studio-results','route-studio-loading','route-normalized-context','btn-carry-followup-state'], true);
  renderRouteChoicePool(null);
}
async function runRouteTaskStudio() {
  invalidateRouterStudio();
  const request = routeStudioRequest;
  const input = document.getElementById('route-task-input');
  const query = input?.value || '';
  if (!query.trim()) {
    showRouterError('missing');
    input?.focus();
    return;
  }
  const mode = routerMode();
  const semantic = mode === 'semantic';
  let context;
  try { context = semantic ? null : readRouteContext(); } catch { showRouterError('context'); return; }
  const includeGraph = !semantic && Boolean(document.getElementById('route-include-graph')?.checked);
  const readTaskId = () => (document.getElementById('route-task-id-input')?.value || '').trim() || null;
  const taskId = semantic ? null : readTaskId();
  const isCurrent = () => {
    if (request !== routeStudioRequest || input.value !== query || routerMode() !== mode) return false;
    if (semantic) return true;
    try {
      return JSON.stringify(readRouteContext()) === JSON.stringify(context) &&
        Boolean(document.getElementById('route-include-graph')?.checked) === includeGraph && readTaskId() === taskId;
    } catch { return false; }
  };
  routeStudioLoadingMode = mode;
  updateRouterLoading(mode);
  routerHide(['route-studio-loading'], false);
  try {
    const resp = semantic ? await apiFetch('semantic?q=' + encodeURIComponent(query) + '&limit=25') :
      await apiFetch('route_task', {method: 'POST', body: {
        state: query, ...(context === null ? {} : {task_state: context}),
        retrieval_mode: mode, include_graph: includeGraph, task_id: taskId
      }});
    if (!isCurrent()) return;
    const data = resp.data || resp;
    routeStudioResult = {data, mode, query, isCurrent, selected: new Set(), receipts: new Map(),
      errors: new Map(), contextClaims: context?.loaded_skill_ids_and_versions || [], loading: false, zero: false};
    if (semantic) renderSemanticStudioResults(data, query);
    else renderRouteTaskStudioResults(data, routeStudioResult);
  } catch (error) {
    if (isCurrent()) showRouterError('request', error.message);
  } finally {
    if (request === routeStudioRequest) {
      routeStudioLoadingMode = null;
      routerHide(['route-studio-loading'], true);
    }
  }
}

const routeDigest = /^[a-f0-9]{64}$/;
function qualifiedRouteId(id, kind) {
  if (typeof id !== 'string' || !id) return null;
  if (/^(skill|prompt):.+/.test(id)) return id;
  return ['skill','prompt'].includes(kind) ? kind + ':' + id : null;
}
function validRoutePin(item) {
  return routeDigest.test(item.source_raw_sha256 || '') &&
    (!item.id.startsWith('skill:') || routeDigest.test(item.bundle_sha256 || ''));
}
function matchesRouteReceipt(item, receipt) {
  return receipt?.load_verified === true && receipt.verified_in_client === true &&
    (receipt.resource_id || qualifiedRouteId(receipt.id, receipt.kind || receipt.type)) === item.id &&
    receipt.body_raw_sha256 === item.source_raw_sha256 &&
    (!item.id.startsWith('skill:') || receipt.bundle_sha256 === item.bundle_sha256) &&
    typeof receipt.body === 'string' && Number.isSafeInteger(receipt.body_bytes) && receipt.body_bytes > 0 &&
    new TextEncoder().encode(receipt.body).length === receipt.body_bytes;
}
function mergeRouteClaims(prior, receipts) {
  const claims = new Map(prior.filter(claim => claim && typeof claim.id === 'string').map(claim => [claim.id, claim]));
  for (const [id, receipt] of receipts) {
    claims.set(id, {id, content_sha256: receipt.body_raw_sha256,
      ...(receipt.bundle_sha256 ? {bundle_sha256: receipt.bundle_sha256} : {}),
      ...(receipt.version ? {version: receipt.version} : {})});
  }
  return [...claims.values()];
}
async function loadRouterChoices(view, byId) {
  if (!view.isCurrent() || view.loading) return;
  if (!view.selected.size) {
    view.zero = true;
    refreshRouteChoices(view);
    return;
  }
  const seen = new Set();
  const ordered = [];
  function visit(id) {
    const item = byId.get(id);
    if (seen.has(id) || !item || !validRoutePin(item) || view.receipts.has(id)) return;
    seen.add(id);
    // Order explicit dependencies only. The Hub validates dependency pins and cycles.
    for (const dep of Array.isArray(item.dependencies) ? item.dependencies : []) {
      if (view.selected.has(dep)) visit(dep);
    }
    ordered.push(item);
  }
  view.selected.forEach(visit);
  view.loading = true;
  refreshRouteChoices(view);
  updateRouterFollowup(view);
  for (const item of ordered) {
    if (!view.isCurrent()) return;
    const id = item.id;
    view.errors.delete(id);
    const claims = mergeRouteClaims(view.contextClaims, view.receipts);
    try {
      const response = await apiFetch('load_resource_choice', {method: 'POST', body: {
        resource_id: id, source_raw_sha256: item.source_raw_sha256,
        ...(item.bundle_sha256 ? {bundle_sha256: item.bundle_sha256} : {}),
        ...(claims.length ? {loaded_skill_ids_and_versions: claims} : {})
      }});
      if (!view.isCurrent()) return;
      const receipt = response.data || response;
      if (matchesRouteReceipt(item, receipt)) view.receipts.set(id, receipt);
      else view.errors.set(id, receipt?.error || 'UNVERIFIED_RECEIPT');
    } catch (error) {
      if (!view.isCurrent()) return;
      view.errors.set(id, error.message);
    }
    refreshRouteChoices(view);
    renderRouterLoaded(view.data, view);
    renderRouteScoresTable();
  }
  view.loading = false;
  refreshRouteChoices(view);
  updateRouterFollowup(view);
}

function refreshRouteChoices(view) {
  const items = document.getElementById('route-choice-items');
  if (!items || !view) return;
  const disabled = view.loading || !view.isCurrent();
  for (const card of items.querySelectorAll('[data-choice-id]')) {
    const id = card.dataset.choiceId;
    card.querySelector('.choice-status').textContent = view.receipts.has(id) ? routeText('Verified load', 'Đã nạp và xác minh') :
      view.errors.has(id) ? routeText('Load refused', 'Nạp bị từ chối') :
      view.selected.has(id) ? routeText('Selected by you', 'Bạn đã chọn') : routeText('Not selected', 'Chưa chọn');
    const input = card.querySelector('input');
    input.checked = view.selected.has(id);
    input.disabled = disabled || input.dataset.unavailable === 'true' || view.receipts.has(id);
  }
  routerPut('route-choice-selected-count', view.loading ?
    routeText('Loading selected resources…', 'Đang nạp tài nguyên đã chọn…') :
    routeText('Selected by you: ', 'Bạn đã chọn: ') + view.selected.size +
      (view.zero && !view.selected.size ? routeText(' · No load requested', ' · Không yêu cầu nạp') : ''));
  for (const id of ['btn-load-selected-choices','btn-clear-selected-choices']) {
    const control = document.getElementById(id);
    if (control) control.setAttribute('aria-disabled', String(disabled));
  }
  routerPut('choice-load-results', [...view.errors].map(([id, reason]) => id + ' · ' + reason).join('\n'));
}

function routeChoiceMenu(data) {
  const pool = data?.choice_pool;
  return Array.isArray(pool?.skills) && Array.isArray(pool?.prompts) && Array.isArray(pool?.excluded) ? pool : null;
}

function renderRouteChoicePool(data, view = routeStudioResult) {
  const panel = document.getElementById('route-choice-pool');
  const items = document.getElementById('route-choice-items');
  if (!panel || !items) return;
  const pool = routeChoiceMenu(data);
  panel.hidden = !pool;
  items.replaceChildren();
  if (!pool || !view) return;
  const byId = new Map([...pool.skills, ...pool.prompts].filter(c => /^(skill|prompt):.+/.test(c?.id || '')).map(c => [c.id, c]));
  for (const choice of byId.values()) {
    const card = document.createElement('div');
    card.className = 'loaded-resource-card';
    card.dataset.choiceId = choice.id;
    const kind = choice.id.split(':')[0];
    const recommendation = choice.recommended === true ? routeText('Recommended by Hub', 'Hub đề xuất') :
      choice.recommended === false ? routeText('Alternative', 'Phương án khác') : routeText('Recommendation unknown', 'Chưa rõ đề xuất');
    const deps = Array.isArray(choice.dependencies) ? choice.dependencies.join(', ') : '';
    const caps = Array.isArray(choice.capabilities) ? choice.capabilities.join(', ') : '';
    const pinned = validRoutePin(choice);
    card.innerHTML = `<strong>${escapeHtml(choice.id)}</strong>
      <div class="text-xs">${recommendation} · <span class="choice-status"></span></div>
      <div class="text-xs text-muted">${routeText('Score', 'Điểm')}: ${routerMetric(choice.score)} · ${escapeHtml(choice.reason || '')}</div>
      ${caps ? `<div class="text-xs">${routeText('Capabilities', 'Năng lực')}: ${escapeHtml(caps)}</div>` : ''}
      ${deps ? `<div class="text-xs">${routeText('Dependencies', 'Phụ thuộc')}: ${escapeHtml(deps)}</div>` : ''}
      ${choice.role ? `<div class="text-xs">${routeText('Role', 'Vai trò')}: ${escapeHtml(choice.role)}</div>` : ''}
      ${pinned ? `<div class="text-xs font-mono">${escapeHtml(choice.source_raw_sha256.slice(0,16))}…</div>` :
        `<div class="text-xs text-muted">${routeText('Load pins unavailable', 'Thiếu mã băm để nạp')}</div>`}
      <label class="text-xs"><input type="checkbox" class="choice-select-box" data-unavailable="${!pinned}"> ${routeText('Select for loading', 'Chọn để nạp')}</label>
      <button type="button" class="btn btn-subtle btn-xs">${routeText('Details', 'Chi tiết')}</button>`;
    card.querySelector('button').addEventListener('click', () => openResourceInspector(choice.resource_id || choice.id.slice(kind.length + 1), kind));
    card.querySelector('input').addEventListener('change', event => {
      if (!view.isCurrent()) return;
      if (event.target.checked) view.selected.add(choice.id);
      else view.selected.delete(choice.id);
      view.zero = false;
      refreshRouteChoices(view);
      renderRouteScoresTable();
    });
    items.appendChild(card);
  }
  for (const exclusion of pool.excluded) {
    if (typeof exclusion?.id !== 'string') continue;
    const row = document.createElement('div');
    row.className = 'text-xs text-muted';
    row.textContent = [routeText('Excluded', 'Bị loại'), exclusion.id, exclusion.reason, exclusion.related_id].filter(Boolean).join(' · ');
    items.appendChild(row);
  }
  const controls = document.createElement('div');
  if (byId.size) {
    controls.innerHTML = `<div id="route-choice-selected-count" class="text-xs" role="status"></div>
      <button type="button" class="btn btn-primary btn-xs" id="btn-load-selected-choices">${routeText('Load selected', 'Nạp mục đã chọn')}</button>
      <button type="button" class="btn btn-subtle btn-xs" id="btn-clear-selected-choices">${routeText('Choose none (0)', 'Không chọn (0)')}</button>
      <div id="choice-load-results" class="text-xs mt-xs" role="status"></div>`;
    controls.querySelector('#btn-load-selected-choices').addEventListener('click', () => loadRouterChoices(view, byId));
    controls.querySelector('#btn-clear-selected-choices').addEventListener('click', () => {
      if (view.loading || !view.isCurrent()) return;
      view.selected.clear();
      view.zero = true;
      refreshRouteChoices(view);
      renderRouteScoresTable();
    });
  } else controls.textContent = routeText('No resources offered.', 'Không có tài nguyên được đề xuất.');
  items.appendChild(controls);
  refreshRouteChoices(view);
}

function updateRouterFollowup(view) {
  const button = document.getElementById('btn-carry-followup-state');
  if (!button) return;
  button.hidden = !view.receipts.size;
  button.disabled = view.loading || !view.isCurrent();
  button.onclick = () => {
    if (!view.isCurrent() || view.loading || !view.receipts.size) return;
    const field = document.getElementById('route-context-loaded_skill_ids_and_versions');
    if (!field) return;
    field.value = JSON.stringify(mergeRouteClaims(view.contextClaims, view.receipts), null, 2);
    const details = field.closest('details');
    if (details) details.open = true;
    field.focus();
    refreshRouteChoices(view);
    button.disabled = true;
  };
}

function routerGpu(value) {
  const gpu = routerPut('decision-gpu', 'GPU: ' +
    (value === true ? routeText('VRAM resident', 'Thường trú VRAM') :
      value === false ? routeText('Not VRAM resident', 'Không thường trú VRAM') : routeText('Unknown', 'Chưa biết')));
  if (gpu) gpu.className = value === true ? 'badge badge-alive' : 'badge badge-subtle';
}

function formatRouterThreshold(kind, threshold) {
  const operator = {'>': '>', '>=': '≥', '≥': '≥'}[threshold?.operator];
  const name = kind === 'skill' ? routeText('Skill threshold', 'Ngưỡng kỹ năng') : routeText('Prompt threshold', 'Ngưỡng lời nhắc');
  return name + ': ' + (operator && Number.isFinite(threshold?.value) ?
    operator + ' ' + String(threshold.value) : routeText('Unknown', 'Chưa biết'));
}

function renderRouterCoverage(data) {
  const cov = data.coverage || {};
  const retrieval = data.retrieval || {};
  routerPut('cov-scored', routerMetric(cov.scored_candidates ?? cov.scored));
  routerPut('cov-retrieved', routerMetric(cov.retrieved_candidates ?? cov.retrieved));
  routerPut('cov-total', routerMetric(cov.catalog_total));
  routerPut('coverage-catalog-stats', retrieval.method || routeText('Retrieval method unknown', 'Chưa biết phương pháp truy xuất'));
  for (const kind of ['skill','prompt']) routerPut('route-threshold-' + kind, formatRouterThreshold(kind, data.thresholds?.[kind]));
  for (const name of ['semantic','keyword','graph','temporal']) {
    const info = retrieval.engines?.[name];
    const status = routerPut('engine-status-' + name, info?.status || routeText('Unknown', 'Chưa biết'));
    if (status) {
      status.title = info?.reason || '';
      status.className = ['OK','READY'].includes(info?.status) ? 'badge badge-ready text-xs' :
        ['ERROR','TIMEOUT','UNAVAILABLE'].includes(info?.status) ? 'badge badge-error text-xs' : 'badge badge-neutral text-xs';
    }
    routerPut('engine-count-' + name, routerMetric(info?.count ?? (Array.isArray(info?.hits) ? info.hits.length : info?.hits)));
    routerPut('engine-latency-' + name, routerMetric(info?.elapsed_ms, ' ms'));
  }
  const partial = data.partial === true ? routeText('Partial result', 'Kết quả một phần') :
    data.partial === false ? routeText('Result marked complete', 'Kết quả được đánh dấu hoàn tất') :
      routeText('Partial status unknown', 'Chưa biết kết quả có một phần hay không');
  const stop = cov.stop_reason || (Array.isArray(retrieval.adaptive_rounds) && retrieval.adaptive_rounds.at(-1)?.reason);
  routerPut('route-partial-status', [partial, data.partial_reason !== stop && data.partial_reason,
    stop && routeText('Stop: ', 'Dừng: ') + stop].filter(Boolean).join(' · '));
  routerHide(['route-partial-status'], false);
  const stages = document.getElementById('route-stage-details');
  if (!stages) return;
  stages.hidden = false;
  stages.replaceChildren();
  const rounds = Array.isArray(retrieval.adaptive_rounds) ? retrieval.adaptive_rounds : retrieval.scoring_stages;
  const caps = retrieval.scoring_budget?.round_caps;
  const heading = document.createElement('div');
  heading.textContent = [
    routeText('Scoring: ', 'Chấm điểm: ') + (retrieval.scoring_budget?.mode ||
      (Array.isArray(retrieval.adaptive_rounds) ? 'dynamic' :
        Array.isArray(retrieval.scoring_stages) ? 'staged' : routeText('Unknown', 'Chưa biết'))),
    Array.isArray(caps) && caps.length ? routeText('Cumulative limits: ', 'Giới hạn cộng dồn: ') + caps.join(' → ') : null,
    Array.isArray(rounds) ? routeText('Rounds reported: ', 'Số vòng đã ghi nhận: ') + rounds.length : null,
    Number.isFinite(cov.batches_completed) ? routeText('Completed batches: ', 'Lô đã hoàn tất: ') + cov.batches_completed : null
  ].filter(Boolean).join(' · ');
  stages.appendChild(heading);
  if (Array.isArray(rounds) && rounds.length) {
    const list = document.createElement('ol');
    for (const row of rounds) {
      const item = document.createElement('li');
      item.textContent = [
        routeText('Limit: ', 'Giới hạn: ') + routerMetric(row.cap),
        Number.isFinite(row.attempted) ? routeText('Attempted: ', 'Đã thử: ') + row.attempted : null,
        row.verified_counts ? routeText('Verified candidates: ', 'Ứng viên đã xác minh: ') +
          routerMetric(row.verified_counts.skill) + ' skill / ' + routerMetric(row.verified_counts.prompt) + ' prompt' : null,
        row.reason || row.stop_reason
      ].filter(Boolean).join(' · ');
      list.appendChild(item);
    }
    stages.appendChild(list);
  }
}

function renderRouteTaskStudioResults(data, view = routeStudioResult) {
  renderRouteChoicePool(data, view);
  if (view) updateRouterFollowup(view);
  const context = document.getElementById('route-normalized-context');
  if (context) {
    const explicit = data.retrieval?.explicit_resolution;
    context.hidden = !data.task_state && !explicit?.length;
    context.textContent = context.hidden ? '' : JSON.stringify({task_state: data.task_state || null,
      task_state_sha256: data.task_state_sha256 || null, ...(explicit?.length ? {explicit_resolution: explicit} : {})}, null, 2);
  }
  routerHide(['decision-banner','tempr-coverage-panel','loaded-resources-section','scored-candidates-section','route-studio-results'], false);
  routerHide(['semantic-hits-section'], true);
  const outcomeRaw = data.outcome || 'UNKNOWN';
  const outcome = formatStudioOutcome(outcomeRaw, !!routeChoiceMenu(data));
  const badge = routerPut('decision-outcome-badge', outcome.text);
  if (badge) badge.className = 'decision-badge ' + outcome.badgeClass;
  routerPut('decision-title', routeText('Routing result', 'Kết quả định tuyến'));
  routerPut('decision-subtitle', routeText('Requested: ', 'Đã yêu cầu: ') + routerModeLabel(view?.mode || data.query?.retrieval_mode));
  routerPut('decision-elapsed', routerMetric(data.elapsed_ms, ' ms'));
  routerPut('decision-trigger', routeText('Trigger: ', 'Kích hoạt: ') + (data.trigger || routeText('Unknown', 'Chưa biết')));
  routerGpu(data.gpu_resident);
  const provider = data.provider;
  const requests = Array.isArray(provider?.requests) ? provider.requests : [];
  const resolved = [...new Set([provider?.resolved_model, ...requests.map(row => row?.resolved_model)].filter(value => typeof value === 'string' && value))];
  const requested = [...new Set([provider?.model, ...requests.map(row => row?.model)].filter(value => typeof value === 'string' && value))];
  routerPut('decision-provider', [
    resolved.length ? routeText('Resolved model: ', 'Model đã xác định: ') + resolved.join(', ') : routeText('Resolved model unknown', 'Chưa biết model thực tế'),
    requested.length ? routeText('Requested model: ', 'Model đã yêu cầu: ') + requested.join(', ') : null,
    Number.isFinite(provider?.latency_ms) ? routerMetric(provider.latency_ms, ' ms') : null,
    Number.isFinite(provider?.cost_usd) ? '$' + provider.cost_usd : null
  ].filter(Boolean).join(' · '));
  routerPut('decision-reason-text', data.reason || data.error || data.fallback || data.fallback_discovery?.reason || routeText('Unknown', 'Chưa biết'));
  routerPut('decision-route-id', data.telemetry?.route_id || data.route_id || routeText('Unknown', 'Chưa biết'));
  renderRouterCoverage(data);
  renderRouterLoaded(data, view);
  state.routeStudio.lastScores = Array.isArray(data.scores) ? data.scores : [];
  renderRouteScoresTable();
}

function renderRouterLoaded(data, view) {
  const grid = document.getElementById('loaded-cards-grid');
  if (!grid) return;
  const receipts = [...(view?.receipts || [])];
  const legacy = ['skills','prompts'].flatMap(key => (data.loads?.[key] || []).map(item => [item, key.slice(0,-1)]));
  const deferred = data.loads?.deferred || [];
  routerPut('loaded-count-badge', routeText('Verified loads: ', 'Đã nạp và xác minh: ') + receipts.length +
    (legacy.length ? routeText(' · Reported by Hub: ', ' · Hub báo đã nạp: ') + legacy.length : ''));
  const openIds = new Set([...grid.querySelectorAll('details[open]')].map(node => node.parentElement.dataset.receiptId));
  grid.replaceChildren();
  if (!receipts.length && !legacy.length && !deferred.length) {
    grid.textContent = routeText('No resource content loaded for this request.', 'Lượt này chưa nạp nội dung tài nguyên.');
  }
  for (const [id, receipt] of receipts) {
    const card = createLoadedResourceCard({id: id.slice(id.indexOf(':')+1), name: id, body: receipt.body,
      sha256: receipt.body_raw_sha256, body_bytes: receipt.body_bytes, verified: true}, id.split(':')[0]);
    card.dataset.receiptId = id;
    const details = card.querySelector('details');
    if (details && openIds.has(id)) details.open = true;
    grid.appendChild(card);
  }
  for (const [item, kind] of legacy) grid.appendChild(createLoadedResourceCard(item, kind));
  for (const item of deferred) grid.appendChild(createDeferredResourceCard(item));
  grid.querySelectorAll('.btn-inspect-loaded').forEach(btn => btn.addEventListener('click',
    () => openResourceInspector(btn.dataset.id, btn.dataset.kind)));
}

function routerScoreMarkup(score) {
  return Number.isFinite(score) ? `<div class="flex-align-center gap-xs">
    <progress class="score-meter ${scoreMeterClass(score)}" max="100" value="${Math.min(100,Math.max(0,score * 100))}" aria-label="${routeText('Score', 'Điểm')}"></progress>
    <span class="font-mono text-xs">${String(score)}</span></div>` : routeText('Unknown', 'Chưa biết');
}

function createLoadedResourceCard(item, kind) {
  const card = document.createElement('div');
  card.className = 'loaded-resource-card';
  const name = item.name || item.id || routeText('Unknown', 'Chưa biết');
  const hash = item.sha256 || item.body_raw_sha256 || '';
  card.innerHTML = `<div class="resource-header"><strong>${escapeHtml(name)}</strong>
    <span class="badge ${item.verified ? 'badge-alive' : 'badge-subtle'}">${item.verified ?
      routeText('Verified load', 'Đã nạp và xác minh') : routeText('Reported by Hub', 'Hub báo đã nạp')}</span></div>
    <p class="text-xs font-mono">${Number.isFinite(item.body_bytes) ? item.body_bytes + ' B · ' : ''}SHA-256: ${escapeHtml(hash || '—')}</p>
    <button type="button" class="btn btn-subtle btn-xs btn-inspect-loaded" data-id="${escapeHtml(item.id || name)}" data-kind="${escapeHtml(kind)}">${routeText('Details', 'Chi tiết')}</button>
    ${item.body ? `<details class="resource-body-details"><summary>${routeText('View source text', 'Xem nội dung nguồn')}</summary><pre class="code-block-sm"><code></code></pre></details>` : ''}`;
  if (item.body) card.querySelector('code').textContent = item.body;
  return card;
}
function createDeferredResourceCard(item) {
  const card = document.createElement('div');
  card.className = 'loaded-resource-card';
  const name = item.name || item.id || routeText('Unknown', 'Chưa biết');
  const kind = item.kind === 'prompt' || item.type === 'prompt' || item.id?.startsWith('prompt:') ? 'prompt' : 'skill';
  card.innerHTML = `<div class="resource-header"><strong>${escapeHtml(name)}</strong>
    <span class="badge badge-warning">${routeText('Deferred', 'Hoãn nạp')}</span></div>
    <p class="text-xs text-muted">${escapeHtml(item.reason || '')}</p>
    <button type="button" class="btn btn-subtle btn-xs btn-inspect-loaded" data-id="${escapeHtml(item.id || name)}" data-kind="${kind}">${routeText('Details', 'Chi tiết')}</button>`;
  return card;
}
function routerScoreStatus(item, view) {
  const id = qualifiedRouteId(item.id || item.name, item.kind);
  if (view?.receipts.has(id)) return ['badge-alive', routeText('Verified load', 'Đã nạp và xác minh')];
  if (view?.errors.has(id)) return ['badge-warning', routeText('Load refused', 'Nạp bị từ chối')];
  if (view?.selected.has(id)) return ['badge-accent', routeText('Selected by you', 'Bạn đã chọn')];
  const labels = {
    offered: ['badge-ready', routeText('Offered by Hub', 'Hub đưa vào danh sách')],
    excluded: ['badge-warning', routeText('Excluded', 'Bị loại')],
    below_threshold: ['badge-subtle', routeText('Below threshold', 'Dưới ngưỡng')],
    not_offered: ['badge-subtle', routeText('Not offered', 'Không có trong danh sách')],
    deferred: ['badge-warning', routeText('Deferred', 'Hoãn nạp')],
    loaded: ['badge-alive', routeText('Reported load', 'Hub báo đã nạp')],
    selected: ['badge-subtle', routeText('Selected by Hub', 'Hub đã chọn')]
  };
  return (Object.hasOwn(labels, item.status) ? labels[item.status] : null) || ['badge-subtle', routeText('Unknown', 'Chưa biết')];
}
function renderRouteScoresTable() {
  const tbody = document.getElementById('route-scores-table-body');
  if (!tbody) return;
  const {scoresFilter, scoresKindFilter, lastScores} = state.routeStudio;
  const menu = routeChoiceMenu(routeStudioResult?.data);
  const offered = new Set([...(menu?.skills || []), ...(menu?.prompts || [])].map(row => row.id));
  const filtered = (lastScores || []).filter(item => {
    if (scoresFilter && !String(item.name || '').toLowerCase().includes(scoresFilter)) return false;
    if (scoresKindFilter === 'qualifying') return offered.has(qualifiedRouteId(item.id || item.name,item.kind));
    return !['skill','prompt'].includes(scoresKindFilter) || item.kind === scoresKindFilter;
  });
  tbody.replaceChildren();
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">${routeText('No scored candidates match.', 'Không có ứng viên đã chấm phù hợp.')}</td></tr>`;
    return;
  }
  filtered.forEach((item, index) => {
    const tr = document.createElement('tr');
    const name = item.name || item.id || routeText('Unknown', 'Chưa biết');
    const kind = item.kind;
    const [badge, text] = routerScoreStatus(item, routeStudioResult);
    tr.innerHTML = `<td class="font-mono text-xs">#${routerMetric(item.rank ?? index + 1)}</td>
      <td><strong>${escapeHtml(name)}</strong></td><td>${escapeHtml(kind || '—')}</td>
      <td>${routerScoreMarkup(item.score)}</td><td><span class="badge ${badge}">${text}</span></td>
      <td>${['skill','prompt'].includes(kind) ? `<button type="button" class="btn btn-subtle btn-xs btn-inspect-candidate" data-id="${escapeHtml(item.id || name)}" data-kind="${kind}">${routeText('Details', 'Chi tiết')}</button>` : ''}</td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('.btn-inspect-candidate').forEach(btn => btn.addEventListener('click',
    () => openResourceInspector(btn.dataset.id, btn.dataset.kind)));
}

function renderSemanticStudioResults(data, queryText) {
  renderRouteChoicePool(null);
  routerHide(['tempr-coverage-panel','loaded-resources-section','scored-candidates-section','btn-carry-followup-state','route-normalized-context'], true);
  routerHide(['route-studio-results','decision-banner','semantic-hits-section'], false);
  const ok = data.status === 'ok' || data.status === 'OK';
  const badge = routerPut('decision-outcome-badge', ok ? routeText('Search complete', 'Đã tìm kiếm') :
    (data.status || routeText('Search status unknown', 'Chưa biết trạng thái tìm kiếm')));
  if (badge) badge.className = 'decision-badge ' + (ok ? 'badge-alive' : 'badge-subtle');
  routerPut('decision-title', routeText('Semantic search', 'Tìm kiếm ngữ nghĩa'));
  routerPut('decision-subtitle', routeText('Retrieval only · no routing score', 'Chỉ truy xuất · không chấm điểm định tuyến'));
  routerPut('decision-elapsed', routerMetric(data.elapsed_ms, ' ms'));
  routerPut('decision-trigger', routerModeLabel('semantic'));
  routerGpu(data.gpu_resident);
  routerPut('decision-provider', data.model || routeText('Embedding model unknown', 'Chưa biết model embedding'));
  routerPut('decision-reason-text', data.reason || data.error || truncateStr(queryText, 80));
  routerPut('decision-route-id', routeText('Not applicable', 'Không áp dụng'));
  routerPut('semantic-hits-latency', routerMetric(data.elapsed_ms, ' ms'));
  const tbody = document.getElementById('semantic-hits-table-body');
  if (!tbody) return;
  tbody.replaceChildren();
  const hits = Array.isArray(data.hits) ? data.hits : [];
  if (!hits.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">${ok ? routeText('No semantic matches.', 'Không có kết quả ngữ nghĩa.') :
      routeText('No search results available.', 'Chưa có kết quả tìm kiếm.')}</td></tr>`;
  }
  hits.forEach((hit, index) => {
    const tr = document.createElement('tr');
    const id = hit.id || '';
    const match = /^(skill|prompt):(.+)$/.exec(id);
    const name = match?.[2] || id || routeText('Unknown', 'Chưa biết');
    tr.innerHTML = `<td class="font-mono text-xs">#${routerMetric(hit.rank ?? index + 1)}</td>
      <td><strong>${escapeHtml(name)}</strong></td><td>${escapeHtml(match?.[1] || '—')}</td>
      <td>${routerScoreMarkup(hit.raw_score)}</td><td>${escapeHtml(hit.evidence?.summary || hit.evidence?.chunk || '—')}</td>
      <td>${match ? `<button type="button" class="btn btn-subtle btn-xs">${routeText('Details', 'Chi tiết')}</button>` : ''}</td>`;
    if (match) tr.querySelector('button').addEventListener('click', () => openResourceInspector(name, match[1]));
    tbody.appendChild(tr);
  });
}

async function loadRoutingView() {
  const resp = await apiFetch('routing');
  const data = resp.data || {};
  state.routing.items = data.items || [];
  renderRoutingTable();
}

function renderRoutingTable() {
  const tbody = document.getElementById('routing-table-body');
  tbody.innerHTML = '';

  const { source } = state.routing;
  const filtered = state.routing.items.filter(r => {
    if (source !== 'all' && r.source !== source) return false;
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Không tìm thấy bản ghi điều hướng nào.</td></tr>';
    return;
  }

  filtered.forEach(route => {
    const tr = document.createElement('tr');
    const rawOutcome = route.outcome || 'ok';
    const { text, badgeClass } = formatRouteOutcome(rawOutcome);
    const outcomeBadge = `<span class="badge ${badgeClass}">${escapeHtml(text)}</span>`;

    const elapsed = route.elapsed_ms !== undefined && route.elapsed_ms !== null ? `${route.elapsed_ms} ms` : '—';
    const coverage = (route.coverage_scored !== undefined && route.coverage_total !== undefined) ?
      `${route.coverage_scored}/${route.coverage_total}` : '—';
    const cost = route.cost_usd !== null && route.cost_usd !== undefined ? `$${route.cost_usd}` : 'Unknown';

    tr.innerHTML = `
      <td class="font-mono text-xs">${formatIsoTime(route.timestamp)}</td>
      <td><span class="badge badge-subtle">${escapeHtml(route.source || 'native')}</span></td>
      <td><code>${escapeHtml(route.trigger || 'task')}</code></td>
      <td>${outcomeBadge}</td>
      <td class="font-mono">${escapeHtml(elapsed)}</td>
      <td class="font-mono">${escapeHtml(coverage)}</td>
      <td class="font-mono text-xs">${escapeHtml(cost)}</td>
      <td>
        <button class="btn btn-subtle btn-inspect-route" data-rid="${escapeHtml(route.id)}">Chi tiết chuỗi</button>
      </td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('.btn-inspect-route').forEach(b => {
    b.addEventListener('click', () => {
      const rid = b.getAttribute('data-rid');
      const route = state.routing.items.find(r => r.id === rid);
      if (route) openRouteInspector(route);
    });
  });
}

function openRouteInspector(route) {
  openInspectorDrawer();
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const nameEl = document.getElementById('inspector-name');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = 'CHI TIẾT CHUỖI ĐIỀU HƯỚNG';
  nameEl.textContent = `Route: ${route.id || '—'}`;

  let eventsHtml = '';
  (route.events || []).forEach(ev => {
    eventsHtml += `
      <div class="card mb-sm">
        <div class="card-header p-header-sm">
          <strong>${escapeHtml(ev.kind || 'event')}</strong>
          <span class="font-mono text-xs text-muted">${formatIsoTime(ev.emitted_at)}</span>
        </div>
        <div class="card-body p-sm">
          <pre class="code-block-sm"><code>${escapeHtml(JSON.stringify(ev.fields || {}, null, 2))}</code></pre>
        </div>
      </div>
    `;
  });

  const { text: outcomeText, badgeClass: outcomeBadgeClass } = formatRouteOutcome(route.outcome);

  // Fallback detection: absence != false
  let fallbackHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  if (route.outcome === 'FALLBACK' || route.fallback === true || (route.events || []).some(e => e.kind === 'fallback' || e.fields?.fallback === true)) {
    fallbackHtml = '<span class="badge badge-warning">Có kích hoạt</span>';
  } else if (route.fallback === false) {
    fallbackHtml = '<span class="badge badge-subtle">Không kích hoạt</span>';
  }

  // Selected skill detection
  let selectedSkill = 'Chưa ghi nhận';
  if (route.fields && route.fields.selected_skill) {
    selectedSkill = route.fields.selected_skill;
  } else {
    const selEv = (route.events || []).find(e => e.kind === 'select' && e.fields?.selected_skill);
    if (selEv) selectedSkill = selEv.fields.selected_skill;
  }

  bodyEl.innerHTML = `
    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Kết quả Điều hướng (Outcome)</span>
      <div class="inspector-prop-val"><span class="badge ${outcomeBadgeClass}">${escapeHtml(outcomeText)}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Nguồn phát sinh (Source)</span>
      <div class="inspector-prop-val"><span class="badge badge-subtle">${escapeHtml(route.source || 'native')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Thời gian phát sinh (Timestamp)</span>
      <div class="inspector-prop-val font-mono text-xs">${formatIsoTime(route.timestamp)}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Thời gian xử lý (Elapsed)</span>
      <div class="inspector-prop-val font-mono">${route.elapsed_ms !== null && route.elapsed_ms !== undefined ? route.elapsed_ms + ' ms' : '—'}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Các trường Quyết định Router (Decision Fields)</span>
      <div class="notice-box notice-warning mt-xs">
        <p class="text-xs">Dữ liệu router tuân thủ nguyên tắc trung thực: các trường không được producer ghi nhận sẽ hiển thị là Unknown.</p>
        <div><strong>should_use_skill:</strong> <span class="badge badge-subtle">${route.fields && route.fields.should_use_skill !== undefined ? escapeHtml(route.fields.should_use_skill) : 'Unknown'}</span></div>
        <div><strong>selected_skill:</strong> <code>${escapeHtml(selectedSkill)}</code></div>
        <div><strong>confidence:</strong> <span class="badge badge-subtle">${route.fields && route.fields.confidence !== undefined ? escapeHtml(route.fields.confidence) : 'Unknown'}</span></div>
        <div><strong>candidates:</strong> <span class="badge badge-subtle">${route.fields && route.fields.candidates !== undefined ? escapeHtml(route.fields.candidates) : 'Unknown'}</span></div>
        <div><strong>fallback:</strong> ${fallbackHtml}</div>
      </div>
    </div>

    <div class="inspector-prop-row mt-md">
      <span class="inspector-prop-label">Chuỗi sự kiện trong Route (${(route.events || []).length} sự kiện):</span>
      <div class="event-chain-container">${eventsHtml || '<p class="text-muted text-xs">Không có sự kiện con.</p>'}</div>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// 05: Agent Registry View
// ----------------------------------------------------------------------------

async function loadAgentsView() {
  const searchInput = document.getElementById('agents-search-input');
  const q = searchInput ? searchInput.value.trim() : '';
  const statsEl = document.getElementById('agents-stats');
  const tbody = document.getElementById('agents-table-body');
  const noticeBox = document.getElementById('agents-notice-box');
  const noticeDesc = document.getElementById('agents-notice-desc');

  try {
    const query = q ? `agents?q=${encodeURIComponent(q)}` : 'agents';
    const resp = await apiFetch(query);
    const data = resp.data || {};
    const items = data.items || [];

    if (statsEl) {
      statsEl.textContent = `${items.length} agents`;
    }

    if (items.length > 0) {
      if (noticeBox) noticeBox.hidden = true;
      tbody.innerHTML = '';
      items.forEach(agent => {
        const tr = document.createElement('tr');
        let skillsCell = '';
        if (agent.skills === null || agent.skills === undefined) {
          skillsCell = '<span class="badge badge-subtle text-xs">Chưa ghi nhận (Unknown)</span>';
        } else if (Array.isArray(agent.skills)) {
          const skillsCount = agent.skills.length;
          const skillsPreview = agent.skills.slice(0, 3).map(s => `<span class="agent-skill-tag">${escapeHtml(s)}</span>`).join(' ') + (skillsCount > 3 ? ` <small class="text-muted">+${skillsCount - 3}</small>` : '');
          skillsCell = `<div class="skills-tags-cloud">${skillsPreview || '<span class="text-muted text-xs">0 kỹ năng</span>'}</div>`;
        } else {
          skillsCell = '<span class="badge badge-subtle text-xs">Chưa ghi nhận (Unknown)</span>';
        }

        let mcpCell = '';
        if (agent.mcp === null || agent.mcp === undefined) {
          mcpCell = '<span class="badge badge-subtle text-xs">Chưa ghi nhận (Unknown)</span>';
        } else if (Array.isArray(agent.mcp)) {
          mcpCell = `<span class="badge badge-subtle">${agent.mcp.length} tools</span>`;
        } else {
          mcpCell = '<span class="badge badge-subtle text-xs">Chưa ghi nhận (Unknown)</span>';
        }

        tr.innerHTML = `
          <td>
            <strong>${escapeHtml(agent.name || agent.id)}</strong>
            <div class="font-mono text-xs text-muted">${escapeHtml(agent.id)}</div>
          </td>
          <td class="text-sm">${escapeHtml(agent.role || '—')}</td>
          <td><span class="badge badge-alive font-mono text-xs">${escapeHtml(agent.model || 'Chưa gán')}</span></td>
          <td><code>${escapeHtml(agent.runtime || 'openbot')}</code></td>
          <td>${skillsCell}</td>
          <td>${mcpCell}</td>
          <td>
            <button class="btn btn-subtle btn-inspect-agent" data-id="${escapeHtml(agent.id)}">Chi tiết</button>
          </td>
        `;
        tbody.appendChild(tr);
      });

      tbody.querySelectorAll('.btn-inspect-agent').forEach(btn => {
        btn.addEventListener('click', () => {
          const id = btn.getAttribute('data-id');
          const agent = items.find(a => a.id === id);
          if (agent) openAgentInspector(agent);
        });
      });
    } else {
      if (noticeBox) {
        noticeBox.hidden = false;
        if (resp.limits && resp.limits.length > 0) {
          noticeDesc.textContent = resp.limits.join(' ');
        }
      }
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">Adapter chưa sẵn sàng hoặc không có agent nào phù hợp với bộ lọc.</td></tr>';
    }
  } catch (err) {
    if (noticeBox) noticeBox.hidden = false;
    tbody.innerHTML = `<tr><td colspan="7" class="text-center text-danger">Lỗi tải dữ liệu agent: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function openAgentInspector(agent) {
  openInspectorDrawer();
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const nameEl = document.getElementById('inspector-name');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = 'CHI TIẾT AGENT';
  nameEl.textContent = agent.name || agent.id;

  let skillsHtml = '';
  let skillsCountLabel = 'Chưa ghi nhận';
  if (agent.skills === null || agent.skills === undefined) {
    skillsHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  } else if (Array.isArray(agent.skills)) {
    skillsCountLabel = `${agent.skills.length}`;
    skillsHtml = agent.skills.length > 0 ?
      agent.skills.map(s => `<span class="agent-skill-tag">${escapeHtml(s)}</span>`).join(' ') :
      '<span class="text-muted text-xs">Không có kỹ năng gán</span>';
  } else {
    skillsHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  }

  let mcpHtml = '';
  let mcpCountLabel = 'Chưa ghi nhận';
  if (agent.mcp === null || agent.mcp === undefined) {
    mcpHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  } else if (Array.isArray(agent.mcp)) {
    mcpCountLabel = `${agent.mcp.length}`;
    mcpHtml = agent.mcp.length > 0 ?
      agent.mcp.map(m => `<span class="tool-tag">${escapeHtml(m)}</span>`).join(' ') :
      '<span class="text-muted text-xs">Không có công cụ MCP</span>';
  } else {
    mcpHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  }

  let permHtml = '';
  if (agent.permissions === null || agent.permissions === undefined) {
    permHtml = '<span class="badge badge-subtle">Chưa ghi nhận (Unknown)</span>';
  } else {
    permHtml = `<code>${escapeHtml(agent.permissions)}</code>`;
  }

  bodyEl.innerHTML = `
    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Agent ID</span>
      <div class="inspector-prop-val"><code>${escapeHtml(agent.id)}</code></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Vai trò &amp; Nhiệm vụ (Role)</span>
      <div class="inspector-prop-val text-sm">${escapeHtml(agent.role || '—')}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Mô hình LLM (Model)</span>
      <div class="inspector-prop-val"><span class="badge badge-alive">${escapeHtml(agent.model || 'Chưa gán')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Runtime / Provider</span>
      <div class="inspector-prop-val"><code>${escapeHtml(agent.runtime || 'openbot')}</code></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Trạng thái cấu hình</span>
      <div class="inspector-prop-val"><span class="badge badge-subtle">${escapeHtml(agent.status || 'configured')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Kỹ năng được gán (${skillsCountLabel})</span>
      <div class="skills-tags-cloud mt-xs">${skillsHtml}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Công cụ MCP khả dụng (${mcpCountLabel})</span>
      <div class="mcp-tools-list mt-xs">${mcpHtml}</div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Quyền hạn &amp; Nguồn (Permissions)</span>
      <div class="inspector-prop-val text-xs text-muted">${permHtml} (Nguồn: ${escapeHtml(agent.source || 'projection')})</div>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// 06: MCP & Integration Manager View
// ----------------------------------------------------------------------------

async function loadIntegrationsView(probeTarget = null) {
  const query = probeTarget ? `integrations?probe=${encodeURIComponent(probeTarget)}` : 'integrations';
  const resp = await apiFetch(query);
  const data = resp.data || {};
  const items = data.items || [];

  const statsEl = document.getElementById('mcp-stats');
  if (statsEl) {
    statsEl.textContent = `Tổng: ${items.length} MCP servers (Đã cấu hình: ${data.configured_count || 0})`;
  }

  const grid = document.getElementById('mcp-servers-grid');
  grid.innerHTML = '';

  if (items.length === 0) {
    grid.innerHTML = '<div class="text-center text-muted col-span-full">Không có máy chủ MCP nào được ghi nhận.</div>';
    return;
  }

  items.forEach(srv => {
    const card = document.createElement('div');
    card.className = 'mcp-card';
    const tools = srv.tools || [];
    let toolsHtml = '';
    tools.slice(0, 15).forEach(t => {
      toolsHtml += `<span class="tool-tag">${escapeHtml(t.name)}</span>`;
    });
    if (tools.length > 15) {
      toolsHtml += `<span class="tool-tag">+${tools.length - 15} công cụ khác</span>`;
    }

    let healthBadge = '';
    if (srv.health) {
      if (srv.health.status === 'observed') {
        const skillsCount = srv.health.fields?.catalog_skills !== undefined ? `${srv.health.fields.catalog_skills} skills` : 'healthy';
        const gpuInfo = srv.health.fields?.warmup?.gpu_resident ? ' · dGPU VRAM' : '';
        healthBadge = `<span class="badge health-badge-observed">Health OK (${skillsCount}${gpuInfo})</span>`;
      } else if (srv.health.status === 'degraded') {
        healthBadge = `<span class="badge badge-warning">Health: Suy giảm (503)</span>`;
      } else {
        healthBadge = `<span class="badge health-badge-unavailable">Health: ${escapeHtml(srv.health.status)}</span>`;
      }
    } else {
      healthBadge = `<span class="badge badge-subtle">Chưa probe health</span>`;
    }

    const enabledBadge = srv.enabled === true ? '<span class="badge badge-alive">Bật</span>' :
      (srv.enabled === false ? '<span class="badge badge-muted">Tắt</span>' : '<span class="badge badge-subtle">Catalog</span>');

    card.innerHTML = `
      <div class="mcp-card-header">
        <span class="mcp-name">${escapeHtml(srv.name)}</span>
        <div>
          ${enabledBadge}
          ${healthBadge}
        </div>
      </div>
      <div class="text-xs text-muted mb-xs">Transport: <code>${escapeHtml(srv.transport || 'stdio/http')}</code> · Trạng thái: <strong>${escapeHtml(srv.status || 'catalogued')}</strong></div>
      <div class="text-xs text-muted">Tổng số công cụ khai báo: <strong>${tools.length}</strong></div>
      <div class="mcp-tools-list mt-xs">${toolsHtml || '<span class="text-muted text-xs">Không có công cụ cụ thể</span>'}</div>
    `;
    grid.appendChild(card);
  });
}

// ----------------------------------------------------------------------------
// 07: Prompt & Knowledge Library View
// ----------------------------------------------------------------------------

async function loadLibraryView() {
  const kindSelect = document.getElementById('library-kind-select');
  const kind = kindSelect ? kindSelect.value : 'prompt';
  state.library.kind = kind;

  const { page, limit, q, domain } = state.library;
  const offset = (page - 1) * limit;
  let queryUrl = `library?kind=${encodeURIComponent(kind)}&offset=${offset}&limit=${limit}`;
  if (q) queryUrl += `&q=${encodeURIComponent(q)}`;
  if (domain) queryUrl += `&domain=${encodeURIComponent(domain)}`;

  const resp = await apiFetch(queryUrl);
  const data = resp.data || {};
  state.library.total = data.total || 0;
  state.library.items = data.items || [];

  document.getElementById('library-search-mode').textContent = 'Chế độ tìm kiếm: ' + (data.search_mode || 'Đọc catalog');
  document.getElementById('library-total-count').textContent = `Tổng: ${state.library.total.toLocaleString('vi-VN')} ${kind === 'document' ? 'tài liệu' : 'prompts'}`;

  // Populate domains if empty
  const domainSelect = document.getElementById('library-domain-select');
  if (domainSelect && domainSelect.options.length <= 1) {
    const domains = new Set();
    state.library.items.forEach(it => { if (it.domain) domains.add(it.domain); });
    domains.forEach(dom => {
      const opt = document.createElement('option');
      opt.value = dom;
      opt.textContent = dom;
      domainSelect.appendChild(opt);
    });
  }

  // Table
  const tbody = document.getElementById('library-table-body');
  tbody.innerHTML = '';
  if (state.library.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">Không tìm thấy ${kind === 'document' ? 'tài liệu' : 'mẫu prompt'} nào phù hợp.</td></tr>`;
  } else {
    state.library.items.forEach(item => {
      const tr = document.createElement('tr');
      const activeBadge = item.active ? '<span class="badge badge-alive">Hoạt động</span>' : '<span class="badge badge-muted">Tắt</span>';
      const versionShort = item.version ? item.version.slice(0, 12) + '…' : '—';

      const isEditable = item.editable !== false && item.kind !== 'document' && kind !== 'document';
      tr.innerHTML = `
        <td>
          <strong>${escapeHtml(item.name)}</strong>
          <div class="text-xs text-muted">${escapeHtml(item.description || '')}</div>
        </td>
        <td><code>${escapeHtml(item.domain || 'general')}</code></td>
        <td class="font-mono text-xs" title="${escapeHtml(item.version || '')}">${escapeHtml(versionShort)}</td>
        <td>${activeBadge}</td>
        <td>
          <div class="pagination-controls">
            <button class="btn btn-subtle btn-inspect-library" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind || kind)}">Chi tiết</button>
            ${isEditable ? `<button class="btn btn-secondary btn-revise-library" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind || kind)}">Sửa đổi</button>` : '<span class="badge badge-subtle">Chỉ đọc</span>'}
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  tbody.querySelectorAll('.btn-inspect-library').forEach(b => {
    b.addEventListener('click', () => {
      openResourceInspector(b.getAttribute('data-id'), b.getAttribute('data-kind'));
    });
  });

  tbody.querySelectorAll('.btn-revise-library').forEach(b => {
    b.addEventListener('click', () => {
      initiateCatalogRevision(b.getAttribute('data-id'), b.getAttribute('data-kind'));
    });
  });
}

// ----------------------------------------------------------------------------
// 08: Observability & Telemetry View
// ----------------------------------------------------------------------------

async function loadTelemetryView() {
  const resp = await apiFetch('telemetry');
  const data = resp.data || {};
  state.telemetry.items = data.items || [];
  renderTelemetryTable();
}

function renderTelemetryTable() {
  const tbody = document.getElementById('telemetry-table-body');
  tbody.innerHTML = '';

  const { source, kind } = state.telemetry;
  const filtered = state.telemetry.items.filter(ev => {
    if (source !== 'all' && ev.source !== source) return false;
    if (kind !== 'all' && ev.kind !== kind) return false;
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Không tìm thấy sự kiện telemetry phù hợp.</td></tr>';
    return;
  }

  filtered.slice(0, 100).forEach(ev => {
    const tr = document.createElement('tr');
    const fieldsStr = JSON.stringify(ev.fields || {});
    tr.innerHTML = `
      <td class="font-mono text-xs">${formatIsoTime(ev.emitted_at)}</td>
      <td><span class="badge badge-subtle">${escapeHtml(ev.source || 'native')}</span></td>
      <td><code>${escapeHtml(ev.kind || '—')}</code></td>
      <td class="font-mono text-xs">${escapeHtml(ev.correlation_id || '—')}</td>
      <td class="font-mono text-xs"><pre class="code-block-sm"><code>${escapeHtml(fieldsStr)}</code></pre></td>
    `;
    tbody.appendChild(tr);
  });
}

// ----------------------------------------------------------------------------
// 09: Quality & Evaluation View
// ----------------------------------------------------------------------------

let qualitySecOffset = 0;
let originalSourceOffset = 0;
let curationOffset = 0;
let curationSearchTimer = null;
let catalogQualityOffset = 0;
let catalogQualityTotal = 0;

function getQualityLimit() {
  const limitEl = document.getElementById('sec-filter-limit');
  const parsed = limitEl ? parseInt(limitEl.value, 10) : 100;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 100;
}

function getQualityFilterQuery() {
  const params = new URLSearchParams();
  const bucketEl = document.getElementById('sec-filter-bucket');
  const kindEl = document.getElementById('sec-filter-kind');
  const ruleEl = document.getElementById('sec-filter-rule');
  const queryEl = document.getElementById('sec-filter-query');
  if (bucketEl && bucketEl.value) params.set('sec_bucket', bucketEl.value);
  if (kindEl && kindEl.value) params.set('sec_kind', kindEl.value);
  if (ruleEl && ruleEl.value) params.set('sec_rule', ruleEl.value);
  if (queryEl && queryEl.value.trim()) params.set('sec_q', queryEl.value.trim());
  const originalFilter = document.getElementById('original-source-filter');
  if (originalFilter) params.set('original_disposition', originalFilter.value);
  params.set('original_offset', String(originalSourceOffset));
  params.set('original_limit', '20');
  params.set('curation_offset', String(curationOffset));
  params.set('curation_state', document.getElementById('curation-filter').value);
  params.set('curation_q', document.getElementById('curation-search').value.trim());
  params.set('sec_offset', String(Math.max(0, qualitySecOffset)));
  params.set('sec_limit', String(getQualityLimit()));
  const qs = params.toString();
  return qs ? `quality?${qs}` : 'quality';
}

function bindQualityFilterEvents() {
  const wrap = document.getElementById('security-scan-filters');
  if (!wrap || wrap.dataset.bound === '1') return;
  wrap.dataset.bound = '1';
  let timer = null;
  ['sec-filter-bucket', 'sec-filter-kind', 'sec-filter-rule', 'sec-filter-limit'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => { qualitySecOffset = 0; loadQualityView(); });
  });
  const queryEl = document.getElementById('sec-filter-query');
  if (queryEl) {
    queryEl.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { qualitySecOffset = 0; loadQualityView(); }, 250);
    });
  }
  const prevBtn = document.getElementById('sec-page-prev');
  if (prevBtn) {
    prevBtn.addEventListener('click', () => {
      qualitySecOffset = Math.max(0, qualitySecOffset - getQualityLimit());
      loadQualityView();
    });
  }
  const nextBtn = document.getElementById('sec-page-next');
  if (nextBtn) {
    nextBtn.addEventListener('click', () => {
      qualitySecOffset += getQualityLimit();
      loadQualityView();
    });
  }
}

async function loadQualityView() {
  bindQualityFilterEvents();
  await loadCatalogQuality();
  const resp = await apiFetch(getQualityFilterQuery());
  let duplicateEvidence = {};
  try {
    const groups = await apiFetch('catalog-quality?view=groups&limit=100');
    for (const group of groups.data?.items || []) {
      const hash = group.members?.[0]?.body_sha256;
      if (hash) duplicateEvidence[hash] = group.distinct_bundle_count;
    }
  } catch (_) { /* The body scan remains useful if the pinned receipt is unavailable. */ }
  const data = resp.data || {};
  const items = data.items || [];
  const emptyBodies = data.empty_bodies || {};

  const countBadge = document.getElementById('duplicates-count-badge');
  if (countBadge) {
    const totalGroups = typeof data.total === 'number' ? data.total : items.length;
    const isTruncated = data.truncated === true;
    const totalDups = items.reduce((acc, x) => acc + (x.count || 0), 0);
    countBadge.textContent = `${totalGroups} nhóm trùng${isTruncated ? ' (hiển thị 100)' : ''} (${totalDups} tài nguyên)`;
  }

  const tbody = document.getElementById('duplicates-table-body');
  tbody.innerHTML = '';

  if (items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">Không phát hiện nhóm kỹ năng trùng lặp exact body hash (không rỗng).</td></tr>';
  } else {
    items.forEach(dup => {
      const tr = document.createElement('tr');
      const members = dup.members || (dup.resources || dup.member_ids || []).map(rId => ({ id: rId, name: rId, kind: 'skill' }));
      const resBadges = members.map(m => {
        const mId = typeof m === 'object' ? m.id : m;
        const mKind = (typeof m === 'object' && m.kind) ? m.kind : 'skill';
        const mName = (typeof m === 'object' && m.name) ? m.name : mId;
        return `<button type="button" class="btn btn-xs btn-subtle resource-inspect-link font-mono" data-id="${escapeHtml(mId)}" data-kind="${escapeHtml(mKind)}" title="Xem chi tiết ${escapeHtml(mKind)} ${escapeHtml(mName)}">${escapeHtml(mName)}</button>`;
      }).join(' ');

      tr.innerHTML = `
        <td class="font-mono text-xs" title="${escapeHtml(dup.sha256)}">${escapeHtml(truncateStr(dup.sha256, 16))}</td>
        <td><span class="badge badge-warning">${escapeHtml(dup.count)} lần</span></td>
        <td>Trùng phần thân${duplicateEvidence[dup.sha256] != null ? `<br><small>${escapeHtml(duplicateEvidence[dup.sha256])} bộ tệp khác nhau</small>` : ''}</td>
        <td><div class="d-flex flex-wrap gap-xs align-center">${resBadges || '<span class="text-muted text-xs">Không có danh sách</span>'}</div></td>
      `;
      tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.resource-inspect-link').forEach(btn => {
      btn.addEventListener('click', () => {
        const rId = btn.getAttribute('data-id');
        const kind = btn.getAttribute('data-kind') || 'skill';
        if (rId) openResourceInspector(rId, kind);
      });
    });
  }

  // Handle empty / unextracted bodies panel
  const emptyPanel = document.getElementById('empty-bodies-panel');
  const samplesWrap = document.getElementById('empty-bodies-samples-wrap');
  const samplesContainer = document.getElementById('empty-bodies-samples');

  // Always reset samples container first to prevent stale state on reload
  if (samplesContainer) samplesContainer.innerHTML = '';
  if (samplesWrap) samplesWrap.hidden = true;

  if (emptyPanel && (emptyBodies.count > 0 || emptyBodies.missing_or_null_hash_count > 0)) {
    emptyPanel.hidden = false;
    const emptyBadge = document.getElementById('empty-bodies-badge');
    if (emptyBadge) {
      const parts = [];
      if (emptyBodies.count > 0) parts.push(`${emptyBodies.count} thân rỗng`);
      if (emptyBodies.missing_or_null_hash_count > 0) parts.push(`${emptyBodies.missing_or_null_hash_count} thiếu hash`);
      emptyBadge.textContent = parts.join(' · ') || 'Chưa trích xuất';
    }

    const emptyDesc = document.getElementById('empty-bodies-desc');
    if (emptyDesc) {
      emptyDesc.textContent = emptyBodies.description || 'Tài nguyên chưa trích xuất phần thân (catalog body unextracted).';
    }

    const samplesList = emptyBodies.samples || (emptyBodies.sample_resources || []).map(rId => ({ id: rId, name: rId, kind: 'skill' }));
    if (samplesWrap && samplesContainer && samplesList.length > 0) {
      samplesWrap.hidden = false;
      samplesContainer.innerHTML = samplesList.map(s => {
        const sId = typeof s === 'object' ? s.id : s;
        const sKind = (typeof s === 'object' && s.kind) ? s.kind : 'skill';
        const sName = (typeof s === 'object' && s.name) ? s.name : sId;
        const qReason = (typeof s === 'object' && s.quarantine_reason) ? ` (${s.quarantine_reason})` : '';
        return `<button type="button" class="btn btn-xs btn-subtle resource-inspect-link font-mono" data-id="${escapeHtml(sId)}" data-kind="${escapeHtml(sKind)}" title="Xem chi tiết ${escapeHtml(sKind)} ${escapeHtml(sName)}${escapeHtml(qReason)}">${escapeHtml(sName)}</button>`;
      }).join(' ');

      samplesContainer.querySelectorAll('.resource-inspect-link').forEach(btn => {
        btn.addEventListener('click', () => {
          const rId = btn.getAttribute('data-id');
          const kind = btn.getAttribute('data-kind') || 'skill';
          if (rId) openResourceInspector(rId, kind);
        });
      });
    }
  } else if (emptyPanel) {
    emptyPanel.hidden = true;
  }

  // Security scan findings (full catalog: skills + prompts)
  const sec = data.security_scan;
  const secBadge = document.getElementById('security-scan-badge');
  const secSummary = document.getElementById('security-scan-summary');
  const secScope = document.getElementById('security-scan-scope-text');
  const secWrap = document.getElementById('security-findings-table-wrap');
  const secTbody = document.getElementById('security-scan-table-body');

  if (sec) {
    const cov = sec.coverage || {};
    const sum = sec.summary || {};
    const byBucket = sum.by_bucket || {};
    const byRule = sum.by_rule || {};
    const secReviewCount = byBucket.security_review || 0;
    const dataQualityCount = byBucket.data_quality || 0;
    const exampleCount = byBucket.resolved_example || 0;
    const cleanCount = byBucket.clean || 0;
    const skillsScanned = typeof cov.skills_scanned === 'number' ? cov.skills_scanned : (sec.total || 0);
    const promptsScanned = typeof cov.prompts_scanned === 'number' ? cov.prompts_scanned : 0;

    secBadge.textContent = `Đã quét ${skillsScanned.toLocaleString('vi-VN')} skill + ${promptsScanned.toLocaleString('vi-VN')} prompt · ${secReviewCount} cần rà soát · ${dataQualityCount} thân rỗng`;
    secBadge.className = secReviewCount > 0 ? 'badge badge-warning' : 'badge badge-subtle';
    if (secScope) secScope.textContent = 'Quy tắc tĩnh trên nội dung đã lập chỉ mục; chưa kiểm tra frontmatter. Thân rỗng được tách riêng. Kết quả này không chứng nhận an toàn hay xác nhận thông tin nhạy cảm còn hiệu lực.';
    renderOriginalSources(sec.original_sources);
    renderCuration(sec.catalog_curation);

    const offset = typeof cov.offset === 'number' ? cov.offset : 0;
    const returned = typeof cov.returned === 'number' ? cov.returned : (sec.items ? sec.items.length : 0);
    const filteredTotal = typeof cov.filtered_total === 'number' ? cov.filtered_total : returned;
    qualitySecOffset = offset;
    const rangeEl = document.getElementById('security-scan-range');
    if (rangeEl) {
      rangeEl.textContent = (filteredTotal === 0 || returned === 0)
        ? `Hiển thị 0 / ${filteredTotal} kết quả`
        : `Hiển thị ${offset + 1}–${offset + returned} / ${filteredTotal} kết quả${cov.truncated ? ' (còn trang sau)' : ''}`;
    }
    const prevBtn = document.getElementById('sec-page-prev');
    if (prevBtn) prevBtn.disabled = offset <= 0;
    const nextBtn = document.getElementById('sec-page-next');
    if (nextBtn) nextBtn.disabled = (offset + returned) >= filteredTotal;

    if (secSummary) {
      secSummary.innerHTML = [
        `<span class="badge badge-warning">Cần rà soát: ${escapeHtml(secReviewCount)}</span>`,
        `<span class="badge badge-subtle">Thân rỗng: ${escapeHtml(dataQualityCount)}</span>`,
        `<span class="badge badge-subtle">Ví dụ theo quy tắc / rà soát: ${escapeHtml(exampleCount)}</span>`,
        `<span class="badge badge-subtle">Không khớp quy tắc: ${escapeHtml(cleanCount)}</span>`,
        `<span class="badge badge-warning">EXEC/INJ: ${escapeHtml(byRule.EXECUTION_OR_INJECTION_REVIEW || 0)}</span>`,
        `<span class="badge badge-subtle">REDACTED_CRED: ${escapeHtml(byRule.REDACTED_CREDENTIAL_REVIEW || 0)}</span>`,
      ].join(' ');
    }

    secWrap.hidden = false;
    secTbody.innerHTML = '';

    if (sec.items && sec.items.length > 0) {
      sec.items.forEach(f => {
        const tr = document.createElement('tr');
        const rKind = f.kind || 'skill';
        const checksHtml = (f.checks && f.checks.length > 0) ?
          f.checks.map(c => {
            const badgeClass = c === 'EMPTY_BODY' ? 'badge badge-subtle text-xs' : 'badge badge-warning text-xs';
            return `<span class="${badgeClass}">${escapeHtml(c)}</span>`;
          }).join(' ') :
          '<span class="badge badge-subtle text-xs">no-rule-hit</span>';

        const sigHtml = (f.credential_signals && f.credential_signals.length > 0) ?
          f.credential_signals.map(s => `<span class="badge badge-subtle text-xs">${escapeHtml(s)}</span>`).join(' ') : '';
        const evidenceHtml = (f.evidence && f.evidence.length > 0) ?
          `<div class="text-xs text-muted mt-xs font-mono">${f.evidence.map(ev => `L${escapeHtml(ev.line)}: ${escapeHtml(ev.snippet || '')}`).join('<br>')}</div>` : '';

        const disp = f.disposition || f.status || 'review';
        let statusBadge;
        if (f.bucket === 'data_quality' || (f.checks && f.checks.includes('EMPTY_BODY'))) {
          statusBadge = '<span class="badge badge-warning" title="Thân rỗng: DATA_QUALITY, không thể chứng nhận an toàn (no false PASS)">DATA_QUALITY · unscannable_empty_body</span>';
        } else if (disp === 'confirmed_finding') {
          statusBadge = '<span class="badge badge-warning">confirmed_finding</span>';
        } else if (disp === 'confirmed_example') {
          statusBadge = f.automatic_triage?.example_only ? '<span class="badge badge-subtle">Ví dụ theo quy tắc tự động</span>' : '<span class="badge badge-subtle">Ví dụ đã đánh dấu thủ công</span>';
        } else if (f.status === 'review' || disp === 'needs_source_review') {
          statusBadge = `<span class="badge badge-warning">${escapeHtml(disp)}${f.stale_disposition ? ' · stale-reopened' : ''}</span>`;
        } else {
          statusBadge = '<span class="badge badge-subtle">no-rule-hit</span>';
        }

        const remText = f.remediation && f.remediation.suggested_action ?
          `<div class="text-xs text-muted mt-xs">${f.automatic_triage?.example_only ? 'Quy tắc nhận diện ví dụ; chưa phải chứng nhận an toàn.' : 'Đối chiếu nguồn gốc và bối cảnh trước khi kết luận.'}</div>` : '';

        tr.innerHTML = `
          <td><button type="button" class="btn btn-xs btn-subtle resource-inspect-link font-mono" data-id="${escapeHtml(f.id)}" data-kind="${escapeHtml(rKind)}">${escapeHtml(f.id)}</button></td>
          <td class="font-mono text-xs" title="${escapeHtml(f.version || '')}"><span class="badge badge-subtle text-xs">${escapeHtml(rKind)}</span> ${escapeHtml(truncateStr(f.version || '', 10))}</td>
          <td><div>${checksHtml} ${sigHtml}</div>${evidenceHtml}</td>
          <td><div>${statusBadge}</div>${remText}</td>
        `;
        secTbody.appendChild(tr);
      });

      secTbody.querySelectorAll('.resource-inspect-link').forEach(btn => {
        btn.addEventListener('click', () => {
          const rId = btn.getAttribute('data-id');
          const rKind = btn.getAttribute('data-kind') || 'skill';
          if (rId) openResourceInspector(rId, rKind);
        });
      });
    } else {
      secTbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">Không có tài nguyên nào khớp bộ lọc hiện tại.</td></tr>';
    }
  } else {
    secBadge.textContent = 'Quy tắc tĩnh trên catalog';
    if (secSummary) secSummary.innerHTML = '';
    secWrap.hidden = true;
  }

  // Benchmark metrics (Historical regression only from AUDIT.json, never current SLA)
  const bm = data.benchmark;
  const bmBadge = document.getElementById('benchmark-status-badge');
  const bmDisplay = document.getElementById('benchmark-metrics-display');

  if (bm) {
    const verdictText = bm.verdict ? String(bm.verdict) : 'Chưa ghi nhận (Unknown)';
    bmBadge.textContent = `Hồi quy lịch sử: ${verdictText}`;
    bmBadge.className = bm.verdict === 'PASS' ? 'badge badge-alive' : (bm.verdict ? 'badge badge-warning' : 'badge badge-subtle');
    bmDisplay.hidden = false;

    const selRecall = typeof bm.selection_recall === 'number' ? `${(bm.selection_recall * 100).toFixed(1)}%` : 'Chưa ghi nhận (Unknown)';
    const loadRecall = typeof bm.loaded_recall === 'number' ? `${(bm.loaded_recall * 100).toFixed(1)}%` : 'Chưa ghi nhận (Unknown)';
    const candRecall = typeof bm.candidate_recall === 'number' ? `${(bm.candidate_recall * 100).toFixed(1)}%` : 'Chưa ghi nhận (Unknown)';

    let p95Text = 'Chưa ghi nhận (Unknown)';
    if (typeof bm.timing_ms?.p95_frozen === 'number') {
      p95Text = `${(bm.timing_ms.p95_frozen / 1000).toFixed(2)}s`;
    } else if (typeof bm.timing_ms?.p95 === 'number') {
      p95Text = `${(bm.timing_ms.p95 / 1000).toFixed(2)}s`;
    } else if (typeof bm.timing_ms?.p95_nearest_rank === 'number') {
      p95Text = `${(bm.timing_ms.p95_nearest_rank / 1000).toFixed(2)}s`;
    } else if (typeof bm.timing_ms?.nearest_rank_p95 === 'number') {
      p95Text = `${(bm.timing_ms.nearest_rank_p95 / 1000).toFixed(2)}s`;
    }

    const casesText = (bm.cases !== undefined && bm.cases !== null) ? `${bm.cases} ca kiểm thử` : 'Chưa ghi nhận (Unknown)';

    bmDisplay.innerHTML = `
      <div class="benchmark-stat-card">
        <div class="benchmark-stat-label">Kết luận Hồi quy Lịch sử</div>
        <div class="benchmark-stat-val ${bm.verdict === 'PASS' ? 'text-success' : 'text-muted'}">${escapeHtml(verdictText)}</div>
        <small class="text-muted text-xs">Mẫu: ${escapeHtml(casesText)} (Đối soát AUDIT cũ, không phải SLA thời gian thực)</small>
      </div>

      <div class="benchmark-stat-card">
        <div class="benchmark-stat-label">Selection Recall (Lịch sử)</div>
        <div class="benchmark-stat-val text-success">${escapeHtml(selRecall)}</div>
        <small class="text-muted text-xs">Tỷ lệ chọn đúng trong bộ dữ liệu đối soát</small>
      </div>

      <div class="benchmark-stat-card">
        <div class="benchmark-stat-label">Loaded Recall (Lịch sử)</div>
        <div class="benchmark-stat-val text-success">${escapeHtml(loadRecall)}</div>
        <small class="text-muted text-xs">Tỷ lệ nạp đúng trong bộ dữ liệu đối soát</small>
      </div>

      <div class="benchmark-stat-card">
        <div class="benchmark-stat-label">Candidate Recall</div>
        <div class="benchmark-stat-val text-muted">${escapeHtml(candRecall)}</div>
        <small class="text-muted text-xs">Chưa thiết lập cam kết đo lường</small>
      </div>

      <div class="benchmark-stat-card">
        <div class="benchmark-stat-label">Độ trễ p95 (Lịch sử)</div>
        <div class="benchmark-stat-val font-mono">${escapeHtml(p95Text)}</div>
        <small class="text-muted text-xs">Chưa thiết lập SLA thời gian thực</small>
      </div>
    `;
  } else {
    bmBadge.textContent = 'Runner benchmark offline';
    bmBadge.className = 'badge badge-subtle';
    bmDisplay.hidden = true;
  }
}

function formatReviewTime(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf()) ? new Intl.DateTimeFormat('vi-VN',{dateStyle:'short',timeStyle:'short',timeZone:'Asia/Ho_Chi_Minh'}).format(date) : 'chưa rõ thời điểm';
}
let catalogSearchTimer;
async function loadCatalogQuality() {
  const source = document.getElementById('catalog-quality-source');
  const rows = document.getElementById('catalog-quality-rows');
  const params = new URLSearchParams({limit:'8', offset:String(catalogQualityOffset)});
  const filter = document.getElementById('catalog-quality-filter').value;
  const search = document.getElementById('catalog-quality-search').value.trim();
  if (filter) params.set('disposition', filter);
  if (search) params.set('q', search);
  try {
    const response = await apiFetch(`catalog-quality?${params}`);
    if (!response.data || !response.data.summary) throw new Error('Không có biên nhận hợp lệ');
    const data = response.data, summary = data.summary, counts = summary.dispositions || {};
    source.textContent = `Đối chiếu ${formatReviewTime(data.observed_at)} · catalog ${String(data.index_sha256 || '').slice(0,12)}`;
    document.getElementById('catalog-quality-summary').innerHTML = [
      ['Cách ly', summary.quarantined, 'Danh tính trong catalog'],
      ['Sẵn sàng rà soát', counts.review_required || 0, 'Chưa được kích hoạt'],
      ['Nguồn chưa hợp lệ', counts.source_invalid || 0, 'Cần sửa tại nguồn'],
      ['Thân chưa trích xuất', summary.index_body_empty, 'Không coi là nhóm trùng']
    ].map(([label,value,detail]) => `<article class="quality-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(typeof value === 'number' ? value.toLocaleString('vi-VN') : (value ?? '—'))}</strong><small>${escapeHtml(detail)}</small></article>`).join('');
    catalogQualityTotal = data.total || 0;
    rows.innerHTML = (data.items || []).map(item => {
      const reason = item.disposition === 'review_required' ? 'Sẵn sàng rà soát' : item.disposition === 'source_invalid' ? 'Nguồn chưa hợp lệ' : item.disposition;
      const sourceLabels = {present:'Có nguồn',missing:'Không có nguồn',unavailable:'Không đọc được nguồn'};
      const details = item.missing_reference_count ? `Thiếu ${item.missing_reference_count} tham chiếu` : (sourceLabels[item.source_status] || item.source_status || 'Chưa rõ');
      return `<tr><td><button type="button" class="resource-link catalog-inspect" data-id="${escapeHtml(item.id)}">${escapeHtml(item.id)}</button></td><td><span class="quality-tag">${escapeHtml(reason)}</span></td><td>${escapeHtml(details)}</td></tr>`;
    }).join('') || '<tr><td colspan="3">Không có mục phù hợp.</td></tr>';
    rows.querySelectorAll('.catalog-inspect').forEach(button => button.addEventListener('click', () => openResourceInspector(button.dataset.id.replace(/^skill:/, ''), 'skill')));
    document.getElementById('catalog-quality-range').textContent = `${data.total ? data.offset+1 : 0}–${data.offset+(data.items || []).length} / ${data.total}`;
    document.getElementById('catalog-quality-prev').disabled = data.offset <= 0;
    document.getElementById('catalog-quality-next').disabled = data.offset + (data.items || []).length >= data.total;
  } catch (error) {
    source.textContent = 'Không có biên nhận catalog hợp lệ';
    document.getElementById('catalog-quality-summary').innerHTML = '';
    rows.innerHTML = '<tr><td colspan="3">Không thể tải dữ liệu. Kiểm tra nguồn rồi thử làm mới.</td></tr>';
    document.getElementById('catalog-quality-range').textContent = '';
    document.getElementById('catalog-quality-prev').disabled = true;
    document.getElementById('catalog-quality-next').disabled = true;
  }
}

function renderCuration(data) {
  const badge=document.getElementById('curation-badge'), summary=document.getElementById('curation-summary'), rows=document.getElementById('curation-rows');
  const labels={active_reviewed:'Đã rà soát',active_repaired:'Đã sửa',active_admitted:'Đã tiếp nhận',active_review_pending:'Đang dùng · còn cảnh báo',active_policy_blocked:'Bị chính sách chặn khi nạp',quarantine_security:'Cách ly bảo mật',quarantine_structure:'Cách ly cấu trúc',deleted_duplicate:'Đã loại trùng'};
  if (!data || data.status!=='available') {
    badge.textContent='Chưa có dữ liệu phân loại'; summary.innerHTML=''; rows.innerHTML='<tr><td colspan="3">Catalog này chưa có bảng phân loại.</td></tr>';
    document.getElementById('curation-range').textContent=''; document.getElementById('curation-prev').disabled=true; document.getElementById('curation-next').disabled=true; return;
  }
  badge.textContent='Có dữ liệu';
  summary.innerHTML=Object.entries(labels).map(([key,label])=>`<article class="quality-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(data.counts[key] || 0)}</strong></article>`).join('');
  rows.innerHTML=(data.items || []).map(item=>`<tr><td>${escapeHtml(item.kind)}: ${escapeHtml(item.id)}</td><td>${escapeHtml(labels[item.state] || item.state)}</td><td>${escapeHtml(item.reason)}</td></tr>`).join('') || '<tr><td colspan="3">Không có mục phù hợp.</td></tr>';
  document.getElementById('curation-range').textContent=`${data.total ? data.offset+1 : 0}–${data.offset+data.items.length} / ${data.total}`;
  document.getElementById('curation-prev').disabled=data.offset<=0;
  document.getElementById('curation-next').disabled=data.offset+data.items.length>=data.total;
}

function renderOriginalSources(original) {
  const badge=document.getElementById('original-source-badge'), box=document.getElementById('original-source-summary'), rows=document.getElementById('original-source-rows');
  if (!original || original.status !== 'verified-receipt') {
    badge.textContent='Biên nhận không khả dụng'; box.innerHTML=''; rows.innerHTML='<tr><td colspan="3">Chưa thể đối chiếu nguồn gốc.</td></tr>';
    document.getElementById('original-source-range').textContent='';
    document.getElementById('original-source-prev').disabled=true; document.getElementById('original-source-next').disabled=true; return;
  }
  badge.textContent=`Đã ghim · ${formatReviewTime(original.reviewed_at)}`;
  const sum=original.summary || {}, cov=original.coverage || {};
  box.innerHTML=[['Cần rà soát tiếp',sum.needs_source_review || 0],['Ví dụ đã đối chiếu',sum.confirmed_example || 0],['Không khớp quy tắc',sum.no_rule_hit || 0],['Không khả dụng / đã đổi',(sum.source_unavailable || 0)+(sum.source_changed || 0)]].map(([label,value])=>`<article class="quality-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join('');
  const labels={needs_source_review:'Cần rà soát tiếp',confirmed_example:'Ví dụ đã đối chiếu',no_rule_hit:'Không khớp quy tắc',source_unavailable:'Không đọc được',source_changed:'Nguồn đã thay đổi'};
  const reasons={environment_reference:'Tham chiếu biến môi trường',explicit_placeholder:'Giá trị minh họa',python_value_reference:'Tham chiếu giá trị Python',literal_or_expression_requires_review:'Giá trị hoặc biểu thức cần rà soát',original_redacted_marker_requires_review:'Dấu ẩn trong nguồn cần kiểm tra',provider_format_literal:'Chuỗi theo định dạng nhà cung cấp',quoted_literal_requires_review:'Chuỗi đặt trong dấu nháy cần kiểm tra',dynamic_expression_unverified:'Biểu thức động chưa xác minh',unquoted_literal_or_type_unverified:'Giá trị chưa xác minh',educational_attack_example:'Ví dụ hướng dẫn bảo mật',unverified_execution_or_override:'Lệnh thực thi hoặc ghi đè chưa xác minh'};
  rows.innerHTML=(original.items || []).map(item=>`<tr><td><button class="resource-link original-inspect" data-id="${escapeHtml(item.id)}" data-kind="${escapeHtml(item.kind)}">${escapeHtml(item.id)}</button></td><td>${escapeHtml(labels[item.disposition] || item.disposition)}${item.manual_decision ? `<br><small>Quyết định thủ công (không kích hoạt): ${escapeHtml(item.manual_decision.status || item.manual_decision.outcome)}</small>` : ''}</td><td>${escapeHtml((item.reasons || []).map(reason => reasons[reason] || reason).join(', ') || '—')}<br><small class="font-mono" title="${escapeHtml(item.source_content_sha256 || '')}">Nguồn: ${escapeHtml(truncateStr(item.source_content_sha256 || 'chưa có mã', 14))}</small></td></tr>`).join('') || '<tr><td colspan="3">Không có mục phù hợp.</td></tr>';
  rows.querySelectorAll('.original-inspect').forEach(button=>button.addEventListener('click',()=>openResourceInspector(button.dataset.id,button.dataset.kind)));
  document.getElementById('original-source-range').textContent=`${cov.filtered_total ? cov.offset+1 : 0}–${(cov.offset || 0)+(cov.returned || 0)} / ${cov.filtered_total || 0}`;
  document.getElementById('original-source-prev').disabled=(cov.offset || 0)<=0;
  document.getElementById('original-source-next').disabled=(cov.offset || 0)+(cov.returned || 0)>=(cov.filtered_total || 0);
}

function setupQualityWorkspace() {
  document.getElementById('curation-filter').addEventListener('change',()=>{curationOffset=0;loadQualityView();});
  document.getElementById('curation-search').addEventListener('input',()=>{clearTimeout(curationSearchTimer);curationSearchTimer=setTimeout(()=>{curationOffset=0;loadQualityView();},300);});
  document.getElementById('curation-prev').addEventListener('click',()=>{curationOffset=Math.max(0,curationOffset-20);loadQualityView();});
  document.getElementById('curation-next').addEventListener('click',()=>{curationOffset+=20;loadQualityView();});
  document.getElementById('catalog-quality-filter').addEventListener('change',()=>{catalogQualityOffset=0;loadCatalogQuality();});
  document.getElementById('catalog-quality-search').addEventListener('input',()=>{clearTimeout(catalogSearchTimer);catalogSearchTimer=setTimeout(()=>{catalogQualityOffset=0;loadCatalogQuality();},300);});
  document.getElementById('catalog-quality-prev').addEventListener('click',()=>{catalogQualityOffset=Math.max(0,catalogQualityOffset-8);loadCatalogQuality();});
  document.getElementById('catalog-quality-next').addEventListener('click',()=>{if(catalogQualityOffset+8<catalogQualityTotal){catalogQualityOffset+=8;loadCatalogQuality();}});
  document.getElementById('original-source-filter').addEventListener('change',()=>{originalSourceOffset=0;loadQualityView();});
  document.getElementById('original-source-prev').addEventListener('click',()=>{originalSourceOffset=Math.max(0,originalSourceOffset-20);loadQualityView();});
  document.getElementById('original-source-next').addEventListener('click',()=>{originalSourceOffset+=20;loadQualityView();});
  document.getElementById('retrieval-check-btn').addEventListener('click',async event=>{
    const button=event.currentTarget, status=document.getElementById('retrieval-check-status'), out=document.getElementById('retrieval-check-results');
    button.disabled=true; status.textContent='Đang kiểm tra…'; out.textContent='';
    try {
      const response=await apiFetch('retrieval-check',{method:'POST',body:{}}), data=response.data;
      if(!data || !Array.isArray(data.cases)) throw new Error('Không nhận được kết quả');
      status.textContent=data.status==='ok'?'Đã đối chiếu 3 câu':'Có câu chưa khớp hoặc không khả dụng';
      out.innerHTML=`<p class="quality-caption">${escapeHtml(formatReviewTime(data.observed_at))} · Phạm vi: top 5 tìm kiếm ngữ nghĩa cục bộ</p>`+data.cases.map(item=>`<article class="retrieval-result"><strong>${escapeHtml(item.query)}</strong><span>${item.status==='matched'?'Đã khớp':item.status==='miss'?'Chưa khớp':'Không khả dụng'} · ${escapeHtml(item.elapsed_ms ?? '—')} ms</span><small>Kỳ vọng: ${escapeHtml((item.expected_skill_families || []).join(', '))}</small><small>Kết quả: ${escapeHtml((item.hits || []).join(', ') || 'Không có')}</small></article>`).join('');
    } catch(error) { status.textContent='Không thể kiểm tra'; out.textContent=error.message || 'Thử lại sau.'; }
    finally { button.disabled=false; }
  });
}

function setupMobileNavigation() {
  const button=document.getElementById('mobile-menu-toggle'), nav=document.getElementById('primary-navigation');
  button.addEventListener('click',()=>{const open=nav.classList.toggle('is-open');button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'Đóng menu điều hướng':'Mở menu điều hướng');});
  nav.addEventListener('click', event=>{if(event.detail !== 0 && event.target.closest('.nav-item') && window.matchMedia('(max-width: 860px)').matches){nav.classList.remove('is-open');button.setAttribute('aria-expanded','false');button.setAttribute('aria-label','Mở menu điều hướng');}});
  nav.addEventListener('keydown',event=>{if(!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End'].includes(event.key))return; const tabs=[...nav.querySelectorAll('.nav-item')], i=tabs.indexOf(document.activeElement); if(i<0)return; event.preventDefault(); const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+tabs.length)%tabs.length;tabs[next].focus();tabs[next].click();});
}

// ----------------------------------------------------------------------------
// 10: System Configuration View
// ----------------------------------------------------------------------------

async function loadConfigView() {
  if (window.skillHubIntake) window.skillHubIntake.refresh();
  const resp = await apiFetch('config');
  const data = resp.data || {};

  state.capabilities = data.capabilities || [];
  if (data.csrf_token) {
    state.csrfToken = data.csrf_token;
  }
  state.owned = data.owned || null;

  // Render Owned Dashboard Settings Controls
  if (state.owned) {
    const cfg = state.owned.settings || {};
    const stState = state.owned.settings_state || {};
    const snapState = state.owned['source-snapshot_state'] || {};

    const resInput = document.getElementById('cfg-resource-limit');
    if (resInput && typeof cfg.resource_limit === 'number') resInput.value = cfg.resource_limit;

    const graphInput = document.getElementById('cfg-graph-limit');
    if (graphInput && typeof cfg.graph_limit === 'number') graphInput.value = cfg.graph_limit;

    const routingSelect = document.getElementById('cfg-routing-source');
    if (routingSelect && cfg.routing_source) routingSelect.value = cfg.routing_source;

    const stBadge = document.getElementById('owned-settings-status-badge');
    if (stBadge) {
      stBadge.textContent = stState.present ? 'Trạng thái: Đã lưu trữ' : 'Trạng thái: Mặc định runtime';
      stBadge.className = stState.present ? 'badge badge-alive' : 'badge badge-subtle';
    }

    const hashBadge = document.getElementById('owned-settings-hash-badge');
    if (hashBadge) {
      hashBadge.textContent = `Hash: ${stState.sha256 ? stState.sha256.slice(0, 10) + '…' : '—'}`;
    }

    const backupSelect = document.getElementById('cfg-settings-backup-select');
    if (backupSelect) {
      backupSelect.innerHTML = '<option value="">-- Chọn bản sao lưu để khôi phục --</option>';
      const backups = stState.backups || [];
      backups.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b;
        opt.textContent = `${b.slice(0, 12)}… (${b.slice(0, 6)})`;
        backupSelect.appendChild(opt);
      });
      const rollbackBtn = document.getElementById('btn-rollback-owned-settings');
      if (rollbackBtn) rollbackBtn.disabled = backups.length === 0;
    }

    const snapBadge = document.getElementById('snapshot-status-badge');
    if (snapBadge) {
      snapBadge.textContent = snapState.present ? 'Trạng thái: Đã xuất' : 'Trạng thái: Chưa xuất';
      snapBadge.className = snapState.present ? 'badge badge-alive' : 'badge badge-subtle';
    }

    const snapHashBadge = document.getElementById('snapshot-hash-badge');
    if (snapHashBadge) {
      snapHashBadge.textContent = `Hash: ${snapState.sha256 ? snapState.sha256.slice(0, 10) + '…' : '—'}`;
    }
  }

  // Live Runtime Configuration Badges
  const liveCfg = data.live_configuration || {};
  const liveStatusBadge = document.getElementById('live-runtime-status-badge');
  const liveGpuBadge = document.getElementById('live-runtime-gpu-badge');
  const liveVecBadge = document.getElementById('live-runtime-vectors-badge');
  const liveTime = document.getElementById('live-runtime-observed-time');

  if (liveStatusBadge) {
    if (liveCfg.status === 'observed') {
      liveStatusBadge.textContent = `Server: Hoạt động (HTTP ${liveCfg.http_status || 200})`;
      liveStatusBadge.className = 'badge badge-alive';
    } else if (liveCfg.status === 'degraded') {
      liveStatusBadge.textContent = `Server: Suy giảm (HTTP ${liveCfg.http_status || 503})`;
      liveStatusBadge.className = 'badge badge-warning';
    } else {
      liveStatusBadge.textContent = 'Server: Không khả dụng (Offline)';
      liveStatusBadge.className = 'badge badge-blocked';
    }
  }

  if (liveGpuBadge) {
    const warmup = liveCfg.warmup || {};
    if (warmup.gpu_resident) {
      liveGpuBadge.textContent = 'dGPU: Thường trú VRAM (BGE-M3)';
      liveGpuBadge.className = 'badge badge-alive';
    } else if (warmup.ok) {
      liveGpuBadge.textContent = 'dGPU: Không thường trú (Phối hợp CPU/RAM)';
      liveGpuBadge.className = 'badge badge-alive';
    } else if (warmup.ok === false) {
      liveGpuBadge.textContent = 'dGPU: Chưa sẵn sàng / Lỗi';
      liveGpuBadge.className = 'badge badge-blocked';
    } else {
      liveGpuBadge.textContent = 'dGPU: Không xác định';
      liveGpuBadge.className = 'badge badge-subtle';
    }
  }

  if (liveVecBadge) {
    const warmup = liveCfg.warmup || {};
    const vecs = warmup.semantic_vectors !== undefined ? warmup.semantic_vectors : '—';
    liveVecBadge.textContent = `Vectors: ${vecs}`;
    liveVecBadge.className = vecs > 0 ? 'badge badge-alive font-mono text-xs' : 'badge badge-subtle font-mono text-xs';
  }

  if (liveTime && liveCfg.observed_at) {
    liveTime.textContent = `Quan sát lúc: ${formatIsoTime(liveCfg.observed_at)}`;
  }

  // Capabilities Table
  const capTbody = document.getElementById('capabilities-table-body');
  capTbody.innerHTML = '';
  state.capabilities.forEach(cap => {
    const tr = document.createElement('tr');
    const supBadge = cap.supported ?
      '<span class="badge badge-alive">Hỗ trợ</span>' :
      '<span class="badge badge-blocked">Không hỗ trợ</span>';
    const riskBadge = cap.risk === 'isolated-write' ?
      '<span class="badge badge-warning">isolated-write</span>' :
      `<span class="badge badge-subtle">${escapeHtml(cap.risk || 'none')}</span>`;

    tr.innerHTML = `
      <td><code>${escapeHtml(cap.operation)}</code></td>
      <td>${supBadge}</td>
      <td>${riskBadge}</td>
      <td class="text-sm">${escapeHtml(cap.reason || '—')}</td>
    `;
    capTbody.appendChild(tr);
  });

  // Sync Table
  const syncTbody = document.getElementById('sync-table-body');
  syncTbody.innerHTML = '';
  const syncList = data.sync || [];
  if (syncList.length === 0) {
    syncTbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Không có biên lai đồng bộ.</td></tr>';
  } else {
    syncList.forEach(s => {
      const tr = document.createElement('tr');
      const okBadge = s.ok ? '<span class="badge badge-alive">OK</span>' : '<span class="badge badge-blocked">Lỗi</span>';
      tr.innerHTML = `
        <td class="font-mono text-xs">${escapeHtml(s.schema || 'sync')}</td>
        <td class="font-mono text-xs">${formatIsoTime(s.generated_at)}</td>
        <td>${okBadge}</td>
        <td>${s.dry_run ? 'Có' : 'Không'}</td>
        <td>${s.runtime_verified ? 'Đã kiểm tra' : 'Chưa'}</td>
      `;
      syncTbody.appendChild(tr);
    });
  }

  // Quarantine Table
  const qTbody = document.getElementById('quarantine-table-body');
  qTbody.innerHTML = '';
  const qList = data.quarantine || [];
  document.getElementById('quarantine-count-badge').textContent = `${qList.length} bản ghi`;
  const pillQ = document.getElementById('pill-count-quarantine');
  if (pillQ) pillQ.textContent = String(qList.length);

  if (qList.length === 0) {
    qTbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">Khu vực cách ly hiện đang rỗng.</td></tr>';
  } else {
    qList.forEach(qItem => {
      const tr = document.createElement('tr');
      const backupsCount = (qItem.backups || []).length;
      let rollbackBtn = '—';
      if (backupsCount > 0) {
        const latestBackup = qItem.backups[0];
        rollbackBtn = `<button class="btn btn-subtle btn-rollback" data-kind="${escapeHtml(qItem.kind)}" data-name="${escapeHtml(qItem.name)}" data-sha="${escapeHtml(qItem.sha256)}" data-version="${escapeHtml(latestBackup)}">Rollback (${backupsCount})</button>`;
      }

      tr.innerHTML = `
        <td><span class="badge badge-subtle">${escapeHtml(qItem.kind)}</span></td>
        <td><strong>${escapeHtml(qItem.name)}</strong></td>
        <td class="font-mono text-xs" title="${escapeHtml(qItem.version)}">${escapeHtml(truncateStr(qItem.version, 10))}</td>
        <td><span class="badge badge-subtle">${backupsCount} bản</span></td>
        <td>
          <div class="pagination-controls">
            <button class="btn btn-subtle btn-edit-quarantine" data-kind="${escapeHtml(qItem.kind)}" data-name="${escapeHtml(qItem.name)}">Sửa</button>
            ${rollbackBtn}
          </div>
        </td>
      `;
      qTbody.appendChild(tr);
    });

    qTbody.querySelectorAll('.btn-edit-quarantine').forEach(b => {
      b.addEventListener('click', () => {
        openQuarantineModalWithUpdate({
          kind: b.getAttribute('data-kind'),
          name: b.getAttribute('data-name')
        });
      });
    });

    qTbody.querySelectorAll('.btn-rollback').forEach(b => {
      b.addEventListener('click', () => {
        openQuarantineModalWithRollback(
          {
            kind: b.getAttribute('data-kind'),
            name: b.getAttribute('data-name'),
            sha256: b.getAttribute('data-sha')
          },
          b.getAttribute('data-version')
        );
      });
    });
  }
}

// ============================================================================
// Resource Inspector Drawer Logic
// ============================================================================

function openInspectorDrawer() {
  state.activeDrawerOpener = document.activeElement;
  const drawer = document.getElementById('inspector-drawer');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('inspector-close-btn')?.focus();
}

function closeInspectorDrawer() {
  const drawer = document.getElementById('inspector-drawer');
  if (state.activeDrawerOpener && typeof state.activeDrawerOpener.focus === 'function') {
    state.activeDrawerOpener.focus();
  }
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  document.querySelectorAll('.graph-node.selected, .graph-edge.selected').forEach(el => el.classList.remove('selected'));
}

async function openResourceInspector(resourceId, kind) {
  if (typeof resourceId !== 'string' || !resourceId) return;
  if (resourceId.startsWith(kind + ':')) resourceId = resourceId.slice(kind.length + 1);
  openInspectorDrawer();
  const nameEl = document.getElementById('inspector-name');
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = `CHI TIẾT ${kind.toUpperCase()}`;
  nameEl.textContent = resourceId;
  bodyEl.innerHTML = '<p class="text-muted">Đang tải thông tin chi tiết từ backend...</p>';

  try {
    const resp = await apiFetch(`resources/${encodeURIComponent(resourceId)}?kind=${encodeURIComponent(kind)}`);
    const data = resp.data || {};
    const item = data.item;

    if (!item) {
      bodyEl.innerHTML = '<p class="text-danger">Không tìm thấy tài nguyên trong catalog.</p>';
      return;
    }

    const isDoc = kind === 'document' || item.kind === 'document';
    const isEditable = item.editable !== false && !isDoc;
    const hasRevision = Boolean(item.revision);

    let actionBtnHtml = '';
    if (isEditable) {
      const actionBtnText = hasRevision ? 'Tạo bản sửa đổi trong Quarantine' : 'Cập nhật trong Quarantine';
      actionBtnHtml = `
        <div class="inspector-prop-row mt-sm">
          <button class="btn btn-primary" id="inspector-update-btn">${actionBtnText}</button>
        </div>
      `;
    } else {
      actionBtnHtml = `
        <div class="inspector-prop-row mt-sm">
          <span class="badge badge-subtle">Tài liệu chỉ đọc (editable: false)</span>
        </div>
      `;
    }

    bodyEl.innerHTML = `
      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Tên chuẩn (Canonical Name)</span>
        <div class="inspector-prop-val"><code>${escapeHtml(item.canonical_name || item.name)}</code></div>
      </div>

      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Chuyên mục (Domain)</span>
        <div class="inspector-prop-val"><span class="badge badge-subtle">${escapeHtml(item.domain || 'general')}</span></div>
      </div>

      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Phiên bản (Body SHA256)</span>
        <div class="inspector-prop-val font-mono text-xs">${escapeHtml(item.version || '—')}</div>
      </div>

      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Phụ thuộc (Dependencies)</span>
        <div class="inspector-prop-val text-muted text-xs">Chưa có liên kết phụ thuộc ghi nhận trong catalog.</div>
      </div>

      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Mô tả</span>
        <div class="inspector-prop-val text-sm">${escapeHtml(item.description || 'Không có mô tả')}</div>
      </div>

      <div class="inspector-prop-row">
        <span class="inspector-prop-label">Nội dung Thân tài nguyên (Body)</span>
        <div class="inspector-body-preview">${renderSafeMarkdownText(item.body)}</div>
      </div>

      ${actionBtnHtml}
    `;

    if (isEditable) {
      document.getElementById('inspector-update-btn')?.addEventListener('click', () => {
        closeInspectorDrawer();
        if (hasRevision) {
          openQuarantineModalWithRevision(item);
        } else {
          openQuarantineModalWithPrefill('resource.update', item.kind || kind, item.name, item.version);
        }
      });
    }
  } catch (err) {
    bodyEl.innerHTML = `<p class="text-danger">Lỗi đọc chi tiết: ${escapeHtml(err.message)}</p>`;
  }
}

function openEdgeInspector(edge) {
  openInspectorDrawer();
  const eyebrowEl = document.getElementById('inspector-eyebrow');
  const nameEl = document.getElementById('inspector-name');
  const bodyEl = document.getElementById('inspector-body');

  eyebrowEl.textContent = 'KIỂM TRA LIÊN KẾT ĐỒ THỊ';
  nameEl.textContent = `${edge.source} → ${edge.target}`;

  const evidenceStr = typeof edge.evidence === 'object' ? JSON.stringify(edge.evidence, null, 2) : String(edge.evidence || '—');

  bodyEl.innerHTML = `
    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Loại quan hệ (Relationship Type)</span>
      <div class="inspector-prop-val"><span class="badge badge-alive">${escapeHtml(edge.type || 'alias_of')}</span></div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Nút nguồn (Source Node)</span>
      <div class="inspector-prop-val">
        <code>${escapeHtml(edge.source)}</code>
        <button class="btn btn-subtle btn-inspect-source mt-xs" data-id="${escapeHtml(edge.source)}">Xem chi tiết nút nguồn</button>
      </div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Nút đích (Target Node)</span>
      <div class="inspector-prop-val">
        <code>${escapeHtml(edge.target)}</code>
        <button class="btn btn-subtle btn-inspect-target mt-xs" data-id="${escapeHtml(edge.target)}">Xem chi tiết nút đích</button>
      </div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Bằng chứng quan hệ (Relationship Evidence)</span>
      <div class="inspector-prop-val">
        <pre class="code-block-sm"><code>${escapeHtml(evidenceStr)}</code></pre>
      </div>
    </div>

    <div class="inspector-prop-row">
      <span class="inspector-prop-label">Chuỗi phụ thuộc (Dependency Trace)</span>
      <div class="inspector-prop-val text-sm">
        Đường liên kết trực tiếp giữa các thành phần đã được kiểm chứng từ dữ liệu catalog và projection.
      </div>
    </div>
  `;

  bodyEl.querySelector('.btn-inspect-source')?.addEventListener('click', () => {
    openGraphNodeInspector(edge.source);
  });

  bodyEl.querySelector('.btn-inspect-target')?.addEventListener('click', () => {
    openGraphNodeInspector(edge.target);
  });
}

function setupInspector() {
  const closeBtn = document.getElementById('inspector-close-btn');
  if (closeBtn) {
    closeBtn.addEventListener('click', closeInspectorDrawer);
  }
}

// ============================================================================
// Quarantine Management Modal (Preview & Confirmed Apply)
// ============================================================================

function openModal() {
  state.activeModalOpener = document.activeElement;
  const modal = document.getElementById('quarantine-modal');
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.getElementById('app-main')?.setAttribute('inert', '');
  document.getElementById('op-select')?.focus();
}

function closeModal() {
  const modal = document.getElementById('quarantine-modal');
  document.getElementById('app-main')?.removeAttribute('inert');
  if (state.activeModalOpener && typeof state.activeModalOpener.focus === 'function') {
    state.activeModalOpener.focus();
  }
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  if (state.modal.timerId) {
    clearInterval(state.modal.timerId);
    state.modal.timerId = null;
  }
}

function openModalWithPlan(plan, targetLabel) {
  resetQuarantineModal();
  state.modal.planId = plan.plan_id;
  state.modal.planExpiry = plan.expires_in || 300;

  document.getElementById('plan-id-display').textContent = plan.plan_id;
  document.getElementById('plan-target-display').textContent = targetLabel || plan.ownership || 'dashboard-owned';
  document.getElementById('plan-backup-display').textContent = plan.backup || 'Không có (hoặc bản ghi mới)';
  document.getElementById('plan-ownership-display').textContent = 'Phạm vi: ' + plan.ownership;

  const changesPre = document.getElementById('plan-changes-preview');
  if (changesPre) {
    changesPre.textContent = typeof plan.changes === 'object' ? JSON.stringify(plan.changes, null, 2) : String(plan.changes || '');
  }

  startPlanExpiryTimer(state.modal.planExpiry);

  document.getElementById('modal-step-1').hidden = true;
  document.getElementById('modal-step-2').hidden = false;
  document.getElementById('modal-footer-step-1').hidden = true;
  document.getElementById('modal-footer-step-2').hidden = false;

  const confirmCheck = document.getElementById('confirm-apply-check');
  const applyBtn = document.getElementById('modal-apply-btn');
  if (confirmCheck) confirmCheck.checked = false;
  if (applyBtn) {
    applyBtn.disabled = true;
    applyBtn.textContent = 'Xác nhận & Áp dụng Kế hoạch';
  }

  openModal();
}

async function handleApplyOwnedSettings() {
  const resLimit = parseInt(document.getElementById('cfg-resource-limit').value, 10);
  const graphLimit = parseInt(document.getElementById('cfg-graph-limit').value, 10);
  const routingSource = document.getElementById('cfg-routing-source').value;
  const msgEl = document.getElementById('owned-settings-msg');

  if (isNaN(resLimit) || resLimit < 1 || resLimit > 100) {
    if (msgEl) msgEl.textContent = 'Lỗi: resource_limit phải nằm trong khoảng 1..100';
    return;
  }
  if (isNaN(graphLimit) || graphLimit < 1 || graphLimit > 200) {
    if (msgEl) msgEl.textContent = 'Lỗi: graph_limit phải nằm trong khoảng 1..200';
    return;
  }
  if (!['all', 'native', 'docker'].includes(routingSource)) {
    if (msgEl) msgEl.textContent = 'Lỗi: routing_source phải là all, native, hoặc docker';
    return;
  }

  const expectedSha = state.owned?.settings_state?.sha256 || 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

  const payload = {
    operation: 'config.apply',
    expected_sha256: expectedSha,
    settings: {
      resource_limit: resLimit,
      graph_limit: graphLimit,
      routing_source: routingSource
    }
  };

  try {
    if (msgEl) msgEl.textContent = 'Đang lập kế hoạch preview...';
    const resp = await apiFetch('operations/preview', {
      method: 'POST',
      body: payload
    });
    if (msgEl) msgEl.textContent = '';
    openModalWithPlan(resp.data, 'config/settings');
  } catch (err) {
    if (msgEl) msgEl.textContent = `Lỗi preview config: ${err.message}`;
  }
}

async function handleRollbackOwnedSettings() {
  const select = document.getElementById('cfg-settings-backup-select');
  const version = select ? select.value : '';
  const msgEl = document.getElementById('owned-settings-msg');

  if (!version) {
    if (msgEl) msgEl.textContent = 'Vui lòng chọn một bản sao lưu để khôi phục.';
    return;
  }

  const expectedSha = state.owned?.settings_state?.sha256 || 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

  const payload = {
    operation: 'config.rollback',
    expected_sha256: expectedSha,
    version: version
  };

  try {
    if (msgEl) msgEl.textContent = 'Đang lập kế hoạch rollback preview...';
    const resp = await apiFetch('operations/preview', {
      method: 'POST',
      body: payload
    });
    if (msgEl) msgEl.textContent = '';
    openModalWithPlan(resp.data, `config/rollback (${version.slice(0, 10)}…)`);
  } catch (err) {
    if (msgEl) msgEl.textContent = `Lỗi rollback preview: ${err.message}`;
  }
}

async function handleCreateSourceSnapshot() {
  const expectedSha = state.owned?.['source-snapshot_state']?.sha256 || 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

  const payload = {
    operation: 'config.snapshot',
    expected_sha256: expectedSha
  };

  try {
    const resp = await apiFetch('operations/preview', {
      method: 'POST',
      body: payload
    });
    openModalWithPlan(resp.data, 'config/source-snapshot');
  } catch (err) {
    showGlobalAlert(`Lỗi snapshot preview: ${err.message}`);
  }
}

async function initiateCatalogRevision(id, kind) {
  try {
    const resp = await apiFetch(`resources/${encodeURIComponent(id)}?kind=${encodeURIComponent(kind)}`);
    const item = resp.data?.item;
    if (!item) {
      showGlobalAlert(`Không tìm thấy tài nguyên ${id} để sửa đổi.`);
      return;
    }
    openQuarantineModalWithRevision(item);
  } catch (err) {
    showGlobalAlert(`Lỗi tải chi tiết tài nguyên: ${err.message}`);
  }
}

function openQuarantineModalWithRevision(item) {
  resetQuarantineModal();
  const rev = item.revision || {};
  document.getElementById('op-select').value = 'resource.revise';
  document.getElementById('op-select').dispatchEvent(new Event('change'));
  document.getElementById('op-kind').value = item.kind || 'skill';
  document.getElementById('op-name').value = rev.name || ('revision-' + item.name);
  document.getElementById('op-expected-sha').value = rev.expected_sha256 || 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
  document.getElementById('op-catalog-source').value = JSON.stringify(rev.source || { kind: item.kind, id: item.id, version: item.version });

  const contentEl = document.getElementById('op-content');
  contentEl.value = item.body || '';
  contentEl.dispatchEvent(new Event('input'));
  openModal();
}

async function openQuarantineModalWithUpdate(qItem) {
  try {
    const resp = await apiFetch(`resources/${encodeURIComponent(qItem.name)}?kind=${encodeURIComponent(qItem.kind)}&source=quarantine`);
    const item = resp.data?.item;
    if (!item) {
      showGlobalAlert('Không tìm thấy tài nguyên trong quarantine để sửa.');
      return;
    }
    resetQuarantineModal();
    document.getElementById('op-select').value = 'resource.update';
    document.getElementById('op-select').dispatchEvent(new Event('change'));
    document.getElementById('op-kind').value = qItem.kind;
    document.getElementById('op-name').value = qItem.name;
    document.getElementById('op-expected-sha').value = item.sha256 || qItem.sha256;
    const contentEl = document.getElementById('op-content');
    contentEl.value = item.content || '';
    contentEl.dispatchEvent(new Event('input'));
    openModal();
  } catch (err) {
    showGlobalAlert(`Lỗi tải dữ liệu quarantine: ${err.message}`);
  }
}

function openQuarantineModalWithRollback(qItem, backupSha) {
  resetQuarantineModal();
  document.getElementById('op-select').value = 'resource.rollback';
  document.getElementById('op-select').dispatchEvent(new Event('change'));
  document.getElementById('op-kind').value = qItem.kind;
  document.getElementById('op-name').value = qItem.name;
  document.getElementById('op-expected-sha').value = qItem.sha256;
  document.getElementById('op-version').value = backupSha;
  openModal();
}

function openQuarantineModalWithPrefill(op, kind, name, expectedSha, version = '') {
  resetQuarantineModal();
  document.getElementById('op-select').value = op;
  document.getElementById('op-select').dispatchEvent(new Event('change'));
  document.getElementById('op-kind').value = kind || 'skill';
  document.getElementById('op-name').value = name || '';
  if (expectedSha) {
    document.getElementById('op-expected-sha').value = expectedSha;
  }
  if (version) {
    document.getElementById('op-version').value = version;
  }
  openModal();
}

function setupQuarantineModal() {
  const openBtn = document.getElementById('open-quarantine-btn');
  const skillsImportBtn = document.getElementById('skills-import-btn');
  const libraryImportBtn = document.getElementById('library-import-btn');
  const closeBtn = document.getElementById('modal-close-btn');
  const cancelBtn = document.getElementById('modal-cancel-btn');
  const backBtn = document.getElementById('modal-back-btn');
  const doneBtn = document.getElementById('modal-done-btn');
  const previewBtn = document.getElementById('modal-preview-btn');
  const applyBtn = document.getElementById('modal-apply-btn');
  const confirmCheck = document.getElementById('confirm-apply-check');
  const opSelect = document.getElementById('op-select');
  const contentArea = document.getElementById('op-content');
  const contentBytesEl = document.getElementById('content-bytes');

  if (openBtn) openBtn.addEventListener('click', () => {
    resetQuarantineModal();
    openModal();
  });

  if (skillsImportBtn) skillsImportBtn.addEventListener('click', () => {
    openQuarantineModalWithPrefill('resource.import', 'skill', '', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });

  if (libraryImportBtn) libraryImportBtn.addEventListener('click', () => {
    openQuarantineModalWithPrefill('resource.import', 'prompt', '', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });

  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  if (doneBtn) doneBtn.addEventListener('click', () => {
    closeModal();
    loadViewData(state.currentView);
  });

  // Content byte counter with CSS class (zero inline styles)
  if (contentArea && contentBytesEl) {
    contentArea.addEventListener('input', () => {
      const bytes = new TextEncoder().encode(contentArea.value).length;
      contentBytesEl.textContent = bytes.toLocaleString('vi-VN');
      if (bytes > 262144) {
        contentBytesEl.classList.add('text-danger');
      } else {
        contentBytesEl.classList.remove('text-danger');
      }
    });
  }

  // Operation change hint & version field toggle
  if (opSelect) {
    opSelect.addEventListener('change', () => {
      const op = opSelect.value;
      const versionGroup = document.getElementById('op-version-group');
      const contentGroup = document.getElementById('op-content-group');
      const expectedShaInput = document.getElementById('op-expected-sha');
      const hintEl = document.getElementById('op-hint');

      if (op === 'resource.import') {
        versionGroup.hidden = true;
        contentGroup.hidden = false;
        expectedShaInput.value = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
        hintEl.textContent = 'Nhập mới tài nguyên chưa từng tồn tại vào quarantine.';
      } else if (op === 'resource.revise') {
        versionGroup.hidden = true;
        contentGroup.hidden = false;
        hintEl.textContent = 'Tạo hoặc cập nhật bản sửa đổi cách ly cho tài nguyên catalog có sẵn.';
      } else if (op === 'resource.update') {
        versionGroup.hidden = true;
        contentGroup.hidden = false;
        hintEl.textContent = 'Cập nhật tài nguyên đang có trong quarantine. Cần expected_sha256 khớp với bản ghi hiện tại.';
      } else if (op === 'resource.rollback') {
        versionGroup.hidden = false;
        contentGroup.hidden = true;
        hintEl.textContent = 'Khôi phục bản sao lưu cũ theo version hash trong thư mục versions.';
      }
    });
  }

  // Step 1: Preview Plan
  if (previewBtn) {
    previewBtn.addEventListener('click', async () => {
      const errBox = document.getElementById('modal-step-1-error');
      errBox.hidden = true;

      const op = opSelect.value;
      const kind = document.getElementById('op-kind').value;
      const name = document.getElementById('op-name').value.trim();
      const expectedSha = document.getElementById('op-expected-sha').value.trim();
      const content = contentArea.value;
      const version = document.getElementById('op-version').value.trim();
      const catalogSourceStr = document.getElementById('op-catalog-source').value;

      if (!name) {
        errBox.textContent = 'Vui lòng nhập tên tài nguyên hợp lệ.';
        errBox.hidden = false;
        return;
      }

      if (op !== 'resource.rollback' && !content) {
        errBox.textContent = 'Nội dung tài nguyên không được để trống.';
        errBox.hidden = false;
        return;
      }

      const payload = {
        operation: op,
        kind: kind,
        name: name,
        expected_sha256: expectedSha
      };

      if (op === 'resource.revise') {
        if (catalogSourceStr) {
          try {
            payload.source = JSON.parse(catalogSourceStr);
          } catch (e) {
            // ignore
          }
        }
        payload.content = content;
      } else if (op === 'resource.rollback') {
        payload.version = version;
      } else {
        payload.content = content;
      }

      previewBtn.disabled = true;
      previewBtn.textContent = 'Đang xử lý preview...';

      try {
        const resp = await apiFetch('/api/v1/operations/preview', {
          method: 'POST',
          body: payload
        });

        const plan = resp.data || {};
        state.modal.planId = plan.plan_id;
        state.modal.planExpiry = plan.expires_in || 300;

        // Populate step 2
        document.getElementById('plan-id-display').textContent = plan.plan_id;
        document.getElementById('plan-target-display').textContent = `${kind}/${name}`;
        document.getElementById('plan-backup-display').textContent = plan.backup || 'Không có (nhập mới)';
        document.getElementById('plan-changes-preview').textContent = JSON.stringify(plan.changes, null, 2);
        document.getElementById('plan-ownership-display').textContent = 'Phạm vi: ' + plan.ownership;

        // Timer countdown
        startPlanExpiryTimer(state.modal.planExpiry);

        // Switch to Step 2
        document.getElementById('modal-step-1').hidden = true;
        document.getElementById('modal-step-2').hidden = false;
        document.getElementById('modal-footer-step-1').hidden = true;
        document.getElementById('modal-footer-step-2').hidden = false;
        confirmCheck.checked = false;
        applyBtn.disabled = true;
      } catch (err) {
        errBox.textContent = `Lỗi preview: ${err.message}`;
        errBox.hidden = false;
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = 'Xem trước Kế hoạch (Preview)';
      }
    });
  }

  // Checkbox confirmation
  if (confirmCheck) {
    confirmCheck.addEventListener('change', () => {
      applyBtn.disabled = !confirmCheck.checked;
    });
  }

  // Step 2: Back button
  if (backBtn) {
    backBtn.addEventListener('click', () => {
      if (state.modal.timerId) clearInterval(state.modal.timerId);
      document.getElementById('modal-step-2').hidden = true;
      document.getElementById('modal-step-1').hidden = false;
      document.getElementById('modal-footer-step-2').hidden = true;
      document.getElementById('modal-footer-step-1').hidden = false;
    });
  }

  // Step 2: Apply Plan
  if (applyBtn) {
    applyBtn.addEventListener('click', async () => {
      const errBox = document.getElementById('modal-step-2-error');
      errBox.hidden = true;
      applyBtn.disabled = true;
      applyBtn.textContent = 'Đang thực thi apply...';

      try {
        const resp = await apiFetch('/api/v1/operations/apply', {
          method: 'POST',
          body: {
            plan_id: state.modal.planId,
            confirmed: true
          }
        });

        const result = resp.data || {};
        document.getElementById('modal-success-msg').textContent = result.status === 'dashboard-owned' ?
          'Đã lưu trạng thái dashboard. Cấu hình host không thay đổi.' :
          'Đã lưu bản ghi trong vùng cách ly; chưa kích hoạt tài nguyên trong Hub.';
        if (state.modal.timerId) clearInterval(state.modal.timerId);

        // Switch to Step 3
        document.getElementById('applied-sha-display').textContent = result.sha256 || '—';
        document.getElementById('applied-backup-display').textContent = result.backup || 'Không có bản sao lưu trước đó';

        document.getElementById('modal-step-2').hidden = true;
        document.getElementById('modal-step-3').hidden = false;
        document.getElementById('modal-footer-step-2').hidden = true;
        document.getElementById('modal-footer-step-3').hidden = false;
      } catch (err) {
        errBox.textContent = `Lỗi thực thi apply: ${err.message}`;
        errBox.hidden = false;
        applyBtn.disabled = false;
        applyBtn.textContent = 'Xác nhận Thực thi (Apply)';
      }
    });
  }
}

function startPlanExpiryTimer(seconds) {
  if (state.modal.timerId) clearInterval(state.modal.timerId);
  let remaining = seconds;
  const display = document.getElementById('plan-expiry-display');

  state.modal.timerId = setInterval(() => {
    remaining--;
    if (remaining <= 0) {
      clearInterval(state.modal.timerId);
      if (display) display.textContent = 'Kế hoạch đã hết hạn!';
      const applyBtn = document.getElementById('modal-apply-btn');
      if (applyBtn) applyBtn.disabled = true;
    } else {
      if (display) display.textContent = `${remaining} giây`;
    }
  }, 1000);
}

function resetQuarantineModal() {
  document.getElementById('modal-step-1').hidden = false;
  document.getElementById('modal-step-2').hidden = true;
  document.getElementById('modal-step-3').hidden = true;
  document.getElementById('modal-footer-step-1').hidden = false;
  document.getElementById('modal-footer-step-2').hidden = true;
  document.getElementById('modal-footer-step-3').hidden = true;

  document.getElementById('modal-step-1-error').hidden = true;
  document.getElementById('modal-step-2-error').hidden = true;
  document.getElementById('op-select').value = 'resource.import';
  document.getElementById('op-select').dispatchEvent(new Event('change'));
  document.getElementById('op-catalog-source').value = '';
}

// ============================================================================
// Event Listeners & Global Setup
// ============================================================================

function setupEventListeners() {
  // Global Alert Close
  const alertClose = document.getElementById('global-alert-close');
  if (alertClose) alertClose.addEventListener('click', hideGlobalAlert);

  // Manual Refresh
  const refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      loadViewData(state.currentView);
    });
  }

  // Auto Refresh
  const autoCheck = document.getElementById('auto-refresh-check');
  if (autoCheck) {
    autoCheck.addEventListener('change', (e) => {
      state.autoRefresh = e.target.checked;
      if (state.autoRefresh) {
        startAutoRefresh();
      } else {
        stopAutoRefresh();
      }
    });
  }

  // Topbar quick-action for route_task
  const btnOpenRouter = document.getElementById('btn-open-router-studio');
  if (btnOpenRouter) {
    btnOpenRouter.addEventListener('click', () => {
      switchView('routing');
      setTimeout(() => {
        const input = document.getElementById('route-task-input');
        if (input) input.focus();
      }, 50);
    });
  }

  // Overview quick launcher for route_task
  const btnOverviewRun = document.getElementById('btn-overview-run-route');
  const overviewInput = document.getElementById('overview-route-input');
  if (btnOverviewRun && overviewInput) {
    btnOverviewRun.addEventListener('click', () => {
      triggerOverviewQuickRoute(overviewInput.value.trim());
    });
    overviewInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        triggerOverviewQuickRoute(overviewInput.value.trim());
      }
    });
  }

  document.querySelectorAll('.preset-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const taskText = chip.getAttribute('data-task');
      if (overviewInput) overviewInput.value = taskText;
      overviewInput?.focus();
    });
  });

  // Skills search, mode & pagination
  const skillsSearchBtn = document.getElementById('skills-search-btn');
  const skillsSearchInput = document.getElementById('skills-search-input');
  const skillSearchModeSelect = document.getElementById('skill-search-mode');
  const skillsDomainSelect = document.getElementById('skills-domain-select');
  const skillsPrev = document.getElementById('skills-prev-page');
  const skillsNext = document.getElementById('skills-next-page');

  if (skillsSearchBtn && skillsSearchInput) {
    const doSkillsSearch = () => {
      state.skills.q = skillsSearchInput.value.trim();
      state.skills.page = 1;
      loadSkillsView();
    };
    skillsSearchBtn.addEventListener('click', doSkillsSearch);
    skillsSearchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doSkillsSearch();
    });
    skillsSearchInput.addEventListener('input', () => {
      if (skillsSearchInput.value.trim() === '' && state.skills.q !== '') {
        doSkillsSearch();
      }
    });
  }

  if (skillSearchModeSelect) {
    skillSearchModeSelect.addEventListener('change', (e) => {
      state.skills.mode = e.target.value;
      state.skills.page = 1;
      loadSkillsView();
    });
  }

  if (skillsDomainSelect) {
    skillsDomainSelect.addEventListener('change', () => {
      state.skills.domain = skillsDomainSelect.value;
      state.skills.page = 1;
      loadSkillsView();
    });
  }

  if (skillsPrev) {
    skillsPrev.addEventListener('click', () => {
      if (state.skills.page > 1) {
        state.skills.page--;
        loadSkillsView();
      }
    });
  }

  if (skillsNext) {
    skillsNext.addEventListener('click', () => {
      state.skills.page++;
      loadSkillsView();
    });
  }

  // Library search, kind & pagination
  const libSearchBtn = document.getElementById('library-search-btn');
  const libSearchInput = document.getElementById('library-search-input');
  const libKindSelect = document.getElementById('library-kind-select');
  const libDomainSelect = document.getElementById('library-domain-select');
  const libPrev = document.getElementById('library-prev-page');
  const libNext = document.getElementById('library-next-page');

  if (libSearchBtn && libSearchInput) {
    const doLibSearch = () => {
      state.library.q = libSearchInput.value.trim();
      state.library.page = 1;
      loadLibraryView();
    };
    libSearchBtn.addEventListener('click', doLibSearch);
    libSearchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doLibSearch();
    });
  }

  if (libKindSelect) {
    libKindSelect.addEventListener('change', () => {
      state.library.kind = libKindSelect.value;
      state.library.page = 1;
      loadLibraryView();
    });
  }

  if (libDomainSelect) {
    libDomainSelect.addEventListener('change', () => {
      state.library.domain = libDomainSelect.value;
      state.library.page = 1;
      loadLibraryView();
    });
  }

  if (libPrev) {
    libPrev.addEventListener('click', () => {
      if (state.library.page > 1) {
        state.library.page--;
        loadLibraryView();
      }
    });
  }

  if (libNext) {
    libNext.addEventListener('click', () => {
      state.library.page++;
      loadLibraryView();
    });
  }

  // Agents search
  const agentsSearchInput = document.getElementById('agents-search-input');
  if (agentsSearchInput) {
    agentsSearchInput.addEventListener('input', () => {
      loadAgentsView();
    });
  }

  // MCP Probe button
  const mcpProbeBtn = document.getElementById('mcp-probe-btn');
  if (mcpProbeBtn) {
    mcpProbeBtn.addEventListener('click', () => {
      loadIntegrationsView('skill-hub');
    });
  }

  // Routing source filter
  const routingSourceSelect = document.getElementById('routing-source-select');
  if (routingSourceSelect) {
    routingSourceSelect.addEventListener('change', (e) => {
      state.routing.source = e.target.value;
      renderRoutingTable();
    });
  }

  // Interactive route_task Studio controls & events
  document.querySelectorAll('.route-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const taskText = btn.getAttribute('data-task');
      const input = document.getElementById('route-task-input');
      if (input) {
        input.value = taskText;
        invalidateRouterStudio();
        input.focus();
      }
    });
  });

  const routeTaskInput = document.getElementById('route-task-input');
  if (routeTaskInput) {
    routeTaskInput.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        runRouteTaskStudio();
      }
    });
  }

  const btnRunRoute = document.getElementById('btn-run-route-task');
  if (btnRunRoute) {
    btnRunRoute.addEventListener('click', runRouteTaskStudio);
  }

  const btnClearRoute = document.getElementById('btn-clear-route-task');
  if (btnClearRoute) {
    btnClearRoute.addEventListener('click', () => {
      invalidateRouterStudio();
      const input = document.getElementById('route-task-input');
      if (input) input.value = '';
      for (const key of routeContextFields) {
        const field = document.getElementById('route-context-' + key);
        if (field) field.value = '';
      }
      const context = document.getElementById('route-normalized-context');
      if (context) { context.hidden = true; context.textContent = ''; }
      const taskIdInput = document.getElementById('route-task-id-input');
      if (taskIdInput) taskIdInput.value = '';
      const errBox = document.getElementById('route-studio-error');
      if (errBox) { errBox.hidden = true; errBox.textContent = ''; }
      const results = document.getElementById('route-studio-results');
      if (results) results.hidden = true;
    });
  }

  for (const id of ['route-task-input','route-task-id-input',...routeContextFields.map(key => 'route-context-' + key)]) {
    document.getElementById(id)?.addEventListener('input', invalidateRouterStudio);
  }
  document.getElementById('route-include-graph')?.addEventListener('change', invalidateRouterStudio);
  document.getElementById('route-studio-mode')?.addEventListener('change', () => {
    invalidateRouterStudio();
    updateRouterControls();
  });
  localizeRouterStudio();
  window.addEventListener('skillhub:languagechange', localizeRouterStudio);

  const btnSyncHist = document.getElementById('btn-sync-routing-history');
  if (btnSyncHist) {
    btnSyncHist.addEventListener('click', () => {
      loadRoutingView();
    });
  }

  const scoresFilterInput = document.getElementById('route-scores-filter');
  if (scoresFilterInput) {
    scoresFilterInput.addEventListener('input', (e) => {
      state.routeStudio.scoresFilter = e.target.value.toLowerCase();
      renderRouteScoresTable();
    });
  }

  const scoresKindSelect = document.getElementById('route-scores-kind-filter');
  if (scoresKindSelect) {
    scoresKindSelect.addEventListener('change', (e) => {
      state.routeStudio.scoresKindFilter = e.target.value;
      renderRouteScoresTable();
    });
  }

  // Telemetry filters
  const telemSourceFilter = document.getElementById('telemetry-source-filter');
  const telemKindFilter = document.getElementById('telemetry-kind-filter');
  if (telemSourceFilter) {
    telemSourceFilter.addEventListener('change', (e) => {
      state.telemetry.source = e.target.value;
      renderTelemetryTable();
    });
  }
  if (telemKindFilter) {
    telemKindFilter.addEventListener('change', (e) => {
      state.telemetry.kind = e.target.value;
      renderTelemetryTable();
    });
  }

  // Owned Dashboard Settings
  document.getElementById('btn-save-owned-settings')?.addEventListener('click', handleApplyOwnedSettings);
  document.getElementById('btn-rollback-owned-settings')?.addEventListener('click', handleRollbackOwnedSettings);
  document.getElementById('btn-create-source-snapshot')?.addEventListener('click', handleCreateSourceSnapshot);

  // Modal focus trap & accessibility escape
  const modal = document.getElementById('quarantine-modal');
  if (modal) {
    modal.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        const focusables = modal.querySelectorAll('button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled])');
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });
  }

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const modalEl = document.getElementById('quarantine-modal');
      const drawerEl = document.getElementById('inspector-drawer');
      if (modalEl && modalEl.classList.contains('open')) {
        closeModal();
      } else if (drawerEl && drawerEl.classList.contains('open')) {
        closeInspectorDrawer();
      }
    }
  });
}

function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(() => {
    if (state.autoRefresh && !state.isRefreshing) {
      loadViewData(state.currentView);
    }
  }, 15000);
}

function stopAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

// ============================================================================
// Application Bootstrap
// ============================================================================

async function init() {
  setupNavigation();
  setupEventListeners();
  setupGraphControls();
  setupInspector();
  setupQuarantineModal();
  setupQualityWorkspace();
  setupMobileNavigation();
  document.getElementById('open-batch-intake').addEventListener('click', () => {
    switchView('config');
    document.getElementById('intake-panel').scrollIntoView({behavior:'smooth',block:'start'});
  });
  if (window.SkillHubIntake) window.skillHubIntake = window.SkillHubIntake.mount(document.getElementById('intake-panel'), {apiFetch});

  // Initial config load to get CSRF token and capabilities
  try {
    const configResp = await apiFetch('config');
    if (configResp.data) {
      if (configResp.data.csrf_token) {
        state.csrfToken = configResp.data.csrf_token;
        const badge = document.getElementById('csrf-badge');
        if (badge) {
          badge.textContent = 'CSRF: Sẵn sàng';
          badge.className = 'badge badge-alive';
        }
      }
      state.capabilities = configResp.data.capabilities || [];
      if (configResp.data.owned) {
        state.owned = configResp.data.owned;
        if (state.owned.settings) {
          if (typeof state.owned.settings.resource_limit === 'number') {
            state.skills.limit = state.owned.settings.resource_limit;
            state.library.limit = state.owned.settings.resource_limit;
          }
          if (state.owned.settings.routing_source) {
            state.routing.source = state.owned.settings.routing_source;
            state.telemetry.source = state.owned.settings.routing_source;
          }
        }
      }
    }
  } catch (err) {
    console.warn('Initial config fetch failed:', err);
  }

  // Do not replace a view selected while the initial config request was in flight.
  if (state.currentView === null) switchView('overview');

  // Start auto-refresh
  startAutoRefresh();
}

window.addEventListener('DOMContentLoaded', init);
