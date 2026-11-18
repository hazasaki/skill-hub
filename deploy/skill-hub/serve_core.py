"""Standalone MCP service over a local, read-only content directory."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

sys.path.insert(0, str(Path(__file__).parent / 'app'))
import skill_index as index
import skill_mcp


def build_server(hub=None):
    hub = Path(hub or os.environ['SKILL_HUB_ROOT'])
    model_enabled = os.environ.get('SKILL_HUB_ENABLE_MODEL_ROUTING') == '1'
    key_file = os.environ.get('SKILL_HUB_KEY_PATH') if model_enabled else None
    if model_enabled and (not key_file or not Path(key_file).is_file()):
        raise ValueError('Model routing requires an explicit SKILL_HUB_KEY_PATH file.')
    provider = os.environ.get('SKILL_HUB_MODEL_PROVIDER', 'baseten')
    if model_enabled and provider not in ('baseten', 'liquid'):
        raise ValueError('SKILL_HUB_MODEL_PROVIDER must be baseten or liquid.')
    index.build(hub)
    server = skill_mcp.build_server(
        hub, os.environ.get('SKILL_HUB_MATERIALIZE_ROOT'), core_only=True,
        model_key_path=Path(key_file) if model_enabled else None, model_provider=provider)
    server.settings.host = '0.0.0.0'
    server.settings.port = 8765
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=['127.0.0.1:*', 'localhost:*', 'skill-hub:*'],
        allowed_origins=['http://127.0.0.1:*', 'http://localhost:*'])
    transport_security = TransportSecurityMiddleware(server.settings.transport_security)
    routing = asyncio.Lock()

    @server.custom_route('/route_task', methods=['POST'])
    async def route_for_dashboard(request):
        rejection = await transport_security.validate_request(request, is_post=True)
        if rejection is not None:
            return rejection
        if not model_enabled:
            return JSONResponse({'error': 'MODEL_ROUTING_DISABLED'}, status_code=503)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 300_000:
                return JSONResponse({'error': 'REQUEST_TOO_LARGE'}, status_code=413)
            raw.extend(chunk)
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError('object required')
        except ValueError:
            return JSONResponse({'error': 'INVALID_ARGUMENTS'}, status_code=400)
        if routing.locked():
            return JSONResponse({'error': 'ROUTER_BUSY'}, status_code=429)
        async with routing:
            def invoke():
                return asyncio.run(server.call_tool('route_task', args))
            try:
                result = await run_in_threadpool(invoke)
            except Exception:
                return JSONResponse({'error': 'ROUTE_CALL_FAILED'}, status_code=400)
        content = result[0] if isinstance(result, tuple) else result
        payload = json.loads(content[0].text)
        return Response(content[0].text, media_type='application/json',
                        status_code=400 if payload.get('error') else 200)

    @server.custom_route('/health', methods=['GET'])
    async def health(request):
        try:
            with closing(index.connect(hub)) as con:
                if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('catalog integrity check failed')
                con.execute('SELECT rowid FROM fts LIMIT 1').fetchall()
                con.execute('SELECT rowid FROM fts_prompts LIMIT 1').fetchall()
            return JSONResponse({
                'status': 'ok', 'mode': 'provider-metadata' if model_enabled else 'keyword', 'catalog': index.stats(hub),
                'capabilities': {'keyword_search': 'available', 'resource_load': 'available',
                                 'model_routing': 'configured' if model_enabled else 'disabled', 'semantic_search': 'disabled',
                                 'host_broker': 'disabled'},
                'catalog_refresh': 'restart after editing content',
                'execution': 'not evaluated; resources are instruction data'})
        except (sqlite3.Error, OSError):
            return JSONResponse({'status': 'unavailable', 'mode': 'provider-metadata' if model_enabled else 'keyword',
                                 'error': 'catalog unreadable or invalid'}, status_code=503)

    return server


if __name__ == '__main__':
    build_server().run(transport='streamable-http')
