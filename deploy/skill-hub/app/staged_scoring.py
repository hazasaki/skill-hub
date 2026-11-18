"""Opt-in scheduling and score reuse for independent resource questions."""
from collections import OrderedDict
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re
import threading
import time

CAPS = (24, 48, 96)
REVISION_CONTEXT = ContextVar('score_revision', default=None)
CANCEL_CONTEXT = ContextVar('score_cancel', default=None)
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL = 300
_LIMIT = 4096


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')).hexdigest()


def score_scope(query, policy_gate, host_policy, verdict, key, endpoint, model, language, rubric):
    revision = REVISION_CONTEXT.get()
    # Provider reports ALIAS_ONLY. Never infer that its mutable label is an exact revision.
    if not revision or not key or not getattr(policy_gate, '__file__', None):
        return None
    try:
        policy_hash = hashlib.sha256(Path(policy_gate.__file__).read_bytes()).hexdigest()
        return digest([query, policy_hash, host_policy, verdict, digest(key),
                       endpoint, model, revision, language, rubric])
    except (OSError, TypeError, ValueError):
        return None


def resource_key(scope, entry, question):
    card = entry.get('resource_card') or {}
    pins = [card.get('card_sha256'), card.get('source_raw_sha256'), entry.get('content_sha256')]
    if entry['type'] == 'skill':
        pins.append(entry.get('bundle_sha256'))
    if scope and all(isinstance(pin, str) and re.fullmatch('[a-f0-9]{64}', pin) for pin in pins):
        return digest([scope, entry['id'], question, pins, entry.get('state'), entry.get('risk')])
    return None


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


def words(text):
    return set(re.findall(r'[^\W_]+', text.casefold()))


class Stages:
    def __init__(self, entries, task_state, explicit, ranks, skill_bar, prompt_bar):
        self.bars = {'skill': skill_bar, 'prompt': prompt_bar}
        self.explicit = set(explicit)
        goals = [task_state.get('current_step'), task_state.get('desired_output'),
                 *(task_state.get('artifact_types') or [])]
        self.goals = [terms for value in goals if value and (terms := words(value))]
        self.matches = {}
        for entry in entries:
            fields = (entry.get('resource_card') or {}).get('fields') or {}
            authored = ' '.join(str(fields.get(field, {}).get('value') or '')
                                for field in ('capability', 'use_when', 'produces'))
            terms = words(authored)
            self.matches[entry['id']] = {i for i, goal in enumerate(self.goals) if goal <= terms}
        rare = {}
        for entry in entries:
            evidence = ranks.get(entry['id'], {})
            if len(evidence) == 1:
                engine, rank = next(iter(evidence.items()))
                if rank <= 5 and (engine not in rare or rank < rare[engine][0]):
                    rare[engine] = (rank, entry['id'])
        self.rare = {identifier for _, identifier in rare.values()}
        self.remaining = sorted(entries, key=lambda e: (e['id'] not in self.explicit,
                                e['id'] not in self.rare))
        self.admitted = []
        self.ledger = []

    def advance(self, scores):
        if self.ledger:
            covered = set()
            for entry in self.admitted:
                score = scores.get(entry['id'], -1)
                bar = self.bars[entry['type']]
                if score > bar or (entry['type'] == 'prompt' and score == bar):
                    covered.update(self.matches[entry['id']])
            missing = set(range(len(self.goals))) - covered
            self.ledger[-1]['goal_coverage'] = {
                'supplied': len(self.goals), 'covered_indices': sorted(covered),
                'semantics': 'literal authored metadata overlap for qualifying scores',
            }
            if not self.remaining:
                self.ledger[-1]['stop_detail'] = 'POOL_EXHAUSTED'
                return []
            if len(self.ledger) == len(CAPS):
                self.ledger[-1]['stop_detail'] = 'STAGE_CAP_REACHED'
                return []
            explicit_pending = any(e['id'] in self.explicit for e in self.remaining)
            gap_pending = any(self.matches[e['id']] & missing for e in self.remaining)
            if explicit_pending:
                reason = 'EXPLICIT_PENDING'
            elif gap_pending:
                reason = 'UNRESOLVED_CURRENT_STEP_WITH_AUTHORED_EVIDENCE'
            else:
                self.ledger[-1]['stop_detail'] = 'NO_REMAINING_AUTHORED_COVERAGE_EVIDENCE'
                return []
            self.remaining.sort(key=lambda e: (e['id'] not in self.explicit,
                                -len(self.matches[e['id']] & missing)))
        else:
            reason = 'INITIAL'
        if not self.remaining:
            return []
        cap = CAPS[len(self.ledger)]
        count = cap - len(self.admitted)
        selected, self.remaining = self.remaining[:count], self.remaining[count:]
        self.admitted.extend(selected)
        self.ledger.append({'cap': cap, 'reason': reason, 'ids': [e['id'] for e in selected]})
        return selected
