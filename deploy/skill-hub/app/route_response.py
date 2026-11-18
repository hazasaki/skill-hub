"""Bounded route transport with process-local, expiring, capability-addressed snapshots."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict

from pydantic_core import to_json

BUDGET = 16384
TEXT_BUDGET = 3900  # FastMCP emits both text and structured result, escaping each JSON string.


def encode(value):
    return to_json(value, inf_nan_mode='constants').decode('utf-8')



class ResponseStore:
    # shortcut: snapshots expire on restart or after ten minutes; reroute after expiry.
    def __init__(self, *, ttl=600, max_bytes=64 * 1024 * 1024, clock=time.monotonic):
        self.ttl, self.max_bytes, self.clock = ttl, max_bytes, clock
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def _expire(self):
        now = self.clock()
        for key, (expires, _, _) in list(self.entries.items()):
            if expires <= now:
                del self.entries[key]

    def render(self, payload, mode='full'):
        if mode not in ('compact', 'full'):
            return encode({'error': 'INVALID_RESPONSE_MODE'})
        raw_bytes = to_json(payload, inf_nan_mode='constants')
        raw = raw_bytes.decode('utf-8')
        size = len(raw_bytes)
        if mode == 'full' or size <= TEXT_BUDGET:
            return raw
        if size > self.max_bytes:
            return encode({'error': 'RESPONSE_STORE_FULL', 'bytes': size,
                           'detail': 'Result exceeds snapshot capacity; request response_mode=full explicitly.'})
        digest = hashlib.sha256(raw_bytes).hexdigest()
        token = secrets.token_hex(24)
        with self.lock:
            self._expire()
            used = sum(entry[2] for entry in self.entries.values())
            while self.entries and (used + size > self.max_bytes or len(self.entries) >= 32):
                _, old = self.entries.popitem(last=False)
                used -= old[2]
            self.entries[token] = (self.clock() + self.ttl, raw, size)
        result = dict(payload)
        transport = {'mode': 'compact', 'complete': False, 'full_sha256': digest,
                     'full_bytes': size, 'full_chars': len(raw), 'expires_in_seconds': self.ttl,
                     'fetch': {'tool': 'get_route_result', 'result_id': token, 'offset': 0},
                     'counts': {'selected': len(payload.get('selected') or []),
                                'loads': {key: len(value) for key, value in (payload.get('loads') or {}).items()
                                          if isinstance(value, list)}},
                     'omitted': {}, 'body_delivery': 'Full bodies require full fetch before use.'}
        result['transport'] = transport
        loads = result.get('loads')
        if isinstance(loads, dict):
            loads = {key: [dict(item) if isinstance(item, dict) else item for item in value]
                     if isinstance(value, list) else value for key, value in loads.items()}
            result['loads'] = loads
            for group, items in loads.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get('body'), str):
                        body = item.pop('body')
                        item['body_delivery'] = 'DEFERRED_FULL_FETCH'
                        item['body_bytes'] = len(body.encode('utf-8'))
                        item['deferred_body_sha256'] = hashlib.sha256(body.encode('utf-8')).hexdigest()
        # Keep whole fields whenever possible. Every omitted field has an exact count and hash.
        fields = {key: encode(value).encode('utf-8') for key, value in result.items()
                  if key != 'transport'}
        total = sum(len(value) + len(encode(key).encode('utf-8')) + 2
                    for key, value in fields.items()) + len(encode(transport).encode('utf-8')) + 16
        for key in sorted(fields, key=lambda key: len(fields[key]), reverse=True):
            if total <= TEXT_BUDGET:
                break
            original = payload[key]
            field_bytes = fields[key] if original is result[key] else encode(original).encode('utf-8')
            descriptor = {'delivery': 'DEFERRED_FULL_FETCH', 'path': '/' + key,
                          'items': len(original) if isinstance(original, (dict, list)) else 1,
                          'bytes': len(field_bytes), 'sha256': hashlib.sha256(field_bytes).hexdigest()}
            transport['omitted'][key] = descriptor
            result[key] = descriptor
            total += 2 * len(encode(descriptor).encode('utf-8')) + len(encode(key)) + 4 - len(fields[key])
        rendered = encode(result)
        if len(rendered.encode('utf-8')) > TEXT_BUDGET:
            # Per-field descriptors can exceed the envelope on ordinary diagnostic-rich routes.
            transport['omitted'] = {'snapshot': {
                'delivery': 'DEFERRED_FULL_FETCH', 'path': '/', 'items': len(payload),
                'bytes': size, 'sha256': digest}}
            summary = {}
            for key in ('schema_version', 'outcome', 'reason', 'error', 'fallback', 'trigger',
                        'partial', 'partial_reason', 'coverage'):
                if key in payload:
                    value = payload[key]
                    summary[key] = value if len(encode(value).encode('utf-8')) <= (
                        512 if key == 'coverage' else 128) else {
                            'delivery': 'DEFERRED_FULL_FETCH', 'path': '/' + key}
            rendered = encode({**summary, 'transport': transport})
            if len(rendered.encode('utf-8')) > TEXT_BUDGET:
                return encode({'error': 'RESPONSE_ENVELOPE_TOO_LARGE', 'transport': {
                    'fetch': transport['fetch'],
                    'full_sha256': digest, 'full_bytes': size}})
        return rendered

    def read(self, result_id, authorize=None):
        """Release one immutable snapshot only after this request's current authorization."""
        if (not isinstance(result_id, str) or len(result_id) != 48
                or any(c not in '0123456789abcdef' for c in result_id)):
            return None, {'error': 'INVALID_RESULT_ID'}
        with self.lock:
            self._expire()
            entry = self.entries.get(result_id)
        if entry is None:
            return None, {'error': 'RESULT_EXPIRED_OR_NOT_FOUND', 'detail': 'Reroute to create a new snapshot.'}
        _, raw, _ = entry
        if authorize is not None:
            problem = authorize(json.loads(raw))
            if problem:
                return None, problem
        return raw, None

    def fetch(self, result_id, offset=0, max_chars=8192, authorize=None):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return encode({'error': 'INVALID_OFFSET'})
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            return encode({'error': 'INVALID_PAGE_SIZE'})
        raw, problem = self.read(result_id, authorize)
        if problem:
            return encode(problem)
        if offset > len(raw):
            return encode({'error': 'INVALID_OFFSET'})
        size = len(raw.encode('utf-8'))
        end = min(len(raw), offset + min(max_chars, 8192))
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        while True:
            page = raw[offset:end]
            rendered = encode({'result_id': result_id, 'offset': offset,
                               'next_offset': end if end < len(raw) else None,
                               'complete': end == len(raw), 'total_chars': len(raw), 'total_bytes': size,
                               'sha256': digest, 'page_sha256': hashlib.sha256(page.encode('utf-8')).hexdigest(),
                               'text': page})
            wire_bytes = len(json.dumps({'content': [{'type': 'text', 'text': rendered}],
                                         'structuredContent': {'result': rendered},
                                         'isError': False}, ensure_ascii=False).encode('utf-8'))
            if wire_bytes <= BUDGET - 256:
                return rendered
            end = offset + max(1, (end - offset) * (BUDGET - 512) // wire_bytes)
