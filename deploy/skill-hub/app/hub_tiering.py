"""D3: evidence-based catalog tiering.

A resource is ``hot``, ``warm`` or ``cold`` only after telemetry has observed it enough
times. Everything else stays ``unclassified``, which consumers must treat exactly like
``warm``. The neutral default is the point: no seeded hot list, no guessed 300, no score
guess standing in for usage.

Promotion needs two independent signals, loads and attested use, because a load alone does
not prove the resource was helpful. Demotion to ``cold`` needs age *and* silence, so a
resource is never demoted for being new or for having no traffic in a short window.

This module is a pure function over recorded events. It never writes to the catalog, never
scores, never changes thresholds and never imports the router.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

HOT = 'hot'
WARM = 'warm'
COLD = 'cold'
NEUTRAL = 'unclassified'  # consumers treat this as warm; it is the default for a new resource

TIERS = (HOT, WARM, COLD, NEUTRAL)
CONSUMER_TIER = {HOT: HOT, WARM: WARM, COLD: COLD, NEUTRAL: WARM}

DEFAULT_POLICY = {
    # Minimum recorded observations before any tier may be assigned.
    'min_observations': 3,
    # Hot: enough loads, and attested use, inside the window.
    'hot_min_loads': 5,
    'hot_min_attestations': 2,
    'hot_min_attested_rate': 0.5,
    # Warm: at least one load inside the window.
    'warm_min_loads': 1,
    # Cold: zero loads for this many days, and the resource is at least this old.
    'cold_after_days': 30,
    'cold_min_age_days': 30,
    # The observation window itself.
    'window_days': 14,
}


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        text = value.replace('Z', '+00:00')
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def validate_policy(policy):
    """Reject a policy that would let the module invent tiers without evidence."""
    merged = dict(DEFAULT_POLICY)
    if policy:
        unknown = set(policy) - set(DEFAULT_POLICY)
        if unknown:
            raise ValueError('unknown policy keys: %s' % ','.join(sorted(unknown)))
        merged.update({k: v for k, v in policy.items() if v is not None})
    for name in ('min_observations', 'hot_min_loads', 'hot_min_attestations',
                 'warm_min_loads', 'cold_after_days', 'cold_min_age_days', 'window_days'):
        value = merged[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('policy %s must be a non-negative integer' % name)
    if merged['min_observations'] < 1:
        raise ValueError('min_observations must be at least 1; a tier without an '
                         'observation is a guess')
    if merged['hot_min_loads'] < 1:
        raise ValueError('hot_min_loads must be at least 1')
    if merged['hot_min_attestations'] < 1:
        raise ValueError('hot_min_attestations must be at least 1; loads alone do not '
                         'show a resource was used')
    rate = merged['hot_min_attested_rate']
    if not isinstance(rate, (int, float)) or not (0 < float(rate) <= 1):
        raise ValueError('hot_min_attested_rate must be in (0, 1]')
    return merged


def observe(events, as_of=None, now=None):
    """Count unique served loads and matching attestations, preserving lifetime age.

    Recent evidence controls promotion; historical observations allow an old, silent
    resource to become cold. Invalid/future timestamps cannot supply evidence.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    observations, loads, attestations = {}, {}, {}
    for event in events:
        if not isinstance(event, dict):
            continue
        when = _parse_ts(event.get('emitted_at'))
        resource, load_id = event.get('resource_id'), event.get('load_id')
        if not when or when > now or not resource or not load_id:
            continue
        kind, fields = event.get('kind'), event.get('fields') or {}
        if kind == 'load' and fields.get('served') is True:
            # One load id identifies one served body, even if a sink repeats the event.
            loads.setdefault(load_id, (event, when))
        elif kind == 'attest' and fields.get('verdict') in ('used', 'not_used'):
            previous = attestations.get(load_id)
            if previous is None or previous[1] <= when:
                attestations[load_id] = (event, when)
    for load_id, (event, when) in loads.items():
        resource = event['resource_id']
        entry = observations.setdefault(resource, {
            'resource_id': resource, 'loads': 0, 'attestations': 0, 'used': 0,
            'observations': 0, 'lifetime_observations': 0,
            'last_seen': when, 'first_seen': when, 'load_ids': 0,
        })
        entry['first_seen'] = min(entry['first_seen'], when)
        entry['last_seen'] = max(entry['last_seen'], when)
        entry['lifetime_observations'] += 1
        entry['load_ids'] += 1
        if as_of is None or when >= as_of:
            entry['loads'] += 1
        attested = attestations.get(load_id)
        if attested is not None:
            evidence, attested_at = attested
            if (evidence.get('resource_id') == resource
                    and evidence.get('route_id') == event.get('route_id')
                    and attested_at >= when):
                entry['last_seen'] = max(entry['last_seen'], attested_at)
                entry['lifetime_observations'] += 1
                if as_of is None or attested_at >= as_of:
                    entry['attestations'] += 1
                    entry['used'] += evidence['fields']['verdict'] == 'used'
    for entry in observations.values():
        entry['observations'] = entry['loads'] + entry['attestations']
    return observations


def assign(events, catalog=None, policy=None, now=None):
    """Compute a tier manifest. Deterministic for a given event set and clock.

    ``catalog`` is an optional iterable of resource ids that must appear in the manifest.
    A resource in the catalog with no observations is ``unclassified``, never ``hot``.
    """
    rules = validate_policy(policy)
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    cutoff = now - dt.timedelta(days=rules['window_days'])
    observations = observe(events, as_of=cutoff, now=now)

    ids = set(observations)
    if catalog is not None:
        ids |= {str(item) for item in catalog}
    if not ids:
        return _manifest({}, rules, now, observations)

    tiers = {}
    for resource in sorted(ids):
        entry = observations.get(resource, {'loads': 0, 'attestations': 0, 'used': 0,
                                            'observations': 0, 'first_seen': None,
                                            'last_seen': None})
        tier, basis = _classify(entry, rules, now)
        attestations = entry['attestations']
        tiers[resource] = {
            'tier': tier,
            'consumer_tier': CONSUMER_TIER[tier],
            'basis': basis,
            'evidence': {
                'loads_in_window': entry['loads'],
                'attestations_in_window': attestations,
                'used_in_window': entry['used'],
                'observations': entry['observations'],
                'first_seen': entry['first_seen'].isoformat() if entry['first_seen'] else None,
                'last_seen': entry['last_seen'].isoformat() if entry['last_seen'] else None,
            },
        }
    return _manifest(tiers, rules, now, observations)


def _classify(entry, rules, now):
    observations = entry.get('observations', 0)
    if entry.get('lifetime_observations', observations) < rules['min_observations']:
        return NEUTRAL, 'insufficient_observations'

    loads = entry['loads']
    attestations = entry['attestations']
    used = entry['used']
    if (observations >= rules['min_observations']
            and loads >= rules['hot_min_loads']
            and attestations >= rules['hot_min_attestations']):
        rate = used / attestations
        if rate >= float(rules['hot_min_attested_rate']):
            return HOT, 'observed_loads_and_attested_use'
        return WARM, 'attested_use_below_rate'
    if loads >= rules['warm_min_loads']:
        return WARM, 'observed_loads'
    if attestations >= rules['hot_min_attestations']:
        return WARM, 'attested_without_loads'

    last = entry.get('last_seen')
    first = entry.get('first_seen')
    if last is not None and first is not None:
        silent_days = (now - last).total_seconds() / 86400.0
        age_days = (now - first).total_seconds() / 86400.0
        if silent_days >= rules['cold_after_days'] and age_days >= rules['cold_min_age_days']:
            return COLD, 'silent_beyond_window'
    return NEUTRAL, 'insufficient_positive_evidence'


def _manifest(tiers, rules, now, observations):
    counts = {name: 0 for name in TIERS}
    for entry in tiers.values():
        counts[entry['tier']] += 1
    body = {
        'schema': 'skill-hub.tiering.v1',
        'as_of': now.isoformat(timespec='seconds'),
        'policy': rules,
        'counts': counts,
        'resources': len(tiers),
        'tiers': tiers,
    }
    body['manifest_sha256'] = hashlib.sha256(
        json.dumps({'as_of': None, 'policy': rules, 'tiers': tiers},
                   sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if counts[HOT] and not observations:
        raise AssertionError('a hot tier without observations is not evidence')
    return body


def tier_of(manifest, resource_id):
    """The consumer-facing tier for one resource: unclassified reads as warm."""
    entry = (manifest.get('tiers') or {}).get(resource_id)
    if not entry:
        return WARM
    return entry.get('consumer_tier', WARM)


def hot_ids(manifest):
    """Resources with real evidence. Empty until telemetry has been observed."""
    return sorted(name for name, entry in (manifest.get('tiers') or {}).items()
                  if entry.get('tier') == HOT)
