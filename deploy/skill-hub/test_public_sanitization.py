"""Public intake identity derives from local input instead of embedded operator state."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('SKILL_HUB_ROOT', str(ROOT.parents[1] / 'examples/hub'))
from github_intake import GitHubIntakeScanner, format_scan_report


class PublicIntakeIdentityTests(unittest.TestCase):
    def test_catalog_identity_uses_selected_database_and_each_scan_has_own_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'catalog.db'
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE skill_resources(name, content_sha256, bundle_sha256, active, risk)')
                con.execute('CREATE TABLE prompt_resources(name, content_sha256, body_sha256, active, risk)')
                con.commit()
            scanner = GitHubIntakeScanner(db, root / 'staging')
            first = scanner.build_intake_manifest([], 'local fixture')
            second = scanner.build_intake_manifest([], 'local fixture')
            self.assertEqual(first['catalog_baseline']['catalog_pin_hash'], hashlib.sha256(db.read_bytes()).hexdigest())
            self.assertNotEqual(first['task_id'], second['task_id'])
            uuid.UUID(first['task_id'])
            self.assertIn(first['catalog_baseline']['catalog_pin_hash'], format_scan_report(first))
            with closing(sqlite3.connect(db)) as con:
                con.execute('PRAGMA user_version=1')
            changed = GitHubIntakeScanner(db, root / 'staging').build_intake_manifest([], 'changed fixture')
            self.assertNotEqual(first['catalog_baseline']['catalog_pin_hash'], changed['catalog_baseline']['catalog_pin_hash'])

    def test_absent_catalog_identity_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scanner = GitHubIntakeScanner(root / 'absent.db', root / 'staging')
            manifest = scanner.build_intake_manifest([], 'missing local catalog')
            self.assertIsNone(manifest['catalog_baseline']['catalog_pin_hash'])
            self.assertIn('unavailable', format_scan_report(manifest))
            self.assertNotIn('hermes', json.dumps(manifest).lower())


if __name__ == '__main__':
    unittest.main()
