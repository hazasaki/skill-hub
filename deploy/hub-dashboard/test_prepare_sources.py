import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from prepare_sources import aggregate_audit, snapshot_catalog, write_bundle
from container_entry import validate_inputs
from attach_reviews import attach_reviews


class PrepareSourcesTests(unittest.TestCase):
    def test_decisions_receipt_is_pinned_and_preserved_when_reattached(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'catalog.db'
            with closing(sqlite3.connect(source)) as db: db.execute('CREATE TABLE fixture(value INTEGER)')
            original=root/'original'; write_bundle(original,{}, {'cases':1},source)
            manifest=json.loads((original/'manifest.json').read_text('utf8')); pin=manifest['sha256']['skill-index.db']
            quality=root/'quality.json'; quality.write_text(json.dumps({'index_sha256':pin}),encoding='utf8')
            security=root/'security.json'; security.write_text(json.dumps({'catalog_sha256':pin}),encoding='utf8')
            decisions=root/'decisions.json'; decisions.write_text(json.dumps({'schema':'skill-hub.original-decisions.v1','entries':{}}),encoding='utf8')
            first=root/'first'; attach_reviews(original,first,quality,security,decisions)
            validate_inputs(first)
            second=root/'second'; attach_reviews(first,second,quality,security)
            self.assertEqual((second/'original-decisions.json').read_bytes(),decisions.read_bytes())
            (second/'original-decisions.json').write_text('{}',encoding='utf8')
            with self.assertRaisesRegex(ValueError,'hash mismatch'): validate_inputs(second)

    def test_startup_rejects_changed_native_snapshot_and_raw_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'catalog.db'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE fixture(value INTEGER)')
            destination = Path(folder) / 'valid'
            write_bundle(destination, {'schema': 'fixture'}, {'cases': 20}, source)
            validate_inputs(destination)
            (destination / 'native.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                validate_inputs(destination)
            raw = Path(folder) / 'raw'
            write_bundle(raw, {}, {'cases': ['private fixture']}, source)
            with self.assertRaisesRegex(ValueError, 'Nested evaluation'):
                validate_inputs(raw)

    def test_consistent_catalog_backup_preserves_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.db', Path(folder) / 'copy.db'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE fixture(value INTEGER)')
                db.execute('INSERT INTO fixture VALUES (42)')
                db.commit()
            before = source.read_bytes()
            snapshot_catalog(source, target)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT value FROM fixture').fetchone()[0], 42)
            self.assertEqual(source.read_bytes(), before)

    def test_raw_cases_and_nested_values_do_not_cross_mount_boundary(self):
        result = aggregate_audit({'cases': ['private fixture'], 'selection_recall': 0.8,
                                  'gold': 'private fixture', 'timing_ms': {'p95_frozen': 1000,
                                                                         'raw': ['private']}})
        self.assertNotIn('cases', result)
        self.assertNotIn('gold', result)
        self.assertEqual(result['timing_ms'], {'p95_frozen': 1000})
        self.assertEqual(result['selection_recall'], 0.8)

    def test_versioned_bundle_integrity_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'version-1'
            write_bundle(destination, {'schema': 'fixture'}, {'cases': 20})
            manifest = json.loads((destination / 'manifest.json').read_text('utf-8'))
            for name, expected in manifest['sha256'].items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), expected)
            self.assertEqual(json.loads((destination / 'AUDIT.json').read_text('utf-8')), {'cases': 20})
            with self.assertRaises(FileExistsError):
                write_bundle(destination, {}, {})


if __name__ == '__main__':
    unittest.main()
