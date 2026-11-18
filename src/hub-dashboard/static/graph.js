/* Evidence-backed capability explorer. Catalog names and evidence are rendered as text. */
'use strict';

const graphExplorer = (() => {
  const copy = {
    en: {
      lead: 'Explore the relationships recorded for catalog resources and integrations.',
      search: 'Search by name', searchHint: 'Find a skill or other resource', type: 'Resource type',
      all: 'All types', skill: 'Skill', prompt: 'Prompt', agent: 'Agent', mcp: 'MCP server', project: 'Project',
      capability: 'Capability', concept: 'Concept', tool: 'Tool', document: 'Document',
      matches: 'Matching nodes', context: 'Related', contextGroup: 'Related nodes',
      visibleMatches: (first, last, total) => 'Matches ' + first + '–' + last + ' of ' + total,
      relatedCount: count => 'Related nodes ' + count,
      linkCount: count => 'Visible relationships ' + count,
      matchLinks: count => 'Recorded relationships touching matches ' + count,
      focusLinks: count => 'Recorded relationships touching this neighborhood ' + count,
      isolatedCount: count => 'Matches with no recorded relationships ' + count,
      isolated: 'No recorded relationships', unknownDegree: 'The total number of relationships is unavailable.',
      outside: 'Some recorded relationships are outside this view.',
      unknownCount: count => 'Matches with unknown relationship coverage ' + count,
      neighborhood: count => 'Nodes visible ' + count,
      pagination: 'Graph pages', pageNumber: 'Page', pagePrev: 'Previous', pageNext: 'Next', pageGo: 'Go',
      pageText: pages => 'of ' + pages,
      provides: 'Provides capability', specializes: 'Specializes capability', uses_tool: 'Uses tool',
      requires_tool: 'Requires tool', toolLink: 'Tool use or requirement', depends_on: 'Depends on',
      nodeLinks: (shown, total) => shown + ' of ' + total + ' recorded relationships shown',
      depth: 'Depth', one: '1 link away', two: '2 links away', three: '3 links away', zero: 'Selected node only',
      focus: 'Focus on node', clear: 'Exit focus', map: 'Map', list: 'List', modes: 'Graph display',
      resultList: 'Graph results', mapRegion: 'Relationship map. Scroll to explore and select a node for details.',
      zoomOut: 'Zoom out', zoomIn: 'Zoom in', fit: 'Fit', legend: 'Relationship legend',
      alias_of: 'Alias → canonical resource', installed_skill: 'Agent → installed skill', other: 'Other recorded relationship',
      loading: 'Loading capability graph…', error: 'The capability graph could not be loaded.',
      empty: 'No resources match this view.', retry: 'Retry',
      focused: 'Focused on', details: 'Node details', select: 'Select a node to see its recorded relationships.',
      relations: 'Relationships in view', noLinks: 'No relationships are visible for this node in the current result.',
      open: 'Open resource details', back: 'Back to selected node', evidenceSource: 'Source', evidenceClass: 'Class',
      evidenceDate: 'Recorded at', noEvidence: 'The projection did not report a source.',
      fitHint: 'Use List to read full labels and browse with the keyboard.',
    },
    vi: {
      lead: 'Khám phá các quan hệ đã được ghi nhận giữa tài nguyên và tích hợp.',
      search: 'Tìm theo tên', searchHint: 'Tìm kỹ năng hoặc tài nguyên khác', type: 'Loại tài nguyên',
      all: 'Tất cả loại', skill: 'Kỹ năng', prompt: 'Lời nhắc', agent: 'Agent', mcp: 'Máy chủ MCP', project: 'Dự án',
      capability: 'Năng lực', concept: 'Khái niệm', tool: 'Công cụ', document: 'Tài liệu',
      matches: 'Nút khớp', context: 'Liên quan', contextGroup: 'Nút liên quan',
      visibleMatches: (first, last, total) => 'Mục khớp ' + first + '–' + last + ' trên ' + total,
      relatedCount: count => 'Nút liên quan ' + count,
      linkCount: count => 'Quan hệ đang hiển thị ' + count,
      matchLinks: count => 'Quan hệ đã ghi nhận có mục khớp ' + count,
      focusLinks: count => 'Quan hệ đã ghi nhận có nút trong vùng tập trung ' + count,
      isolatedCount: count => 'Mục khớp chưa ghi nhận quan hệ ' + count,
      isolated: 'Chưa ghi nhận quan hệ', unknownDegree: 'Chưa có tổng số quan hệ.',
      outside: 'Một số quan hệ đã ghi nhận nằm ngoài khung đang xem.',
      unknownCount: count => 'Mục khớp chưa rõ độ phủ quan hệ ' + count,
      neighborhood: count => 'Nút đang hiển thị ' + count,
      pagination: 'Phân trang sơ đồ', pageNumber: 'Trang', pagePrev: 'Trước', pageNext: 'Sau', pageGo: 'Đi',
      pageText: pages => 'trên ' + pages,
      provides: 'Cung cấp năng lực', specializes: 'Chuyên biệt hóa năng lực', uses_tool: 'Dùng công cụ',
      requires_tool: 'Yêu cầu công cụ', toolLink: 'Dùng hoặc yêu cầu công cụ', depends_on: 'Phụ thuộc vào',
      nodeLinks: (shown, total) => 'Hiển thị ' + shown + ' trên ' + total + ' quan hệ đã ghi nhận',
      depth: 'Độ sâu', one: 'Cách 1 liên kết', two: 'Cách 2 liên kết', three: 'Cách 3 liên kết', zero: 'Chỉ nút đã chọn',
      focus: 'Tập trung vào nút', clear: 'Thoát chế độ tập trung', map: 'Sơ đồ', list: 'Danh sách', modes: 'Chế độ hiển thị sơ đồ',
      resultList: 'Kết quả sơ đồ', mapRegion: 'Sơ đồ quan hệ. Cuộn để khám phá và chọn nút để xem chi tiết.',
      zoomOut: 'Thu nhỏ', zoomIn: 'Phóng to', fit: 'Vừa khung', legend: 'Chú giải quan hệ',
      alias_of: 'Bí danh → tài nguyên chuẩn', installed_skill: 'Agent → kỹ năng đã cài', other: 'Quan hệ khác đã ghi nhận',
      loading: 'Đang tải sơ đồ năng lực…', error: 'Không tải được sơ đồ năng lực.',
      empty: 'Không có tài nguyên khớp với chế độ xem này.', retry: 'Thử lại',
      focused: 'Đang tập trung vào', details: 'Chi tiết nút', select: 'Chọn nút để xem các quan hệ đã ghi nhận.',
      relations: 'Quan hệ trong chế độ xem', noLinks: 'Kết quả hiện tại không hiển thị quan hệ nào của nút này.',
      open: 'Mở chi tiết tài nguyên', back: 'Quay lại nút đã chọn', evidenceSource: 'Nguồn', evidenceClass: 'Phân loại',
      evidenceDate: 'Thời điểm ghi nhận', noEvidence: 'Dữ liệu sơ đồ chưa ghi nguồn.',
      fitHint: 'Dùng Danh sách để đọc nhãn đầy đủ và duyệt bằng bàn phím.',
    },
  };
  const t = key => copy[document.documentElement.lang === 'vi' ? 'vi' : 'en'][key];
  const svgNamespace = 'http://www.w3.org/2000/svg';
  const byId = id => document.getElementById('graph-' + id);
  const findNode = id => nodes.get(id);
  const nodeTypes = ['skill', 'prompt', 'agent', 'mcp', 'project', 'document', 'capability', 'concept', 'tool'];
  const edgeTypes = ['alias_of', 'installed_skill', 'provides', 'specializes', 'uses_tool', 'requires_tool', 'depends_on'];
  const nodeType = node => nodeTypes.includes(node.type) ? t(node.type) : node.type;
  const edgeType = type => edgeTypes.includes(type) ? type : 'other';
  const isContext = node => node.role === 'context';
  const asCount = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const formatCount = value => new Intl.NumberFormat(document.documentElement.lang).format(value);
  const nodeDegree = node => node.isolated === true ? 0 : asCount(node.degree) || null;

  // Cap interactive pages to keep the map readable.
  const displayLimit = 24;

  let fetchGraph;
  let openInspector;
  let getConfiguredLimit;
  let nodes = new Map();
  let edges = [];
  let total = 0;
  let offset = 0;
  let browseOffset = 0;
  let pageLimit = displayLimit;
  let hasPaging = false;
  let matchCount = 0;
  let totalLinks = null;
  let isolatedCount = null;
  let unknownCount = null;
  let nextOffset = null;
  let mapResizeFrame = 0;
  let selection = null;
  let focusRoot = null;
  let mode;
  let phase = 'loading';
  let requestId = 0;
  let renderedQuery;
  let renderedPayload;
  let searchTimer;
  let zoom = 1;
  let mapWidth = 1;
  let mapHeight = 1;
  let fitted = false;
  const mapNodes = new Map();
  let mapEdges = [];
  const listNodes = new Map();

  function element(tag, label = '', className) {
    const item = document.createElement(tag);
    item.textContent = label;
    if (className) item.className = className;
    return item;
  }

  function svg(tag, attrs) {
    const item = document.createElementNS(svgNamespace, tag);
    Object.entries(attrs).forEach(([key, value]) => item.setAttribute(key, value));
    return item;
  }

  function updateCopy() {
    const view = document.getElementById('view-graph');
    view.querySelectorAll('[data-graph-copy]').forEach(el => { el.textContent = t(el.dataset.graphCopy); });
    view.querySelectorAll('[data-graph-label]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.graphLabel)); });
    byId('filter-input').placeholder = t('searchHint');
    updateStatus();
    renderList();
    renderMap();
    renderDetails();
  }

  function updateStatus() {
    const root = findNode(focusRoot);
    const stats = byId('stats');
    let label;
    if (phase !== 'ready') {
      label = t(phase);
    } else if (!nodes.size) {
      label = t('empty');
    } else {
      const range = focusRoot ? t('neighborhood')(formatCount(nodes.size)) :
        t('visibleMatches')(formatCount(offset + 1), formatCount(offset + matchCount), formatCount(total));
      label = [
        root && (t('focused') + ' ' + root.label), range,
        !focusRoot && t('relatedCount')(formatCount(nodes.size - matchCount)),
        t('linkCount')(formatCount(edges.length)),
        totalLinks !== null && t(focusRoot ? 'focusLinks' : 'matchLinks')(formatCount(totalLinks)),
        !focusRoot && isolatedCount !== null && t('isolatedCount')(formatCount(isolatedCount)),
        !focusRoot && unknownCount > 0 && t('unknownCount')(formatCount(unknownCount)),
      ].filter(Boolean).join(' · ');
    }
    if (stats.textContent !== label) stats.textContent = label;
    byId('results').setAttribute('aria-busy', String(phase === 'loading'));
    byId('retry').hidden = phase !== 'error';
    byId('results').hidden = phase !== 'ready' || nodes.size === 0;
    byId('focus-btn').disabled = !selection || selection === focusRoot || phase !== 'ready';
    byId('reset-focus-btn').hidden = !focusRoot;
    byId('depth-filter').disabled = !focusRoot;
    // Neighborhoods include all resource types so intermediate links remain visible.
    byId('type-filter').disabled = Boolean(focusRoot);
    byId('filter-input').disabled = Boolean(focusRoot);
    updatePagination();
  }

  function updatePagination() {
    const pages = Math.max(1, Math.ceil(total / pageLimit));
    const page = Math.floor(offset / pageLimit) + 1;
    const pager = byId('pagination');
    pager.hidden = !hasPaging || pages === 1 || phase === 'error';
    pager.setAttribute('aria-busy', String(phase === 'loading'));
    const target = byId('page-number');
    target.max = String(pages);
    if (document.activeElement !== target) target.value = String(page);
    byId('page-text').textContent = t('pageText')(formatCount(pages));
    byId('page-prev').setAttribute('aria-disabled', String(page <= 1 || phase !== 'ready'));
    byId('page-next').setAttribute('aria-disabled', String(nextOffset === null || phase !== 'ready'));
    byId('page-go').setAttribute('aria-disabled', String(phase !== 'ready'));
  }

  function goToPage(page) {
    if (phase !== 'ready' || !hasPaging || !Number.isInteger(page) ||
        page < 1 || page > Math.ceil(total / pageLimit)) return;
    const targetOffset = (page - 1) * pageLimit;
    if (targetOffset === offset) return;
    offset = targetOffset;
    byId('page-number').value = String(page);
    load();
  }

  async function load() {
    clearTimeout(searchTimer);
    const id = ++requestId;
    const configuredLimit = asCount(getConfiguredLimit?.());
    const limit = Math.min(displayLimit, configuredLimit || displayLimit);
    offset = Math.floor(offset / limit) * limit;
    const params = new URLSearchParams({ offset, limit, neighbor_limit: displayLimit });
    if (focusRoot) {
      params.set('node_id', focusRoot);
      params.set('depth', byId('depth-filter').value);
    } else {
      params.set('type', byId('type-filter').value);
      const query = byId('filter-input').value.trim();
      if (query) params.set('q', query);
    }

    const query = 'graph?' + params;
    const refresh = phase === 'ready' && query === renderedQuery;
    if (!refresh) {
      phase = 'loading';
      updateStatus();
    }
    try {
      const response = await fetchGraph(query);
      if (id !== requestId) return;
      if (!Array.isArray(response.data?.nodes) || !Array.isArray(response.data?.edges)) throw new Error('Graph data missing');
      const data = response.data;
      const payload = JSON.stringify(data);
      if (refresh && payload === renderedPayload) return;
      renderedQuery = query;
      renderedPayload = payload;
      nodes = new Map(data.nodes.map(node => [node.id, node]));
      edges = data.edges.filter(edge => nodes.has(edge.source) && nodes.has(edge.target));
      matchCount = [...nodes.values()].filter(node => !isContext(node)).length;
      total = asCount(data.total) ?? matchCount;
      hasPaging = asCount(data.offset) !== null && asCount(data.limit) > 0;
      if (hasPaging) {
        pageLimit = data.limit;
        offset = data.offset;
        if (offset > 0 && offset >= total) {
          offset = Math.floor(Math.max(0, total - 1) / pageLimit) * pageLimit;
          return load();
        }
      }
      nextOffset = asCount(data.next_offset);
      totalLinks = asCount(data.counts?.matched?.edges);
      isolatedCount = asCount(data.counts?.matched?.isolated_nodes);
      unknownCount = asCount(data.counts?.matched?.unknown_nodes);
      if (!nodes.has(selection)) selection = focusRoot && nodes.has(focusRoot) ? focusRoot : null;
      phase = 'ready';
      updateStatus();
      renderList();
      renderMap();
      renderDetails();
      if (focusRoot && !refresh) byId('viewport').scrollTo(0, 0);
    } catch (error) {
      if (id !== requestId) return;
      phase = 'error';
      updateStatus();
    }
  }

  function renderList() {
    const list = byId('node-list');
    const focusedId = document.activeElement?.dataset.graphNode;
    listNodes.clear();
    const fragment = document.createDocumentFragment();
    const ordered = [...nodes.values()].sort((a, b) => Number(isContext(a)) - Number(isContext(b)));
    let shownGroup;
    for (const node of ordered) {
      const group = isContext(node) ? 'contextGroup' : 'matches';
      if (!focusRoot && group !== shownGroup) {
        fragment.append(element('li', t(group), 'graph-list-group'));
        shownGroup = group;
      }
      const row = element('li');
      const button = element('button', '', 'graph-list-node' + (isContext(node) ? ' is-context' : ''));
      button.type = 'button';
      button.dataset.graphNode = node.id;
      const meta = element('span', '', 'graph-node-meta');
      meta.append(element('span', nodeType(node)));
      if (isContext(node)) meta.append(element('span', t('context'), 'graph-context-tag'));
      if (nodeDegree(node) === 0) meta.append(element('span', t('isolated')));
      button.append(element('strong', node.label), meta);
      button.addEventListener('click', event => {
        select(node.id);
        if (!event.detail) byId('details-heading').focus();
      });
      row.append(button);
      fragment.append(row);
      listNodes.set(node.id, button);
    }
    list.replaceChildren(fragment);
    updateSelection();
    if (focusedId && !byId('list-panel').hidden) listNodes.get(focusedId)?.focus();
  }

  function updateSelection() {
    for (const [id, item] of [...mapNodes, ...listNodes]) item.setAttribute('aria-pressed', String(selection === id));
    byId('edges-group').classList.toggle('has-selection', Boolean(selection));
    for (const { edge, line } of mapEdges) {
      line.classList.toggle('is-incident', edge.source === selection || edge.target === selection);
    }
    updateStatus();
  }

  function select(id) {
    selection = id;
    fitted = false;
    updateSelection();
    renderDetails();
  }

  function renderDetails() {
    const panel = byId('details-content');
    const node = findNode(selection);
    if (!node) {
      panel.replaceChildren(element('p', t('select'), 'graph-muted'));
      return;
    }
    const title = element('h3', node.label);
    const metadata = element('p', [
      nodeType(node), isContext(node) && t('context'), node.id,
    ].filter(Boolean).join(' · '), 'graph-muted');
    let open = panel.querySelector('button[data-graph-resource]');
    if (open?.dataset.graphResource !== node.id) {
      open = element('button', '', 'btn btn-subtle');
      open.type = 'button';
      open.dataset.graphResource = node.id;
      open.addEventListener('click', () => openInspector(findNode(open.dataset.graphResource)));
    }
    open.textContent = t('open');
    const wasOpenFocused = document.activeElement === open;
    const relationships = element('ul', '', 'graph-relations');
    for (const edge of edges) {
      if (edge.source !== node.id && edge.target !== node.id) continue;
      const source = findNode(edge.source);
      const target = findNode(edge.target);
      const other = edge.source === node.id ? target : source;
      const relation = element('li');
      relation.append(
        element('strong', source.label + ' → ' + target.label),
        element('span', t(edgeType(edge.type)) + ' (' + edge.type + ')', 'graph-muted')
      );
      const evidence = edge.evidence;
      const evidenceText = typeof evidence === 'string' ? evidence : [
        evidence?.source && t('evidenceSource') + ' · ' + evidence.source,
        evidence?.class && t('evidenceClass') + ' · ' + evidence.class,
        evidence?.projection_built_at && t('evidenceDate') + ' · ' + evidence.projection_built_at,
      ].filter(Boolean).join('\n');
      relation.append(element('p', evidenceText || t('noEvidence'), 'graph-evidence'));
      if (other.id !== node.id) {
        const button = element('button', other.label, 'graph-link');
        button.type = 'button';
        button.addEventListener('click', () => {
          select(other.id);
          (mode === 'list' ? listNodes : mapNodes).get(other.id)?.focus();
        });
        relation.append(button);
      }
      relationships.append(relation);
    }
    const visible = relationships.childElementCount;
    const degree = nodeDegree(node);
    const coverage = degree === 0 ? t('isolated') : degree === null
      ? [visible ? null : t('noLinks'), t('unknownDegree')].filter(Boolean).join(' ')
      : [
        t('nodeLinks')(formatCount(visible), formatCount(degree)),
        degree > visible && t('outside'),
      ].filter(Boolean).join('. ');
    const linkInfo = element('p', coverage, 'graph-muted');
    const back = element('button', t('back'), 'graph-link');
    back.type = 'button';
    back.addEventListener('click', () => (mode === 'list' ? listNodes : mapNodes).get(node.id)?.focus());
    panel.replaceChildren(title, metadata, open, element('h3', t('relations')), linkInfo);
    if (visible) panel.append(relationships);
    panel.append(back);
    if (wasOpenFocused) open.focus();
  }

  function orderNodes() {
    const neighbors = new Map(Array.from(nodes.keys(), id => [id, []]));
    for (const edge of edges) {
      neighbors.get(edge.source).push(edge.target);
      neighbors.get(edge.target).push(edge.source);
    }
    const remaining = new Set(nodes.keys());
    const ordered = [];
    const starts = focusRoot ? [focusRoot, ...remaining] : remaining;
    for (const id of starts) {
      if (!remaining.delete(id)) continue;
      const group = [id];
      for (const next of group) {
        for (const linked of neighbors.get(next)) {
          if (remaining.delete(linked)) group.push(linked);
        }
      }
      ordered.push(...group);
    }
    return ordered;
  }

  function renderMap() {
    const viewport = byId('viewport');
    if (mode !== 'map' || viewport.clientWidth === 0 || nodes.size === 0) return;
    const activeId = document.activeElement?.dataset.graphNode;
    const nodeWidth = 250;
    const nodeHeight = 60;
    const gapX = 56;
    const gapY = 46;
    const padding = 24;
    const columns = Math.min(nodes.size, 4, Math.max(1, Math.floor((viewport.clientWidth - 2 * padding + gapX) / (nodeWidth + gapX))));
    const ordered = orderNodes();
    const positions = new Map();
    ordered.forEach((id, i) => {
      const row = Math.floor(i / columns);
      const column = row % 2 ? columns - 1 - i % columns : i % columns;
      positions.set(id, { x: padding + column * (nodeWidth + gapX), y: padding + row * (nodeHeight + gapY) });
    });
    mapWidth = 2 * padding + columns * nodeWidth + (columns - 1) * gapX;
    const rows = Math.ceil(ordered.length / columns);
    mapHeight = 2 * padding + rows * nodeHeight + (rows - 1) * gapY;
    const diagram = byId('svg');
    diagram.setAttribute('viewBox', '0 0 ' + mapWidth + ' ' + mapHeight);
    const lines = document.createDocumentFragment();
    mapEdges = [];
    for (const edge of edges) {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      const right = source.x < target.x;
      const sameColumn = source.x === target.x;
      const x1 = source.x + (right || sameColumn ? nodeWidth : 0);
      const x2 = target.x + (right ? 0 : nodeWidth);
      const y1 = source.y + nodeHeight / 2;
      const y2 = target.y + nodeHeight / 2;
      const bend = right || sameColumn ? 25 : -25;
      let curve;
      if (sameColumn && source.y === target.y) {
        curve = 'M' + x1 + ',' + (y1 - 12) + ' C' + (x1 + 30) + ',' + (y1 - 20) + ' ' + (x1 + 30) + ',' + (y1 + 20) + ' ' + x2 + ',' + (y2 + 12);
      } else if (source.y === target.y && Math.abs(source.x - target.x) > nodeWidth + gapX) {
        // Route around cards between the endpoints on the same row.
        const lane = y1 + nodeHeight / 2 + 10;
        curve = 'M' + x1 + ',' + y1 + ' H' + (x1 + bend) + ' V' + lane +
          ' H' + (x2 - bend) + ' V' + y2 + ' H' + x2;
      } else {
        curve = 'M' + x1 + ',' + y1 + ' C' + (x1 + bend) + ',' + y1 + ' ' + (x2 + (sameColumn ? bend : -bend)) + ',' + y2 + ' ' + x2 + ',' + y2;
      }
      const path = svg('path', {
        class: 'graph-map-edge ' + edgeType(edge.type),
        d: curve,
        'marker-end': 'url(#graph-arrow)',
      });
      lines.append(path);
      mapEdges.push({ edge, line: path });
    }
    byId('edges-group').replaceChildren(lines);
    const rendered = document.createDocumentFragment();
    mapNodes.clear();
    for (const [id, { x, y }] of positions) {
      const node = findNode(id);
      const type = [nodeType(node), isContext(node) && t('context'), nodeDegree(node) === 0 && t('isolated')]
        .filter(Boolean).join(' · ');
      const group = svg('g', {
        class: 'graph-map-node ' + (isContext(node) ? 'is-context' : ''),
        transform: 'translate(' + x + ',' + y + ')',
        role: 'button', tabindex: '0', 'aria-label': node.label + ' · ' + type,
      });
      group.dataset.graphNode = id;
      const background = svg('rect', { width: nodeWidth, height: nodeHeight, rx: 8 });
      const title = svg('title', {});
      title.textContent = node.label;
      const name = svg('text', { x: 14, y: 25, class: 'graph-map-name' });
      name.textContent = node.label;
      const subline = svg('text', { x: 14, y: 44, class: 'graph-map-type' });
      subline.textContent = type;
      group.append(title, background, name, subline);
      group.addEventListener('click', () => select(id));
      group.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        select(id);
        byId('details-heading').focus();
      });
      rendered.append(group);
      mapNodes.set(id, group);
    }
    byId('nodes-group').replaceChildren(rendered);
    const labelLimit = nodeWidth - 28;
    for (const label of byId('nodes-group').querySelectorAll('.graph-map-name, .graph-map-type')) {
      if (label.getComputedTextLength() <= labelLimit) continue;
      const letters = Array.from(label.textContent).slice(0, 50);
      while (letters.length > 1) {
        letters.pop();
        label.textContent = letters.join('') + '…';
        if (label.getComputedTextLength() <= labelLimit) break;
      }
    }
    updateSelection();
    if (fitted) fit();
    else updateZoom();
    if (activeId) mapNodes.get(activeId)?.focus();
  }

  function updateZoom() {
    const diagram = byId('svg');
    diagram.setAttribute('width', Math.round(mapWidth * zoom));
    diagram.setAttribute('height', Math.round(mapHeight * zoom));
    byId('zoom-level').textContent = new Intl.NumberFormat(document.documentElement.lang, { style: 'percent' }).format(zoom);
  }

  function fit() {
    const viewport = byId('viewport');
    fitted = true;
    zoom = Math.min(1, viewport.clientWidth / mapWidth, viewport.clientHeight / mapHeight);
    updateZoom();
    viewport.scrollTo(0, 0);
  }

  function adjustZoom(scale) {
    fitted = false;
    zoom = Math.min(2.5, Math.max(0.01, zoom * scale));
    updateZoom();
  }

  function setMode(next) {
    mode = next;
    byId('map-panel').hidden = next !== 'map';
    byId('list-panel').hidden = next !== 'list';
    byId('map-mode').setAttribute('aria-pressed', String(next === 'map'));
    byId('list-mode').setAttribute('aria-pressed', String(next === 'list'));
    renderMap();
  }

  function setup(fetchResource, inspectResource, configuredLimit) {
    fetchGraph = fetchResource;
    openInspector = inspectResource;
    getConfiguredLimit = configuredLimit;
    setMode(matchMedia('(max-width: 700px)').matches ? 'list' : 'map');
    updateCopy();
    window.addEventListener('skillhub:languagechange', updateCopy);
    const viewport = byId('viewport');
    new ResizeObserver(() => {
      cancelAnimationFrame(mapResizeFrame);
      mapResizeFrame = requestAnimationFrame(renderMap);
    }).observe(viewport);

    byId('retry').addEventListener('click', load);
    byId('type-filter').addEventListener('change', () => {
      offset = 0;
      load();
    });
    byId('page-prev').addEventListener('click', () => goToPage(Math.floor(offset / pageLimit)));
    byId('page-next').addEventListener('click', () => {
      if (nextOffset !== null) goToPage(Math.floor(nextOffset / pageLimit) + 1);
    });
    byId('pagination').addEventListener('submit', event => {
      event.preventDefault();
      goToPage(byId('page-number').valueAsNumber);
    });
    byId('depth-filter').addEventListener('change', () => {
      offset = 0;
      load();
    });
    byId('filter-input').addEventListener('input', () => {
      offset = 0;
      clearTimeout(searchTimer);
      ++requestId;
      phase = 'loading';
      updateStatus();
      searchTimer = setTimeout(load, 220);
    });
    byId('focus-btn').addEventListener('click', () => {
      if (!focusRoot) browseOffset = offset;
      focusRoot = selection;
      offset = 0;
      zoom = 1;
      fitted = false;
      load();
      byId('depth-filter').focus();
    });
    byId('reset-focus-btn').addEventListener('click', () => {
      focusRoot = null;
      offset = browseOffset;
      load();
      byId('filter-input').focus();
    });
    byId('map-mode').addEventListener('click', () => setMode('map'));
    byId('list-mode').addEventListener('click', () => setMode('list'));
    byId('zoom-out').addEventListener('click', () => adjustZoom(1 / 1.25));
    byId('zoom-in').addEventListener('click', () => adjustZoom(1.25));
    byId('zoom-fit').addEventListener('click', fit);
  }

  return { findNode, load, setup };
})();
