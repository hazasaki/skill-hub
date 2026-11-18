"""Standalone MCP service over a local, read-only content directory."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent / 'app'))
import skill_index as index
import skill_mcp


def build_server(hub=None):
    hub = Path(hub or os.environ['SKILL_HUB_ROOT'])
    model_enabled = os.environ.get('SKILL_HUB_ENABLE_MODEL_ROUTING') == '1'
    key_file = os.environ.get('SKILL_HUB_KEY_PATH') if model_enabled else None
    if model_enabled and (not key_file or not Path(key_file).is_file()):
        raise ValueError('Model routing requires an explicit SKILL_HUB_KEY_PATH file.')
    index.build(hub)
    server = skill_mcp.build_server(
        hub, os.environ.get('SKILL_HUB_MATERIALIZE_ROOT'), core_only=True, model_key_path=Path(key_file) if model_enabled else None)
    server.settings.host = '0.0.0.0'
    server.settings.port = 8765
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=['127.0.0.1:*', 'localhost:*', 'skill-hub:*'],
        allowed_origins=['http://127.0.0.1:*', 'http://localhost:*'])

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
