"""Metadata for successful manual MCP loads after the caller's host safety gate.

These events prove served text only, never route approval, application or execution.
"""
from __future__ import annotations

import hashlib
import uuid

import hub_telemetry as telemetry


def record_served_resource(item, kind, store, *, host_allowed=False, load_id=None):
    """Record exact returned body bytes without persisting body, query or prose.

    Call only on the successful response path, after body truncation/identity handling.
    ``host_allowed`` describes that caller's host gate, not runtime-shortlist approval.
    A telemetry failure must never turn a successful load into a tool error.
    """
    result = {'recorded': False, 'load_id': None}
    try:
        if host_allowed is not True or kind not in ('skill', 'prompt') or not isinstance(item, dict):
            return result
        name, body = item.get('name'), item.get('body')
        if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
            return result
        if 'error' in item:
            return result
        identifier = load_id if load_id is not None else uuid.uuid4().hex
        identity = item.get('identity') if isinstance(item.get('identity'), dict) else {}
        encoded = body.encode('utf-8')
        fields = {'resource_kind': kind, 'loader_route': 'MCP_GET', 'served': True,
                  'content_sha256': hashlib.sha256(encoded).hexdigest(),
                  'bytes': len(encoded), 'policy_status': 'CHECKS_PASSED',
                  'body_truncated': identity.get('body_truncated') is True}
        result['recorded'] = bool(telemetry.emit_load(
            store, fields, load_id=identifier, resource_id=kind + ':' + name))
        if result['recorded']:
            result['load_id'] = identifier
    except Exception:
        # Diagnostics are best effort; never echo a body or sink exception into the MCP result.
        return {'recorded': False, 'load_id': None}
    return result
