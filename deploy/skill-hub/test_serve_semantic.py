"""Tests for serve.py semantic route:
- GPU observation false but query-ready bounded shared backend
- Returned retrieval error/timeout honored and not masked as ok
- Slow semantic query keeps health responsive (event-loop offload) and bounded admission queue
- Real shared backend chain with mocked Ollama /api/ps partial offload (size_vram < size)
- Rejection of wrong digest and excessive host RAM
- Admission survives handler cancellation (held until executor thread actually finishes)
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Configure isolated environment before importing serve
TEMP_DIR = tempfile.mkdtemp(prefix='test_serve_semantic_')
HUB = Path(TEMP_DIR) / 'hub'
HUB.mkdir(parents=True, exist_ok=True)
STATE = HUB / 'state'
STATE.mkdir(parents=True, exist_ok=True)
SDB = STATE / 'skill-index.db'
with sqlite3.connect(SDB) as con:
    con.execute('CREATE TABLE skills (name TEXT)')
    con.execute("INSERT INTO skills VALUES ('test-skill')")
    con.commit()

TEST_ENV = {'SKILL_HUB_ROOT': str(HUB), 'SKILL_HUB_INDEX_PATH': str(HUB / 'live.db'),
            'SKILL_HUB_MATERIALIZE_ROOT': str(HUB / 'mat'), 'SKILL_HUB_DISABLE_BROKER': '1'}

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / 'agent-memory'))
sys.path.insert(0, str(HERE / 'app'))
sys.path.insert(0, str(HERE))

with patch.dict(os.environ, TEST_ENV):
    import serve
import httpx


class ServeSemanticEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(patch.dict(os.environ, TEST_ENV))
        cls.app = serve.server.streamable_http_app()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    def test_semantic_query_gpu_observation_false_but_query_ready(self):
        """When warmup ok is True but gpu_resident is False, query is ready and reports gpu_resident=False."""
        mock_warmup = {
            'ok': True,
            'gpu_resident': False,
            'catalog_candidates': 10,
            'semantic_vectors': 100,
            'graph_loaded': False,
            'temporal_loaded': False,
            'tempr_configured': True,
            'errors': []
        }
        mock_retrieve_res = {
            'engine': 'semantic',
            'status': 'OK',
            'hits': [{'id': 'skill:search-helper', 'rank': 1, 'raw_score': 0.88, 'evidence': {'chunk_index': 0}}],
            'elapsed_ms': 15.2,
            'reason': None
        }

        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', return_value=mock_retrieve_res):

                    resp = await client.get('/semantic?q=helper&limit=5')
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    self.assertEqual(data['status'], 'ok')
                    self.assertFalse(data['gpu_resident'])  # Truthful observed GPU residency
                    self.assertEqual(len(data['hits']), 1)
                    self.assertEqual(data['hits'][0]['id'], 'skill:search-helper')

        asyncio.run(run())

    def test_semantic_query_returns_truthful_timeout_and_error(self):
        """When retrieval returns TIMEOUT or ERROR, response returns 504/500 and is not masked as ok."""
        mock_warmup = {'ok': True, 'gpu_resident': True}

        # 1) Timeout
        timeout_res = {
            'engine': 'semantic',
            'status': 'TIMEOUT',
            'hits': [],
            'elapsed_ms': 2500.0,
            'reason': 'Deadline exceeded before retrieval started'
        }
        async def run_timeout():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', return_value=timeout_res):

                    resp = await client.get('/semantic?q=timeout_query')
                    self.assertEqual(resp.status_code, 504)
                    data = resp.json()
                    self.assertEqual(data['status'], 'timeout')
                    self.assertEqual(data['error'], 'Deadline exceeded before retrieval started')
                    self.assertEqual(data['hits'], [])
                    self.assertTrue(data['gpu_resident'])

        asyncio.run(run_timeout())

        # 2) Error
        error_res = {
            'engine': 'semantic',
            'status': 'ERROR',
            'hits': [],
            'elapsed_ms': 5.0,
            'reason': 'Index file damaged'
        }
        async def run_error():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', return_value=error_res):

                    resp = await client.get('/semantic?q=error_query')
                    self.assertEqual(resp.status_code, 500)
                    data = resp.json()
                    self.assertEqual(data['status'], 'error')
                    self.assertEqual(data['error'], 'Index file damaged')
                    self.assertEqual(data['hits'], [])

        asyncio.run(run_error())

    def test_slow_semantic_query_keeps_health_responsive(self):
        """A slow retrieval executing in executor does not block concurrent health checks."""
        mock_warmup = {'ok': True, 'gpu_resident': True}

        def slow_retrieve(*args, **kwargs):
            time.sleep(0.4)
            return {'engine': 'semantic', 'status': 'OK', 'hits': [], 'reason': None}

        async def run_async():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', side_effect=slow_retrieve):

                    task1 = asyncio.create_task(client.get('/semantic?q=slow'))
                    await asyncio.sleep(0.05)

                    t_health0 = time.perf_counter()
                    health_resp = await client.get('/health')
                    health_elapsed = time.perf_counter() - t_health0

                    self.assertEqual(health_resp.status_code, 200)
                    self.assertLess(health_elapsed, 0.25, f'Health probe was blocked by slow query! took {health_elapsed:.3f}s')

                    slow_resp = await task1
                    self.assertEqual(slow_resp.status_code, 200)

        asyncio.run(run_async())

    def test_concurrent_admission_limit_busy_rejection(self):
        """When another query holds the admission semaphore beyond timeout, subsequent query returns 503 busy."""
        mock_warmup = {'ok': True, 'gpu_resident': True}

        def very_slow_retrieve(*args, **kwargs):
            time.sleep(0.8)
            return {'engine': 'semantic', 'status': 'OK', 'hits': [], 'reason': None}

        async def run_async():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', side_effect=very_slow_retrieve):

                    task1 = asyncio.create_task(client.get('/semantic?q=slow_holder'))
                    await asyncio.sleep(0.05)

                    overflow_resp = await client.get('/semantic?q=overflow')
                    self.assertEqual(overflow_resp.status_code, 503)
                    data = overflow_resp.json()
                    self.assertEqual(data['status'], 'busy')
                    self.assertIn('admission limit reached', data['error'])

                    res1 = await task1
                    self.assertEqual(res1.status_code, 200)

        asyncio.run(run_async())

    def test_warmup_refuses_changed_index_before_loading_vectors(self):
        cfg = {'configured': True, 'semantic': {'path': 'wrong.idx', 'sha256': 'pinned'}}
        with patch.dict(os.environ, {'SKILL_HUB_TEMPR_CONFIG': 'fixture'}), \
             patch('shortlist_runtime.load_catalog', return_value=(None, HUB)), \
             patch('shortlist_runtime.whole_catalog_candidates', return_value=[]), \
             patch('retrieval_fusion.load_config', return_value=cfg), \
             patch('retrieval_fusion.bind_artifacts', return_value={'artifacts': {'semantic': {'match': False}}}), \
             patch('retrieval_semantic.get_semantic_index') as loader:
            status = serve.skill_mcp._warm_runtime_caches(HUB)
        loader.assert_not_called()
        self.assertFalse(status['ok'])
        self.assertIn('fingerprint mismatch: semantic', status['errors'][0])

    def test_real_shared_backend_chain_with_mocked_ollama_partial_offload(self):
        """Realistic Ollama /api/ps row with size_vram < size succeeds through the full real warmup, embedding, and semantic query chain."""
        import urllib.request
        import retrieval_semantic
        import skill_mcp

        partial_ps_row = {
            'name': 'bge-m3:latest',
            'model': 'bge-m3:latest',
            'size': 689187061,
            'size_vram': 400000000,
            'digest': retrieval_semantic.DEFAULT_DIGEST,
        }

        class FakeOllamaResp:
            def __init__(self, data):
                self._body = json.dumps(data).encode('utf-8')
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, 'full_url') else req
            if '/api/ps' in url:
                return FakeOllamaResp({'models': [partial_ps_row]})
            if '/api/embed' in url:
                return FakeOllamaResp({'embeddings': [[0.05] * 1024]})
            raise AssertionError(f'Unexpected URL in test: {url}')

        with patch.object(urllib.request, 'urlopen', side_effect=fake_urlopen):
            # 1. Real ensure_gpu_resident check
            res_meta = retrieval_semantic.ensure_gpu_resident('http://fake-ollama:11434', 'bge-m3:latest', 5.0)
            self.assertTrue(res_meta['ready'])
            self.assertFalse(res_meta['gpu_resident'])  # Truthfully reflects partial offload
            self.assertEqual(res_meta['ram_estimate'], 689187061 - 400000000)

            # 2. Real embed_bge_m3 call
            vec = retrieval_semantic.embed_bge_m3('test query', endpoint='http://fake-ollama:11434', timeout=5.0)
            self.assertEqual(len(vec), 1024)

            # 3. Real _warm_runtime_caches check
            with patch('shortlist_runtime.load_catalog', return_value=('dummy_catalog', HUB)), \
                 patch('shortlist_runtime.whole_catalog_candidates', return_value={'candidate_count': 1}), \
                 patch('retrieval_fusion.load_config', return_value={'configured': True, 'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                 patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                 patch('retrieval_semantic.get_semantic_index') as mock_gsi:
                mock_idx = MagicMock()
                mock_idx.load_vectors.return_value = (None, [1, 2, 3])
                mock_gsi.return_value = mock_idx
                warm_status = skill_mcp._warm_runtime_caches(HUB)
                self.assertTrue(warm_status['ok'])
                self.assertFalse(warm_status['gpu_resident'])
                self.assertEqual(warm_status['errors'], [])

    def test_real_shared_backend_chain_rejects_wrong_digest_and_excessive_ram(self):
        """Model with wrong digest or excessive RAM beyond conservative budget must be rejected."""
        import urllib.request
        import retrieval_semantic

        class FakeOllamaResp:
            def __init__(self, data):
                self._body = json.dumps(data).encode('utf-8')
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        # Case 1: Wrong digest
        wrong_digest_row = {
            'name': 'bge-m3:latest',
            'model': 'bge-m3:latest',
            'size': 689187061,
            'size_vram': 400000000,
            'digest': '0000000000000000000000000000000000000000000000000000000000000000',
        }
        with patch.object(urllib.request, 'urlopen', return_value=FakeOllamaResp({'models': [wrong_digest_row]})):
            with self.assertRaises(retrieval_semantic.SemanticRetrievalError) as ctx:
                retrieval_semantic.ensure_gpu_resident('http://fake-ollama:11434', 'bge-m3:latest', 5.0)
            self.assertIn('WRONG_DIGEST', str(ctx.exception))

        # Case 2: Excessive host RAM (5GB total, 500MB on VRAM -> 4.5GB host RAM exceeds 1GB budget)
        excessive_ram_row = {
            'name': 'bge-m3:latest',
            'model': 'bge-m3:latest',
            'size': 5000000000,
            'size_vram': 500000000,
            'digest': retrieval_semantic.DEFAULT_DIGEST,
        }
        with patch.object(urllib.request, 'urlopen', return_value=FakeOllamaResp({'models': [excessive_ram_row]})):
            with self.assertRaises(retrieval_semantic.SemanticRetrievalError) as ctx:
                retrieval_semantic.ensure_gpu_resident('http://fake-ollama:11434', 'bge-m3:latest', 5.0)
            self.assertIn('RESOURCE_EXCEEDED', str(ctx.exception))

    def test_cancelled_slow_worker_holds_admission_until_worker_done(self):
        """When an in-flight request handler is cancelled, the admission semaphore remains held until the executor thread completes."""
        mock_warmup = {'ok': True, 'gpu_resident': True}
        thread_done = threading.Event()

        def slow_retrieve(*args, **kwargs):
            try:
                time.sleep(1.0)
                return {'engine': 'semantic', 'status': 'OK', 'hits': [], 'reason': None}
            finally:
                thread_done.set()

        async def run_async_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch('retrieval_fusion.load_config', return_value={'semantic': {'path': 'fake.idx', 'sha256':'pinned'}, 'catalog': {'sha256':'pinned'}}), \
                     patch('retrieval_fusion.bind_artifacts', return_value={'artifacts':{'catalog':{'match':True},'semantic':{'match':True}}}), \
                     patch('retrieval_fusion._manifest_for', return_value={}), \
                     patch('retrieval_semantic.retrieve', side_effect=slow_retrieve):

                    # 1. Launch slow task with short timeout (simulating client cancellation)
                    try:
                        await asyncio.wait_for(client.get('/semantic?q=slow_cancel'), timeout=0.1)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass

                    # 2. Worker thread is still sleeping in executor
                    self.assertFalse(thread_done.is_set(), 'Worker thread should still be running')

                    # 3. Immediately launch another request while worker is running
                    resp = await client.get('/semantic?q=concurrent_during_cancelled')
                    self.assertEqual(resp.status_code, 503)
                    self.assertEqual(resp.json()['status'], 'busy')

                    # 4. Wait for background worker to complete
                    thread_done.wait(timeout=2.0)
                    self.assertTrue(thread_done.is_set())
                    await asyncio.sleep(0.05)

                    # 5. Now that thread is finished, new request succeeds
                    resp2 = await client.get('/semantic?q=after_worker_finished')
                    self.assertEqual(resp2.status_code, 200)

        asyncio.run(run_async_test())

    def test_route_task_200_ok(self):
        """POST /route_task returns 200 with status ok and all router payload fields merged."""
        mock_route_result = {
            'schema_version': 'openbot.elephant.route_task.v2',
            'outcome': 'SELECTED',
            'trigger': 'EXPLICIT',
            'reason': 'High relevance to git tools',
            'selected': ['skill:git-commit'],
            'loads': {
                'skills': [{'id': 'skill:git-commit', 'name': 'git-commit', 'body': '# Git Commit'}],
                'prompts': [],
                'deferred': []
            },
            'scores': [{'id': 'skill:git-commit', 'score': 0.92}],
            'coverage': {'scored_candidates': 10, 'retrieved_candidates': 10, 'catalog_total': 9533},
            'retrieval': {'mode': 'tempr', 'semantic': 5, 'keyword': 5, 'graph': 0, 'temporal': 0},
            'provider': {'model': 'convaiinnovations/laya-typed-decisions', 'duration_ms': 120.0, 'cost_usd': None},
            'telemetry': {'route_id': 'test-route-123'}
        }
        mock_warmup = {'ok': True, 'gpu_resident': True}

        async def run_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch.dict(serve.server._tool_manager._tools, {'route_task': MagicMock(fn=lambda **kw: json.dumps(mock_route_result))}):

                    resp = await client.post('/route_task', json={
                        'state': 'inspect git repository',
                        'retrieval_mode': 'tempr',
                        'include_graph': True,
                        'task_id': 'test-task-1'
                    })
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    self.assertEqual(data['status'], 'ok')
                    self.assertEqual(data['outcome'], 'SELECTED')
                    self.assertEqual(data['selected'], ['skill:git-commit'])
                    self.assertEqual(len(data['loads']['skills']), 1)
                    self.assertTrue(data['gpu_resident'])
                    self.assertIn('elapsed_ms', data)

        asyncio.run(run_test())

    def test_route_task_validation_errors(self):
        """POST /route_task rejects missing/empty state and invalid retrieval_mode with 400."""
        async def run_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                # 1. Missing state
                resp1 = await client.post('/route_task', json={'retrieval_mode': 'tempr'})
                self.assertEqual(resp1.status_code, 400)
                self.assertEqual(resp1.json()['error'], 'INVALID_STATE')

                # 2. Empty state
                resp2 = await client.post('/route_task', json={'state': '   '})
                self.assertEqual(resp2.status_code, 400)
                self.assertEqual(resp2.json()['error'], 'INVALID_STATE')

                # 3. Invalid retrieval_mode
                resp3 = await client.post('/route_task', json={'state': 'valid state', 'retrieval_mode': 'invalid_mode'})
                self.assertEqual(resp3.status_code, 400)
                self.assertEqual(resp3.json()['error'], 'INVALID_RETRIEVAL_MODE')

                # 4. Router tool error returns 502/503
                mock_tool_err = {'error': 'CONFIG_MISSING', 'detail': 'Missing decision model credentials'}
                with patch.dict(serve.server._tool_manager._tools, {'route_task': MagicMock(fn=lambda **kw: json.dumps(mock_tool_err))}):
                    resp4 = await client.post('/route_task', json={'state': 'valid state'})
                    self.assertEqual(resp4.status_code, 503)
                    self.assertEqual(resp4.json()['status'], 'error')

        asyncio.run(run_test())

    def test_route_task_busy_admission(self):
        """When an in-flight route_task holds semaphore beyond timeout, concurrent request returns 503 busy."""
        mock_warmup = {'ok': True, 'gpu_resident': True}
        mock_route_result = {'outcome': 'SELECTED', 'selected': []}

        def slow_route(**kw):
            time.sleep(0.8)
            return json.dumps(mock_route_result)

        async def run_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch.dict(serve.server._tool_manager._tools, {'route_task': MagicMock(fn=slow_route)}):

                    task1 = asyncio.create_task(client.post('/route_task', json={'state': 'slow state 1'}))
                    await asyncio.sleep(0.05)

                    resp2 = await client.post('/route_task', json={'state': 'overflow state'})
                    self.assertEqual(resp2.status_code, 503)
                    data = resp2.json()
                    self.assertEqual(data['status'], 'busy')
                    self.assertEqual(data['outcome'], 'ROUTER_BUSY')
                    self.assertIn('admission limit reached', data['error'])

                    resp1 = await task1
                    self.assertEqual(resp1.status_code, 200)

        asyncio.run(run_test())

    def test_route_task_cancellation_holds_semaphore_until_worker_done(self):
        """When route_task handler is cancelled, semaphore remains held until executor thread completes."""
        mock_warmup = {'ok': True, 'gpu_resident': True}
        thread_done = threading.Event()
        mock_route_result = {'outcome': 'SELECTED', 'selected': []}

        def slow_route(**kw):
            try:
                time.sleep(1.0)
                return json.dumps(mock_route_result)
            finally:
                thread_done.set()

        async def run_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch.dict(serve.server._tool_manager._tools, {'route_task': MagicMock(fn=slow_route)}):

                    try:
                        await asyncio.wait_for(client.post('/route_task', json={'state': 'cancelled task'}), timeout=0.1)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass

                    self.assertFalse(thread_done.is_set(), 'Worker thread should still be running')

                    resp = await client.post('/route_task', json={'state': 'concurrent during cancelled'})
                    self.assertEqual(resp.status_code, 503)
                    self.assertEqual(resp.json()['status'], 'busy')

                    thread_done.wait(timeout=2.0)
                    self.assertTrue(thread_done.is_set())
                    await asyncio.sleep(0.05)

                    resp2 = await client.post('/route_task', json={'state': 'after worker finished'})
                    self.assertEqual(resp2.status_code, 200)

        asyncio.run(run_test())

    def test_slow_route_task_keeps_health_responsive(self):
        """A slow route_task executing in executor does not block concurrent health checks."""
        mock_warmup = {'ok': True, 'gpu_resident': True}

        def slow_route(**kw):
            time.sleep(0.4)
            return json.dumps({'outcome': 'SELECTED', 'selected': []})

        async def run_test():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                with patch.object(serve, '_maybe_schedule_warmup_retry', return_value=mock_warmup), \
                     patch.dict(serve.server._tool_manager._tools, {'route_task': MagicMock(fn=slow_route)}):

                    task1 = asyncio.create_task(client.post('/route_task', json={'state': 'slow task'}))
                    await asyncio.sleep(0.05)

                    t_health0 = time.perf_counter()
                    health_resp = await client.get('/health')
                    health_elapsed = time.perf_counter() - t_health0

                    self.assertEqual(health_resp.status_code, 200)
                    self.assertLess(health_elapsed, 0.25, f'Health probe was blocked! took {health_elapsed:.3f}s')

                    resp1 = await task1
                    self.assertEqual(resp1.status_code, 200)

        asyncio.run(run_test())


if __name__ == '__main__':
    unittest.main()
