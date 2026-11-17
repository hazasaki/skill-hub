"""Fallback searches share the remaining route deadline, without hiding incomplete results."""
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'app'))
import elephant_runtime as runtime
import shortlist_runtime as shortlist
import skill_index


class FallbackDeadlineTests(unittest.TestCase):
    def test_expired_searches_are_explicitly_incomplete(self):
        result = shortlist.catalog_search(skill_index, None, 'test', deadline=time.monotonic() - 1)
        self.assertEqual(result['skills'], [])
        self.assertEqual(result['prompts'], [])
        self.assertTrue(result['partial'])
        self.assertEqual(result['search_status'], {'skill': 'TIMEOUT', 'prompt': 'TIMEOUT'})

    def test_completed_kind_survives_other_kind_timeout(self):
        class Catalog:
            def search(self, *args, **kwargs):
                return [{'name': 'completed'}]

            def search_prompts(self, *args, **kwargs):
                raise TimeoutError('deadline')

        result = shortlist.catalog_search(Catalog(), None, 'test', deadline=time.monotonic() + 1)
        self.assertEqual(result['skills'], [{'name': 'completed'}])
        self.assertEqual(result['search_status'], {'skill': 'OK', 'prompt': 'TIMEOUT'})
        self.assertTrue(result['partial'])

    def test_ordinary_search_errors_are_not_hidden_as_empty_results(self):
        class Catalog:
            def search(self, *args, **kwargs):
                raise sqlite3.OperationalError('schema is broken')

        with self.assertRaises(sqlite3.OperationalError):
            shortlist.catalog_search(Catalog(), None, 'test', deadline=time.monotonic() + 1)

    def test_sqlite_progress_interrupt_is_reported_as_timeout(self):
        class Catalog:
            def search(self, *args, **kwargs):
                con = sqlite3.connect(':memory:')
                try:
                    con.set_progress_handler(lambda: 1, 1)
                    con.execute('SELECT 1').fetchall()
                finally:
                    con.close()

            search_prompts = search

        result = shortlist.catalog_search(Catalog(), None, 'test', deadline=time.monotonic() + 1)
        self.assertEqual(result['search_status'], {'skill': 'TIMEOUT', 'prompt': 'TIMEOUT'})

    def test_search_refuses_expired_deadline(self):
        for name in ('search', 'search_prompts'):
            with self.subTest(name=name), self.assertRaises(TimeoutError):
                getattr(skill_index, name)('test', hub=None, deadline=time.monotonic() - 1)

    def test_real_fts_searches_are_interrupted_during_execution(self):
        for name, table, fts in (('search', 'skills', 'fts'), ('search_prompts', 'prompts', 'fts_prompts')):
            with self.subTest(name=name):
                con = sqlite3.connect(':memory:')
                con.row_factory = sqlite3.Row
                con.execute(f'CREATE TABLE {table}(name, description, tags, category, prompt_type, files, path, active)')
                con.execute(f'CREATE VIRTUAL TABLE {fts} USING fts5(name, description, tags, body)')
                con.executemany(f'INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)', [('test', 'test', '', '', 'task', 1, None, 1)] * 1000)
                con.executemany(f'INSERT INTO {fts} VALUES (?,?,?,?)', [('test', 'test', '', 'test')] * 1000)
                clock_calls = iter([0.0] + [2.0] * 100)
                with patch.object(skill_index, 'connect', return_value=con), \
                     patch.object(skill_index.time, 'monotonic', side_effect=lambda: next(clock_calls)):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        getattr(skill_index, name)('test', deadline=1.0)
                self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)

    def test_existing_catalog_search_shape_is_unchanged_without_deadline(self):
        class Catalog:
            def search(self, *args):
                return [{'name': 'skill'}]

            def search_prompts(self, *args):
                return [{'name': 'prompt'}]

        self.assertEqual(shortlist.catalog_search(Catalog(), None, 'test'), {
            'skills': [{'name': 'skill'}], 'prompts': [{'name': 'prompt'}], 'eligibility_applied': False})

    def test_search_without_deadline_keeps_existing_error_behavior(self):
        class Catalog:
            def search(self, *args):
                raise TimeoutError('transport failed')

        with self.assertRaises(TimeoutError):
            shortlist.catalog_search(Catalog(), None, 'test')

    def test_route_forwards_overall_deadline_to_fallback(self):
        entries = [{'type': 'skill', 'id': 'skill:test', 'resource_id': 'test', 'description': 'test', 'content_sha256': 'a' * 64}]
        calls = iter([0.0] + [19.0] * 30)
        with patch.object(runtime.shortlist, '_catalog_revision', return_value={}), \
             patch.object(runtime.shortlist, 'whole_catalog_candidates', return_value={'outcome': 'CANDIDATES', 'candidates': entries, 'candidate_count': 1}), \
             patch.object(runtime.preparation, 'indexed_ranks', return_value=({}, {'status': 'OK'})), \
             patch.object(runtime.preparation, 'prepare', return_value=(entries, {})), \
             patch.object(runtime.shortlist, 'catalog_search', return_value={}) as fallback, \
             patch.object(runtime, '_policy', return_value={'allowed': True}):
            result = runtime.run_multi(catalog_module=object(), hub=None, query={'text': 'test'}, transport=object(), monotonic=lambda: next(calls), use_cache=False)
        self.assertEqual(result['trigger'], 'DEADLINE')
        self.assertEqual(fallback.call_args.kwargs['deadline'], runtime.OVERALL_DEADLINE_SECONDS)


if __name__ == '__main__':
    unittest.main()
