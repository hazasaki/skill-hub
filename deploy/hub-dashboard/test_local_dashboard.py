"""Public local-catalog startup exercises the real MCP indexer and HTTP boundary."""
from contextlib import closing
import http.client
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
