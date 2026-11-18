"""Single request-local task-state contract; no inference or persistence."""
import hashlib
import json
import re

VERSION = 'skill_hub.task_state.v1'
TEXT = ('current_step', 'desired_output')
LISTS = ('artifact_types', 'available_tools', 'constraints', 'explicit_skill_ids')
LOADED = 'loaded_skill_ids_and_versions'


def normalize_task_state(state, task_state=None):
    def invalid(field, reason):
        raise ValueError(f'{field}: {reason}')
    if not isinstance(state, str) or not state.strip():
        invalid('state', 'must be a nonempty string')
    if task_state is not None and not isinstance(task_state, dict):
        invalid('task_state', 'must be an object or null')
    supplied = task_state if task_state is not None else {}
    for key in supplied:
        if key not in (*TEXT, *LISTS, LOADED, 'schema_version'):
            invalid(str(key), 'unknown field')
    if supplied.get('schema_version', VERSION) != VERSION:
        invalid('schema_version', 'unsupported version')
    result = {'schema_version': VERSION, 'state': state}
    for key in (*TEXT, *LISTS, LOADED):
        value = supplied.get(key)
        if value is not None:
            if key in TEXT:
                if not isinstance(value, str):
                    invalid(key, 'must be text or null')
            elif not isinstance(value, list):
                invalid(key, 'must be an array or null')
            elif key != LOADED:
                if any(not isinstance(item, str) for item in value):
                    invalid(key, 'items must be strings')
            else:
                for i, item in enumerate(value):
                    field = f'{key}[{i}]'
                    if not isinstance(item, dict) or set(item) - {'id', 'version', 'content_sha256'}:
                        invalid(field, 'invalid identity fields')
                    if not isinstance(item.get('id'), str) or not item['id'].strip():
                        invalid(field + '.id', 'must be nonempty text')
                    for attr in ('version', 'content_sha256'):
                        if item.get(attr) is not None and not isinstance(item[attr], str):
                            invalid(field + '.' + attr, 'must be text or null')
                    if item.get('content_sha256') is not None and not re.fullmatch('[0-9a-f]{64}', item['content_sha256']):
                        invalid(field + '.content_sha256', 'must be a SHA256 hex digest')
        # Copy caller containers without rewriting literal values or list order.
        result[key] = json.loads(json.dumps(value, ensure_ascii=False))
    return result


def task_state_hash(normalized):
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def render_task_state(normalized):
    fields = (*TEXT, *LISTS, LOADED)
    if all(normalized[key] is None for key in fields):
        return normalized['state']
    return normalized['state'] + '\n\nSupplied task context (null means unknown):\n' + '\n'.join(
        key.replace('_', ' ').capitalize() + ': ' + json.dumps(normalized[key], ensure_ascii=False)
        for key in fields)
