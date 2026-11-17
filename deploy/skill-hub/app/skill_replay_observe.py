#!/usr/bin/env python3
"""Observation half of the native replay harness: read a child's stream and session.

Split out of `skill_replay_runner.py` (round 12) along the boundary Hermes set:
this module observes and summarises evidence, it never launches a process and never
decides a verdict. `execution_preflight` stays in the runner because it is the gate.

Everything here is pure or read-only: parsing already-captured JSONL, hashing an
artifact tree, and reading the exact own session file. Text, prompts, arguments and
command output are never retained.

`skill_replay_runner` re-exports every public name, so existing callers and the six
external consumers keep working unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ID_KEYS = ('thread_id', 'session_id', 'conversation_id', 'threadId', 'sessionId')
MODEL_KEYS = ('model', 'model_name', 'modelId')
# Item kinds that represent a tool or command the CLI actually ran. Matched after
# normalising case, because the CLI uses `CommandExecution` and `command_execution` alike.
TOOL_ITEM_KINDS = ('mcp_tool_call', 'function_call', 'custom_tool_call', 'command_execution',
                   'tool_call', 'web_search', 'local_shell_call', 'patch_apply')
# Items whose name is a wrapper the CLI ran, not a nested tool it observed.
WRAPPER_KINDS = ('custom_tool_call', 'function_call')
SHA64 = re.compile(r'[0-9a-f]{64}')
# An MCP result packs its payload as JSON text; bound what is parsed from a string field.
MAX_INLINE_JSON = 1_000_000
IDENT = re.compile(r'[\w.:/-]{1,120}', re.ASCII)
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
# Error classes only; the raw stderr text is never stored.
STDERR_CLASSES = ((re.compile(r'auth|login|credential|token', re.I), 'auth'),
                  (re.compile(r'permission|denied|EACCES|EPERM', re.I), 'permission'),
                  (re.compile(r'not found|ENOENT|No such file', re.I), 'not_found'),
                  (re.compile(r'rate.?limit|quota|429', re.I), 'rate_limit'),
                  (re.compile(r'timeout|timed out', re.I), 'timeout'),
                  (re.compile(r'sandbox', re.I), 'sandbox'),
                  (re.compile(r'model|profile', re.I), 'model_or_profile'))


def _identify(value):
    return value if isinstance(value, str) and IDENT.fullmatch(value) else None


def _kind(value):
    """Canonical item kind: `CommandExecution`, `command_execution` and
    `command-execution` all become `command_execution`."""
    name = _identify(value)
    if not name:
        return None
    return _CAMEL.sub('_', name).replace('-', '_').lower()


def _find_identity_hash(node, depth=0):
    """A `returned_body_sha256` actually present in a structured event, else None.

    Only the hash is kept; the surrounding payload is never read into the report. An MCP
    result carries its payload as a JSON *string* inside `content[].text`, so a bounded
    JSON-looking string is parsed and searched too; anything else is not executed.
    """
    if depth > 6:
        return None
    if isinstance(node, str):
        text = node.strip()
        if not text.startswith('{') or len(text) > MAX_INLINE_JSON:
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            return None
        # Same depth: unwrapping a JSON string is not one more level of nesting.
        return _find_identity_hash(parsed, depth)
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'returned_body_sha256' and isinstance(value, str) and SHA64.fullmatch(value):
                return value
            found = _find_identity_hash(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for entry in node[:20]:
            found = _find_identity_hash(entry, depth + 1)
            if found:
                return found
    return None


def _item_of(event):
    """The item object inside one event, for both stream shapes.

    `codex exec --json` puts it at `item`; the session rollout puts the item at `payload`,
    or one level deeper at `payload.item`.
    """
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    for candidate in (event.get('item'), payload.get('item'), payload):
        if isinstance(candidate, dict) and _identify(candidate.get('type')):
            return candidate
    return {}


def summarize_events(lines):
    """Allowlist extraction from `codex exec --json` JSONL. Text is never stored.

    Unnamed command items are kept with `name: None`: dropping them would make "no tool
    events" indistinguishable from "tool events the collector cannot label".
    """
    types = {}
    item_types = {}
    thread_ids = []
    models = []
    tools = []
    identity_hashes = []
    unparsed = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        if not isinstance(event, dict):
            unparsed += 1
            continue
        kind = _identify(event.get('type'))
        item = _item_of(event)
        raw_item_type = _identify(item.get('type'))
        item_type = _kind(raw_item_type)
        label = kind or raw_item_type
        if label:
            types[label] = types.get(label, 0) + 1
        if item_type:
            item_types[item_type] = item_types.get(item_type, 0) + 1
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        for holder in (event, payload, item):
            for key in ID_KEYS + (('id',) if kind == 'session_meta' else ()):
                found = _identify(holder.get(key))
                if found and found not in thread_ids:
                    thread_ids.append(found)
            for key in MODEL_KEYS:
                found = _identify(holder.get(key))
                if found and found not in models:
                    models.append(found)
        if item_type in TOOL_ITEM_KINDS:
            name = item.get('name') or item.get('tool') or item.get('tool_name')
            tools.append({'kind': item_type,
                          'name': _identify(name),
                          'status': _identify(item.get('status')),
                          'name_observed': bool(name)})
        found = _find_identity_hash(event)
        if found and found not in identity_hashes:
            identity_hashes.append(found)
    return {'event_types': dict(sorted(types.items())),
            'item_types': dict(sorted(item_types.items())),
            'thread_ids': thread_ids,
            'models': models,
            'tool_calls': tools,
            'identity_hashes': identity_hashes,
            'unparsed_lines': unparsed}


def tool_coverage(events):
    """Explicit verdict on what the stream let us observe. Absence is unknown, not zero."""
    calls = events['tool_calls']
    named = [call for call in calls if call['name']]
    unnamed = [call for call in calls if not call['name']]
    wrappers = sorted({call['name'] for call in calls
                       if call['name'] and call['kind'] in WRAPPER_KINDS})
    if not events['item_types']:
        status = 'unknown_no_item_events'
    elif not calls:
        status = 'unknown_no_tool_items'
    elif unnamed and not named:
        status = 'observed_unnamed_commands'
    else:
        status = 'observed_tool_items'
    return {'status': status,
            'tool_item_count': len(calls) if calls else None,
            'named_count': len(named) if calls else None,
            'unnamed_count': len(unnamed) if calls else None,
            'wrapper_names': wrappers,
            'error_item_count': events['item_types'].get('error', 0),
            'nested_mentions_are_not_calls': True}


def classify_stderr(stderr):
    """Error classes and sizes only; the message text itself is dropped."""
    text = (stderr or '').strip()
    classes = sorted({label for pattern, label in STDERR_CLASSES if pattern.search(text)})
    return {'stderr_bytes': len(text.encode('utf-8', 'replace')),
            'stderr_lines': len(text.splitlines()) if text else 0,
            'stderr_classes': classes}


def snapshot_tree(root):
    """Relative path, size and sha256 of every file the worker left behind."""
    root = Path(root)
    entries = []
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        data = path.read_bytes()
        entries.append({'path': path.relative_to(root).as_posix(),
                        'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    return entries


def snapshot_dirs(root):
    """Directories that hold no file at all: a skeleton request creates them empty."""
    root = Path(root)
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*')
                  if path.is_dir() and not any(child.is_file() for child in path.rglob('*')))


def compare_trees(before, after):
    """Created, modified (hash really changed) and deleted files; unchanged is not modified."""
    before_map = {entry['path']: entry['sha256'] for entry in before}
    after_map = {entry['path']: entry['sha256'] for entry in after}
    created = [entry for entry in after if entry['path'] not in before_map]
    modified = [entry for entry in after
                if entry['path'] in before_map and before_map[entry['path']] != entry['sha256']]
    unchanged = [entry['path'] for entry in after
                 if before_map.get(entry['path']) == entry['sha256']]
    deleted = sorted(path for path in before_map if path not in after_map)
    return {'created': created, 'existing_modified': modified,
            'existing_unchanged': unchanged, 'deleted': deleted}


def collect_native_session(thread_id, started, env=None):
    """Read only the exact own thread under the inherited native home, metadata only."""
    env = os.environ if env is None else env
    home = Path(env.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    root = home / 'sessions'
    result = {'session_root': str(root),
              'root_source': 'CODEX_HOME' if env.get('CODEX_HOME') else 'native_default',
              'coverage': 'unknown_missing_session', 'wrapper_count': None,
              'nested_operation_count': None, 'policy_rejection_count': None,
              'hub_surface_observed': None,
              'contexts': [], 'attribution': None}
    if not isinstance(thread_id, str) or not re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread_id):
        result['coverage'] = 'unknown_thread_identity'
        return result
    # Date-bounded filename matching reads no unrelated session contents.
    day = datetime.fromtimestamp(started, timezone.utc)
    matches = set()
    for delta in (-1, 0, 1):
        folder = root / (day + timedelta(days=delta)).strftime('%Y/%m/%d')
        matches.update(folder.glob('rollout-*-' + thread_id + '.jsonl'))
    if len(matches) != 1:
        if matches:
            result['coverage'] = 'unknown_ambiguous_session'
        return result
    path = next(iter(matches))
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        result['coverage'] = 'unknown_session_path'
        return result
    try:
        data = path.read_bytes()
        events = [json.loads(line) for line in data.decode('utf-8').splitlines() if line.strip()]
        if any(not isinstance(e, dict) for e in events):
            raise ValueError('malformed session')
    except (OSError, ValueError, UnicodeError):
        result['coverage'] = 'unknown_unreadable_session'
        return result
    metas = [e.get('payload', {}) for e in events if e.get('type') == 'session_meta']
    if len(metas) != 1 or metas[0].get('id') != thread_id:
        result['coverage'] = 'unknown_session_identity'
        return result
    wrappers, rejected, hub_seen = 0, 0, False
    for event in events:
        payload = event.get('payload', {})
        if not isinstance(payload, dict):
            continue
        if event.get('type') in ('session_meta', 'turn_context'):
            context = {'type': event['type']}
            for key in ('id', 'turn_id', 'model', 'effort', 'approval_policy', 'cli_version', 'model_provider'):
                value = _identify(payload.get(key))
                if value:
                    context[key] = value
            sandbox = payload.get('sandbox_policy')
            if isinstance(sandbox, dict) and _identify(sandbox.get('type')):
                context['sandbox_policy'] = {'type': sandbox['type']}
            result['contexts'].append(context)
        if event.get('type') != 'response_item':
            continue
        if payload.get('type') == 'custom_tool_call':
            wrappers += 1
        if payload.get('type') in ('custom_tool_call_output', 'function_call_output'):
            output = payload.get('output', '')
            if isinstance(output, list):
                output = '\n'.join(p.get('text', '') for p in output if isinstance(p, dict))
            # Only the child's tool OUTPUT, not policy prose, inputs or tool lists.
            # This is a lower bound on exposure, never authentication or skill use.
            if payload.get('type') == 'custom_tool_call_output' and isinstance(output, str):
                hub_seen |= 'mcp__skill_hub__' in output
            if isinstance(output, str) and 'blocked by policy' in output.lower():
                rejected += 1
    from skill_replay_attribution import summarize_source_events
    result.update({'coverage': 'observed_partial', 'wrapper_count': wrappers,
                   'hub_surface_observed': hub_seen,
                   'hub_surface_evidence_source': 'own_custom_tool_call_output',
                   'policy_rejection_count': rejected,
                   'policy_error_reason': 'blocked_by_policy' if rejected else None,
                   'session_sha256': hashlib.sha256(data).hexdigest(),
                   'attribution': summarize_source_events(events, expected_source='hub')})
    return result
