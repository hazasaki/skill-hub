"""Public local-catalog startup exercises the real MCP indexer and HTTP boundary."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/hub-dashboard'))
sys.path.append(str(ROOT / 'deploy/skill-hub/app'))
from container_entry import create_local_app
from server import Handler, ThreadingHTTPServer


class LocalDashboardTests(unittest.TestCase):
    def test_generated_graph_uses_authored_markdown_from_both_resource_types(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = ROOT / 'examples/hub'
            app = create_local_app(content, root / 'catalog.db', root / 'state')
            graph = app.get('graph', {'type': 'prompt'})['data']
            self.assertEqual(graph['total'], 1)
            self.assertIn('prompt:change-summary', {node['id'] for node in graph['nodes']})
            self.assertIn('cap:review', {node['id'] for node in graph['nodes'] if node['role'] == 'context'})
            self.assertTrue(any(edge['source'] == 'prompt:change-summary'
                                and edge['target'] == 'cap:review'
                                and edge['evidence']['source'] == 'authored:tags'
                                for edge in graph['edges']))
            focus = app.get('graph', {'node_id': 'prompt:change-summary', 'depth': '2'})['data']
            self.assertIn('skill:review-checklist', {node['id'] for node in focus['nodes']})
            self.assertEqual(app.sources.graph_projection.parent, root)
            stored = json.loads(app.sources.graph_projection.read_bytes())
            self.assertEqual(set(stored), {'nodes', 'edges', 'concepts', 'built_at', 'gaps'})

    def test_projection_rejects_symlink_even_when_content_hash_matches(self):
        import retrieval_projection

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = root / 'hub'
            skill = content / 'skills/sample'
            skill.mkdir(parents=True)
            doc = skill / 'SKILL.md'
            doc.write_text('---\nname: sample\ndescription: Example\ntags: testing\n---\nBody.', encoding='utf8')
            app = create_local_app(content, root / 'catalog.db', root / 'state')
            outside = doc.rename(root / 'outside.md')
            try:
                doc.symlink_to(outside)
            except OSError:
                self.skipTest('symlinks unavailable')
            document, _ = retrieval_projection.build(content, app.catalog)
            self.assertFalse(document['graph']['edges'])
            self.assertIn('unreadable_source', {gap['kind'] for gap in document['gaps']})

    def test_real_local_catalog_search_detail_and_empty_native_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = root / 'hub'
            skill = content / 'skills/sample'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: sample\ndescription: Portable example\n---\nA local searchable needle.', encoding='utf8')
            app = create_local_app(content, root / 'catalog.db', root / 'state')
            self.assertEqual(app.get('overview', {})['data']['counts']['skills'], 1)
            self.assertEqual(app.resources({'q': 'needle'})['items'][0]['id'], 'sample')
            self.assertIn('local searchable', app.resources({'kind': 'skill'}, 'sample')['item']['body'])
            self.assertFalse((content / 'state').exists(), 'read-only content must stay unmodified')
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.app = app
            app.public_port = 12345
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for host, expected in [('127.0.0.1:12345', 200), ('attacker.invalid:12345', 403)]:
                    with closing(http.client.HTTPConnection('127.0.0.1', server.server_port)) as client:
                        client.request('GET', '/api/v1/resources?kind=skill', headers={'Host': host})
                        response = client.getresponse()
                        self.assertEqual(response.status, expected)
                        response.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
