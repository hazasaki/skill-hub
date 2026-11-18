const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const pool = require('./choice_pool_fixture.json');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const panel = {hidden: true};
const items = {
  children: [],
  get childElementCount() { return this.children.length; },
  replaceChildren() { this.children = []; this.textContent = ''; },
  appendChild(child) { this.children.push(child); }
};
const inspected = [];
const ctx = vm.createContext({
  document: {
    getElementById: id => ({'route-choice-pool': panel, 'route-choice-items': items})[id],
    createElement: () => {
      const node = {};
      node.querySelector = () => ({addEventListener: (_, action) => { node.inspect = action; }});
      return node;
    }
  },
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
