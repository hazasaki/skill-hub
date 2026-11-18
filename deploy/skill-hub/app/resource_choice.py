"""Select a request-local set using verified resources and authored declarations."""

def references(metadata, field):
    raw = metadata.get(field, [])
    if not isinstance(raw, list):
        raise ValueError(field)
    values = [item.get('resource_id') if isinstance(item, dict) and field == 'dependencies' else item
              for item in raw]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(field)
    return dict.fromkeys(values)


def recommend(items, entries, task, explicit, verify, limits, byte_limit, *, identify=lambda _: None,
              qualify=None, requested_kinds=(), language=None, compatible_menu=False):
    from shortlist_runtime import is_language_compatible, parse_canonical_name
    pool = {'skills': [], 'prompts': [], 'excluded': []}
    by_id = {item['entry']['id']: item for item in items}
    names = {}
    for identifier, entry in entries.items():
        names.setdefault(entry['resource_id'], []).append(identifier)

    def resolve(name):
        if name in entries:
            return name
        matches = names.get(name, [])
        return matches[0] if len(matches) == 1 else None

    qualify = qualify or resolve
    known = {}
    for claim in task.get('loaded_skill_ids_and_versions') or []:
        identifier = qualify(claim['id'])
        if identifier in entries and claim.get('content_sha256'):
            entry = entries[identifier]
            if (claim['content_sha256'] == (entry.get('resource_card') or {}).get('source_raw_sha256')
                    and (claim.get('bundle_sha256') is None or claim['bundle_sha256'] == entry.get('bundle_sha256'))
                    and (claim.get('version') is None
                         or claim['version'] == entry.get('choice_metadata', {}).get('version'))):
                known[identifier] = claim

    checked, issues, stack = {}, {}, set()

    def inspect(identifier):
        if identifier in checked:
            return checked[identifier]
        if identifier in issues:
            return None
        if identifier in stack:
            issues[identifier] = ('DEPENDENCY_CYCLE', identifier)
            return None
        if len(stack) >= 20:
            issues[identifier] = ('DEPENDENCY_POOL_LIMIT', identifier)
            return None
        stack.add(identifier)
        try:
            entry = entries[identifier]
            metadata = entry.get('choice_metadata', {})
            try:
                deps = {raw: resolve(raw) for raw in references(metadata, 'dependencies')}
                conflicts = {target for raw in references(metadata, 'conflicts_with')
                             if (target := qualify(raw)) is not None}
            except ValueError:
                issues[identifier] = ('INVALID_DECLARATION', None)
                return None
            for raw, target in deps.items():
                if not target:
                    issues[identifier] = ('DEPENDENCY_MISSING_OR_AMBIGUOUS', raw)
                    return None
                if target not in by_id and target not in known:
                    issues[identifier] = ('DEPENDENCY_NOT_QUALIFIED', target)
                    return None
                if inspect(target) is None:
                    cause, related = issues[target]
                    issues[identifier] = (cause if cause.startswith('DEPENDENCY_') else 'DEPENDENCY_DENIED',
                                          related or target)
                    return None
            receipt, rejected = verify(entry, deps)
            if rejected:
                issues[identifier] = (rejected['outcome'], None)
                return None
            if identifier in known and receipt['body_raw_sha256'] != known[identifier]['content_sha256']:
                issues[identifier] = ('CONTENT_CHANGED', None)
                return None
            cap = metadata.get('capability') or []
            cap = [cap] if isinstance(cap, str) else cap
            capabilities = ({label.strip().casefold() for label in cap if isinstance(label, str) and label.strip()}
                            if isinstance(cap, list) else set())
            role = metadata.get('prompt_role') or {'agent_role': 'role', 'task_template': 'task'}.get(
                metadata.get('prompt_type'))
            family, _, inferred_language = parse_canonical_name(entry['resource_id'])
            authored_language = metadata.get('language')
            checked[identifier] = {'dependencies': deps, 'conflicts': conflicts,
                                   'source_raw_sha256': receipt['body_raw_sha256'],
                                   'bundle_sha256': receipt.get('bundle_sha256'),
                                   'version': metadata.get('version'),
                                   'family': entry.get('family_id') or family,
                                   'language': authored_language if isinstance(authored_language, str) else inferred_language,
                                   'capabilities': capabilities, 'body_bytes': receipt['body_bytes'],
                                   'conflict_identity': receipt.get('conflict_identity'),
                                   'identity': (entry['type'], receipt['body_raw_sha256'], receipt.get('bundle_sha256')),
                                   'role': role if entry['type'] == 'prompt' and role in ('core', 'role', 'task') else None}
            return checked[identifier]
        finally:
            stack.remove(identifier)

    active = {identifier for identifier in known if inspect(identifier) is not None}
    while True:
        complete = {node for node in active if set(checked[node]['dependencies'].values()) <= active}
        if complete == active:
            break
        active = complete
    for identifier in known.keys() - active:
        if identifier not in by_id:
            issues[identifier] = ('DEPENDENCY_NOT_LOADED', identifier)
            checked.pop(identifier, None)
    for identifier in sorted(by_id):
        inspect(identifier)

    # Inspection inserts dependencies before dependents.
    for identifier, info in checked.items():
        missing = next((node for node in info['dependencies'].values()
                        if node not in checked or node in issues), None)
        if missing:
            issues[identifier] = ('DEPENDENCY_NOT_LOADED', missing)
    for identifier in issues:
        checked.pop(identifier, None)

    from rust_routing import Operation, decision
    verified = {
        identifier: {
            **info, 'dependencies': list(info['dependencies'].values()),
            'conflicts': sorted(info['conflicts']), 'capabilities': sorted(info['capabilities']),
            'kind': entries[identifier]['type'],
            'language_compatible': is_language_compatible(info['language'], language),
            'score': round(by_id[identifier]['score'], 2) if identifier in by_id else None,
        }
        for identifier, info in checked.items()
    }
    conflict_ids = {target for info in checked.values() for target in info['conflicts']} - checked.keys()
    selection = decision(Operation.RESOURCE_CHOICE, {
        'verified': verified, 'active': sorted(active), 'explicit': sorted(explicit),
        'kinds': sorted(requested_kinds),
        'required': [label.strip().casefold() for label in task.get('required_capabilities') or []],
        'step': (task.get('current_step') or '').strip().casefold(),
        'limits': limits, 'byte_limit': byte_limit, 'compatible_menu': compatible_menu,
        'conflict_identities': {target: identify(target) for target in sorted(conflict_ids)},
    })
    admitted, chosen = selection['admitted'], selection['chosen']
    reasons = selection['reasons']
    issues.update(selection['issues'])

    for identifier in admitted:
        entry, info = entries[identifier], checked[identifier]
        pool[entry['type'] + 's'].append({
            'id': identifier, 'resource_id': entry['resource_id'], 'score': by_id[identifier]['score'],
            'recommended': identifier in chosen, 'reason': reasons.get(identifier, 'DEPENDENCY_ALTERNATIVE'),
            'body_bytes': info['body_bytes'], 'role': info['role'],
            'source_raw_sha256': info['source_raw_sha256'],
            'bundle_sha256': info['bundle_sha256'], 'version': info['version'],
            'capabilities': sorted(info['capabilities']), 'language': info['language'],
            'dependencies': list(dict.fromkeys(info['dependencies'].values())),
        })
    for identifier in sorted(by_id.keys() & issues.keys() - set(admitted)):
        reason, related = issues[identifier]
        pool['excluded'].append({'id': identifier, 'reason': reason,
                                 **({'related_id': related} if related else {})})
    return pool, list(chosen), active, {identifier: info['dependencies'] for identifier, info in checked.items()}
