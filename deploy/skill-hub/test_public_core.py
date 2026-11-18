import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('SKILL_HUB_ROOT', str(ROOT.parents[1] / 'examples/hub'))
import serve_core
from starlette.testclient import TestClient


class PublicCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = self.root / 'hub'
        shutil.copytree(ROOT.parents[1] / 'examples/hub', self.hub)
        self.env = patch.dict(os.environ, {
            'SKILL_HUB_ROOT': str(self.hub),
            'SKILL_HUB_INDEX_PATH': str(self.root / 'catalog/skill-index.db'),
            'SKILL_HUB_MATERIALIZE_ROOT': str(self.root / 'materialized'),
            'SKILL_HUB_TELEMETRY_ROOT': str(self.root / 'telemetry'),
            'SKILL_HUB_KEY_PATH': str(self.root / 'no-credential')})
        self.env.start()
        for name in ('SKILL_HUB_ENABLE_MODEL_ROUTING', 'SKILL_HUB_MODEL_PROVIDER', 'SKILL_HUB_SCORE_MODE'):
            os.environ.pop(name, None)
        self.addCleanup(self.env.stop)
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.server = serve_core.build_server(self.hub)

    async def call(self, name, arguments):
        result = await self.server.call_tool(name, arguments)
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    async def test_examples_search_load_and_no_network(self):
        names = {tool.name for tool in await self.server.list_tools()}
        self.assertEqual(names, {'search_skills', 'get_skill', 'search_prompts', 'get_prompt', 'list_skills', 'skill_stats'})
        skills = await self.call('search_skills', {'query': 'review'})
        self.assertEqual(skills['results'][0]['name'], 'review-checklist')
        skill = await self.call('get_skill', {'name': 'review-checklist'})
        self.assertIn('Review a small change', skill['body'])
        self.assertIn('references/checklist.md', skill['files'])
        self.assertTrue((Path(skill['path']) / 'references/checklist.md').is_file())
        prompts = await self.call('search_prompts', {'query': 'summary'})
        self.assertEqual(prompts['results'][0]['name'], 'change-summary')
        prompt = await self.call('get_prompt', {'name': 'change-summary'})
        self.assertIn('user-visible result', prompt['body'])
        self.assertFalse((self.root / 'no-credential').exists())

    async def test_disable_and_traversal(self):
        (self.hub / 'skills/review-checklist/DISABLED').touch()
        self.server = serve_core.build_server(self.hub)
        self.assertEqual((await self.call('list_skills', {}))['results'], [])
        self.assertEqual((await self.call('search_skills', {'query': 'review'}))['results'], [])
        self.assertIn('error', await self.call('get_skill', {'name': 'review-checklist'}))
        self.assertIn('error', await self.call('get_skill', {'name': '../review-checklist'}))

    async def test_new_local_content_is_indexed_on_restart(self):
        new = self.hub / 'skills/custom-skill'
        new.mkdir()
        (new / 'SKILL.md').write_text('---\nname: custom-skill\ndescription: Custom local tracing guide\nrisk: safe\n---\nTrace a local function and report evidence.\n', encoding='utf-8')
        self.server = serve_core.build_server(self.hub)
        rows = await self.call('search_skills', {'query': 'tracing'})
        self.assertEqual(rows['results'][0]['name'], 'custom-skill')

    async def test_health_is_capability_specific_and_fails_for_corrupt_catalog(self):
        with TestClient(self.server.streamable_http_app(), base_url='http://localhost:8765') as client:
            response = client.get('/health')
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body['catalog']['active'], 1)
            self.assertEqual(body['catalog']['prompts'], 1)
            self.assertEqual(body['capabilities']['model_routing'], 'disabled')
            self.assertEqual(client.post('/route_task', json={'state': 'Review'}).status_code, 503)
            self.assertEqual(client.post('/route_task', json={'state': 'Review'},
                                         headers={'Origin': 'https://example.invalid'}).status_code, 403)
            Path(os.environ['SKILL_HUB_INDEX_PATH']).write_bytes(b'corrupt')
            response = client.get('/health')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['status'], 'unavailable')


    async def test_provider_opt_in_exposes_structured_staged_choices_and_full_snapshot(self):
        import hashlib
        import sqlite3
        import elephant_shadow
        import skill_index

        skill_path = self.hub / 'skills/review-checklist/SKILL.md'
        original = skill_path.read_bytes()
        closing = original.index(b'---', 3)
        raw = original[:closing] + b'dependencies: [skill:setup]\n' + original[closing:] + ('Dữ liệu nguyên văn 🧪\r\n' * 12500).encode()
        skill_path.write_bytes(raw)
        alt = self.hub / 'skills' / 'alternate'
        alt.mkdir()
        (alt / 'SKILL.md').write_text(
            '---\nname: alternate\ndescription: Competing review\n'
            'conflicts_with: [skill:review-checklist]\n---\nReview differently.\n', encoding='utf-8')
        setup = self.hub / 'skills/setup'
        setup.mkdir()
        (setup / 'SKILL.md').write_text(
            '---\nname: setup\ndescription: Prepare review\n---\nPrepare the fixture.\n', encoding='utf-8')
        key_path = self.root / 'fixture-key'
        key_path.write_text('fixture-only', encoding='utf-8')
        os.environ['SKILL_HUB_KEY_PATH'] = str(key_path)
        # A key file alone does not enable outbound routing.
        self.assertNotIn('route_task', {tool.name for tool in await serve_core.build_server(self.hub).list_tools()})
        with patch.dict(os.environ, {'SKILL_HUB_ENABLE_MODEL_ROUTING': '1', 'SKILL_HUB_SCORE_MODE': 'staged'}):
            self.server = serve_core.build_server(self.hub)
            names = {tool.name for tool in await self.server.list_tools()}
            self.assertIn('route_task', names)
            self.assertIn('get_route_result', names)
            self.assertNotIn('call_hub_tool', names)
            with sqlite3.connect(os.environ['SKILL_HUB_INDEX_PATH']) as con:
                con.execute('INSERT INTO aliases VALUES (?,?,?,?)',
                            ('review-alias', 'review-checklist', 'skill', skill_index.content_digest(raw.decode())))
            sent = []

            def score(body, headers, timeout):
                request = json.loads(body)
                self.assertEqual(request['model'], elephant_shadow.MODEL)
                self.assertEqual(headers['Authorization'], 'Api-Key fixture-only')
                sent.append(request)
                return {'answers': {key: {'type': 'noul', 'noul': .99 if key == 'skill:review-alias' else .9}
                                    for key in request['questions']},
                        'usage': {'input_tokens': 0, 'output_tokens': 0}}, 200

            args = {'state': 'Review a fixture', 'task_state': {'current_step': 'review',
                    'explicit_skill_ids': ['review-alias', 'alternate']}}
            rejected = await self.call('route_task', {**args, 'retrieval_mode': 'tempr'})
            self.assertEqual(rejected['error'], 'UNSUPPORTED_RETRIEVAL_MODE')
            with patch.object(elephant_shadow, '_urllib_transport', side_effect=score):
                result = await self.call('route_task', args)
                self.assertEqual(result['outcome'], 'SELECTED', result)
                self.assertEqual(result['task_state']['current_step'], 'review')
                self.assertEqual(len(result['task_state_sha256']), 64)
                retrieval = result['retrieval']
                self.assertEqual(retrieval['scoring_stages'][0]['cap'], 24)
                self.assertIn('skill:review-alias', retrieval['cards'])
                self.assertFalse(result['cache']['exact_revision_available'])
                choices = {entry['id']: entry for entry in result['choice_pool']['skills']}
                self.assertEqual(choices['skill:alternate']['reason'], 'DECLARED_CONFLICT')
                self.assertEqual(choices['skill:review-alias']['dependencies'], ['skill:setup'])
                loaded_ids = [row['id'] for row in result['loads']['loaded']]
                self.assertLess(loaded_ids.index('setup'), loaded_ids.index('review-alias'))
                load = next(row for row in result['loads']['loaded'] if row['id'] == 'review-alias')
                self.assertEqual(load['body'].encode(), raw)
                self.assertEqual(load['body_raw_sha256'], hashlib.sha256(raw).hexdigest())
                compact = await self.call('route_task', {**args, 'response_mode': 'compact'})
                transport = compact['transport']
                self.assertFalse(transport['complete'])
                self.assertNotIn('download', transport)
                segments, offset = [], 0
                while offset is not None:
                    page = await self.call('get_route_result', {'result_id': transport['fetch']['result_id'],
                                              'offset': offset})
                    segments.append(page['text'])
                    offset = page['next_offset']
                full = ''.join(segments)
                self.assertEqual(hashlib.sha256(full.encode()).hexdigest(), transport['full_sha256'])
                skill_path.write_bytes(raw + b'drift')
                refused = await self.call('get_route_result', {'result_id': transport['fetch']['result_id']})
                self.assertIn(refused['error'], ('CONTENT_CHANGED', 'RESOURCE_UNAVAILABLE'))
            self.assertEqual(len(sent), 2)

    async def test_liquid_default_and_baseline_preserve_dated_source_and_overlap_reads(self):
        import hashlib
        import elephant_shadow
        import skill_index
        from mcp.shared.memory import create_connected_server_and_client_session

        skill_path = self.hub / 'skills/review-checklist/SKILL.md'
        original = skill_path.read_bytes()
        closing = original.index(b'---', 3)
        raw = original[:closing] + b'date_added: 2026-01-02\n' + original[closing:]
        skill_path.write_bytes(raw)
        key_path = self.root / 'fixture-key'
        key_path.write_text('fixture-liquid', encoding='utf-8')
        os.environ.update(SKILL_HUB_ENABLE_MODEL_ROUTING='1', SKILL_HUB_MODEL_PROVIDER='liquid',
                          SKILL_HUB_KEY_PATH=str(key_path))
        self.server = serve_core.build_server(self.hub)
        self.assertEqual(skill_index.get('review-checklist', self.hub, materialize=False)['metadata']['date_added'],
                         '2026-01-02')
        read_skill, read_prompt = skill_index.get, skill_index.get_prompt
        skill_started, prompt_started = threading.Event(), threading.Event()
        send_count = 0

        def skill(name, hub, **kwargs):
            if threading.current_thread().name.startswith('route-card'):
                skill_started.set()
                self.assertTrue(prompt_started.wait(2))
            return read_skill(name, hub, **kwargs)

        def prompt(name, hub, **kwargs):
            if threading.current_thread().name.startswith('route-card'):
                prompt_started.set()
                self.assertTrue(skill_started.wait(2))
            return read_prompt(name, hub, **kwargs)

        def post(endpoint, send, body, headers, timeout):
            nonlocal send_count
            send_count += 1
            self.assertEqual(endpoint, elephant_shadow.LIQUID_ENDPOINT)
            self.assertEqual(headers['Authorization'], 'Bearer fixture-liquid')
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)
            request = json.loads(body)
            self.assertEqual(request['model'], 'd1:latest')
            return {'model': 'd1.2', 'answers': {
                key: {'type': 'noul', 'noul': .92 if key == 'skill:review-checklist' else .1}
                for key in request['questions']}, 'usage': {'input_tokens': 10}}, 200

        with patch.object(skill_index, 'get', side_effect=skill), \
             patch.object(skill_index, 'get_prompt', side_effect=prompt), \
             patch.object(elephant_shadow, '_systemone_post', side_effect=post):
            async with create_connected_server_and_client_session(self.server) as client:
                for mode in ('dynamic', 'baseline'):
                    if mode == 'baseline':
                        os.environ['SKILL_HUB_SCORE_MODE'] = mode
                    skill_started.clear()
                    prompt_started.clear()
                    reply = await client.call_tool('route_task', {'state': f'Review a synthetic change ({mode})'})
                    self.assertFalse(reply.isError, reply.content)
                    result = json.loads(reply.content[0].text)
                    self.assertEqual(result['outcome'], 'SELECTED', result)
                    self.assertEqual(result['retrieval']['scoring_budget']['mode'], mode)
                    self.assertNotIn('scoring_stages', result['retrieval'])
                    self.assertEqual(result['retrieval']['card_coverage']['derived'], 2)
                    self.assertEqual(result['provider']['requests'][0]['resolved_model'], 'd1.2')
                    if mode == 'dynamic':
                        self.assertEqual(result['loads']['loaded'], [])
                        self.assertEqual(result['retrieval']['adaptive_rounds'][0]['cap'], 24)
                        choice = result['choice_pool']['skills'][0]
                        args = {key: choice[key] for key in ('source_raw_sha256', 'bundle_sha256')}
                        args['resource_id'] = choice['id']
                        refused = await self.call('load_resource_choice',
                                                  {**args, 'source_raw_sha256': '0' * 64})
                        self.assertEqual(refused['error'], 'CONTENT_CHANGED')
                        load = await self.call('load_resource_choice', args)
                        self.assertTrue(load['load_verified'])
                    else:
                        load = result['loads']['loaded'][0]
                    self.assertEqual(load['body'].encode(), raw)
                    self.assertEqual(load['body_raw_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(send_count, 2)

    async def test_model_profile_rejects_unknown_provider(self):
        key_path = self.root / 'fixture-key'
        key_path.write_text('fixture', encoding='utf-8')
        os.environ.update(SKILL_HUB_ENABLE_MODEL_ROUTING='1', SKILL_HUB_MODEL_PROVIDER='unknown',
                          SKILL_HUB_KEY_PATH=str(key_path))
        with self.assertRaisesRegex(ValueError, 'SKILL_HUB_MODEL_PROVIDER'):
            serve_core.build_server(self.hub)

    async def test_liquid_rejects_unexpected_model_and_redirects(self):
        import elephant_shadow

        with patch.object(elephant_shadow, '_systemone_post',
                          return_value=({'model': 'd1.2-ultra'}, 200)):
            with self.assertRaisesRegex(elephant_shadow.ShadowError, 'unexpected Liquid model identity'):
                elephant_shadow._liquid_transport(b'{}', {}, 1)
        self.assertIsNone(elephant_shadow._NoRedirect().redirect_request(
            None, None, 307, 'Redirect', {}, 'https://example.invalid'))

    async def test_no_key_returns_large_skill_and_prompt_bytes_exactly(self):
        import hashlib

        cases = [('get_skill', 'review-checklist', self.hub / 'skills/review-checklist/SKILL.md'),
                 ('get_prompt', 'change-summary', self.hub / 'prompts/change-summary.md')]
        for tool, name, file in cases:
            raw = file.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            raw += ('Dữ liệu nguyên văn 🧪\r\n' * 12500).encode()
            file.write_bytes(raw)
            self.server = serve_core.build_server(self.hub)
            item = await self.call(tool, {'name': name})
            self.assertEqual(item['body'].encode(), raw)
            self.assertFalse(item['identity']['body_truncated'])
            self.assertEqual(item['file_sha256'], hashlib.sha256(raw).hexdigest())
            page = await self.call(tool, {'name': name, 'offset': 7, 'max_chars': 11})
            self.assertEqual(page['body'], raw.decode()[7:18])
            self.assertTrue(page['identity']['body_truncated'])


if __name__ == '__main__':
    unittest.main()
