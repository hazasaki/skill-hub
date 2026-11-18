"""Container transport binding; host publication stays loopback in Compose."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import hashlib
import json
import os
import sys
import shutil
import sqlite3
from contextlib import closing

from server import Dashboard, Handler
from sources import HEALTH


def validate_inputs(inputs):
    manifest = json.loads((inputs / 'manifest.json').read_text('utf-8'))
    for name in ('native.json', 'AUDIT.json', 'skill-index.db'):
        with (inputs / name).open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != manifest['sha256'][name]:
            raise ValueError('Input bundle hash mismatch: ' + name)
    for name in ('catalog-quality.json', 'security-review.json'):
        path = inputs / name
        expected = manifest['sha256'].get(name)
        if expected is None and not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Invalid review receipt: ' + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Review receipt hash mismatch: ' + name)
        receipt = json.loads(raw)
        catalog_pin = receipt.get('index_sha256') if name == 'catalog-quality.json' else receipt.get('catalog_sha256')
        if catalog_pin != manifest['sha256']['skill-index.db']:
            raise ValueError('Review receipt belongs to another catalog: ' + name)
    name='original-decisions.json'
    path=inputs/name
    expected=manifest['sha256'].get(name)
    if expected is not None or path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size>8_000_000: raise ValueError('Invalid decisions receipt')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('Decisions receipt hash mismatch')
        receipt=json.loads(raw)
        if set(receipt)!={'schema','entries'} or receipt['schema']!='skill-hub.original-decisions.v1' or not isinstance(receipt['entries'],dict): raise ValueError('Invalid decisions receipt schema')
    audit = json.loads((inputs / 'AUDIT.json').read_text('utf-8'))
    allowed = {'verified_at_utc', 'mode', 'verdict', 'cases', 'selection_recall',
               'loaded_recall', 'candidate_recall', 'broad_relevance_precision',
               'provenance_limit', 'runtime_limit', 'timing_ms'}
    if not isinstance(audit, dict) or set(audit) - allowed:
        raise ValueError('Only aggregate audit fields may be mounted')
    for key, value in audit.items():
        if key == 'timing_ms':
            if not isinstance(value, dict) or any(type(v) not in (int, float) for v in value.values()):
                raise ValueError('Only numeric aggregate timing fields may be mounted')
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError('Nested evaluation data may not be mounted')
    return manifest


def create_local_app(content, catalog, state):
    """Build only local Markdown; absent native/telemetry sources stay unavailable."""
    # Use the same indexer as MCP so resource IDs and full-text search agree.
    import retrieval_projection
    import skill_index
    skill_index.build(content, output_path=catalog)
    app = Dashboard(project=Path('/data'), state=state)
    app.catalog = catalog
    app.sources.catalog = catalog
    document, _ = retrieval_projection.build(content, catalog)
    graph_path = catalog.with_name('dashboard-graph.json')
    graph_path.write_bytes(retrieval_projection.canonical({
        **document['graph'], 'built_at': document['built_at'], 'gaps': document['gaps'],
    }))
    app.sources.graph_projection = graph_path
    return app


def main():
    sys.path.append(os.environ.get('SKILL_HUB_INDEXER_PATH', '/indexer'))
    HEALTH['skill-hub'] = (os.environ.get('SKILL_HUB_CORE_HOST', '127.0.0.1'), 8765, '/health')
    catalog = Path('/tmp/catalog/skill-index.db')
    app = create_local_app(Path('/hub'), catalog, Path('/state'))
    port = int(os.environ.get('HUB_DASHBOARD_PORT', '8769'))
    if not 1 <= port <= 65535:
        raise ValueError('HUB_DASHBOARD_PORT must be 1..65535')
    app.public_port = port
    server = ThreadingHTTPServer(('0.0.0.0', 8769), Handler)
    server.app = app
    server.serve_forever()


if __name__ == '__main__':
    main()
