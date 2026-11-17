/* Batch intake owns staging only. Publication requires the separate native publisher. */
'use strict';
(function (global) {
  const encoder = new TextEncoder();
  const identity = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
  const reserved = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;
  const instances = new WeakMap();

  function frontmatter(content) {
    return content.replace(/\r\n?/g, '\n').match(/^---\s*\n([\s\S]*?)\n---\s*\n([\s\S]*)$/);
  }

  function fromDocument(content, filename, kind) {
    const metadata = frontmatter(content);
    const declared = metadata && metadata[1].match(/^name:[ \t]*(.*)$/m);
    return {kind: /^SKILL\.md$/i.test(filename) ? 'skill' : kind,
      name: declared ? declared[1].trim().replace(/^["']|["']$/g, '') : filename.replace(/\.(md|txt)$/i, ''), content};
  }

  function validateDraft(items) {
    if (!Array.isArray(items) || !items.length || items.length > 20) throw new Error('Mỗi lô cần từ 1 đến 20 tài nguyên.');
    const keys = new Set();
    let bytes = 0;
    items.forEach((item, index) => {
      const prefix = `Mục ${index + 1}: `;
      if (!['skill', 'prompt'].includes(item.kind)) throw new Error(prefix + 'chọn Skill hoặc Prompt.');
      if (typeof item.name !== 'string' || typeof item.content !== 'string') throw new Error(prefix + 'tên và nội dung phải là văn bản.');
      if (!identity.test(item.name) || item.name.endsWith('.') || reserved.test(item.name)) throw new Error(prefix + 'tên cần 1–100 ký tự a–z, A–Z, 0–9, dấu chấm, gạch dưới hoặc gạch ngang; bắt đầu bằng chữ/số và tránh tên thiết bị Windows.');
      const size = encoder.encode(item.content).length;
      if (!size || size > 65536 || item.content.includes('\0')) throw new Error(prefix + 'nội dung cần 1–65.536 byte UTF-8, không chứa ký tự NUL.');
      if (item.kind === 'skill') {
        const meta = frontmatter(item.content);
        if (!meta || !meta[2].trim()) throw new Error(prefix + 'Skill cần frontmatter YAML và phần hướng dẫn bên dưới.');
        for (const key of ['name', 'description']) {
          const values = [...meta[1].matchAll(new RegExp('^' + key + ':[ \\t]*(.*)$', 'gm'))];
          if (values.length !== 1 || !values[0][1].trim().replace(/^["']|["']$/g, '')) throw new Error(prefix + `frontmatter cần một ${key} không trống.`);
        }
        if (fromDocument(item.content, '', 'skill').name !== item.name) throw new Error(prefix + 'name trong frontmatter phải khớp tên tài nguyên.');
      } else if (!item.content.trim()) throw new Error(prefix + 'Prompt không được để trống.');
      const key = item.kind + ':' + item.name.toLowerCase();
      if (keys.has(key)) throw new Error(prefix + 'trùng tên và loại trong lô.');
      keys.add(key); bytes += size;
    });
    if (bytes > 250000) throw new Error('Tổng nội dung lô vượt 250.000 byte UTF-8.');
    return bytes;
  }

  function errorText(error, fallback) {
    const message = typeof error?.message === 'string' ? error.message : '';
    if (/credential-looking|unsafe staged|unsafe or changed/i.test(message)) return 'Nội dung không đạt kiểm tra an toàn hoặc chứa dữ liệu giống thông tin xác thực. Hãy loại bỏ dữ liệu nhạy cảm trước khi thử lại.';
    if (/identity conflicts/i.test(message)) return 'Tên/loại đã trùng catalog, vùng cách ly hoặc một phiên bản khác trong kho chờ. Hãy đổi tên rồi xem trước lại.';
    if (/expired or consumed plan/i.test(message)) return 'Bản xem trước đã hết hiệu lực hoặc được dùng. Hãy xem trước lại lô.';
    return message.slice(0, 300) || fallback;
  }

  function mount(root, options = {}) {
    if (!root || typeof options.apiFetch !== 'function') throw new Error('Intake cần phần tử gắn và apiFetch của dashboard.');
    if (instances.has(root)) return instances.get(root);
    const api = async (path, request) => {
      const response = await options.apiFetch('/api/v1/' + path, request);
      if (!response || !response.data) throw new Error('Máy chủ trả dữ liệu không hợp lệ.');
      return response.data;
    };
    let items = [], plan = null, busy = false, offset = 0, total = 0, generation = 0;
    let destroyed = false;
    const events = new AbortController();
    const on = (target, name, action) => target.addEventListener(name, action, {signal: events.signal});
    function el(tag, text, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    }
    function button(text, action, className = 'btn btn-secondary') {
      const node = el('button', text, className); node.type = 'button'; on(node, 'click', action); return node;
    }
    function field(label, control) {
      const node = el('label', undefined, 'form-group'); node.append(el('span', label), control); return node;
    }
    function kindSelect(value) {
      const node = el('select', undefined, 'form-control');
      for (const kind of ['skill', 'prompt']) { const option = el('option', kind === 'skill' ? 'Skill' : 'Prompt'); option.value = kind; node.append(option); }
      node.value = value; return node;
    }
    root.replaceChildren(); root.classList.add('card', 'intake-panel');
    const title = el('h2', 'Nhập nhiều Skills & Prompts', 'card-title');
    const note = el('p', 'Dán nội dung hoặc chọn nhiều tệp. Xem trước rồi lưu vào kho chờ; xuất bản và kích hoạt là bước riêng.', 'text-muted');
    const draft = el('fieldset', undefined, 'intake-fieldset card-body');
    const controls = el('div', undefined, 'intake-grid');
    const kind = kindSelect('skill'); const name = el('input', undefined, 'form-control'); name.type = 'text'; name.maxLength = 100;
    name.placeholder = 'ví dụ: huong-dan-python';
    const content = el('textarea', undefined, 'form-control'); content.rows = 8; content.spellcheck = false;
    content.placeholder = 'Skill: --- + name + description + --- rồi đến hướng dẫn. Prompt: nội dung hướng dẫn.';
    controls.append(field('Loại khi thêm nội dung/tệp', kind), field('Tên tài nguyên khi dán', name));
    const files = el('input', undefined, 'form-control'); files.type = 'file'; files.multiple = true; files.accept = '.md,.txt,.json';
    const message = el('p', '', 'intake-message'); message.setAttribute('role', 'status'); message.setAttribute('aria-live', 'polite');
    const drafts = el('div', undefined, 'intake-items');
    const previewInfo = el('div', undefined, 'intake-preview'); previewInfo.setAttribute('aria-live', 'polite');
    const confirmed = el('input'); confirmed.type = 'checkbox';
    const confirmation = field('Tôi đã kiểm tra bản xem trước và đồng ý chỉ lưu lô vào kho chờ.', confirmed);
    const applyButton = button('Lưu lô đã duyệt', apply, 'btn btn-primary');
    const previewButton = button('Xem trước lô', preview, 'btn btn-primary');
    const clear = button('Xóa bản nháp', () => { items = []; invalidate(); renderDrafts(); });
    const actions = el('div', undefined, 'intake-actions'); actions.append(previewButton, clear);
    draft.append(controls, field('Nội dung Markdown', content), button('Thêm nội dung vào lô', () => {
      const item = fromDocument(content.value, name.value.trim(), kind.value);
      item.name = name.value.trim() || item.name;
      if (!item.content.trim()) { show('Cần nhập nội dung trước khi thêm.'); return; }
      if (add([item])) { content.value = ''; name.value = ''; }
    }), field('Chọn nhiều tệp Markdown hoặc JSON lô đã xuất', files), drafts, actions, previewInfo, confirmation, applyButton, message);
    const listPanel = el('fieldset', undefined, 'intake-fieldset card-body');
    const listHeading = el('h3', 'Đã lưu trong kho chờ'); const list = el('div', undefined, 'intake-list');
    const page = el('span', '', 'text-muted'); const back = button('Trang trước', () => { offset = Math.max(0, offset - 20); refresh(); });
    const next = button('Trang sau', () => { offset += 20; refresh(); });
    const reload = button('Tải lại danh sách', () => refresh());
    const pages = el('div', undefined, 'intake-actions'); pages.append(back, page, next, reload);
    listPanel.append(listHeading, list, pages);
    const native = el('details', undefined, 'card-body'); native.append(el('summary', 'Xuất bản bằng CLI trên máy chủ'));
    native.append(el('p', 'Chọn “Xuất lô JSON”, rồi chạy các lệnh sau trong PowerShell trên máy chủ. Công cụ xuất bản kiểm tra lại nội dung và tên đã có trong catalog.', 'text-muted'),
      el('pre', [
        "$batch = Read-Host 'Đường dẫn tệp JSON vừa tải'",
        "$hub = Read-Host 'Đường dẫn thư mục nội dung Skill Hub'",
        'python deploy/skill-hub/app/publish_batch.py $batch --directory ./state/skill-hub-published --catalog "$hub/state/skill-index.db" --hub $hub',
        'if ($LASTEXITCODE -eq 0) { docker compose restart skill-hub }'
      ].join('\n')),
      el('p', 'Sau khi dịch vụ sẵn sàng, kiểm tra /health trên cổng 8765 rồi tìm và mở ID mới qua MCP. Tài nguyên mới xuất hiện trong tìm kiếm từ khóa sau khi khởi động lại. Tìm kiếm ngữ nghĩa cần xây lại chỉ mục.', 'text-muted'));
    const header = el('div', undefined, 'card-header'); header.append(title, note);
    root.append(header, draft, listPanel, native);
    function show(text, error = false) { message.textContent = text; message.setAttribute('role', error ? 'alert' : 'status'); }
    function sync() {
      draft.disabled = busy; listPanel.disabled = busy;
      previewButton.disabled = busy || !items.length;
      applyButton.disabled = busy || !plan || !confirmed.checked;
      confirmed.disabled = !plan;
      back.disabled = busy || offset === 0; next.disabled = busy || offset + 20 >= total;
      clear.disabled = busy || !items.length;
    }
    function invalidate() { generation++; plan = null; confirmed.checked = false; previewInfo.replaceChildren(); sync(); }
    function add(incoming) {
      if (items.length + incoming.length > 20 || [...items, ...incoming].reduce((sum, item) => sum + encoder.encode(item.content).length, 0) > 250000) {
        show('Lô tối đa 20 mục và 250.000 byte UTF-8. Chưa thêm tệp/nội dung mới.', true); return false;
      }
      items.push(...incoming); invalidate(); renderDrafts(); show(`Bản nháp có ${items.length} mục; chưa lưu lên máy chủ.`); return true;
    }
    function renderDrafts() {
      drafts.replaceChildren(el('h3', `Bản nháp (${items.length}/20)`));
      items.forEach((item, index) => {
        const row = el('details', undefined, 'intake-draft'); row.append(el('summary', `${index + 1}. ${item.kind === 'skill' ? 'Skill' : 'Prompt'} · ${item.name || 'Chưa đặt tên'}`));
        const select = kindSelect(item.kind); const input = el('input', undefined, 'form-control'); input.value = item.name; input.maxLength = 100;
        const text = el('textarea', undefined, 'form-control'); text.value = item.content; text.rows = 6; text.spellcheck = false;
        const updateSummary = () => { row.firstChild.textContent = `${index + 1}. ${item.kind === 'skill' ? 'Skill' : 'Prompt'} · ${item.name || 'Chưa đặt tên'}`; };
        on(select, 'change', () => { item.kind = select.value; invalidate(); updateSummary(); });
        on(input, 'input', () => { item.name = input.value; invalidate(); updateSummary(); });
        on(text, 'input', () => { item.content = text.value; invalidate(); });
        row.append(field('Loại', select), field('Tên', input), field('Nội dung', text), button('Bỏ mục này', () => { items.splice(index, 1); invalidate(); renderDrafts(); }));
        drafts.append(row);
      }); sync();
    }
    async function preview() {
      invalidate();
      try {
        validateDraft(items); busy = true; sync(); show('Đang kiểm tra và lập bản xem trước…');
        const revision = generation;
        const body = {operation: 'resource.import_batch', items: items.map(({kind, name, content}) => ({kind, name, content}))};
        if (encoder.encode(JSON.stringify(body)).length > 400000) throw new Error('Lô sau đóng gói JSON vượt 400.000 byte. Hãy chia thành lô nhỏ hơn.');
        const data = await api('operations/preview', {method: 'POST', body});
        if (destroyed || revision !== generation) return;
        if (typeof data.plan_id !== 'string' || !Array.isArray(data.items) || !/^[a-f0-9]{64}$/.test(data.batch_id)) throw new Error('Bản xem trước thiếu định danh hợp lệ.');
        plan = data;
        previewInfo.replaceChildren(el('h3', 'Bản xem trước — chưa lưu'));
        previewInfo.append(el('p', `${data.items.length} mục · hiệu lực ${data.expires_in || 300} giây`, 'text-muted'));
        data.items.forEach(item => previewInfo.append(el('p', `${item.kind}: ${item.name}${item.prompt_frontmatter_added ? ' · Publisher sẽ bổ sung frontmatter cho Prompt.' : ''}`)));
        for (const duplicate of data.duplicates || []) {
          if (duplicate.same_staged_identity) previewInfo.append(el('p', `${duplicate.name}: cùng phiên bản đã có trong kho chờ; lưu lại không tạo bản trùng.`));
          if (duplicate.same_catalog_document_ids?.length) previewInfo.append(el('p', `${duplicate.name}: nội dung trùng tài liệu catalog (${duplicate.same_catalog_document_ids.join(', ')}); chưa xác nhận tương đương cả bundle.`));
        }
        show('Kiểm tra tên, loại và nội dung trước khi xác nhận lưu.');
      } catch (error) { show(errorText(error, 'Không thể xem trước lô.'), true); }
      finally { busy = false; sync(); }
    }
    async function apply() {
      if (!plan || !confirmed.checked || busy) return;
      busy = true; sync(); show('Đang lưu lô đã xác nhận…');
      try {
        const data = await api('operations/apply', {method: 'POST', body: {plan_id: plan.plan_id, confirmed: true}});
        if (destroyed) return;
        if (data.applied !== true || data.status !== 'staged' || data.active !== false) throw new Error('Kết quả lưu chưa xác định; tải lại danh sách để kiểm tra.');
        items = []; invalidate(); renderDrafts(); offset = 0;
        show(data.replayed ? 'Lô này đã được lưu trước đó; không tạo bản trùng. Thao tác này không xuất bản hoặc kích hoạt.' : 'Đã lưu lô vào kho chờ. Thao tác này không xuất bản hoặc kích hoạt.');
        await refresh();
      } catch (error) {
        const stale = /expired|changed|conflict/i.test(error.message || '');
        if (stale) invalidate();
        show(errorText(error, 'Không xác định kết quả lưu.') + (stale ? ' Cần lập bản xem trước mới.' : ' Có thể tải lại danh sách hoặc thử lại cùng bản xem trước.'), true);
      }
      finally { busy = false; sync(); }
    }
    function download(value, filename, type) {
      const url = URL.createObjectURL(new Blob([value], {type}));
      const anchor = el('a'); anchor.href = url; anchor.download = filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    async function exportRow(item, batch) {
      busy = true; sync();
      try {
        const params = new URLSearchParams(batch ? {batch_id: item.batch_id} : {kind: item.kind, name: item.name});
        const data = await api('intake/export?' + params);
        if (batch) download(JSON.stringify(data, null, 2), `batch-${item.batch_id.slice(0, 12)}.json`, 'application/json');
        else if (typeof data.content === 'string') download(data.content, item.kind === 'skill' ? item.name + '-SKILL.md' : item.name + '.md', 'text/markdown;charset=utf-8');
        else throw new Error('Máy chủ không trả nội dung xuất.');
        show('Đã tải bản xuất. Tệp vẫn có trạng thái kho chờ; tải xuống không kích hoạt tài nguyên.');
      } catch (error) { show(errorText(error, 'Không thể xuất tài nguyên.'), true); }
      finally { busy = false; sync(); }
    }
    async function refresh() {
      const revision = ++refresh.sequence;
      try {
        const data = await api(`intake?offset=${offset}&limit=20`);
        if (destroyed || revision !== refresh.sequence) return;
        total = Number(data.total) || 0;
        if (offset >= total && offset) { offset = Math.max(0, Math.floor((total - 1) / 20) * 20); return refresh(); }
        list.replaceChildren();
        if (!data.items?.length) list.append(el('p', 'Chưa có tài nguyên nào trong kho chờ.', 'text-muted'));
        for (const item of data.items || []) {
          const row = el('div', undefined, 'intake-staged-row');
            row.append(el('strong', `${item.kind === 'skill' ? 'Skill' : 'Prompt'} · ${item.name}`), el('span', 'Đã lưu chờ · chưa xác minh xuất bản', 'badge badge-warning'),
            button('Xuất Markdown', () => exportRow(item, false)), button('Xuất lô JSON', () => exportRow(item, true)));
          list.append(row);
        }
        page.textContent = total ? `${offset + 1}–${Math.min(offset + 20, total)} / ${total}` : '0 tài nguyên'; sync();
      } catch (error) { list.replaceChildren(el('p', errorText(error, 'Không tải được kho chờ.'), 'text-muted')); }
    }
    refresh.sequence = 0;
    on(confirmed, 'change', sync);
    on(files, 'change', async () => {
      if (!files.files.length) return;
      const selected = [...files.files]; busy = true; sync();
      try {
        if (selected.length > 20 || selected.some(file => file.size > 400000)) throw new Error('Chọn tối đa 20 tệp; mỗi tệp Markdown tối đa 65.536 byte hoặc JSON lô tối đa 400.000 byte.');
        const groups = await Promise.all(selected.map(async file => {
          if (!/\.(md|txt|json)$/i.test(file.name)) throw new Error('Chỉ nhận tệp .md, .txt hoặc .json.');
          let text;
          try { text = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer()); }
          catch { throw new Error('Tệp cần mã hóa UTF-8 hợp lệ. Chưa thêm nội dung mới.'); }
          if (!/\.json$/i.test(file.name)) return [fromDocument(text, file.name, kind.value)];
          let payload; try { payload = JSON.parse(text); } catch { throw new Error('Tệp JSON không hợp lệ.'); }
          const rows = Array.isArray(payload) ? payload : payload.items;
          if (!Array.isArray(rows) || rows.some(row => !row || typeof row.kind !== 'string' || typeof row.name !== 'string' || typeof row.content !== 'string')) throw new Error('JSON cần items gồm kind, name và content.');
          return rows.map(({kind, name, content}) => ({kind, name, content}));
        }));
        if (!destroyed) add(groups.flat());
      } catch (error) { show(errorText(error, 'Không đọc được tệp.'), true); }
      finally { files.value = ''; busy = false; sync(); }
    });
    const instance = {refresh, destroy() { destroyed = true; events.abort(); root.replaceChildren(); instances.delete(root); }};
    instances.set(root, instance); renderDrafts(); refresh(); return instance;
  }
  global.SkillHubIntake = {mount};
  if (typeof module !== 'undefined' && module.exports) module.exports = {validateDraft, fromDocument, errorText, mount};
})(typeof window !== 'undefined' ? window : globalThis);
