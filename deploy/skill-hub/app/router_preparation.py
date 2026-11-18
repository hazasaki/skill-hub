"""Bounded candidate preparation and score-only cache; neither grants load permission."""
from __future__ import annotations

from collections import OrderedDict
import sqlite3
import threading
import time

from metadata_hits import query_hits
from shortlist_runtime import query_terms, index_identity

CANDIDATE_LIMIT = 250
CACHE_TTL_SECONDS = 300.0
SCORING_QUESTION_VERSION = 'direct-capability-v2'
# The retrieval stage is bounded inside the shared 18-second end-to-end route budget so a slow
# index cannot eat the model's 10-second cap. It is a ceiling for visibility, FTS, and four engines.
RETRIEVAL_DEADLINE_SECONDS = 7.0
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_FTS_CACHE = OrderedDict()
_FTS_LOCK = threading.Lock()


def indexed_ranks(catalog_module, hub, text, limit=1000, per_term_limit=100, *, deadline=None):
    """Use the owning index's Porter/BM25 body search, without returning bodies."""
    terms = query_terms(text)
    if not terms:
        return {}, {'status': 'NO_TERMS'}
    if deadline is not None and time.monotonic() >= deadline:
        return {}, {'status': 'TIMEOUT', 'reason': 'DEADLINE_EXCEEDED'}
    identity = index_identity(catalog_module, hub)
    cache_key = (catalog_module, identity['hub_root'], identity['index_path'], identity['index_sha256'], text, limit, per_term_limit)
    with _FTS_LOCK:
        cached = _FTS_CACHE.get(cache_key)
        if cached is not None:
            _FTS_CACHE.move_to_end(cache_key)
            return dict(cached[0]), dict(cached[1])
    if deadline is not None and time.monotonic() >= deadline:
        return {}, {'status': 'TIMEOUT', 'reason': 'DEADLINE_EXCEEDED'}
    con = None
    try:
        con = catalog_module.connect(hub)
        if deadline is not None:
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        ranks = {}
        timed_out = False
        for kind, table, source in (('skill', 'fts', 'skills'), ('prompt', 'fts_prompts', 'prompts')):
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            columns = {row[1] for row in con.execute(f'PRAGMA table_info({source})')}
            active = ' AND s.active = 1' if 'active' in columns else ''
            match = ' OR '.join('"' + word.replace('"', '') + '"' for word in terms)
            rows = con.execute(f'SELECT s.name FROM {table} JOIN {source} s '
                               f'ON s.rowid = {table}.rowid WHERE {table} MATCH ? {active} '
                               f"AND {table}.rank MATCH 'bm25(8.0,4.0,3.0,1.0)' "
                               f'ORDER BY {table}.rank LIMIT ?',
                               (match, limit))
            for rank, row in enumerate(rows):
                ident = f'{kind}:{row["name"]}'
                ranks[ident] = min(ranks.get(ident, 999999), rank)
            if len(terms) > 1:
                for term in terms:
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        break
                    term_clean = term.replace('"', '').strip()
                    if not term_clean or len(term_clean) < 2:
                        continue
                    term_match = f'"{term_clean}"'
                    try:
                        t_rows = con.execute(f'SELECT s.name FROM {table} JOIN {source} s '
                                             f'ON s.rowid = {table}.rowid WHERE {table} MATCH ? {active} '
                                             f"AND {table}.rank MATCH 'bm25(8.0,4.0,3.0,1.0)' "
                                             f'ORDER BY {table}.rank LIMIT ?',
                                             (term_match, per_term_limit))
                        for rank, row in enumerate(t_rows):
                            ident = f'{kind}:{row["name"]}'
                            ranks[ident] = min(ranks.get(ident, 999999), rank + 30)
                    except sqlite3.OperationalError as error:
                        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                            raise
                        pass
        res_ranks = dict(ranks)
        res_status = {'status': 'OK', 'matched_in_window': len(ranks)}
        if timed_out:
            res_status['status'] = 'TIMEOUT'
            res_status['degraded'] = True
            res_status['reason'] = 'DEADLINE_EXCEEDED'
        else:
            with _FTS_LOCK:
                _FTS_CACHE[cache_key] = (res_ranks, res_status)
                _FTS_CACHE.move_to_end(cache_key)
                while len(_FTS_CACHE) > 128:
                    _FTS_CACHE.popitem(last=False)
        return res_ranks, res_status
    except (AttributeError, sqlite3.Error, OSError) as error:
        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            return dict(ranks), {'status': 'TIMEOUT', 'degraded': True, 'reason': 'DEADLINE_EXCEEDED'}
        return {}, {'status': 'UNAVAILABLE', 'error_class': type(error).__name__}
    finally:
        if con is not None:
            con.set_progress_handler(None, 0)
            con.close()


def prepare(entries, text, limit=CANDIDATE_LIMIT, *, fts_ranks=None, tempr=None,
            deadline=None, group_map=None, engines=None):
    """Rank metadata over the entire visible catalog, reserving room for both kinds.

    This is candidate retrieval, not a model score. No-match retrieval still sends a
    query-dependent exploration sample to the model; it never fabricates a model decline.

    ``tempr`` enables four-engine fusion. It is either a boolean or the resolved artifact
    configuration; ``False``/``None`` keeps the single metadata ranking, which is what a host
    with no built artifacts must still be able to run. Both paths return the same
    ``(entries, evidence)`` pair, and the evidence says which one produced it.
    """
    if tempr is not None and tempr is not False:
        return _prepare_fused(entries, text, limit, fts_ranks=fts_ranks,
                              config=tempr if isinstance(tempr, dict) else None,
                              deadline=deadline, group_map=group_map, engines=engines)
    from rust_routing import Operation, decision
    terms = set(query_terms(text))
    hits = query_hits(entries, terms)
    by_id = {entry['id']: entry for entry in entries}
    ids, skill_count, prompt_count = decision(Operation.METADATA_RANK, {
        'entries': [{'id': entry['id'], 'kind': entry['type'],
                     'name_hits': sorted(name_hits), 'hits': sorted(matches)}
                    for entry, (name_hits, matches) in zip(entries, hits)],
        'text': text, 'limit': limit, 'fts': fts_ranks,
    })
    chosen = [by_id[identifier] for identifier in ids]
    method = 'fts_bm25_metadata_fallback_v1' if fts_ranks is not None else 'metadata_idf_name_weighted_v1'
    return chosen, {'method': method, 'limit': limit,
                    'retrieved': len(chosen), 'excluded': len(entries) - len(chosen),
                    'excluded_reason': 'candidate_preparation_limit',
                    'skills_retrieved': skill_count, 'prompts_retrieved': prompt_count,
                    'model_saw_full_catalog': len(chosen) == len(entries)}


def _prepare_fused(entries, text, limit, *, fts_ranks, config, deadline, group_map,
                  engines):
    """Four-engine RRF selection, with a named fallback instead of a silent one.

    Fusion decides *order and identity*; the metadata ranking below stays the last resort for
    a query no engine could answer. The fallback is labelled, so a reader can tell an
    engine-backed shortlist from an exploration sample.
    """
    import retrieval_fusion as fusion

    resolved = config if config is not None else fusion.load_config()
    if fts_ranks is not None:
        resolved = {**resolved, 'keyword_ranks': fts_ranks,
                    'keyword_status': {'status': 'OK'}}
    elif 'keyword_ranks' not in resolved:
        resolved = {**resolved, 'keyword_ranks': None,
                    'keyword_status': {'status': 'NO_TERMS'}}
    if deadline is None:
        deadline = time.monotonic() + RETRIEVAL_DEADLINE_SECONDS

    fused = fusion.fuse_candidates(text, entries, config=resolved, deadline=deadline,
                                  limit=limit, engines=engines, group_map=group_map)
    by_id = {entry['id']: entry for entry in entries}
    chosen, missing = [], 0
    for row in fused['selected']:
        entry = by_id.get(row['id'])
        if entry is None:
            missing += 1
            continue
        chosen.append(entry)
    engine_summary = {name: {'status': result['status'], 'reason': result['reason'],
                             'hits': len(result['hits']),
                             'elapsed_ms': result['elapsed_ms']}
                      for name, result in fused['engines'].items()}
    evidence = {
        'method': fused['method'], 'limit': limit, 'retrieved': len(chosen),
        'excluded': len(entries) - len(chosen),
        'excluded_reason': 'candidate_preparation_limit',
        'skills_retrieved': sum(1 for e in chosen if e['type'] == 'skill'),
        'prompts_retrieved': sum(1 for e in chosen if e['type'] == 'prompt'),
        'model_saw_full_catalog': len(chosen) == len(entries),
        'retrieval_status': fused['status'], 'retrieval_reason': fused['reason'],
        'engines': engine_summary, 'degraded': fused['degraded'],
        'weights': fused['weights'], 'rrf_k': fused['k'],
        'temporal_intent': fused['temporal_intent'],
        'catalog_sha256': fused['binding']['catalog_sha256'],
        'catalog_sha256_declared': fused['binding']['catalog_sha256_declared'],
        'catalog_projection_sha256': fused['binding']['catalog_projection_sha256'],
        'catalog_projection_sha256_verified':
            fused['binding']['catalog_projection_sha256_verified'],
        'hash_relation': fused['binding']['relation'],
        # How the artifact seam was found, so "configured by env" and "fell back to the
        # delivered default" are different observable facts rather than the same silence.
        'config_source': fused['binding'].get('config_source'),
        'config_reason': fused['binding'].get('config_reason'),
        'artifacts': {name: {'path': item['path'], 'match': item['match'],
                             'hash_kind': item['hash_kind'], 'reason': item.get('reason')}
                      for name, item in fused['binding']['artifacts'].items()},
        'coverage': fused['coverage'],
        'engine_ranks': {row['id']: row['best_rank'] for row in fused['selected']},
    }
    if missing:
        evidence['unresolvable_selected_ids'] = missing
    if chosen:
        return chosen, evidence

    # Nothing fused: keep the pre-existing exploration sample rather than reporting an empty
    # catalog to the model, and say exactly why this list is not engine-backed.
    fallback, fallback_evidence = prepare(entries, text, limit, fts_ranks=fts_ranks)
    evidence['fallback'] = {'used': True, 'reason': fused['status'],
                            'method': fallback_evidence['method']}
    evidence['retrieved'] = len(fallback)
    evidence['excluded'] = fallback_evidence['excluded']
    evidence['skills_retrieved'] = fallback_evidence['skills_retrieved']
    evidence['prompts_retrieved'] = fallback_evidence['prompts_retrieved']
    evidence['model_saw_full_catalog'] = fallback_evidence['model_saw_full_catalog']
    return fallback, evidence


def interleave(entries):
    from rust_routing import Operation, decision
    by_id = {entry['id']: entry for entry in entries}
    ids = decision(Operation.INTERLEAVE, [(entry['id'], entry['type']) for entry in entries])
    return [by_id[identifier] for identifier in ids]


def coalesce(entries, *, exact_cards=False, explicit=()):
    """Collapse only pinned identities. Aliases retain independent load permissions."""
    from rust_routing import Operation, decision
    fields = ('id', 'type', 'content_sha256', 'bundle_sha256', 'risk', 'description',
              'tags', 'purpose_excerpt', 'state')
    by_id = {entry['id']: entry for entry in entries}
    rows = []
    for entry in entries:
        row = {key: entry.get(key) for key in fields}
        if exact_cards:
            card = entry.get('resource_card') or {}
            row['resource_card'] = {key: card.get(key) for key in ('source_raw_sha256', 'card_sha256')}
        rows.append(row)
    ids, aliases = decision(Operation.COALESCE, {'entries': rows, 'exact': exact_cards, 'explicit': sorted(explicit)})
    return [by_id[identifier] for identifier in ids], {
        identifier: [by_id[alias] for alias in group] for identifier, group in aliases.items()}


def cache_key(text, catalog, entries, model, endpoint, *, retriever=None):
    """Score-cache identity. ``retriever`` pins the retrieval artifacts that built the set.

    Without it, the same task text against the same catalog could reuse scores computed for a
    candidate list built from different projection revisions.
    """
    from staged_scoring import digest
    return digest({'schema': 'route-scores.v2', 'question_version': SCORING_QUESTION_VERSION,
                   'task': text.replace('\r\n', '\n').replace('\r', '\n'),
                   'catalog': catalog, 'entries': entries, 'model': model, 'endpoint': endpoint,
                   'retriever': retriever})


def cached_scores(key, ttl=CACHE_TTL_SECONDS):
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            return None
        created, scores = cached
        if time.monotonic() - created >= ttl:
            del _CACHE[key]
            return None
        _CACHE.move_to_end(key)
        return dict(scores)


def store_scores(key, scores):
    # Only fully scored prepared sets are cached; partial/outage results must be retried.
    with _LOCK:
        _CACHE[key] = (time.monotonic(), dict(scores))
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
