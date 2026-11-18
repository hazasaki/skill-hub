'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const rows = [];
const tbody = {replaceChildren() {rows.length=0;}, appendChild: row => rows.push(row), querySelectorAll: () => []};
const context = vm.createContext({
  document: {createElement: () => {const code={}; return {code, querySelector:()=>code};}, getElementById: () => tbody},
  state: {routeStudio: {lastScores: [], scoresFilter: '', scoresKindFilter: 'all'}},
  escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
  scoreMeterClass: () => '',
});
vm.runInContext(source.slice(source.indexOf('function routeText('),source.indexOf('async function loadRoutingView(')),context);
for (const id of ['same-name', 'same-name--other-source', 'quote"&']) {
  const item = {id, name: 'same-name', score: 0.9, body: 'Đầy đủ tiếng Việt\n'};
  for (const html of [context.createLoadedResourceCard(item, 'skill').innerHTML,
                     context.createDeferredResourceCard(item).innerHTML]) {
    assert.ok(html.includes('data-id="' + context.escapeHtml(id) + '"'));
  }
  context.state.routeStudio.lastScores = [{...item, kind: 'skill'}];
  rows.length = 0;
  context.renderRouteScoresTable();
  assert.ok(rows[0].innerHTML.includes('data-id="' + context.escapeHtml(id) + '"'));
}
const body = '<img src=bad>\r\nĐầy đủ';
const card = context.createLoadedResourceCard({body}, 'prompt');
assert.equal(card.code.textContent,body);
assert.ok(!card.innerHTML.includes(body));
assert.match(context.createLoadedResourceCard({name: 'legacy'}, 'prompt').innerHTML, /data-id="legacy"/);
assert.match(context.createDeferredResourceCard({name: 'legacy'}).innerHTML, /data-id="legacy"/);
console.log('Router identity: distinct catalog IDs, escaping and legacy fallback PASS');
