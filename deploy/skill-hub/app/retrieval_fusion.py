"""TEMPR reciprocal-rank fusion over four independent candidate engines.

Semantic, keyword, graph and temporal retrieval answer different questions. None of them
is authoritative, so this module fuses their *ranks*, never their raw scores: a cosine
similarity, a BM25 score and an edge count are not comparable numbers, but "first for this
engine" is.

Rules this module enforces, one place each:

    one vote per group per engine   An alias, or a second chunk of the same resource, adds
                                    no vote. Collapse happens inside the engine, before the
                                    sum, so a chatty resource cannot outvote a real one.
    ranks only, rank starts at 1     score = sum(weight / (k + rank)).
    eligible ids only               A hit for an id the caller did not offer is dropped and
                                    counted. Engines cannot widen the candidate set.
    collapse before the quota       The 250-slot budget is spent on distinct groups, then
                                    refilled from other groups, never on duplicates.
    stable tie-break                Equal scores order by id, so two runs of the same query
                                    return the same list.
    explicit degradation            An engine that is missing, late or broken is reported by
                                    status and reason. It never looks like "no match".

Two hashes are deliberately kept apart and never compared as if they were the same thing:

    catalog_sha256              SHA-256 of the candidate catalog *bytes* on disk.
    catalog_projection_sha256   The logical digest the metadata projection carries for the
                                rows it describes. It is a different construction over a
                                different input, so equality would be a coincidence.

A caller may pin either; this module reports which one it verified.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from portable_paths import portable_path

SCHEMA_VERSION = 'tempr.fusion.v1'
METHOD = 'tempr_rrf_v1'
ENGINES = ('semantic', 'keyword', 'graph', 'temporal')
# Semantic, keyword and graph carry evidence about *what* the task is. Temporal only says
# *when*, so it votes only when the query actually expresses a time intent; otherwise its
# weight is zero and the engine abstains (NOT_TEMPORAL) rather than ranking by recency noise.
DEFAULT_WEIGHTS = {'semantic': 1.0, 'keyword': 1.0, 'graph': 1.0, 'temporal': 1.0}
TEMPORAL_INTENT_WEIGHT = 0.0
RRF_K = 60
CANDIDATE_LIMIT = 250
SKILL_QUOTA = 200
PROMPT_QUOTA = 50
STATUSES = ('OK', 'EMPTY', 'TIMEOUT', 'ERROR', 'UNAVAILABLE')
DEGRADED_STATUSES = ('TIMEOUT', 'ERROR', 'UNAVAILABLE')
CONFIG_ENV = 'SKILL_HUB_TEMPR_CONFIG'


# ------------------------------------------------------------------ artifact binding

_ARTIFACT_DIGEST_CACHE = {}
_ARTIFACT_DIGEST_LOCK = threading.Lock()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _artifact(path, expected, *, hash_kind):
    """Describe one artifact and whether the pinned digest matches the bytes on disk."""
    entry = {'path': str(path) if path else None, 'hash_kind': hash_kind,
             'expected_sha256': expected, 'exists': False, 'sha256': None,
             'match': False}
    if not path:
        entry['reason'] = 'NOT_CONFIGURED'
        return entry
    target = Path(path)
    if not target.is_file():
        entry['reason'] = 'MISSING'
        return entry
    entry['exists'] = True
    try:
        stat = target.stat()
        cache_key = (str(target), hash_kind, stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        with _ARTIFACT_DIGEST_LOCK:
            cached = _ARTIFACT_DIGEST_CACHE.get(cache_key)
        if cached is not None:
            entry['sha256'] = cached
            if hash_kind != 'bytes':
                entry['match'] = bool(expected) and cached == expected
            else:
                entry['match'] = cached == expected if expected else True
            return entry
    if hash_kind != 'bytes':
        # Compare the artifact's declaration, not the expected value against itself.
        # Row-level identity is validated by the projection loader separately.
        try:
            computed = json.loads(target.read_text(encoding='utf-8')).get('catalog_sha256')
        except (OSError, ValueError, AttributeError):
            entry['reason'] = 'INVALID_LOGICAL_PROJECTION'
            return entry
        if cache_key is not None and computed is not None:
            with _ARTIFACT_DIGEST_LOCK:
                if len(_ARTIFACT_DIGEST_CACHE) >= 64:
                    _ARTIFACT_DIGEST_CACHE.clear()
                _ARTIFACT_DIGEST_CACHE[cache_key] = computed
        entry['sha256'] = computed
        entry['match'] = bool(expected) and computed == expected
        return entry
    computed = _sha256_file(target)
    if cache_key is not None:
        with _ARTIFACT_DIGEST_LOCK:
            if len(_ARTIFACT_DIGEST_CACHE) >= 64:
                _ARTIFACT_DIGEST_CACHE.clear()
            _ARTIFACT_DIGEST_CACHE[cache_key] = computed
    entry['sha256'] = computed
    entry['match'] = computed == expected if expected else True
    return entry


def load_config(path=None):
    """Read the runtime artifact seam, or return an unconfigured one.

    The seam is a JSON document so that a deployment names every artifact and its digest in
    one reviewable place. Nothing is guessed from the catalog itself: a wrong or missing pin
    is reported as such instead of being silently replaced by whatever is on disk.

    Resolution order is explicit and reported in ``config_source``, because "TEMPR is on but no
    artifacts were named" and "TEMPR is off" must not look the same:

        argument           the caller named a file (route_task(retrieval_config=...))
        environment        SKILL_HUB_TEMPR_CONFIG
        none               both absent; reported, never guessed from the filesystem

    This function does not carry a machine-specific default on purpose: a host that wants one
    names it (the delivery adapter does, in its own module), so a stale candidate path can
    never be picked up as if an operator had selected it.
    """
    if path:
        location, origin = path, 'argument'
    elif os.environ.get(CONFIG_ENV):
        location, origin = os.environ[CONFIG_ENV], 'environment'
    else:
        return {'configured': False, 'reason': 'NOT_CONFIGURED', 'config_source': 'none',
                'source': None, 'artifacts': {}}
    return _load_config_file(location, origin)


def _load_config_file(location, origin):
    target = portable_path(location)
    if not target.is_file():
        return {'configured': False, 'reason': 'CONFIG_MISSING', 'config_source': origin,
                'source': str(target), 'artifacts': {}}
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        return {'configured': False, 'reason': f'CONFIG_UNREADABLE:{type(error).__name__}',
                'config_source': origin, 'source': str(target), 'artifacts': {}}
    if not isinstance(payload, dict):
        return {'configured': False, 'reason': 'CONFIG_MALFORMED', 'config_source': origin,
                'source': str(target), 'artifacts': {}}
    # Rewrite only file locations in memory; retain every identity pin and original file.
    for name in ('catalog', 'catalog_projection', 'semantic', 'graph', 'temporal'):
        block = payload.get(name)
        if isinstance(block, dict) and block.get('path'):
            payload[name] = {**block, 'path': str(portable_path(block['path']))}
    return {**payload, 'configured': True, 'config_source': origin, 'source': str(target)}


def bind_artifacts(config):
    """Verify every configured artifact digest and return the binding report.

    The catalog byte hash and the projection logical digest are reported under separate keys
    with their own hash_kind. ``relation`` states that they are different constructions, so a
    reader cannot mistake one for the other.
    """
    config = config or {}
    catalog = config.get('catalog') or {}
    projection = config.get('catalog_projection') or {}
    semantic = config.get('semantic') or {}
    graph = config.get('graph') or {}
    temporal = config.get('temporal') or {}
    artifacts = {
        'catalog': _artifact(catalog.get('path'), catalog.get('sha256'), hash_kind='bytes'),
        'catalog_projection': _artifact(
            projection.get('path'), projection.get('sha256'), hash_kind='logical'),
        'semantic': _artifact(semantic.get('path'), semantic.get('sha256'), hash_kind='bytes'),
        'graph': _artifact(graph.get('path'), graph.get('sha256'), hash_kind='bytes'),
        'temporal': _artifact(temporal.get('path'), temporal.get('sha256'), hash_kind='bytes'),
    }
    return {'configured': bool(config.get('configured')),
            'source': config.get('source'),
            'config_source': config.get('config_source'),
            'config_reason': config.get('reason'),
            'catalog_sha256': (catalog.get('sha256') if artifacts['catalog']['match'] else None),
            'catalog_sha256_declared': catalog.get('sha256'),
            'catalog_projection_sha256': projection.get('sha256'),
            'catalog_projection_sha256_verified': artifacts['catalog_projection']['match'],
            'relation': 'DISTINCT_HASHES: catalog is the file bytes, projection is the '
                        'logical row digest; they are never interchangeable',
            'artifacts': artifacts}


def _manifest_for(config, kind):
    """The pin handed to one engine, built only from keys that artifact actually pins."""
    config = config or {}
    manifest = {'catalog_sha256': (config.get('catalog') or {}).get('sha256')}
    block = config.get(kind) or {}
    for key in ('taxonomy_revision', 'ontology_revision', 'temporal_revision',
                'vector_revision', 'embedding_model_digest', 'embedding_dimension'):
        if block.get(key) is not None:
            manifest[key] = block[key]
    return {key: value for key, value in manifest.items() if value is not None}


# ------------------------------------------------------------------ result contract

def engine_result(engine, status, hits=None, *, reason=None, elapsed_ms=0.0,
                  manifest=None, coverage=None):
    if status not in STATUSES:
        status = 'ERROR'
        reason = reason or 'INVALID_ENGINE_STATUS'
    return {'engine': engine, 'status': status, 'hits': list(hits or []),
            'elapsed_ms': round(float(elapsed_ms), 2), 'reason': reason,
            'manifest': manifest or {}, 'coverage': coverage or {}}


def _normalize(result, engine):
    """Coerce whatever an engine returned into the shared result shape.

    A non-conforming answer becomes ERROR, never an empty hit list: a broken engine and an
    engine that genuinely found nothing must stay distinguishable.
    """
    if not isinstance(result, dict):
        return engine_result(engine, 'ERROR', reason=f'INVALID_RESULT:{type(result).__name__}')
    status = result.get('status')
    hits, dropped = [], 0
    for hit in result.get('hits') or ():
        if not isinstance(hit, dict):
            dropped += 1
            continue
        identifier, rank = hit.get('id'), hit.get('rank')
        if not isinstance(identifier, str) or not identifier:
            dropped += 1
            continue
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            dropped += 1
            continue
        hits.append({'id': identifier, 'rank': rank,
                     'raw_score': hit.get('raw_score'),
                     'equivalence_group_id': hit.get('equivalence_group_id')})
    coverage = dict(result.get('coverage') or {})
    if dropped:
        coverage['malformed_hits_dropped'] = dropped
    return engine_result(engine, status if isinstance(status, str) else 'ERROR', hits,
                         reason=result.get('reason'), elapsed_ms=result.get('elapsed_ms') or 0.0,
                         manifest=result.get('manifest'), coverage=coverage)


# ------------------------------------------------------------------ engine callables

def _engine_semantic(text, entries, *, limit, deadline, config):
    import retrieval_semantic

    block = config.get('semantic') or {}
    if not block.get('path'):
        # No artifact named is a configuration absence, not a broken index. Left to the engine
        # it would open an empty path as a database and report ERROR, which misdescribes it.
        return engine_result('semantic', 'UNAVAILABLE', reason='NOT_CONFIGURED',
                             coverage={'embedding_source': None})
    result = retrieval_semantic.retrieve(
        text, entries, index_path=block.get('path'), limit=limit, deadline=deadline,
        manifest=_manifest_for(config, 'semantic'), embed=block.get('embed'))
    result = dict(result)
    result['coverage'] = {**(result.get('coverage') or {}),
                          'embedding_source': 'injected' if block.get('embed') else 'endpoint'}
    return result


def _projection_engine(module_name, kind):
    def call(text, entries, *, limit, deadline, config):
        block = config.get(kind) or {}
        if not block.get('path'):
            return engine_result(kind, 'UNAVAILABLE', reason='NOT_CONFIGURED')
        module = __import__(module_name)
        return module.retrieve(text, entries, projection_path=block.get('path'),
                               limit=limit, deadline=deadline,
                               manifest=_manifest_for(config, kind))
    return call


def _engine_keyword(text, entries, *, limit, deadline, config):
    """Keyword votes come from the catalog's own Porter/BM25 body index.

    The ranks are supplied by the caller because the catalog module belongs to the runtime,
    not to fusion. A caller that has no catalog cannot fake a keyword answer.
    """
    ranks = config.get('keyword_ranks')
    status = config.get('keyword_status') or {}
    if ranks is None:
        return engine_result('keyword', 'UNAVAILABLE', reason=status.get('status') or 'NO_CATALOG',
                             coverage={'matched_in_window': 0})
    if deadline is not None and time.monotonic() >= deadline:
        return engine_result('keyword', 'TIMEOUT', reason='DEADLINE_BEFORE_KEYWORD')
    # The catalog's own ranks are 0-based ordering keys. RRF needs ranks starting at 1, and
    # an order is all that is taken from BM25: its absolute scores are not comparable with a
    # cosine similarity or an edge score, so only the position is carried across.
    ordered = sorted((position, identifier)
                     for identifier, position in ranks.items()
                     if isinstance(position, int) and not isinstance(position, bool))
    hits = [{'id': identifier, 'rank': index, 'raw_score': None,
             'equivalence_group_id': None}
            for index, (_, identifier) in enumerate(ordered[:limit], start=1)]
    return engine_result('keyword', 'OK' if hits else 'EMPTY', hits,
                         reason=None if hits else 'NO_KEYWORD_MATCH',
                         coverage={'matched_in_window': len(ranks)})


def default_engines():
    return {'semantic': _engine_semantic,
            'keyword': _engine_keyword,
            'graph': _projection_engine('retrieval_graph', 'graph'),
            'temporal': _projection_engine('retrieval_temporal', 'temporal')}


def run_engines(engines, text, entries, *, limit, deadline, config):
    """Run every engine in parallel and return whatever finished before the deadline.

    Raw daemon threads are used on purpose. A ThreadPoolExecutor is joined by its own
    ``__exit__`` and by the interpreter's atexit hook, so a slow engine would hold the caller
    past its deadline. These workers are daemonic and the caller waits only for the remaining
    budget; an engine still running then is reported TIMEOUT and its eventual answer is
    discarded.
    """
    results, lock = {}, threading.Lock()
    installed = {name: engines.get(name) for name in ENGINES}
    engine_limit = max(limit * 4, 1000)

    def worker(name, call):
        started = time.perf_counter()
        try:
            answer = call(text, entries, limit=engine_limit, deadline=deadline, config=config)
        except Exception as error:  # an engine must not take the fusion call down with it
            answer = engine_result(name, 'ERROR',
                                   reason=f'ENGINE_EXCEPTION:{type(error).__name__}')
        if not isinstance(answer, dict) or 'elapsed_ms' not in answer:
            answer = dict(answer) if isinstance(answer, dict) else {}
            answer['elapsed_ms'] = (time.perf_counter() - started) * 1000.0
        with lock:
            results[name] = answer

    threads = []
    for name in ENGINES:
        call = installed[name]
        if call is None:
            with lock:
                results[name] = engine_result(name, 'UNAVAILABLE', reason='ENGINE_NOT_INSTALLED')
            continue
        thread = threading.Thread(target=worker, args=(name, call), daemon=True,
                                  name=f'tempr-engine-{name}')
        thread.start()
        threads.append((name, thread))

    for name, thread in threads:
        if deadline is None:
            thread.join()
            continue
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(remaining)
        if thread.is_alive():
            with lock:
                already = results.get(name)
            if already is None:
                with lock:
                    results[name] = engine_result(name, 'TIMEOUT',
                                                  reason='ENGINE_DEADLINE',
                                                  elapsed_ms=max(0.0, remaining) * 1000.0)
    return {name: _normalize(results.get(name), name) for name in ENGINES}


# ------------------------------------------------------------------ fusion

def _group_getter(entries, group_map):
    """Map every eligible id to its verified equivalence group, or to itself."""
    by_id = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        identifier = entry.get('id')
        if not identifier and entry.get('resource_id') and entry.get('type'):
            identifier = f'{entry["type"]}:{entry["resource_id"]}'
        if not identifier:
            continue
        group = entry.get('equivalence_group_id') or (group_map or {}).get(identifier)
        by_id[str(identifier)] = str(group) if group else str(identifier)
    return by_id


def collapse_engine_hits(result, group_of, eligible):
    """One position per group per engine, best rank wins, ineligible ids dropped.

    This is where an alias and a second chunk stop being extra votes.
    """
    best = {}
    ineligible, duplicates = 0, 0
    for hit in result.get('hits') or ():
        identifier = hit['id']
        if identifier not in eligible:
            ineligible += 1
            continue
        group = group_of.get(identifier, identifier)
        current = best.get(group)
        if current is None:
            best[group] = {'rank': hit['rank'], 'id': identifier}
        elif hit['rank'] < current['rank']:
            best[group] = {'rank': hit['rank'], 'id': identifier}
        else:
            duplicates += 1
    return best, {'ineligible_dropped': ineligible, 'group_duplicates_dropped': duplicates,
                  'groups_ranked': len(best)}


def fuse(engine_results, *, entries, weights=None, k=RRF_K, limit=CANDIDATE_LIMIT,
         skill_quota=SKILL_QUOTA, prompt_quota=PROMPT_QUOTA, group_map=None):
    """Fuse per-engine ranks into one ordered candidate list, then spend the quota.

    The returned ranking is the complete fused order over distinct groups, so a caller can
    see what was refused by budget rather than assuming the list was everything found.
    """
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    eligible = _group_getter(entries, group_map)
    kind_of = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        identifier = entry.get('id')
        if identifier:
            kind_of[str(identifier)] = entry.get('type')

    scores, votes, best_rank, collapse = {}, {}, {}, {}
    for name in ENGINES:
        result = engine_results.get(name) or engine_result(name, 'UNAVAILABLE')
        collapsed, stats = collapse_engine_hits(result, eligible, set(eligible))
        collapse[name] = stats
        if result['status'] != 'OK' or not collapsed:
            continue
        weight = float(weights.get(name, 1.0))
        if weight == 0:
            # A zero-weight engine casts no vote: temporal without time intent must not put
            # its ranking into the fused order at score 0.
            collapse[name] = {**stats, 'zero_weight': True}
            continue
        # Re-rank over groups, not over raw hit rows. Otherwise an alias or a second chunk
        # would push a genuinely different resource one position down the engine's list even
        # though it cast no vote itself.
        ordered = sorted(collapsed.items(), key=lambda item: (item[1]['rank'], item[0]))
        for position, (group, item) in enumerate(ordered, start=1):
            scores[group] = scores.get(group, 0.0) + weight / (k + position)
            votes.setdefault(group, []).append(name)
            candidates = best_rank.setdefault(group, {})
            previous = candidates.get(name)
            if previous is None or position < previous[0]:
                candidates[name] = (position, item['id'])

    ranking = []
    for group, score in scores.items():
        names = votes[group]
        # Representative: most engine confirmations, then the best rank anywhere,
        # then the id, so the choice is reproducible for the same inputs.
        ranked = best_rank[group]
        representative = min(ranked.values(), key=lambda item: (item[0], item[1]))[1]
        ranking.append({'group_id': group, 'id': representative,
                        'score': round(score, 9), 'engines': sorted(names),
                        'best_rank': {name: ranked[name][0] for name in sorted(ranked)}})
    ranking.sort(key=lambda item: (-item['score'], item['id']))

    skills = [row for row in ranking if kind_of.get(row['id']) != 'prompt']
    prompts = [row for row in ranking if kind_of.get(row['id']) == 'prompt']
    total = min(limit, len(ranking))
    skill_take = min(len(skills), max(0, min(skill_quota, total)))
    prompt_take = min(len(prompts), max(0, min(prompt_quota, total - skill_take)))
    # Refill: an unused prompt slot goes to skills and vice versa instead of staying empty.
    skill_take = min(len(skills), total - prompt_take)
    prompt_take = min(len(prompts), total - skill_take)
    chosen = skills[:skill_take] + prompts[:prompt_take]
    chosen.sort(key=lambda item: (-item['score'], item['id']))

    return {'ranking': ranking, 'selected': chosen,
            'coverage': {'eligible_ids': len(eligible), 'fused_groups': len(ranking),
                         'selected': len(chosen),
                         'skills_selected': skill_take, 'prompts_selected': prompt_take,
                         'quota': {'limit': limit, 'skills': skill_quota,
                                   'prompts': prompt_quota},
                         'group_collapse': collapse},
            'weights': {name: weights.get(name, 1.0) for name in ENGINES},
            'k': k}


def fuse_candidates(text, entries, *, config=None, deadline=None, weights=None,
                    limit=CANDIDATE_LIMIT, engines=None, group_map=None,
                    temporal_intent=None, keyword_ranks=None, keyword_status=None):
    """Full candidate routing: run the engines, fuse them, and report what happened.

    ``status`` is OK when every engine answered, DEGRADED when one or more did not but the
    fusion still produced candidates, EMPTY when nothing was found, and UNAVAILABLE when no
    engine could contribute at all. The distinction is the point: a missing index is not
    "no skill".
    """
    started = time.perf_counter()
    config = config or {}
    binding = bind_artifacts(config)
    if temporal_intent is None:
        import retrieval_temporal
        temporal_intent = bool(retrieval_temporal.detect_intent(text).get('intent'))
    # Weights and k are deployment settings, so the pinned config wins over the defaults but
    # never over an explicit argument (that is what tests and callers use to try a variant).
    configured_weights = config.get('weights')
    resolved_weights = {**DEFAULT_WEIGHTS,
                        **(configured_weights if isinstance(configured_weights, dict) else {}),
                        **(weights or {})}
    if not temporal_intent:
        resolved_weights['temporal'] = TEMPORAL_INTENT_WEIGHT
    configured_k = config.get('rrf_k')
    k = configured_k if isinstance(configured_k, int) and configured_k > 0 else RRF_K

    call_config = config
    if not config.get('configured'):
        # Unconfigured means "no artifacts named", not "run against whatever exists nearby".
        call_config = {**config, 'semantic': {}, 'graph': {}, 'temporal': {}}
    if keyword_ranks is not None or keyword_status is not None:
        call_config = {**call_config, 'keyword_ranks': keyword_ranks,
                       'keyword_status': keyword_status or {'status': 'OK'}}
    results = run_engines(engines or default_engines(), text, entries, limit=limit,
                          deadline=deadline, config=call_config)
    quota_cfg = config.get('quota') or {}
    skill_quota = quota_cfg.get('skills', SKILL_QUOTA)
    prompt_quota = quota_cfg.get('prompts', PROMPT_QUOTA)
    fused = fuse(results, entries=entries, weights=resolved_weights, limit=limit,
                 skill_quota=skill_quota, prompt_quota=prompt_quota,
                 k=k, group_map=group_map)

    degraded = sorted(name for name in ENGINES if results[name]['status'] in DEGRADED_STATUSES)
    # A zero-weight engine did not search, so it cannot be the reason a result looks empty.
    healthy = sorted(name for name in ENGINES
                     if results[name]['status'] in ('OK', 'EMPTY')
                     and resolved_weights.get(name, 1.0) > 0)
    if fused['selected']:
        status = 'DEGRADED' if degraded else 'OK'
    elif healthy:
        # At least one engine really searched and genuinely found nothing.
        status = 'EMPTY'
    else:
        # Nothing searched: this is "cannot retrieve", not "no skill". Degraded engines stay
        # named in `degraded` with their own reason, so the cause is still explicit.
        status = 'UNAVAILABLE'

    return {'schema_version': SCHEMA_VERSION, 'method': METHOD, 'status': status,
            'reason': None if status in ('OK', 'DEGRADED') else
                      ('NO_CANDIDATE' if status == 'EMPTY' else 'NO_ENGINE_AVAILABLE'),
            'selected': fused['selected'], 'ranking': fused['ranking'],
            'engines': results, 'degraded': degraded, 'healthy': healthy,
            'weights': fused['weights'], 'k': fused['k'],
            'temporal_intent': temporal_intent,
            'binding': binding,
            'coverage': {**fused['coverage'],
                         'catalog': (config.get('catalog') or {}).get('path')},
            'elapsed_ms': round((time.perf_counter() - started) * 1000.0, 2)}
