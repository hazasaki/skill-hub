"""On-demand MCP tool broker for heavy/specialized MCP servers (rea, excel, computer_use, openbot-reliability)."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from shared_resources import DEFAULT_HUB

CATALOG_PATH = DEFAULT_HUB / 'state' / 'mcp-tool-catalog.json'

SERVER_ALIASES = {
    'mcp__rea': 'rea',
    'rea': 'rea',
    'mcp__excel': 'excel',
    'excel': 'excel',
    'mcp__computer_use': 'computer_use',
    'computer_use': 'computer_use',
    'computer-use': 'computer_use',
    'cua': 'computer_use',
    'mcp__openbot_reliability': 'openbot-reliability',
    'openbot_reliability': 'openbot-reliability',
    'openbot-reliability': 'openbot-reliability',
}


def normalize_server(server: str) -> str:
    key = (server or '').strip().lower()
    return SERVER_ALIASES.get(key, key)


def server_configs() -> dict[str, dict[str, Any]]:
    """Load operator-owned server definitions; no host tools are enabled implicitly."""
    config_path = os.environ.get('SKILL_HUB_BROKER_CONFIG')
    if not config_path:
        return {}
    configs = json.loads(Path(config_path).read_text(encoding='utf-8'))
    if not isinstance(configs, dict):
        raise ValueError('broker configuration must be an object')
    for name, config in configs.items():
        if not isinstance(config, dict) or config.get('transport') not in ('stdio', 'http'):
            raise ValueError(f'invalid broker configuration for {name!r}')
        required = 'url' if config['transport'] == 'http' else 'command'
        if not isinstance(config.get(required), str) or not config[required].strip():
            raise ValueError(f'broker configuration requires {required} for {name!r}')
    return configs


def load_catalog(catalog_path: Path = CATALOG_PATH) -> dict[str, list[dict[str, Any]]]:
    if not catalog_path.is_file():
        return {}
    try:
        data = json.loads(catalog_path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def search_tools(
    query: str = '',
    server: str = '',
    limit: int = 15,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    catalog = load_catalog(catalog_path)
    target_server = normalize_server(server) if server else ''
    tokens = [t for t in re.split(r'[^a-z0-9_]+', (query or '').lower()) if t]
    matches: list[tuple[int, str, dict[str, Any]]] = []

    for srv, tools in catalog.items():
        if target_server and srv != target_server:
            continue
        for tool in tools:
            name = str(tool.get('name', ''))
            desc = str(tool.get('description', ''))
            hay_name = name.lower()
            hay_desc = desc.lower()
            if not tokens:
                score = 1
            else:
                score = 0
                for tok in tokens:
                    if tok == hay_name:
                        score += 20
                    elif tok in hay_name:
                        score += 10
                    elif tok in hay_desc:
                        score += 3
                    elif tok in srv.lower():
                        score += 2
            if score > 0:
                matches.append((score, srv, {
                    'server': srv,
                    'tool': name,
                    'description': desc[:240],
                }))

    matches.sort(key=lambda item: (-item[0], item[1], item[2]['tool']))
    capped = [m[2] for m in matches[:max(1, min(int(limit), 50))]]
    return {
        'query': query,
        'server': target_server or 'all',
        'available_servers': {k: len(v) for k, v in catalog.items()},
        'total_matches': len(matches),
        'results': capped,
    }


def describe_tool(
    server: str,
    tool_name: str,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    srv = normalize_server(server)
    catalog = load_catalog(catalog_path)
    tools = catalog.get(srv, [])
    if not tools:
        return {'error': f'Unknown or unindexed server: {server!r}', 'available_servers': list(catalog)}
    clean_name = re.sub(r'^mcp__[a-z0-9_]+__', '', (tool_name or '').strip())
    for tool in tools:
        if tool.get('name') == clean_name:
            return {
                'server': srv,
                'tool': clean_name,
                'description': tool.get('description', ''),
                'inputSchema': tool.get('inputSchema', {}),
            }
    return {'error': f'Tool {clean_name!r} not found in server {srv!r}'}


def _serialize_call_result(result: Any) -> dict[str, Any]:
    is_error = bool(getattr(result, 'isError', False))
    contents = []
    for item in getattr(result, 'content', []) or []:
        text = getattr(item, 'text', None)
        if text is not None:
            contents.append({'type': 'text', 'text': text})
        else:
            data = getattr(item, 'data', None)
            mime = getattr(item, 'mimeType', None)
            contents.append({'type': getattr(item, 'type', 'unknown'), 'mimeType': mime, 'data': data})
    structured = getattr(result, 'structuredContent', None)
    out: dict[str, Any] = {'ok': not is_error, 'isError': is_error, 'content': contents}
    if structured is not None:
        out['structuredContent'] = structured
    return out


async def _call_tool_async(
    server: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    srv = normalize_server(server)
    cfgs = server_configs()
    if srv not in cfgs:
        return {'ok': False, 'error': f'Unsupported on-demand server: {server!r}', 'supported': list(cfgs)}
    cfg = cfgs[srv]
    clean_name = re.sub(r'^mcp__[a-z0-9_]+__', '', (tool_name or '').strip())

    async def _run_session(read: Any, write: Any) -> dict[str, Any]:
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout_seconds)) as session:
            await session.initialize()
            res = await session.call_tool(clean_name, arguments or {})
            return _serialize_call_result(res)

    try:
        async with asyncio.timeout(timeout_seconds + 10):
            if cfg['transport'] == 'http':
                async with streamablehttp_client(cfg['url'], headers=cfg.get('headers', {})) as (read, write, _):
                    return await _run_session(read, write)
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errlog:
                params = StdioServerParameters(
                    command=cfg['command'],
                    args=cfg.get('args', []),
                    env={**os.environ, **cfg.get('env', {})},
                    cwd=cfg.get('cwd'),
                )
                async with stdio_client(params, errlog=errlog) as (read, write):
                    return await _run_session(read, write)
    except Exception as exc:
        if srv == 'computer_use' and cfg.get('args') and str(cfg['args'][-1]).endswith('-tap'):
            raw_pipe = str(cfg['args'][-1])[:-4]
            try:
                with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as errlog:
                    params = StdioServerParameters(
                        command=cfg['command'],
                        args=['mcp', '--socket', raw_pipe],
                        env={**os.environ, **cfg.get('env', {})},
                    )
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        return await _run_session(read, write)
            except Exception as inner_exc:
                exc = inner_exc
        return {
            'ok': False,
            'server': srv,
            'tool': clean_name,
            'error': type(exc).__name__,
            'detail': str(exc)[:500],
        }


def call_tool(
    server: str,
    tool_name: str,
    arguments_json: str | dict[str, Any] = '{}',
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if isinstance(arguments_json, dict):
        args = arguments_json
    else:
        raw = (arguments_json or '').strip() or '{}'
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as err:
            return {'ok': False, 'error': f'Invalid arguments_json: {err}'}
        if not isinstance(args, dict):
            return {'ok': False, 'error': 'arguments_json must decode to a JSON object'}
    return asyncio.run(_call_tool_async(server, tool_name, args, timeout_seconds=timeout_seconds))
