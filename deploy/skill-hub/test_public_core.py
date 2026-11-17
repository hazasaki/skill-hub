import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
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
        with TestClient(self.server.streamable_http_app(), base_url='http://localhost') as client:
            response = client.get('/health')
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body['catalog']['active'], 1)
            self.assertEqual(body['catalog']['prompts'], 1)
            self.assertEqual(body['capabilities']['model_routing'], 'disabled')
            Path(os.environ['SKILL_HUB_INDEX_PATH']).write_bytes(b'corrupt')
            response = client.get('/health')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
