"""Select a request-local set using verified resources and authored declarations."""

from collections import Counter


def references(metadata, field):
    raw = metadata.get(field, [])
    if not isinstance(raw, list):
        raise ValueError(field)
    values = [item.get('resource_id') if isinstance(item, dict) and field == 'dependencies' else item
              for item in raw]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(field)
    return dict.fromkeys(values)


def recommend(items, entries, task, explicit, verify, limits, byte_limit, *, identify=lambda _: None, qualify=None):
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
            checked[identifier] = {'dependencies': deps, 'conflicts': conflicts,
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

    def closure(identifier):
        ordered = {}
        def add(node):
            if node in active or node in ordered:
                return
            for dependency in checked[node]['dependencies'].values():
                add(dependency)
            ordered[node] = None
        add(identifier)
        return list(ordered)

    def conflict(left, right):
        def declares(source, target):
            refs = checked[source]['conflicts']
            identity = checked[target]['conflict_identity']
            return target in refs or (identity is not None and any(
                (checked[ref]['conflict_identity'] if ref in checked else identify(ref)) == identity
                for ref in refs))
        return declares(left, right) or declares(right, left)

    step = (task.get('current_step') or '').strip().casefold()
    relevant = {identifier for identifier in by_id if identifier in checked and step
                and step in checked[identifier]['capabilities']}
    ranked = sorted((identifier for identifier in by_id if identifier in checked),
                    key=lambda identifier: (identifier not in explicit, identifier not in relevant,
                                             -by_id[identifier]['score'], checked[identifier]['body_bytes'],
                                             identifier))
    admitted, chosen, choices = {}, {}, {}
    covered = set().union(*(checked[node]['capabilities'] for node in active))
    roles = {checked[node]['role'] for node in active} - {None}
    served = 0
    for identifier in ranked:
        if identifier in chosen:
            continue
        group = closure(identifier)
        members = group + sorted(active)
        conflicting = next((right for i, left in enumerate(group) for right in members[i + 1:]
                            if conflict(left, right)), None)
        if conflicting:
            issues[identifier] = ('DECLARED_CONFLICT', conflicting)
            continue
        additions = [node for node in group if node not in admitted]
        if identifier in active and identifier not in admitted:
            additions = [identifier]
        group_counts = Counter(entries[node]['type'] for node in (*admitted, *additions))
        if group_counts['skill'] > 12 or group_counts['prompt'] > 8:
            issues[identifier] = ('CHOICE_POOL_LIMIT', None)
            continue
        for node in additions:
            admitted[node] = None
        info = checked[identifier]
        pending = [node for node in group if node not in chosen]
        cost = sum(checked[node]['body_bytes'] for node in pending)
        counts = Counter(entries[node]['type'] for node in (*chosen, *pending))
        if identifier in active:
            reason = 'ALREADY_LOADED_EXACT'
        elif any(conflict(node, other) for node in pending for other in chosen):
            reason = 'DECLARED_CONFLICT'
        elif (identifier not in explicit and (info['identity'][0] == 'prompt' or info['identity'][2])
              and any(info['identity'] == checked[node]['identity'] for node in (*active, *chosen))):
            reason = 'EXACT_BODY_OVERLAP'
        elif identifier not in explicit and info['capabilities'] and info['capabilities'] <= covered:
            reason = 'CAPABILITY_OVERLAP'
        elif identifier not in explicit and info['role'] in roles:
            reason = 'PROMPT_ROLE_COVERED'
        elif identifier not in explicit and relevant and info['capabilities'] and identifier not in relevant:
            reason = 'OTHER_STEP'
        elif (any(counts[kind] > limits[kind] for kind in counts)
              or (byte_limit is not None and (served or len(pending) > 1) and served + cost > byte_limit)):
            reason = 'LOAD_LIMIT' if any(counts[kind] > limits[kind] for kind in counts) else 'BYTE_BUDGET'
        else:
            reason = ('EXPLICIT_REQUEST' if identifier in explicit else
                      'ADDS_CAPABILITY' if info['capabilities'] else
                      'ADDS_PROMPT_ROLE' if info['role'] else 'SCORE_ONLY')
            for node in pending:
                chosen[node] = reason if node == identifier else 'REQUIRED_DEPENDENCY'
                covered.update(checked[node]['capabilities'])
                if checked[node]['role']:
                    roles.add(checked[node]['role'])
            served += cost
        choices[identifier] = reason

    for identifier in admitted:
        entry, info = entries[identifier], checked[identifier]
        pool[entry['type'] + 's'].append({
            'id': identifier, 'resource_id': entry['resource_id'], 'score': by_id[identifier]['score'],
            'recommended': identifier in chosen, 'reason': chosen.get(identifier, choices.get(identifier, 'DEPENDENCY_ALTERNATIVE')),
            'body_bytes': info['body_bytes'], 'role': info['role'],
            'dependencies': list(dict.fromkeys(info['dependencies'].values())),
        })
    for identifier in sorted(by_id.keys() & issues.keys() - admitted.keys()):
        reason, related = issues[identifier]
        pool['excluded'].append({'id': identifier, 'reason': reason,
                                 **({'related_id': related} if related else {})})
    return pool, list(chosen), active, {identifier: info['dependencies'] for identifier, info in checked.items()}
