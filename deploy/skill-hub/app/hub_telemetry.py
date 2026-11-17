"""C1/D6: metadata-only usage telemetry for the skill hub.

Four evidence stages are recorded and never merged into one number:

``route``   route_task ran. Outcome, trigger, coverage, deadline, provider cost/latency.
``select``  resources crossed the per-kind threshold (ids, kinds, scores, dedupe).
``load``    a resource body was served to a caller (id, digest, loader route, policy verdict).
``attest``  a host or agent stated that a loaded resource informed the turn.

A load is not a use. ``get_skill`` returning a body proves only that the bytes were served.
The follow-up rate is therefore computed **only** over loads that carry an attestation; with
none it returns ``None`` with the reason ``NO_USAGE_EVIDENCE`` rather than 0.0 or 1.0.

Metadata only. Every field is validated against a per-kind allowlist and a value charset that
cannot carry prose, so a task body, a query, a prompt or a credential cannot be written here
even by mistake. There is no free-text field and no nested payload.

The store is inert until an operator points ``SKILL_HUB_TELEMETRY_DIR`` at a directory; the
null store keeps the MCP server's behaviour unchanged until integration is reviewed.

Standard library only. This module never imports the router, the index or the provider.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = 'skill-hub.telemetry.v1'
MAX_STRING = 200
MAX_LIST = 64
MAX_EVENT_BYTES = 4096
DEFAULT_DB_NAME = 'hub-telemetry.db'
DEFAULT_JSONL_NAME = 'hub-telemetry.jsonl'
ENV_DIR = 'SKILL_HUB_TELEMETRY_DIR'

ROUTE = 'route'
SELECT = 'select'
LOAD = 'load'
ATTEST = 'attest'
KINDS = (ROUTE, SELECT, LOAD, ATTEST)

# Per-kind field allowlists. A field absent from its kind's set is rejected, not dropped:
# silently dropping would let a caller believe it recorded something it did not.
ROUTE_FIELDS = frozenset({
    'outcome', 'trigger', 'reason', 'error_class', 'catalog_index_sha256',
    'coverage_scored', 'coverage_total', 'coverage_ratio', 'partial',
    'deadline_hit', 'deadline_ms', 'elapsed_ms',
    'batches_completed', 'batches_planned',
    'provider_status', 'provider_latency_ms', 'input_tokens', 'output_tokens',
    'attempts', 'cost_usd', 'model',
    'skill_threshold', 'prompt_threshold',
})
SELECT_FIELDS = frozenset({
    'skill_ids', 'prompt_ids', 'skill_count', 'prompt_count',
    'max_score', 'dedupe_applied', 'dedupe_removed',
})
LOAD_FIELDS = frozenset({
    'resource_kind', 'content_sha256', 'loader_route', 'served',
    'body_truncated', 'bytes', 'policy_status',
    'refusal_reason', 'checks',
})
ATTEST_FIELDS = frozenset({
    'resource_kind', 'evidence', 'evidence_ref', 'verdict', 'confidence',
})
ALLOWED = {ROUTE: ROUTE_FIELDS, SELECT: SELECT_FIELDS, LOAD: LOAD_FIELDS, ATTEST: ATTEST_FIELDS}

# Exact names that must never appear as a field, at any depth.
FORBIDDEN_KEYS = frozenset({
    'state', 'task', 'task_state', 'task_text', 'query', 'prompt', 'prompt_text',
    'body', 'text', 'content', 'instructions', 'arguments', 'args', 'result',
    'result_json', 'output', 'stdout', 'stderr', 'command', 'cmd',
    'api_key', 'apikey', 'key', 'token', 'secret', 'password', 'passwd',
    'authorization', 'cookie', 'cookies', 'headers', 'header', 'bearer',
    'credential', 'credentials', 'env', 'environment',
})

# A value that must not be recorded even when it has no spaces. Conservative: a false
# positive costs one rejected diagnostic field, a false negative leaks a credential.
SECRET_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_-]{8,}'),
    re.compile(r'gh[pousr]_[A-Za-z0-9]{8,}'),
    re.compile(r'AKIA[0-9A-Z]{12,}'),
    re.compile(r'eyJ[A-Za-z0-9._-]{10,}'),
    re.compile(r'-{5}BEGIN [A-Z ]*PRIVATE KEY'),
    re.compile(r'[Bb]earer\s+\S+'),
    re.compile(r'(?i)(api[_-]?key|token|secret|password)\s*[:=]'),
)

# Identifiers, digests, enums and paths only: no whitespace, no quotes, no markup.
SAFE_VALUE = re.compile(r'^[^\s"\'`\\<>{}\[\]|]{1,%d}$' % MAX_STRING, re.UNICODE)
SAFE_HEX = re.compile(r'^[0-9a-f]{8,64}$')


class TelemetryRejected(ValueError):
    """A field or value that may not be recorded. The reason names the rule, not the value."""


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def _reject(reason):
    raise TelemetryRejected(reason)


def _safe_scalar(key, value):
    """Validate one scalar. Returns the value, or raises with a value-free reason."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _reject('non_finite_number:%s' % key)
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            _reject('value_too_long:%s' % key)
        if not SAFE_VALUE.match(value):
            # Whitespace or quoting means prose, a body or a header line.
            _reject('value_not_an_identifier:%s' % key)
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                _reject('value_looks_secret:%s' % key)
        if key.endswith('sha256') and not SAFE_HEX.match(value):
            _reject('digest_not_hex:%s' % key)
        return value
    _reject('unsupported_value_type:%s' % key)


def _safe_fields(kind, fields):
    """Validate a flat field map against the kind's allowlist."""
    if not isinstance(fields, dict):
        _reject('fields_not_a_mapping')
    allowed = ALLOWED[kind]
    clean = {}
    for key, value in fields.items():
        if not isinstance(key, str):
            _reject('field_name_not_a_string')
        if key in FORBIDDEN_KEYS:
            _reject('forbidden_field:%s' % key)
        if key not in allowed:
            _reject('unknown_field:%s' % key)
        if isinstance(value, dict):
            # Nested payloads are how bodies and tool arguments arrive.
            _reject('nested_object_not_allowed:%s' % key)
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_LIST:
                _reject('list_too_long:%s' % key)
            clean[key] = [_safe_scalar(key, item) for item in value]
        else:
            clean[key] = _safe_scalar(key, value)
    return clean


def _identifier(name, value):
    if value is None:
        return None
    if not isinstance(value, str) or not SAFE_VALUE.match(value):
        _reject('%s_not_an_identifier' % name)
    return _safe_scalar(name, value)


def make_event(kind, fields, route_id=None, load_id=None, resource_id=None, emitted_at=None):
    """Build one validated event envelope. Raises TelemetryRejected on any violation."""
    if kind not in KINDS:
        _reject('unknown_kind:%s' % kind)
    timestamp = emitted_at if emitted_at is not None else now_iso()
    try:
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise ValueError('invalid timestamp')
        dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError:
        _reject('invalid_timestamp')
    event = {
        'schema': SCHEMA,
        'kind': kind,
        'emitted_at': timestamp,
        'route_id': _identifier('route_id', route_id),
        'load_id': _identifier('load_id', load_id),
        'resource_id': _identifier('resource_id', resource_id),
        'fields': _safe_fields(kind, fields or {}),
    }
    if len(json.dumps(event, ensure_ascii=False).encode('utf-8')) > MAX_EVENT_BYTES:
        _reject('event_too_large')
    return event


def validate_event(event):
    """Validate again at the sink; callers may mutate a previously valid envelope."""
    envelope = {'schema', 'kind', 'emitted_at', 'route_id', 'load_id', 'resource_id', 'fields'}
    if not isinstance(event, dict) or set(event) != envelope or event['schema'] != SCHEMA:
        _reject('invalid_envelope')
    return make_event(event['kind'], event['fields'], route_id=event['route_id'],
                      load_id=event['load_id'], resource_id=event['resource_id'],
                      emitted_at=event['emitted_at'])


class NullStore:
    """The default: records nothing, raises nothing. Keeps the server unchanged."""

    enabled = False

    def append(self, event):
        return False

    def events(self, kind=None, limit=None):
        return []


class EventStore:
    """Append-only JSONL (always) plus an optional read-only-friendly SQLite mirror.

    Both sinks live under one operator-chosen directory. Nothing here opens the catalog
    index database, so a telemetry failure cannot corrupt routing state.
    """

    enabled = True

    def __init__(self, root, db=True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.root / DEFAULT_JSONL_NAME
        self.db = (self.root / DEFAULT_DB_NAME) if db else None
        if self.db is not None:
            self._ensure_schema()
        self.rejected = 0
        self.write_errors = 0
        self.corrupt_lines = 0

    def _connect(self):
        return closing(sqlite3.connect(str(self.db), timeout=5))

    def _ensure_schema(self):
        with self._connect() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS hub_telemetry (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                emitted_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                route_id TEXT,
                load_id TEXT,
                resource_id TEXT,
                payload_json TEXT NOT NULL)''')
            con.execute('CREATE INDEX IF NOT EXISTS hub_telemetry_kind '
                        'ON hub_telemetry(kind, emitted_at)')
            con.execute('CREATE INDEX IF NOT EXISTS hub_telemetry_route '
                        'ON hub_telemetry(route_id)')
            con.commit()

    def append(self, event):
        """Write one validated event. Never raises: telemetry must not break routing."""
        try:
            event = validate_event(event)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            self.rejected += 1
            return False
        try:
            with open(self.jsonl, 'a', encoding='utf-8', newline='\n') as fh:
                fh.write(line + '\n')
        except OSError:
            self.write_errors += 1
            return False
        if self.db is not None:
            try:
                with self._connect() as con:
                    con.execute(
                        'INSERT INTO hub_telemetry '
                        '(emitted_at, kind, route_id, load_id, resource_id, payload_json) '
                        'VALUES (?,?,?,?,?,?)',
                        (event['emitted_at'], event['kind'], event['route_id'],
                         event['load_id'], event['resource_id'], line))
                    con.commit()
            except sqlite3.Error:
                self.write_errors += 1
        return True

    def events(self, kind=None, limit=None):
        """Read back recorded events. A corrupt line is counted, not fatal."""
        out = []
        if not self.jsonl.is_file():
            return out
        self.corrupt_lines = 0
        with open(self.jsonl, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = validate_event(json.loads(line))
                except (ValueError, TypeError):
                    self.corrupt_lines += 1
                    continue
                if kind is not None and event.get('kind') != kind:
                    continue
                out.append(event)
        if limit is not None:
            out = out[-int(limit):]
        return out


def store_from_env(environ=None):
    """The store the environment asks for, or the null store. Off unless explicitly set."""
    environ = os.environ if environ is None else environ
    root = environ.get(ENV_DIR)
    if not root:
        return NullStore()
    try:
        return EventStore(root)
    except (OSError, sqlite3.Error):
        return NullStore()


# --- emit helpers: one per evidence stage, each explicitly typed ---------------------

def emit_route(store, fields, route_id=None):
    return _emit(store, ROUTE, fields, route_id=route_id)


def emit_select(store, fields, route_id=None):
    return _emit(store, SELECT, fields, route_id=route_id)


def emit_load(store, fields, load_id=None, route_id=None, resource_id=None):
    return _emit(store, LOAD, fields, load_id=load_id, route_id=route_id,
                 resource_id=resource_id)


def emit_attest(store, fields, load_id=None, route_id=None, resource_id=None):
    return _emit(store, ATTEST, fields, load_id=load_id, route_id=route_id,
                 resource_id=resource_id)


def _emit(store, kind, fields, **ids):
    """Validate then append. A rejection is counted on the store and never propagated."""
    if not getattr(store, 'enabled', False):
        return False
    try:
        event = make_event(kind, fields, **ids)
    except TelemetryRejected:
        store.rejected = getattr(store, 'rejected', 0) + 1
        return False
    return store.append(event)


def record_route_result(result, store, route_id):
    """Project run_multi or route_task.v2 into metadata; never traverse task/body text.

    Records only actually verified served loads. It never creates usage attestations.
    The returned counters contain no router payload and a malformed record cannot
    interrupt the route response.
    """
    recorded = {'route_recorded': False, 'selection_recorded': False, 'load_events': 0}
    if not isinstance(result, dict) or not isinstance(route_id, str):
        return recorded
    coverage = result.get('coverage') if isinstance(result.get('coverage'), dict) else {}
    provider = result.get('provider') if isinstance(result.get('provider'), dict) else {}
    resolution = result.get('resolution') if isinstance(result.get('resolution'), dict) else {}
    outcome = result.get('outcome') or resolution.get('outcome') or result.get('error')
    outcomes = {'SELECTED', 'NO_SKILL', 'NO_CANDIDATE', 'POLICY_DENIED',
                'RESOURCE_UNAVAILABLE', 'ROUTER_ERROR', 'ABSTAIN_LOW_CONFIDENCE',
                'ABSTAIN', 'ROUTE_FAILED', 'LIVE_MODEL_REQUIRED'}
    outcome = outcome if isinstance(outcome, str) and outcome in outcomes else 'UNKNOWN'
    trigger = result.get('trigger') or coverage.get('stop_reason')
    triggers = {'DEADLINE', 'MODEL_ERRORS', 'BUDGET_EXCEEDED', 'COMPLETE', 'DECLINED',
                'NO_CATALOG_ENTRY', 'POLICY_DENIED', 'NOT_CONFIGURED', 'CACHE_HIT'}
    trigger = trigger if isinstance(trigger, str) and trigger in triggers else 'UNKNOWN'
    fields = {'outcome': outcome, 'trigger': trigger, 'deadline_hit': trigger == 'DEADLINE'}
    for source, destination in (('scored', 'coverage_scored'), ('catalog_total', 'coverage_total'),
                                ('scored_ratio', 'coverage_ratio'),
                                ('batches_completed', 'batches_completed'),
                                ('batches_planned', 'batches_planned')):
        value = coverage.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            fields[destination] = value
    if isinstance(result.get('partial'), bool):
        fields['partial'] = result['partial']
    elif 'coverage_total' in fields and 'coverage_scored' in fields:
        fields['partial'] = fields['coverage_scored'] < fields['coverage_total']
    for name in ('input_tokens', 'output_tokens', 'cost_usd'):
        value = provider.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            fields[name] = value
    requests = provider.get('requests') if isinstance(provider.get('requests'), list) else []
    fields['provider_status'] = ('timeout' if trigger == 'DEADLINE' else
                                 'error' if any(isinstance(r, dict) and r.get('error_class') for r in requests)
                                 else 'ok' if requests else 'unknown')
    recorded['route_recorded'] = emit_route(store, fields, route_id=route_id)

    selected = result.get('selected') if isinstance(result.get('selected'), list) else []
    counts = {'skill_count': 0, 'prompt_count': 0}
    for item in selected:
        if isinstance(item, dict) and item.get('type') in ('skill', 'prompt'):
            counts[item['type'] + '_count'] += 1
    # Counts avoid silently truncating a selection larger than the event's ID-list cap.
    recorded['selection_recorded'] = emit_select(store, counts, route_id=route_id)
    loads = result.get('loads') if isinstance(result.get('loads'), dict) else {}
    loaded = loads.get('loaded') if isinstance(loads.get('loaded'), list) else []
    for number, receipt in enumerate(loaded):
        if not isinstance(receipt, dict) or receipt.get('type') not in ('skill', 'prompt'):
            continue
        if receipt.get('load_verified') is not True or not (receipt.get('content_matches_pinned') or receipt.get('content_matches_approved')):
            continue
        name = receipt.get('id')
        digest = receipt.get('served_content_sha256')
        if not isinstance(name, str) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            continue
        resource_id = receipt['type'] + ':' + name
        load_id = hashlib.sha256(json.dumps([route_id, resource_id, number]).encode('utf-8')).hexdigest()
        load_fields = {'served': True, 'resource_kind': receipt['type'],
                       'content_sha256': digest, 'policy_status': 'CHECKS_PASSED',
                       'loader_route': receipt.get('loader_route')
                       if receipt.get('loader_route') in ('IN_PROCESS', 'MCP_DISPATCHED') else 'UNKNOWN'}
        if isinstance(receipt.get('checks'), list):
            load_fields['checks'] = [str(c) for c in receipt['checks']]
        size = receipt.get('body_bytes')
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            load_fields['bytes'] = size
        recorded['load_events'] += bool(emit_load(store, load_fields, route_id=route_id,
                                                 load_id=load_id, resource_id=resource_id))
    refused_items = []
    for bucket in ('denied', 'unavailable'):
        for item in loads.get(bucket, []):
            if isinstance(item, dict) and item.get('type') in ('skill', 'prompt'):
                refused_items.append(item)
    for number, problem in enumerate(refused_items, start=len(loaded)):
        name = problem.get('id')
        if not isinstance(name, str):
            continue
        resource_id = problem['type'] + ':' + name
        load_id = hashlib.sha256(json.dumps([route_id, resource_id, number]).encode('utf-8')).hexdigest()
        digest = problem.get('served_content_sha256') or problem.get('content_sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            digest = None
        outcome = problem.get('outcome')
        reason = outcome if outcome in ('RESOURCE_MISSING', 'RESOURCE_DISABLED', 'CONTENT_EMPTY',
                                        'CONTENT_CHANGED', 'UNSAFE_RESOURCE', 'SAFETY_CHECK_UNAVAILABLE',
                                        'POLICY_DENIED', 'RESOURCE_UNAVAILABLE') else 'OTHER'
        load_fields = {'served': False, 'resource_kind': problem['type'],
                       'content_sha256': digest, 'policy_status': 'REFUSED',
                       'refusal_reason': reason,
                       'loader_route': problem.get('loader_route')
                       if problem.get('loader_route') in ('IN_PROCESS', 'MCP_DISPATCHED') else 'IN_PROCESS'}
        if isinstance(problem.get('checks'), list):
            load_fields['checks'] = [str(c) for c in problem['checks']]
        recorded['load_events'] += bool(emit_load(store, load_fields, route_id=route_id,
                                                 load_id=load_id, resource_id=resource_id))
    return recorded


# --- aggregation ---------------------------------------------------------------------

def _num(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def summarize(events):
    """Deterministic counts over recorded events. No inference beyond what was recorded."""
    events = list(events)
    routes = [e for e in events if e.get('kind') == ROUTE]
    selects = [e for e in events if e.get('kind') == SELECT]
    loads = [e for e in events if e.get('kind') == LOAD]
    attests = [e for e in events if e.get('kind') == ATTEST]

    coverage = [_num(e['fields'].get('coverage_ratio')) for e in routes
                if isinstance(e.get('fields'), dict)]
    costs = [_num(e['fields'].get('cost_usd')) for e in routes
             if isinstance(e.get('fields'), dict) and 'cost_usd' in e['fields']]
    return {
        'schema': SCHEMA,
        'routes': len(routes),
        'selects': len(selects),
        'loads': len(loads),
        'attestations': len(attests),
        'deadline_hits': sum(1 for e in routes
                             if isinstance(e.get('fields'), dict)
                             and e['fields'].get('deadline_hit') is True),
        'partial_routes': sum(1 for e in routes
                              if isinstance(e.get('fields'), dict)
                              and e['fields'].get('partial') is True),
        'provider_errors': sum(1 for e in routes
                               if isinstance(e.get('fields'), dict)
                               and e['fields'].get('provider_status') not in (None, 'ok')),
        'mean_coverage': (round(sum(coverage) / len(coverage), 4) if coverage else None),
        'cost_usd_total': round(sum(costs), 6) if costs else None,
        'load_followup': load_followup(events),
    }


def load_followup(events):
    """Loads vs attested use, kept apart.

    ``rate`` is None unless at least one load carries an attestation. ``attested_ratio``
    reports how much of the load set has any evidence at all, so a small rate over a small
    attested sample cannot read as a whole-catalog measurement.
    """
    loads = {}
    for event in events:
        if (event.get('kind') == LOAD and event.get('load_id')
                and (event.get('fields') or {}).get('served') is True):
            loads[event['load_id']] = event
    verdicts = {}
    for event in events:
        if event.get('kind') != ATTEST or not event.get('load_id'):
            continue
        if event['load_id'] not in loads:
            # An attestation for a load this store never saw is not usable evidence.
            continue
        original = loads[event['load_id']]
        if event.get('resource_id') != original.get('resource_id'):
            continue
        if event.get('route_id') != original.get('route_id'):
            continue
        if (event.get('fields') or {}).get('verdict') not in ('used', 'not_used'):
            continue
        verdicts[event['load_id']] = (event.get('fields') or {}).get('verdict')

    attested = len(verdicts)
    used = sum(1 for verdict in verdicts.values() if verdict == 'used')
    return {
        'loads': len(loads),
        'attested_loads': attested,
        'used_loads': used,
        'unattested_loads': len(loads) - attested,
        'rate': (round(used / attested, 4) if attested else None),
        'rate_basis': 'attested_loads_only',
        'attested_ratio': (round(attested / len(loads), 4) if loads else None),
        'reason': None if attested else 'NO_USAGE_EVIDENCE',
        'limits': ['A load is a served body, not a use.',
                   'No verdict is inferred from a load, a search hit or a later load.'],
    }
