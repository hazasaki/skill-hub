"""Public installs have portable defaults and no implicit host-tool access."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parent / 'app'
sys.path.insert(0, str(APP))
import mcp_hub_broker as broker
import shared_resources


class PublicDefaultsTests(unittest.TestCase):
    def test_broker_requires_explicit_local_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(broker.server_configs(), {})
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'servers.json'
            expected = {'example': {'transport': 'stdio', 'command': 'example-mcp'}}
            config.write_text(json.dumps(expected), encoding='utf-8')
            with patch.dict(os.environ, {'SKILL_HUB_BROKER_CONFIG': str(config)}):
                self.assertEqual(broker.server_configs(), expected)
                for invalid in ([], {'x': {}}, {'x': {'transport': 'stdio', 'command': ''}},
                                {'x': {'transport': 'http', 'url': 42}}):
                    config.write_text(json.dumps(invalid), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        broker.server_configs()

    def test_runtime_imports_without_private_paths_or_host_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith('SKILL_HUB_')}
            env['SKILL_HUB_ROOT'] = str(Path(tmp) / 'hub')
            subprocess.run([sys.executable, '-B', '-c',
                            'import skill_mcp, shared_resources, catalog_maintenance; '
                            'assert shared_resources.DEFAULT_HUB == skill_mcp.si.DEFAULT_HUB; '
                            'assert catalog_maintenance.LIVE_DB_CANONICAL == '
                            '(shared_resources.DEFAULT_HUB / "state/skill-index.db").resolve()'],
                           cwd=APP, env=env, check=True, timeout=20)

    def test_atomic_resource_replacement_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'resource.md'
            target.write_text('before', encoding='utf-8')
            if os.name == 'nt':
                from ctypes import wintypes
                api = ctypes.WinDLL('advapi32', use_last_error=True)
                get = api.GetFileSecurityW
                get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
                get.restype = wintypes.BOOL

                def permissions():
                    needed = wintypes.DWORD()
                    get(str(target), 4, None, 0, ctypes.byref(needed))
                    self.assertGreater(needed.value, 0)
                    descriptor = ctypes.create_string_buffer(needed.value)
                    self.assertTrue(get(str(target), 4, descriptor, needed.value,
                                        ctypes.byref(needed)))
                    return descriptor.raw
            else:
                target.chmod(0o600)

                def permissions():
                    return target.stat().st_mode & 0o777

            before = permissions()
            shared_resources.write_text(target, 'after')
            self.assertEqual(target.read_text(encoding='utf-8'), 'after')
            self.assertEqual(permissions(), before)


if __name__ == '__main__':
    unittest.main()
