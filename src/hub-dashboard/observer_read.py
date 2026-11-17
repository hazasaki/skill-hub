"""Read-only local Skill Hub observer. No router/provider imports or write APIs."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
HUB = ROOT.parent.parent / 'skill-hub'
INDEX = HUB / 'state/skill-index.db'
EVENTS = HUB / 'state/telemetry/hub-telemetry.db'
FRESH_SECONDS = 300
SAFE_ID = re.compile(r'^[\w.:/@+\-]{1,200}$', re.UNICODE)
SECRET = re.compile(r'(?i)(sk-|gh[pousr]_|bearer|password|api.?key|eyJ)')
FIELDS = {
    'route': {'outcome', 'trigger', 'error_class', 'coverage_scored', 'coverage_total',
              'coverage_ratio', 'partial', 'deadline_hit', 'elapsed_ms', 'cost_usd'},
    'select': {'skill_count', 'prompt_count', 'skill_ids', 'prompt_ids'},
    'load': {'resource_kind', 'content_sha256', 'loader_route', 'served',
             'body_truncated', 'bytes', 'policy_status', 'refusal_reason', 'checks'},
    'attest': {'resource_kind', 'evidence', 'verdict'},
}


def safe_id(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) and not SECRET.search(value) else None


def clean_event(raw):
    """Never export task text, resource bodies, free-form errors or unknown fields."""
    kind = raw.get('kind')
    if kind not in FIELDS or raw.get('schema') != 'skill-hub.telemetry.v1':
        raise ValueError('unsupported event')
    timestamp = raw.get('emitted_at')
    if not isinstance(timestamp, str) or len(timestamp) > 40:
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    fields = raw.get('fields')
    if not isinstance(fields, dict):
        raise ValueError('invalid fields')
    clean = {}
    for key in FIELDS[kind]:
        value = fields.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('nonfinite number')
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = safe_id(value)
        elif isinstance(value, list):
            clean[key] = [item for item in (safe_id(v) for v in value[:64]) if item]
    return {'kind': kind, 'emitted_at': timestamp, 'fields': clean,
            **{k: safe_id(raw.get(k)) for k in ('route_id', 'load_id', 'resource_id')}}


def connect_ro(path):
    if not path.is_file():
        raise FileNotFoundError(path.name)
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    con.execute('PRAGMA query_only=ON')
    return con


def read_events(path):
    with closing(connect_ro(path)) as con:
        rows = con.execute('SELECT payload_json FROM hub_telemetry ORDER BY seq DESC LIMIT 1200').fetchall()
    valid, rejected = [], 0
    for (payload,) in reversed(rows):
        try:
            if len(payload) > 16384:
                raise ValueError('event too large')
            valid.append(clean_event(json.loads(payload)))
        except (ValueError, TypeError, AttributeError):
            rejected += 1
    return valid, rejected


def read_catalog(path):
    with closing(connect_ro(path)) as con:
        total, active = con.execute('SELECT COUNT(*), COALESCE(SUM(active != 0),0) FROM skill_resources').fetchone()
        prompts = con.execute('SELECT COUNT(*) FROM prompt_resources').fetchone()[0]
    return {'skills': total, 'active': active, 'disabled': total - active, 'prompts': prompts}


def age(timestamp, now):
    try:
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        seconds = (now - parsed).total_seconds()
        return max(0, seconds) if seconds >= -30 else None
    except (ValueError, TypeError, AttributeError):
        return None


def group_routes(events):
    routes = {}
    for event in events:
        rid = event.get('route_id')
        if not rid:
            continue
        route = routes.setdefault(rid, {'id': rid, 'events': []})
        route['events'].append(event)
        if event['kind'] == 'route':
            route.update(timestamp=event['emitted_at'], **event['fields'])
    # Attestations may reference a load without repeating its route_id.
    loads = {e['load_id']: e.get('route_id') for e in events if e.get('load_id') and e['kind'] == 'load'}
    for event in events:
        if event['kind'] == 'attest' and not event.get('route_id'):
            rid = loads.get(event.get('load_id'))
            if rid in routes:
                routes[rid]['events'].append(event)
    return sorted((v for v in routes.values() if 'timestamp' in v),
                  key=lambda r: datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')), reverse=True)[:20]
