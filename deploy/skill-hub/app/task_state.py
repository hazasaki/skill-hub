"""Single request-local task-state contract; no inference or persistence."""
import hashlib
import json
import re

VERSION = 'skill_hub.task_state.v1'
TEXT = ('current_step', 'desired_output')
LISTS = ('artifact_types', 'available_tools', 'constraints', 'explicit_skill_ids')
LOADED = 'loaded_skill_ids_and_versions'
OPTIONAL = ('required_capabilities', 'requested_resource_types', 'language')


def normalize_task_state(state, task_state=None):
    def invalid(field, reason):
        raise ValueError(f'{field}: {reason}')
    if not isinstance(state, str) or not state.strip():
        invalid('state', 'must be a nonempty string')
    if task_state is not None and not isinstance(task_state, dict):
        invalid('task_state', 'must be an object or null')
    supplied = task_state if task_state is not None else {}
    for key in supplied:
        if key not in (*TEXT, *LISTS, LOADED, *OPTIONAL, 'schema_version'):
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
                    if not isinstance(item, dict) or set(item) - {'id', 'version', 'content_sha256', 'bundle_sha256'}:
                        invalid(field, 'invalid identity fields')
                    if not isinstance(item.get('id'), str) or not item['id'].strip():
                        invalid(field + '.id', 'must be nonempty text')
                    for attr in ('version', 'content_sha256', 'bundle_sha256'):
                        if item.get(attr) is not None and not isinstance(item[attr], str):
                            invalid(field + '.' + attr, 'must be text or null')
                    if item.get('content_sha256') is not None and not re.fullmatch('[0-9a-f]{64}', item['content_sha256']):
                        invalid(field + '.content_sha256', 'must be a SHA256 hex digest')
                    if item.get('bundle_sha256') is not None and not re.fullmatch('[a-f0-9]{64}', item['bundle_sha256']):
                        invalid(field + '.bundle_sha256', 'must be a SHA256 hex digest')
        # Copy caller containers without rewriting literal values or list order.
        result[key] = json.loads(json.dumps(value, ensure_ascii=False))
    for field in OPTIONAL:
        if field not in supplied:
            continue
        value = supplied[field]
        if value is not None:
            if field == 'language':
                if not isinstance(value, str) or not re.fullmatch('[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', value):
                    invalid(field, 'must be a language tag or null')
            elif (not isinstance(value, list) or len(value) > 16
                  or any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in value)):
                invalid(field, 'must be up to 16 nonempty strings of up to 160 characters')
            elif field == 'requested_resource_types' and any(item not in ('skill', 'prompt') for item in value):
                invalid(field, 'items must be skill or prompt')
        result[field] = json.loads(json.dumps(value, ensure_ascii=False))
    return result


def routing_hints(task):
    types = task.get('requested_resource_types')
    state = task.get('state') or ''
    if types is None:
        # Require request wording. Mere mentions such as "fix prompt caching" do not select a type.
        types = re.findall(
            r'\b(?:need|want|find|request|looking for|cần|tìm|xin)\s+(?:an?\s+|one\s+|một\s+)?'
            r'(?:[\w-]+\s+){0,4}?(skill|prompt)\s+(?=for\b|to\b|covering\b|để\b|kiểm\b)',
            state, flags=re.IGNORECASE)
        types = [kind.lower() for kind in types]
        if set(types) and re.search(r'\b(?:and|và)\s+(?:an?|one|một)\s+(skill|prompt)\b', state, re.I):
            types = ['skill', 'prompt']
    language = task.get('language')
    if not language:
        # Vietnamese-specific letters are stronger evidence than accents shared with other languages.
        language = 'vi' if re.search(r'[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]', state, re.I) else None
        if language is None and re.search(r'\b(?:the|and|for|with|from|need|find|write|review)\b', state, re.I):
            language = 'en'
    return set(types), language, set(task.get('required_capabilities') or [])


def task_state_hash(normalized):
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def render_task_state(normalized):
    fields = (*TEXT, *LISTS, LOADED, *(key for key in OPTIONAL if key in normalized))
    if all(normalized[key] is None for key in fields):
        return normalized['state']
    return normalized['state'] + '\n\nSupplied task context (null means unknown):\n' + '\n'.join(
        key.replace('_', ' ').capitalize() + ': ' + json.dumps(normalized[key], ensure_ascii=False)
        for key in fields)
