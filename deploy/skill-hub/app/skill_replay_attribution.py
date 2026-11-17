"""Attribute a parsed native session's *source references* and MCP skill calls to a skill origin.

Round 04 found same-name shadowing: the CLI read a skill from its own
``.system/skill-creator`` tree, while the pilot was measuring
``.agent-hub/skills/skill-creator``. Agreement on the skill *name* therefore says
nothing about which copy ran.

Round 05 added native ``McpToolCall`` embedded-result support: in Codex CLI rollout streams,
``event_msg`` with ``payload.type == "item_completed"`` and ``payload.item.type == "McpToolCall"``
carries ``id``, ``server``, ``tool``, ``status``, and an embedded ``result`` object
(``content``, ``isError``, ``structuredContent``) on the same item rather than a separate
``*_output`` event with ``call_id``.

This module summarises already-parsed events and answers one narrow question: which
explicit, qualified skill paths or completed ``skill-hub`` ``get_skill`` calls did completed
events reference, and does that match an expected origin? It retains no command text,
arguments, output, skill body, reasoning or prompt, and it never treats a name mention as
evidence of selection.

Pure: no filesystem, network, subprocess, or writes. Inputs are parsed dicts; nothing in
them is executed or imported. See SKILL-QUALITY-ATTRIBUTION.md for the limits.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 1

IDENT = re.compile(r'[\w.:/-]{1,120}', re.ASCII)
HEX64 = re.compile(r'[0-9a-f]{64}')
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')

# Qualified skill paths. The leading delimiter keeps a path fragment from matching
# inside a longer arbitrary string; the trailing boundary lets a bare directory path
# (`.../skills/foo`) match without a trailing slash. The captured group is a bounded slug.
_LEAD = r'(?:^|[\s"\'=:(/\\])'
_TAIL = r'(?=/|$|[\s"\'`,;:)])'
_HUB = re.compile(_LEAD + r'\.agent-hub/skills/([A-Za-z0-9._-]{1,64})' + _TAIL, re.I)
_SYSTEM = re.compile(_LEAD + r'\.system/(?:skills/)?([A-Za-z0-9._-]{1,64})' + _TAIL, re.I)
_OTHER = re.compile(
    _LEAD + r'(?:~|/|[A-Za-z]:)?(?:[A-Za-z0-9._-]+/)+skills/([A-Za-z0-9._-]{1,64})' + _TAIL,
    re.I)

# A command with these is compound enough that attributing one source is guesswork.
_SHELL_OPS = ('&&', '||', ';', '|', '$(', '`', '\n')

# Codex 0.166 runs shell through a `functions.exec` JS wrapper, so the command text sits in
# the wrapper's `input`, not in a `command` field. Only a literal cmd/command object argument
# (`cmd: "..."`) is read: an assignment or a variable is not the executed command.
WRAPPER_KINDS = ('custom_tool_call', 'function_call')
_WRAPPER_ARG = re.compile(
    r'''["']?(?:cmd|command)["']?\s*:\s*(?:"((?:[^"\\]|\\.){0,4000})"|'((?:[^'\\]|\\.){0,4000})')''')

TOOL_KINDS = ('mcp_tool_call', 'function_call', 'custom_tool_call', 'command_execution',
              'tool_call', 'local_shell_call', 'patch_apply')
OUTPUT_KINDS = ('custom_tool_call_output', 'function_call_output', 'mcp_tool_call_output',
                'tool_output', 'tool_result')
# Only these fields are ever inspected for a path, and only for completed tool items.
COMMAND_FIELDS = ('command', 'command_line', 'cmd', 'argv', 'args', 'arguments')

CLASSES = ('local_system', 'hub', 'other')
MAX_REFERENCES = 32
MAX_HINTS = 32
MAX_BODY_BYTES = 1_000_000
MAX_INPUT_BYTES = 16_000


def _ident(value):
    return value if isinstance(value, str) and IDENT.fullmatch(value) else None


def _slug(value):
    return value if isinstance(value, str) and SLUG.fullmatch(value) else None


def _kind(value):
    name = _ident(value)
    if not name:
        return None
    return _CAMEL.sub('_', name).replace('-', '_').lower()


def _item_of(event):
    """The item inside one event, for both stream shapes (`item`, `payload.item`, `payload`)."""
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    for candidate in (event.get('item'), payload.get('item'), payload):
        if isinstance(candidate, dict) and _ident(candidate.get('type')):
            return candidate
    return {}


def _call_id(*nodes):
    for node in nodes:
        if isinstance(node, dict):
            found = (_ident(node.get('call_id')) or _ident(node.get('callId'))
                     or _ident(node.get('id')))
            if found:
                return found
    return None


def _completed(item):
    """True only for an event that reports success. Absent status is not success."""
    status = item.get('status')
    if not isinstance(status, str) or status.strip().lower() not in ('completed', 'succeeded',
                                                                    'success', 'ok'):
        return False
    exit_code = item.get('exit_code', item.get('exitCode'))
    if exit_code is None:
        return True
    return isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code == 0


def _command_text(item, kind=None):
    """Joined inspectable text for a completed tool item; never retained.

    For a `functions.exec` wrapper the shell command is the single literal `cmd`/`command`
    argument. Two such arguments in one wrapper are ambiguous, so no text is returned.
    """
    if kind in WRAPPER_KINDS:
        raw = item.get('input') if isinstance(item.get('input'), str) else ''
        if raw:
            matches = [d if d else s for d, s in _WRAPPER_ARG.findall(raw[:MAX_INPUT_BYTES])]
            if len(matches) != 1:
                return ''
            return re.sub(r'\\(.)', r'\1', matches[0]).strip()
    parts = []
    for field in COMMAND_FIELDS:
        value = item.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(entry for entry in value[:64] if isinstance(entry, str))
    return ' '.join(parts) if parts else ''


def _classify_paths(text):
    """Set of (class, skill) from a command string."""
    flat = text.replace('\\', '/')
    found = set()
    for match in _HUB.finditer(flat):
        found.add(('hub', match.group(1)))
    for match in _SYSTEM.finditer(flat):
        found.add(('local_system', match.group(1)))
    for match in _OTHER.finditer(flat):
        name = match.group(1)
        # `<root>/skills/.system/<name>` first matches `.system` itself as the slug; a
        # directory name is not a skill slug, so it is not a second source in that path.
        if not _slug(name):
            continue
        if not any(name == other for _, other in found):
            found.add(('other', name))
    return found


def _parse_json_dict(text):
    if not isinstance(text, str) or len(text) > MAX_BODY_BYTES:
        return None
    stripped = text.strip()
    if not stripped.startswith('{'):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_from_response_dict(doc):
    """Extract identity fields from a single canonical top-level skill response dict."""
    if not isinstance(doc, dict):
        return None
    identity = doc.get('identity') if isinstance(doc.get('identity'), dict) else {}
    raw_sha = doc.get('returned_body_sha256')
    if raw_sha is None:
        raw_sha = identity.get('returned_body_sha256')
    if raw_sha is not None and (not isinstance(raw_sha, str) or not HEX64.fullmatch(raw_sha)):
        return {'malformed': True}
    sha = raw_sha

    raw_trunc = doc.get('body_truncated')
    if raw_trunc is None:
        raw_trunc = identity.get('body_truncated')
    if raw_trunc is not None and not isinstance(raw_trunc, bool):
        return {'malformed': True}
    truncated = raw_trunc

    raw_body = doc.get('body')
    if raw_body is not None and not isinstance(raw_body, str):
        return {'malformed': True}
    body = raw_body if isinstance(raw_body, str) and len(raw_body) <= MAX_BODY_BYTES else None

    skill_name = _slug(doc.get('name'))
    if sha is None and body is None and truncated is None:
        return None
    return {
        'returned_body_sha256': sha,
        'body_truncated': truncated,
        'body': body,
        'name': skill_name,
    }


def _extract_mcp_result(result_obj, _depth=0):
    """Strictly parse canonical MCP result wrapper or legacy output dict."""
    if _depth > 4:
        return {'malformed': True}
    if isinstance(result_obj, str):
        parsed = _parse_json_dict(result_obj)
        if parsed is None:
            return {'malformed': True}
        result_obj = parsed
    if not isinstance(result_obj, dict):
        return {'malformed': True}

    if 'isError' in result_obj or 'is_error' in result_obj:
        err = result_obj.get('isError', result_obj.get('is_error'))
        if err is not False:
            return {'is_error': True}

    candidates = []
    has_wrapper = False

    if 'content' in result_obj:
        has_wrapper = True
        content = result_obj.get('content')
        if not isinstance(content, list) or not content:
            return {'malformed': True}
        text_items = []
        for entry in content[:16]:
            if not isinstance(entry, dict):
                return {'malformed': True}
            if entry.get('type') in (None, 'text') and 'text' in entry:
                if not isinstance(entry.get('text'), str):
                    return {'malformed': True}
                doc = _parse_json_dict(entry['text'])
                if doc is None:
                    return {'malformed': True}
                ext = _extract_from_response_dict(doc)
                if ext is None or ext.get('malformed'):
                    return {'malformed': True}
                text_items.append(ext)
        if not text_items:
            return {'malformed': True}
        candidates.extend(text_items)

    if 'structuredContent' in result_obj and result_obj.get('structuredContent') is not None:
        has_wrapper = True
        sc = result_obj.get('structuredContent')
        if not isinstance(sc, dict):
            return {'malformed': True}
        inner = sc.get('result', sc)
        if isinstance(inner, str):
            inner = _parse_json_dict(inner)
            if inner is None:
                return {'malformed': True}
        elif not isinstance(inner, dict):
            return {'malformed': True}
        ext = _extract_from_response_dict(inner)
        if ext is None or ext.get('malformed'):
            return {'malformed': True}
        candidates.append(ext)

    if not has_wrapper:
        direct = _extract_from_response_dict(result_obj)
        if direct is not None:
            if direct.get('malformed'):
                return {'malformed': True}
            candidates.append(direct)
        else:
            for field in ('output', 'result', 'raw'):
                if field in result_obj:
                    sub = result_obj.get(field)
                    if isinstance(sub, str):
                        sub = _parse_json_dict(sub)
                    if isinstance(sub, dict):
                        return _extract_mcp_result(sub, _depth + 1)
            return {}

    if not candidates:
        return {'malformed': True}

    first = candidates[0]
    for other in candidates[1:]:
        if (other.get('returned_body_sha256') != first.get('returned_body_sha256')
                or other.get('body_truncated') != first.get('body_truncated')
                or other.get('body') != first.get('body')
                or (other.get('name') and first.get('name')
                    and other.get('name') != first.get('name'))):
            return {'inconsistent_wrapper': True}
    return first


def _evaluate_candidate(call_id, extracted, require_body=False):
    """Evaluate one extracted candidate into a body_identity dict."""
    if not isinstance(extracted, dict) or not extracted:
        return None
    if extracted.get('is_error'):
        return {'status': 'unknown', 'observed_body_sha256': None, 'call_id': call_id,
                'body_present': False, 'recomputed_matches_declared': None,
                'body_truncated': None, 'reason': 'mcp_result_is_error'}
    if extracted.get('malformed'):
        return {'status': 'unknown', 'observed_body_sha256': None, 'call_id': call_id,
                'body_present': False, 'recomputed_matches_declared': None,
                'body_truncated': None, 'reason': 'malformed_get_skill_result'}
    if extracted.get('inconsistent_wrapper'):
        return {'status': 'inconsistent', 'observed_body_sha256': None, 'call_id': call_id,
                'body_present': False, 'recomputed_matches_declared': False,
                'body_truncated': None, 'reason': 'conflicting_mcp_result_wrappers'}

    declared = extracted.get('returned_body_sha256')
    body = extracted.get('body')
    truncated = extracted.get('body_truncated')
    if not declared:
        return None

    recomputed = None
    if isinstance(body, str):
        normalized = body.replace('\r\n', '\n').replace('\r', '\n')
        recomputed = hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    if truncated is True:
        status, reason = 'truncated', 'declared_hash_covers_prefix_only'
    elif recomputed is not None and recomputed != declared:
        status, reason = 'inconsistent', 'recomputed_body_differs_from_declared'
    elif require_body and recomputed is None:
        status, reason = 'unknown', 'missing_body_for_recompute'
    else:
        status, reason = 'observed', 'linked_successful_get_skill_result'

    return {
        'status': status,
        'observed_body_sha256': declared if status in ('observed', 'inconsistent', 'truncated') else None,
        'call_id': call_id,
        'body_present': isinstance(body, str),
        'recomputed_matches_declared': None if recomputed is None else recomputed == declared,
        'body_truncated': truncated,
        'reason': reason,
        '_skill_name': extracted.get('name'),
    }


def _strip_internal(identity_dict):
    return {k: v for k, v in identity_dict.items() if not k.startswith('_')}


def summarize_source_events(events, expected_source=None):
    """Summarise which skill origin a parsed event stream actually referenced.

    ``events`` is an iterable of already-parsed event dicts. ``expected_source`` may be a
    class name (``'hub'``/``'local_system'``/``'other'``) or a qualified path; anything
    unrecognised yields ``source_relation: 'unknown'`` rather than a guess.
    """
    if isinstance(events, (str, bytes, dict)) or events is None:
        raise ValueError('events must be an iterable of parsed event objects')

    count = 0
    unparsed = 0
    item_types = {}
    references = []
    hints = []
    calls = {}
    outputs = {}
    embedded_records = {}
    seen_ref_ids = set()

    for event in events:
        count += 1
        if not isinstance(event, dict):
            unparsed += 1
            continue
        item = _item_of(event)
        kind = _kind(item.get('type')) or _kind(event.get('type'))
        if kind:
            item_types[kind] = item_types.get(kind, 0) + 1
        if not item:
            continue
        call_id = _call_id(item, event)
        if kind in TOOL_KINDS:
            name = _kind(item.get('name') or item.get('tool') or item.get('tool_name'))
            has_embedded_result = 'result' in item and item.get('result') is not None
            if call_id and not has_embedded_result:
                calls.setdefault(call_id, []).append((kind, item))

            if not _completed(item):
                if len(hints) < MAX_HINTS:
                    hints.append({'reason': 'incomplete_or_failed_event',
                                  'skill': _slug(name)})
                if has_embedded_result and name == 'get_skill' and call_id:
                    embedded_records.setdefault(call_id, []).append({
                        'status': 'unknown', 'observed_body_sha256': None,
                        'call_id': call_id, 'body_present': False,
                        'recomputed_matches_declared': None, 'body_truncated': None,
                        'reason': 'incomplete_or_failed_get_skill_call',
                        '_skill_name': None,
                    })
                continue

            if has_embedded_result:
                if name == 'get_skill':
                    if not call_id:
                        embedded_records.setdefault('__missing_id__', []).append({
                            'status': 'unknown', 'observed_body_sha256': None,
                            'call_id': None, 'body_present': False,
                            'recomputed_matches_declared': None, 'body_truncated': None,
                            'reason': 'missing_item_id', '_skill_name': None,
                        })
                    else:
                        ext = _extract_mcp_result(item.get('result'))
                        ev_res = _evaluate_candidate(call_id, ext, require_body=True)
                        if ev_res is None:
                            ev_res = {
                                'status': 'unknown', 'observed_body_sha256': None,
                                'call_id': call_id, 'body_present': False,
                                'recomputed_matches_declared': None, 'body_truncated': None,
                                'reason': 'malformed_get_skill_result', '_skill_name': None,
                            }
                        server = _slug(item.get('server'))
                        ev_res['_server'] = server.lower() if server else None
                        ev_res['_kind'] = kind
                        embedded_records.setdefault(call_id, []).append(ev_res)

            text = _command_text(item, kind)
            if not text:
                continue
            if any(op in text for op in _SHELL_OPS):
                if len(hints) < MAX_HINTS:
                    hints.append({'reason': 'compound_command_not_attributed',
                                  'skill': None})
                continue
            found = _classify_paths(text)
            if len(found) > 1:
                if len(hints) < MAX_HINTS:
                    hints.append({'reason': 'multiple_sources_in_one_event',
                                  'skill': None})
                continue
            if not found:
                continue
            source_class, skill = next(iter(found))
            if len(references) < MAX_REFERENCES:
                references.append({'skill': _slug(skill),
                                   'source_class': source_class,
                                   'kind': kind,
                                   'status': 'completed',
                                   'call_id': call_id})
        elif kind and kind in OUTPUT_KINDS:
            if call_id:
                outputs.setdefault(call_id, []).append(item)

    body, verified_mcp_refs = _resolve_body_identity(calls, outputs, embedded_records)
    for ref in verified_mcp_refs:
        if len(references) < MAX_REFERENCES and ref['call_id'] not in seen_ref_ids:
            seen_ref_ids.add(ref['call_id'])
            references.append(ref)

    class_counts = {name: 0 for name in CLASSES}
    for reference in references:
        class_counts[reference['source_class']] += 1
    observed = ([name for name in CLASSES if class_counts[name]] or ['unknown'])
    observed_source = observed[0] if len(observed) == 1 else 'unknown'
    if len(observed) > 1 and len(hints) < MAX_HINTS:
        hints.append({'reason': 'conflicting_source_classes', 'skill': None})

    expected_class = _expected_class(expected_source)
    if observed_source == 'unknown' or expected_class == 'unknown':
        relation = 'unknown'
    elif observed_source == expected_class:
        relation = 'match'
    else:
        relation = 'mismatch'

    return {
        'schema_version': SCHEMA_VERSION,
        'event_count': count,
        'unparsed_events': unparsed,
        'item_types': dict(sorted(item_types.items())),
        'tool_item_count': sum(cnt for k, cnt in item_types.items() if k in TOOL_KINDS),
        'source_references': references,
        'source_class_counts': class_counts,
        'observed_source': observed_source,
        'expected_source': expected_class,
        'source_relation': relation,
        'hints': hints,
        'body_identity': body,
        'version_status': 'observed' if body['status'] == 'observed' else 'unknown',
        'selection_proof': 'body_identity_only' if body['status'] == 'observed' else 'none',
        'limits': [
            'source_references are path references in completed events, not proof of selection',
            'no command text, arguments, output, reasoning or prompt is retained',
            'loaded version is never inferred from disk or artifact hashes',
        ],
    }


def _expected_class(expected_source):
    if expected_source is None:
        return 'unknown'
    if isinstance(expected_source, str):
        token = expected_source.strip().lower()
        if token in CLASSES:
            return token
        found = _classify_paths(token)
        if len(found) == 1:
            return next(iter(found))[0]
    return 'unknown'


def _dedup_group(call_id, group):
    """Collapse duplicate records for the same call_id; conflicting duplicates -> inconsistent."""
    first = group[0]
    for other in group[1:]:
        if (other.get('status') != first.get('status')
                or other.get('observed_body_sha256') != first.get('observed_body_sha256')
                or other.get('recomputed_matches_declared') != first.get('recomputed_matches_declared')
                or other.get('body_truncated') != first.get('body_truncated')
                or other.get('_skill_name') != first.get('_skill_name')
                or other.get('_server') != first.get('_server')):
            return {
                'status': 'inconsistent',
                'observed_body_sha256': None,
                'call_id': None if call_id == '__missing_id__' else call_id,
                'body_present': any(r.get('body_present') for r in group),
                'recomputed_matches_declared': False,
                'body_truncated': None,
                'reason': 'conflicting_duplicate_item_id',
            }
    return first


def _resolve_body_identity(calls, outputs, embedded_records):
    """Resolve body identity across embedded McpToolCall items and legacy paired outputs."""
    unknown = {'status': 'unknown', 'observed_body_sha256': None, 'call_id': None,
               'body_present': False, 'recomputed_matches_declared': None,
               'body_truncated': None, 'reason': 'no_linked_get_skill_result'}

    resolved_items = []
    verified_mcp_refs = []

    for call_id, group in embedded_records.items():
        deduped = _dedup_group(call_id, group)
        resolved_items.append(deduped)
        if (deduped.get('status') == 'observed'
                and deduped.get('recomputed_matches_declared') is True
                and deduped.get('_server') == 'skill-hub'
                and deduped.get('_skill_name')):
            verified_mcp_refs.append({
                'skill': deduped['_skill_name'],
                'source_class': 'hub',
                'kind': deduped.get('_kind', 'mcp_tool_call'),
                'status': 'completed',
                'call_id': call_id,
            })

    for call_id, call_list in calls.items():
        if call_id in embedded_records or call_id not in outputs:
            continue
        valid_calls = []
        invalid_calls = False
        for kind, item in call_list:
            name = _kind(item.get('name') or item.get('tool') or item.get('tool_name'))
            if name != 'get_skill':
                continue
            if not _completed(item):
                invalid_calls = True
            else:
                valid_calls.append((kind, item))
        if not valid_calls and not invalid_calls:
            continue
        if invalid_calls and valid_calls:
            resolved_items.append({
                'status': 'inconsistent', 'observed_body_sha256': None,
                'call_id': call_id, 'body_present': False,
                'recomputed_matches_declared': False, 'body_truncated': None,
                'reason': 'conflicting_duplicate_item_id',
            })
            continue
        if not valid_calls:
            continue
        out_group = []
        for out_item in outputs[call_id]:
            ext = _extract_mcp_result(out_item)
            ev_res = _evaluate_candidate(call_id, ext, require_body=False)
            if ev_res is not None:
                out_group.append(ev_res)
        if out_group:
            resolved_items.append(_dedup_group(call_id, out_group))

    if not resolved_items:
        return unknown, []

    if len(resolved_items) == 1:
        return _strip_internal(resolved_items[0]), verified_mcp_refs

    first = resolved_items[0]
    for other in resolved_items[1:]:
        if (other.get('status') != first.get('status')
                or other.get('observed_body_sha256') != first.get('observed_body_sha256')
                or other.get('recomputed_matches_declared') != first.get('recomputed_matches_declared')
                or other.get('body_truncated') != first.get('body_truncated')):
            return {
                'status': 'inconsistent',
                'observed_body_sha256': None,
                'call_id': first.get('call_id'),
                'body_present': any(r.get('body_present') for r in resolved_items),
                'recomputed_matches_declared': False,
                'body_truncated': None,
                'reason': 'conflicting_get_skill_results',
            }, []

    return _strip_internal(first), verified_mcp_refs
