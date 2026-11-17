"""Container entry point: existing read-only catalog, bounded HTTP MCP, health probe."""
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from starlette.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
import skill_mcp

hub = Path(os.environ['SKILL_HUB_ROOT'])
source_db = hub / 'state/skill-index.db'
db = Path(os.environ['SKILL_HUB_INDEX_PATH'])
if not source_db.is_file():
    raise SystemExit('Existing catalog required; automatic rebuild is disabled')
# Linux SQLite cannot share Windows byte-range locks. Copy a pinned, non-WAL
# catalog into bounded RAM; ordinary SQLite locking remains enabled there.
if any(Path(str(source_db) + suffix).exists() for suffix in ('-wal', '-journal')):
    raise SystemExit('Active journal requires a coherent snapshot before startup')
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
before = digest(source_db)
db.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source_db, db)
if digest(db) != before or digest(source_db) != before:
    raise SystemExit('Catalog changed during snapshot; restart with a stable source')
with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
    if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise SystemExit('Catalog snapshot integrity check failed')
publication_status = {'published_skills': 0, 'published_prompts': 0}
publication_dir = os.environ.get('SKILL_HUB_PUBLICATIONS_DIR')
if publication_dir:
    import publish_batch
    overlay = Path('/tmp/published-hub')
    publication_status = publish_batch.materialize_overlay(publication_dir, overlay, db, hub)
    if publication_status['published_skills'] or publication_status['published_prompts']:
        os.environ['SKILL_HUB_PUBLISHED_ROOT'] = str(overlay)
        # Retrieval artifacts remain pinned to the unmodified source catalog.
        mapping = json.loads(os.environ.get('SKILL_HUB_PATH_MAP', '{}'))
        os.environ['SKILL_HUB_PATH_MAP'] = json.dumps({k: str(source_db) if v == str(db) else v for k, v in mapping.items()})
effective_catalog_sha256 = digest(db)


async def semantic_lookup(query, limit):
    return json.loads((await semantic_response(query, limit)).body)


server = skill_mcp.build_server(hub, os.environ['SKILL_HUB_MATERIALIZE_ROOT'],
                                semantic_lookup=semantic_lookup)
_TRANSIENT_WARMUP_ERRORS = (
    'URLError', 'TimeoutError', 'ConnectionError', 'OSError', 'SemanticRetrievalError',
)
_WARMUP_BACKOFF_INITIAL_S = 2.0
_WARMUP_BACKOFF_MAX_S = 30.0
_warmup_lock = threading.Lock()
_warmup_in_flight = False
_warmup_next_retry_at = 0.0
_warmup_backoff_s = _WARMUP_BACKOFF_INITIAL_S
warmup_status = skill_mcp._warm_runtime_caches(hub)


def _is_transient_warmup_failure(status: dict) -> bool:
    errors = status.get('errors') or []
    return (not status.get('ok')) and any(
        marker in err for err in errors for marker in _TRANSIENT_WARMUP_ERRORS
    )


def _run_background_warmup():
    global warmup_status, _warmup_in_flight, _warmup_next_retry_at, _warmup_backoff_s
    try:
        result = skill_mcp._warm_runtime_caches(hub)
    except Exception as exc:
        result = {
            'ok': False,
            'errors': [f'warmup: {type(exc).__name__}: {exc}'],
        }
    with _warmup_lock:
        try:
            warmup_status = result
            if result.get('ok'):
                _warmup_backoff_s = _WARMUP_BACKOFF_INITIAL_S
                _warmup_next_retry_at = 0.0
            else:
                _warmup_next_retry_at = time.monotonic() + _warmup_backoff_s
                _warmup_backoff_s = min(_warmup_backoff_s * 2.0, _WARMUP_BACKOFF_MAX_S)
        finally:
            _warmup_in_flight = False


def _maybe_schedule_warmup_retry() -> dict:
    global _warmup_in_flight
    with _warmup_lock:
        current = warmup_status
        if (
            _is_transient_warmup_failure(current)
            and not _warmup_in_flight
            and time.monotonic() >= _warmup_next_retry_at
        ):
            _warmup_in_flight = True
            threading.Thread(
                target=_run_background_warmup,
                name='skill-hub-warmup-retry',
                daemon=True,
            ).start()
        return current
server.settings.host = '0.0.0.0'
server.settings.port = 8765
server.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=['127.0.0.1:*', 'localhost:*'],
    allowed_origins=['http://127.0.0.1:*', 'http://localhost:*'])
server.settings.max_sessions = 128
server.settings.session_idle_timeout = 300

@server.custom_route('/health', methods=['GET'])
async def health(request):
    try:
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
            count = con.execute('SELECT count(*) FROM skills').fetchone()[0]
        current_warmup = _maybe_schedule_warmup_retry()
        if not current_warmup.get('ok'):
            return JSONResponse({'status': 'degraded', 'catalog_skills': count, 'catalog_sha256': before,
                                 'effective_catalog_sha256': effective_catalog_sha256, 'publication': publication_status,
                                 'projection_scope': 'baseline snapshot; publications use FTS until rebuild',
                                 'broker_execution': 'disabled', 'warmup': current_warmup}, status_code=503)
        return JSONResponse({'status': 'ok', 'catalog_skills': count, 'catalog_sha256': before,
                             'effective_catalog_sha256': effective_catalog_sha256, 'publication': publication_status,
                                 'projection_scope': 'baseline snapshot; publications use FTS until rebuild',
                                 'broker_execution': 'disabled', 'warmup': current_warmup})
    except sqlite3.Error:
        return JSONResponse({'status': 'unavailable'}, status_code=503)

_SEMANTIC_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='semantic-worker')
_SEMANTIC_SEMAPHORE: Optional[asyncio.Semaphore] = None
_SEMANTIC_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None

def _get_semantic_semaphore() -> asyncio.Semaphore:
    global _SEMANTIC_SEMAPHORE, _SEMANTIC_SEMAPHORE_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if _SEMANTIC_SEMAPHORE is None or _SEMANTIC_SEMAPHORE_LOOP != current_loop:
        _SEMANTIC_SEMAPHORE = asyncio.Semaphore(1)
        _SEMANTIC_SEMAPHORE_LOOP = current_loop
    return _SEMANTIC_SEMAPHORE

_ROUTE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='route-worker')
_ROUTE_SEMAPHORE: Optional[asyncio.Semaphore] = None
_ROUTE_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None

def _get_route_semaphore() -> asyncio.Semaphore:
    global _ROUTE_SEMAPHORE, _ROUTE_SEMAPHORE_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if _ROUTE_SEMAPHORE is None or _ROUTE_SEMAPHORE_LOOP != current_loop:
        _ROUTE_SEMAPHORE = asyncio.Semaphore(1)
        _ROUTE_SEMAPHORE_LOOP = current_loop
    return _ROUTE_SEMAPHORE

@server.custom_route('/semantic', methods=['GET', 'POST'])
async def semantic_query(request):
    if request.method == 'POST':
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({'status': 'invalid', 'hits': []}, status_code=400)
        query = str(body.get('q') or body.get('query') or '')
        limit = body.get('limit', 10)
    else:
        query = str(request.query_params.get('q') or request.query_params.get('query') or '')
        limit = request.query_params.get('limit', 10)
    return await semantic_response(query, limit)


async def semantic_response(query, limit):
    try:
        current_warmup = _maybe_schedule_warmup_retry()
        if not current_warmup.get('ok'):
            return JSONResponse({'status': 'degraded', 'error': 'Semantic engine warmup incomplete or index unavailable',
                                 'warmup': current_warmup, 'hits': []}, status_code=503)
        query = query.strip()[:200]
        try:
            limit = max(1, min(int(limit), 50))
        except (ValueError, TypeError):
            limit = 10

        gpu_res = bool(current_warmup.get('gpu_resident', False))

        if not query:
            return JSONResponse({
                'status': 'ok',
                'query': '',
                'hits': [],
                'total': 0,
                'elapsed_ms': 0.0,
                'gpu_resident': gpu_res,
                'search_mode': 'semantic'
            })

        import retrieval_fusion as fusion
        configured = os.environ.get('SKILL_HUB_TEMPR_CONFIG') or (
            str(skill_mcp.LIVE_TEMPR_CONFIG) if hasattr(skill_mcp, 'LIVE_TEMPR_CONFIG') else None
        )
        if not configured:
            return JSONResponse({'status': 'unavailable', 'error': 'TEMPR config missing', 'hits': []}, status_code=503)

        import retrieval_semantic

        sem = _get_semantic_semaphore()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.5)
        except (asyncio.TimeoutError, TimeoutError):
            return JSONResponse({
                'status': 'busy',
                'error': 'Semantic search busy: admission limit reached',
                'query': query,
                'hits': [],
                'total': 0,
                'gpu_resident': gpu_res,
                'search_mode': 'semantic'
            }, status_code=503)

        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        deadline = time.monotonic() + 2.5
        submitted = False
        try:
            def _run_retrieve():
                try:
                    cfg = fusion.load_config(configured)
                    scoped = {key: cfg.get(key) or {} for key in ('catalog', 'semantic')}
                    pins = fusion.bind_artifacts(scoped)['artifacts']
                    if not all(block.get('sha256') and pins[key]['match'] for key, block in scoped.items()):
                        return {'status': 'UNAVAILABLE', 'reason': 'Semantic or catalog fingerprint mismatch'}
                    if not scoped['semantic'].get('path'):
                        return {'status': 'UNAVAILABLE', 'reason': 'Semantic index path missing'}
                    return retrieval_semantic.retrieve(query, index_path=scoped['semantic']['path'],
                        manifest=fusion._manifest_for(cfg, 'semantic'), limit=limit, deadline=deadline)
                finally:
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(sem.release)

            fut = loop.run_in_executor(_SEMANTIC_EXECUTOR, _run_retrieve)
            submitted = True
            res = await fut
        finally:
            if not submitted:
                sem.release()

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        ret_status = str(res.get('status', 'OK')).upper()
        reason = res.get('reason')
        raw_hits = res.get('hits', [])

        if ret_status in ('TIMEOUT', 'ERROR', 'UNAVAILABLE'):
            status_code = 504 if ret_status == 'TIMEOUT' else (503 if ret_status == 'UNAVAILABLE' else 500)
            return JSONResponse({
                'status': ret_status.lower(),
                'error': reason or f'Semantic retrieval {ret_status.lower()}',
                'query': query,
                'hits': [],
                'total': 0,
                'elapsed_ms': elapsed_ms,
                'model': retrieval_semantic.DEFAULT_MODEL,
                'gpu_resident': gpu_res,
                'search_mode': 'semantic'
            }, status_code=status_code)

        hits = []
        for h in raw_hits[:limit]:
            if isinstance(h, dict):
                hits.append({
                    'id': str(h.get('id', '')),
                    'rank': int(h.get('rank', 0)) if isinstance(h.get('rank'), (int, float)) else 0,
                    'raw_score': float(h.get('raw_score', 0.0)) if isinstance(h.get('raw_score'), (int, float)) else 0.0,
                    'evidence': h.get('evidence') if isinstance(h.get('evidence'), dict) else {}
                })
        return JSONResponse({
            'status': 'ok',
            'query': query,
            'hits': hits,
            'total': len(hits),
            'elapsed_ms': elapsed_ms,
            'model': retrieval_semantic.DEFAULT_MODEL,
            'gpu_resident': gpu_res,
            'search_mode': 'semantic',
            'reason': reason
        })
    except Exception as exc:
        return JSONResponse({'status': 'error', 'error': f'{type(exc).__name__}: {exc}', 'hits': []}, status_code=500)

@server.custom_route('/route_task', methods=['POST'])
async def route_task_endpoint(request):
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        raw_state = body.get('state')
        if not isinstance(raw_state, str) or not raw_state.strip():
            return JSONResponse({
                "status": "error",
                "error": "INVALID_STATE",
                "detail": "state must be a non-empty string"
            }, status_code=400)
        state = raw_state.strip()[:4000]

        retrieval_mode = body.get('retrieval_mode', 'tempr')
        if retrieval_mode not in ('tempr', 'metadata'):
            return JSONResponse({
                "status": "error",
                "error": "INVALID_RETRIEVAL_MODE",
                "detail": "retrieval_mode must be tempr or metadata"
            }, status_code=400)

        include_graph = bool(body.get('include_graph', False))
        task_id = str(body.get('task_id') or '').strip()[:120] or None

        current_warmup = _maybe_schedule_warmup_retry()
        gpu_resident = bool(current_warmup.get('gpu_resident', False))

        sem = _get_route_semaphore()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.5)
        except (asyncio.TimeoutError, TimeoutError):
            return JSONResponse({
                "status": "busy",
                "outcome": "ROUTER_BUSY",
                "error": "route_task busy: admission limit reached",
                "selected": [],
                "loads": {"skills": [], "prompts": [], "deferred": []}
            }, status_code=503)

        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        submitted = False
        try:
            def _run_route():
                try:
                    tool_fn = server._tool_manager._tools['route_task'].fn
                    return tool_fn(
                        state=state,
                        retrieval_mode=retrieval_mode,
                        include_graph=include_graph,
                        task_id=task_id
                    )
                finally:
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(sem.release)

            fut = loop.run_in_executor(_ROUTE_EXECUTOR, _run_route)
            submitted = True
            raw_json = await fut
        finally:
            if not submitted:
                sem.release()

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        try:
            payload = json.loads(raw_json)
        except Exception as e:
            payload = {'error': f'Invalid JSON returned by route_task: {e}', 'raw': str(raw_json)[:500]}

        payload['elapsed_ms'] = elapsed_ms
        payload['gpu_resident'] = gpu_resident

        if payload.get('error'):
            payload['status'] = 'error'
            err_str = (str(payload.get('error')) + ' ' + str(payload.get('detail', ''))).upper()
            if payload.get('error') == 'INVALID_RETRIEVAL_MODE':
                status_code = 400
            elif any(k in err_str for k in ('MISSING_API_KEY', 'CONFIG_MISSING', 'UNAVAILABLE', 'CREDENTIALS', 'MISSING')):
                status_code = 503
            else:
                status_code = 502
            return JSONResponse(payload, status_code=status_code)

        payload['status'] = 'ok'
        return JSONResponse(payload, status_code=200)
    except Exception as exc:
        return JSONResponse({'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}, status_code=500)

if __name__ == '__main__':
    server.run(transport='streamable-http')
