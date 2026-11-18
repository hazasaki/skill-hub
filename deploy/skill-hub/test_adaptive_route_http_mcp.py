"""Exercise adaptive round decisions over loopback HTTP and Streamable HTTP MCP."""
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import uvicorn


async def exercise(hub):
    key_path = hub / 'fixture-key'
    key_path.write_text('fixture-only', encoding='utf-8')
    os.environ.update(SKILL_HUB_ROOT=str(hub), SKILL_HUB_DISABLE_BROKER='1',
                      SKILL_HUB_ENABLE_MODEL_ROUTING='1', SKILL_HUB_MODEL_PROVIDER='liquid',
                      SKILL_HUB_KEY_PATH=str(key_path), SKILL_HUB_INDEX_PATH=str(hub / 'serving.db'),
                      SKILL_HUB_MATERIALIZE_ROOT=str(hub / 'materialized'),
                      SKILL_HUB_TELEMETRY_ROOT=str(hub / 'telemetry'),
                      SKILL_HUB_ADAPTIVE_K='0', SKILL_HUB_ADAPTIVE_H='1000000')
    os.environ.pop('SKILL_HUB_SCORE_MODE', None)

    for kind in ('skill', 'prompt'):
        for i in range(165):
            name = f'fixture-{kind}-{i:03}'
            extra = 'prompt_type: agent_role\n' if kind == 'prompt' else ''
            if kind == 'skill' and i == 0:
                extra = 'required_permissions: [private_fixture]\n'
            path = (hub / 'skills' / name / 'SKILL.md' if kind == 'skill'
                    else hub / 'prompts' / (name + '.md'))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((f'---\nname: {name}\ndescription: Fixture task {kind} {i}\n'
                              f'capability: fixture-{i}\n{extra}---\nResource {name}.\n').encode())
    import serve_core
    import elephant_runtime
    import elephant_shadow
    import rust_routing

    native = rust_routing._decide
    assert native is not None
    calls, sent = Counter(), []
    offset = 0
    targets = []
    denied_case = False

    def observe(op, *args):
        status = native(op, *args)
        if status == 0:
            calls[rust_routing.Operation(op).name] += 1
        return status

    run_multi = elephant_runtime.run_multi

    def timed_run(**kwargs):
        return run_multi(**kwargs, monotonic=lambda: time.monotonic() + offset)

    def provider(body, headers, timeout):
        nonlocal offset
        request = json.loads(body)
        assert request['model'] == 'd1:latest'
        assert 0 < timeout <= 10
        ids = list(request['questions'])
        target = dict(zip(('skill', 'prompt'), targets[len(sent)]))
        sent.append(ids)
        answers = {}
        for identifier in ids:
            kind = identifier.split(':', 1)[0]
            denied = identifier == 'skill:fixture-skill-000'
            eligible = target[kind] > 0 and (denied_case or not denied)
            if eligible:
                target[kind] -= 1
            answers[identifier] = {'type': 'noul', 'noul': .98 if eligible else .1}
        if len(sent) == len(targets) and deadline_case == 'timeout':
            raise elephant_shadow.ShadowError('TIMEOUT', 'fixture provider timeout')
        if len(sent) == len(targets) and deadline_case == 'deadline':
            offset = 11
        return {'model': 'd1.2', 'answers': answers, 'usage': {'input_tokens': 3}}, 200

    body = {'state': 'Fixture task for resource routing.', 'retrieval_mode': 'metadata'}
    server = uvicorn.Server(uvicorn.Config(
        serve_core.build_server(hub).streamable_http_app(), host='127.0.0.1', port=8765, log_level='warning'))
    with patch.object(rust_routing, '_decide', side_effect=observe), \
         patch.object(elephant_shadow, '_liquid_transport', side_effect=provider), \
         patch.object(elephant_runtime, 'run_multi', side_effect=timed_run):
        job = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.02)
            url = 'http://127.0.0.1:8765'
            async with httpx.AsyncClient(base_url=url, timeout=25) as http, \
                    streamablehttp_client(url + '/mcp', terminate_on_close=False) as (r, w, _), \
                    ClientSession(r, w) as mcp:
                await mcp.initialize()
                cases = [
                    ('incomplete', [(5, 3), (7, 3), (0, 0)], [(5, 3), (12, 6), (12, 6)],
                     'RoundLimit'),
                    ('complete', [(5, 3), (7, 5)], [(5, 3), (12, 8)], 'MenuComplete'),
                    ('no_skill', [(0, 0)] * 3, [(0, 0)] * 3, 'RoundLimit'),
                    ('deadline', [(5, 3), (7, 5)], [(5, 3), (5, 3)], 'Deadline'),
                    ('timeout', [(5, 3), (7, 5)], [(5, 3), (5, 3)], 'Deadline'),
                    ('short_positive', [(5, 3), (7, 5)], [(5, 3), (12, 8)], 'MenuComplete'),
                    ('short_conflicting_constraint', [(5, 3), (7, 5)], [(5, 3), (12, 8)], 'MenuComplete'),
                    ('denied', [(12, 8), (2, 0)], [(11, 8), (12, 8)], 'MenuComplete'),
                ]
                for transport in ('HTTP', 'MCP'):
                    async def invoke(request):
                        if transport == 'HTTP':
                            response = await http.post('/route_task', json=request)
                            assert response.status_code == 200, response.text
                            return response.json()
                        result = await mcp.call_tool('route_task', request)
                        assert not result.isError, result
                        return json.loads(result.content[0].text)

                    sent.clear()
                    calls.clear()
                    explicit = await invoke(dict(body, state='Reply OK', task_state={
                        'constraints': ['Do not use skills or prompts']}))
                    assert explicit['outcome'] == 'NO_SKILL', explicit
                    assert explicit['choice_pool']['reason'] == 'ExplicitNoResourceEvidence'
                    assert explicit['retrieval']['no_resource_evidence'] == 'EXPLICIT_TASK_CONSTRAINT'
                    assert explicit['loads']['loaded'] == []
                    assert not sent and calls['NO_RESOURCE_CONSTRAINT'] == 1
                    print(json.dumps({'transport': transport, 'case': 'explicit_opt_out', 'calls': len(sent)}))

                    for name, targets, counts, reason in cases:
                        sent.clear()
                        calls.clear()
                        offset = 0
                        deadline_case = name if name in ('deadline', 'timeout') else None
                        denied_case = name == 'denied'
                        request = dict(body, state=f"Fixture task {name} {transport}.")
                        if denied_case:
                            request['task_state'] = {'explicit_skill_ids': ['fixture-skill-000']}
                        materialized_before = set((hub / 'materialized').rglob('*'))
                        if name == 'short_positive':
                            request['state'] = 'Review my tests'
                        if name == 'short_conflicting_constraint':
                            request['state'] = 'Need a skill for pytest'
                            request['task_state'] = {'required_capabilities': ['pytest'],
                                                     'constraints': ['Do not use skills or prompts']}
                        route = await invoke(request)
                        rounds = route['retrieval']['adaptive_rounds']
                        assert [tuple(row['verified_counts'][kind] for kind in ('skill', 'prompt'))
                                for row in rounds] == counts, (name, rounds)
                        assert [row['cap'] for row in rounds] == [24, 48, 96][:len(counts)]
                        assert rounds[-1]['reason'] == route['choice_pool']['reason'] == reason, rounds
                        assert route['retrieval']['scoring_budget']['candidate_pool'] == 300
                        assert [len(ids) for ids in sent] == [24, 24, 48][:len(counts)]
                        assert [row['ids'] for row in route['retrieval']['adaptive_provider_batches']] == sent
                        flat = [rid for batch in sent for rid in batch]
                        assert len(set(flat)) == len(flat)
                        assert route['retrieval']['card_coverage']['derived'] == len(flat)
                        assert set(route['retrieval']['cards']) == set(flat)
                        assert calls['ADAPTIVE_ROUND'] == 2 * len(counts)
                        assert route['loads']['loaded'] == []
                        assert route['provider']['input_tokens'] == 3 * (len(sent) - (name == 'timeout'))
                        assert route['telemetry']['status'] == 'RECORDED', route['telemetry']
                        from hub_telemetry import EventStore

                        events = EventStore(hub / 'telemetry').events()
                        select = next(event for event in reversed(list(events))
                                      if event['kind'] == 'select'
                                      and event['route_id'] == route['telemetry']['route_id'])
                        actual = select['fields']
                        assert (actual['skill_count'], actual['prompt_count']) == counts[-1], actual
                        assert set((hub / 'materialized').rglob('*')) == materialized_before
                        assert all('body' not in row and row['body_bytes'] > 0 and row['reason']
                                   and row['capabilities'] and 'dependencies' in row
                                   and row['source_raw_sha256'] for row in route['selected'])
                        if name == 'incomplete':
                            assert all(rid.startswith('prompt:') for rid in sent[2])
                        if deadline_case:
                            assert route['trigger'] == route['partial_reason'] == 'DEADLINE'
                            assert route['coverage']['stop_reason'] == 'DEADLINE'
                            assert route['partial'] is True
                        if denied_case:
                            assert any(row['reason'] == 'POLICY_DENIED' for row in route['choice_pool']['excluded'])
                        if name == 'complete':
                            choice = route['choice_pool']['skills'][0]
                            loaded = await mcp.call_tool('load_resource_choice', {
                                'resource_id': choice['id'], 'source_raw_sha256': choice['source_raw_sha256'],
                                'bundle_sha256': choice['bundle_sha256'],
                            })
                            assert not loaded.isError, loaded
                            receipt = json.loads(loaded.content[0].text)
                            assert receipt['body_raw_sha256'] == choice['source_raw_sha256'], receipt
                            expected = hub / 'skills' / choice['resource_id'] / 'SKILL.md'
                            assert receipt['body'].encode() == expected.read_bytes()
                        print(json.dumps({'transport': transport, 'case': name, 'reason': reason,
                                          'outcome': route['outcome'], 'verified_counts': counts,
                                          'caps': [row['cap'] for row in rounds], 'provider_ids': sent,
                                          'native_calls': dict(calls), 'scored': route['coverage']['scored'],
                                          'loaded': len(route['loads']['loaded'])}))
        finally:
            server.should_exit = True
            await job


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='skill-hub-adaptive-') as root:
        asyncio.run(exercise(Path(root)))
