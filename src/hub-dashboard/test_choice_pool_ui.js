const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const pool = require('./choice_pool_fixture.json');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const panel = {hidden: true};
const checks = [];
const items = {
  querySelectorAll() { return checks; },
  children: [],
  get childElementCount() { return this.children.length; },
  replaceChildren() { this.children = []; this.textContent = ''; },
  appendChild(child) { this.children.push(child); }
};
const inspected = [];
const calls = [];
const results = {children: [], replaceChildren(...children) { this.children = children; }};
const controls = new Map();
let receipt;
const ctx = vm.createContext({
  document: {
    getElementById: id => ({'route-choice-pool': panel, 'route-choice-items': items})[id],
    createElement: () => {
      const node = {};
      node.querySelector = selector => {
        if (selector === '#choice-load-results') return results;
        return {addEventListener: (_, action) => {
          if (selector.startsWith('#btn-')) controls.set(selector, action);
          else node.inspect = action;
        }};
      };
      return node;
    }
  },
  apiFetch: async (path, request) => { calls.push({path, ...request}); return {data: receipt}; },
  openResourceInspector: (...args) => inspected.push(args)
});
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
vm.runInContext(extract('function escapeHtml', 'function truncateStr') +
  extract('function renderRouteChoicePool', 'function renderRouteTaskStudioResults'), ctx);
ctx.renderRouteChoicePool({choice_pool: pool, loads: {loaded: [{id: 'giant', type: 'skill'}]}});
assert.equal(panel.hidden, false);
assert.match(items.children[0].innerHTML, /Đề xuất · Đã nạp/);
assert.match(items.children[0].innerHTML, /Phụ thuộc: skill:dependency/);
assert.match(items.children[1].innerHTML, /Phương án khác · Chưa nạp/);
assert.match(items.children[2].innerHTML, /Đề xuất · Chưa nạp/);
assert.match(items.children[2].innerHTML, /Vai trò: task/);
assert.match(items.children[3].textContent, /DEPENDENCY_DENIED · skill:private/);
items.children[0].inspect();
items.children[2].inspect();
assert.deepEqual(inspected, [['giant', 'skill'], ['giant', 'prompt']]);
ctx.renderRouteChoicePool({choice_pool: pool, loads: {loaded: [{id: 'prompt:giant', kind: 'prompt'}]}});
assert.match(items.children[0].innerHTML, /Đề xuất · Chưa nạp/);
assert.match(items.children[2].innerHTML, /Đề xuất · Đã nạp/);
ctx.renderRouteChoicePool({choice_pool: {...pool, skills: [{...pool.skills[0], reason: '<img src=x>'}]}});
assert.match(items.children[0].innerHTML, /&lt;img src=x&gt;/);
ctx.renderRouteChoicePool({choice_pool: {skills: [], prompts: [], excluded: []}});
assert.equal(panel.hidden, false);
assert.match(items.textContent, /Không có tài nguyên/);
for (const response of [null, {}, {choice_pool: {delivery: 'DEFERRED_FULL_FETCH'}}]) {
  ctx.renderRouteChoicePool(response);
  assert.equal(panel.hidden, true);
  assert.equal(items.childElementCount, 0);
}
console.log('Choice pool contract, identity, receipt, exclusions and reset PASS');

(async () => {
  const hash = 'a'.repeat(64);
  const choice = {...pool.prompts[0], source_raw_sha256: hash};
  const body = 'a'.repeat(17000) + '<interior-marker>';
  ctx.renderRouteChoicePool({choice_pool: {skills: [], prompts: [choice], excluded: []}});
  const load = controls.get('#btn-load-selected-choices');
  await load();
  assert.equal(calls.length, 0);
  checks.push({checked: true, dataset: {qid: choice.id}});
  receipt = {load_verified: true, verified_in_client: true, body_raw_sha256: hash, body_bytes: body.length, body};
  await load();
  assert.equal(calls[0].path, 'load_resource_choice');
  assert.equal(calls[0].body.source_raw_sha256, hash);
  assert.equal(results.children[0].open, true);
  assert.match(results.children[0].innerHTML, /&lt;interior-marker&gt;/);
  receipt = {error: 'BODY_REDACTED'};
  await load();
  assert.match(results.children[0].textContent, /BODY_REDACTED/);
  assert.equal(results.children[0].innerHTML, undefined);
  console.log('Choice UI: zero, explicit load, escaped >16 KiB body, redaction refusal PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
