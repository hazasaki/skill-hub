"""Opt-in scheduling and score reuse for independent resource questions."""
from collections import OrderedDict
from contextvars import ContextVar
import hashlib
from pathlib import Path
import threading
import time

from rust_routing import Operation, canonical_json, decision
REVISION_CONTEXT = ContextVar('score_revision', default=None)
CANCEL_CONTEXT = ContextVar('score_cancel', default=None)
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL = 300
_LIMIT = 4096


def digest(value):
    return decision(Operation.DIGEST, canonical_json(value))


def score_scope(query, policy_gate, host_policy, verdict, key, endpoint, model, language, rubric):
    revision = REVISION_CONTEXT.get()
    if not revision or not key or not getattr(policy_gate, '__file__', None):
        return None
    try:
        policy_hash = hashlib.sha256(Path(policy_gate.__file__).read_bytes()).hexdigest()
        return decision(Operation.CACHE_IDENTITY, {'kind': 'scope', 'revision': revision, 'canonical': canonical_json(
            [query, policy_hash, host_policy, verdict, digest(key), endpoint, model, revision,
             language, rubric]).decode('utf-8')})
    except (OSError, TypeError, ValueError):
        return None


def resource_key(scope, entry, question):
    if not scope:
        return None
    card = entry.get('resource_card') or {}
    pins = [card.get('card_sha256'), card.get('source_raw_sha256'), entry.get('content_sha256')]
    if entry['type'] == 'skill':
        pins.append(entry.get('bundle_sha256'))
    return decision(Operation.CACHE_IDENTITY, {'kind': 'resource', 'scope': scope, 'pins': pins, 'canonical': canonical_json(
        [scope, entry['id'], question, pins, entry.get('state'), entry.get('risk')]).decode('utf-8')})


def cached(key):
    with _LOCK:
        value = _CACHE.pop(key, None)
        if value is None or time.monotonic() - value[0] >= _TTL:
            return None
        _CACHE[key] = value
        return value[1]


def publish(fresh):
    with _LOCK:
        now = time.monotonic()
        for key, value in fresh.items():
            _CACHE.pop(key, None)
            _CACHE[key] = (now, value)
        while len(_CACHE) > _LIMIT:
            _CACHE.popitem(last=False)


class Stages:
    def __init__(self, entries, task_state, explicit, ranks, skill_bar, prompt_bar):
        self.bars = {'skill': skill_bar, 'prompt': prompt_bar}
        from shortlist_runtime import query_terms

        goals = (task_state.get('required_capabilities') or
                 [task_state.get('current_step'), task_state.get('desired_output'),
                  *(task_state.get('artifact_types') or [])])
        goals = [tokens for goal in goals if goal and (tokens := query_terms(goal, prefer_ascii=False))]
        self.entries = {entry['id']: entry for entry in entries}
        rows = []
        for entry in entries:
            fields = (entry.get('resource_card') or {}).get('fields') or {}
            authored = ' '.join(str(fields.get(field, {}).get('value') or '')
                                for field in ('capability', 'use_when', 'produces'))
            metadata = ' '.join(str(entry.get(key) or '')
                                for key in ('resource_id', 'description', 'tags'))
            rows.append((entry['id'], entry['type'],
                         query_terms(authored.strip() or metadata, prefer_ascii=False), bool(authored.strip())))
        self.remaining, self.goals = decision(Operation.STAGE_INIT, {
            'entries': rows, 'goals': goals, 'explicit': sorted(explicit), 'ranks': ranks})
        self.admitted = []
        self.ledger = []

    def advance(self, scores):
        stage = decision(Operation.STAGE_NEXT, {'remaining': self.remaining, 'admitted': self.admitted,
                             'step': len(self.ledger), 'goals': self.goals,
                             'bars': self.bars, 'scores': scores})
        if stage['coverage'] is not None:
            self.ledger[-1]['goal_coverage'] = stage['coverage']
        if 'stop' in stage:
            self.ledger[-1]['stop_detail'] = stage['stop']
        if 'next' not in stage:
            return []
        self.remaining = stage['rest']
        self.admitted.extend(stage['next'])
        ids = [entry['id'] for entry in stage['next']]
        self.ledger.append({'cap': stage['cap'], 'reason': stage['reason'], 'ids': ids})
        return [self.entries[identifier] for identifier in ids]
