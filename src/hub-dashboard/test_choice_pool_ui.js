'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const block = source.slice(source.indexOf('function routeText('), source.indexOf('async function loadRoutingView('));
const a = 'a'.repeat(64);
const b = 'b'.repeat(64);
const dep = {id:'skill:dependency', source_raw_sha256:a, bundle_sha256:b};
const target = {id:'skill:target', source_raw_sha256:a, bundle_sha256:b, dependencies:[dep.id]};
const prompt = {id:'prompt:target', source_raw_sha256:a};
const choices = new Map([target, dep, prompt].map(row => [row.id, row]));
const body = '<do-not-render-as-html>\r\n' + 'tiếng Việt 🚀'.repeat(2000);
const receipt = item => ({resource_id:item.id, load_verified:true, verified_in_client:true,
  body_raw_sha256:item.source_raw_sha256, bundle_sha256:item.bundle_sha256,
  version:'synthetic', body_bytes:new TextEncoder().encode(body).length, body});
let nextResponse;
const requests = [];
const follow = {};
const field = {closest:()=>({}), focus() {}};
const ctx = vm.createContext({
  TextEncoder, document:{getElementById:id=>({'btn-carry-followup-state':follow,
    'route-context-loaded_skill_ids_and_versions':field})[id]},
  apiFetch: async (endpoint, request) => {
    requests.push({endpoint, ...request});
    return {data:await nextResponse(request.body.resource_id)};
  }
});
vm.runInContext(block + '\nrefreshRouteChoices = () => {}; renderRouterLoaded = () => {}; renderRouteScoresTable = () => {};',ctx);
const view = (ids=[]) => ({data:{}, mode:'metadata', isCurrent:()=>true, selected:new Set(ids), receipts:new Map(),
  errors:new Map(), contextClaims:[], loading:false, zero:false});
(async () => {
  const empty = view();
  await ctx.loadRouterChoices(empty,choices);
  assert.equal(empty.zero,true);
  assert.equal(requests.length,0);

  const only = view([target.id]);
  nextResponse = () => ({error:'DEPENDENCY_NOT_LOADED'});
  await ctx.loadRouterChoices(only,choices);
  assert.deepEqual(requests.map(row=>row.body.resource_id),[target.id]);
  assert.equal(only.errors.get(target.id),'DEPENDENCY_NOT_LOADED');
  assert.equal(follow.hidden,true);

  requests.length=0;
  const picked = view([target.id,dep.id,prompt.id]);
  nextResponse = id => receipt(choices.get(id));
  await ctx.loadRouterChoices(picked,choices);
  assert.deepEqual(requests.map(row=>row.body.resource_id),[dep.id,target.id,prompt.id]);
  assert.equal(requests[1].body.loaded_skill_ids_and_versions[0].id,dep.id);
  assert.equal(picked.receipts.get(target.id).body,body);
  assert.equal(ctx.routerScoreStatus({id:'target',kind:'skill',status:'offered'},picked)[1],'Verified load');
  assert.equal(follow.hidden,false);
  follow.onclick();
  const carried = JSON.parse(field.value);
  assert.deepEqual(carried.map(c=>c.id),[dep.id,target.id,prompt.id]);
  assert.equal(carried[0].bundle_sha256,b);
  assert.equal(carried[2].content_sha256,a);

  for (const bad of [
    {resource_id:target.id}, {body_raw_sha256:b}, {body_bytes:0}, {body_bytes:1},
    {verified_in_client:false}, {load_verified:false}
  ]) assert.equal(ctx.matchesRouteReceipt(prompt,{...receipt(prompt),...bad}),false);
  assert.equal(ctx.matchesRouteReceipt(target,{...receipt(target),bundle_sha256:a}),false);
  assert.equal(ctx.formatRouterThreshold('skill',{operator:'>=',value:0.65}),'Skill threshold: ≥ 0.65');
  assert.equal(ctx.formatRouterThreshold('prompt',{operator:'>',value:0.725}),'Prompt threshold: > 0.725');
  assert.equal(ctx.formatRouterThreshold('skill',null),'Skill threshold: Unknown');
  assert.equal(ctx.routerMetric(0,' ms'),'0 ms');
  assert.equal(ctx.routerMetric(null,' ms'),'Unknown');

  requests.length=0;
  const stale = view([dep.id,target.id]);
  let current = true, finish;
  stale.isCurrent = () => current;
  nextResponse = () => new Promise(resolve=>{finish=resolve;});
  const work = ctx.loadRouterChoices(stale,choices);
  current=false;
  finish(receipt(dep));
  await work;
  assert.equal(stale.receipts.size,0);
  assert.equal(requests.length,1);
  console.log('Choice identities, zero, explicit dependencies, byte/hash pins, carry, thresholds and stale loads PASS');
})().catch(error => {console.error(error); process.exitCode=1;});
