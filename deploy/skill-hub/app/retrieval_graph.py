"""TEMPR Graph candidate engine: bounded traversal over a versioned, evidence-grounded projection.

The engine's whole value is that an edge means something the source declared. It therefore
refuses three temptations:

1. Co-occurrence is not an edge. Two skills mentioning "security" are siblings of the
   capability `security` only because both declare it in frontmatter -- never because their
   descriptions share a word.
2. A schema term is not a row-level relation. Auto Ontology's `Skill` term with seven column
   attributes proves the glossary exists; it does not prove that skill A depends on skill B.
   `ontology_revision` is pinned so a projection built beside a different glossary is refused.
3. A hit needs a path. A resource that resembles the query but sits on no authored edge is left
   to the Semantic and Keyword branches; this engine returns EMPTY rather than mirror them.

Traversal is bounded: hop 1 from matched concepts and name seeds, hop 2 only across
resource-to-resource edges (`depends_on`, `alias_of`), with a per-concept fanout cap and a
deadline honoured between every batch.
"""
from __future__ import annotations

import math
import time

from retrieval_projection import (ProjectionError, STOPWORDS, load_projection, tokens)

ENGINE = 'graph'

RELATION_WEIGHT = {
    'provides': 1.0,
    'specializes': 1.0,
    'uses_tool': 0.8,
    'requires_tool': 0.8,
    'depends_on': 0.9,
    'alias_of': 0.6,
}
# Only these relations may carry a second hop, and only between resources.
SECOND_HOP_RELATIONS = frozenset({'depends_on', 'alias_of'})
MAX_CONCEPT_FANOUT = 400          # candidates taken from one capability
MAX_SEEDS = 64                    # name seeds that may start a traversal
HOP_DECAY = 0.45
SECOND_HOP_DECAY = 0.2
MAX_EVIDENCE_PATH = 5


class GraphError(RuntimeError):
    """The projection cannot serve this call."""


def _now():
    return time.monotonic()


def _expired(deadline):
    return deadline is not None and _now() >= deadline


def _result(status, *, hits=None, reason=None, elapsed_ms=0.0, manifest=None,
            coverage=None):
    return {
        'engine': ENGINE,
        'status': status,
        'hits': hits or [],
        'elapsed_ms': round(elapsed_ms, 2),
        'reason': reason,
        'manifest': manifest or {},
        'coverage': coverage or {},
    }


def _eligible_ids(entries):
    """The ids this call may return, from the caller's shortlist entries.

    Accepts both `id` (`skill:name`) and `resource_id` + `type`, because the runtime builds
    entries in the qualified form while tests and callers sometimes pass the pair.
    """
    ids = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        rid = entry.get('id')
        if not rid and entry.get('resource_id') and entry.get('type'):
            rid = f'{entry["type"]}:{entry["resource_id"]}'
        if not rid:
            continue
        ids.add(str(rid))
    return ids


def _check_manifest(projection, manifest):
    """Return a reason when the caller's pin and the projection disagree, else None."""
    if not isinstance(manifest, dict) or not manifest:
        return 'MANIFEST_MISSING'
    pinned = projection.get('manifest') or {}
    depends = projection.get('depends_on') or ['catalog_sha256']
    if 'catalog_sha256' not in manifest:
        return 'MANIFEST_INCOMPLETE'
    for key in depends:
        if key in manifest and manifest[key] != pinned.get(key):
            return f'MANIFEST_MISMATCH:{key}'
    if manifest.get('catalog_sha256') != pinned.get('catalog_sha256'):
        return 'MANIFEST_MISMATCH:catalog_sha256'
    return None


def _token_variants(tok):
    variants = {tok}
    if tok.endswith('s') and len(tok) > 3:
        variants.add(tok[:-1])
    if tok.endswith('es') and len(tok) > 4:
        variants.add(tok[:-2])
    if tok.endswith('ing') and len(tok) > 5:
        variants.add(tok[:-3])
    if tok == 'test':
        variants.update({'tests', 'testing'})
    elif tok in ('testing', 'tests'):
        variants.add('test')
    if tok == 'issue':
        variants.add('issues')
    elif tok == 'issues':
        variants.add('issue')
    if tok == 'token':
        variants.add('tokens')
    elif tok == 'tokens':
        variants.add('token')
    return variants


def _expand_tokens(tok_set):
    res = set(tok_set)
    for t in tok_set:
        res.update(_token_variants(t))
    return res


def _concept_match(concept_id, q_tokens, label):
    """How well a declared capability/tool/domain answers the query, 0 when it does not."""
    c_tokens = tokens(label) or tokens(concept_id.split(':', 1)[-1].replace('-', ' '))
    if not c_tokens or not q_tokens:
        return 0.0
    c_exp = _expand_tokens(c_tokens)
    q_exp = _expand_tokens(q_tokens)
    overlap = c_exp & q_exp
    if not overlap:
        return 0.0
    by_concept = len(overlap) / len(c_tokens)
    by_query = len(overlap) / len(q_tokens)
    # A single shared word is enough only when it covers most of the concept name, or when
    # the concept covers a large part of the query. "code" alone must not pull in every
    # capability whose name happens to contain it.
    if by_concept < 0.5 and by_query < 0.5:
        return 0.0
    return max(by_concept, by_query)


def _concept_specificity(count):
    """A capability shared by 900 skills says less than one shared by 12."""
    return 1.0 / math.sqrt(max(1, count))


def _name_seed_score(name, q_tokens):
    """Share of the resource's own name tokens that the query mentions."""
    n_tokens = tokens(name.replace('-', ' '))
    if not n_tokens:
        return 0.0
    n_exp = _expand_tokens(n_tokens)
    q_exp = _expand_tokens(q_tokens)
    overlap = n_exp & q_exp
    if not overlap:
        return 0.0
    return len(overlap) / len(n_tokens)


def _collapse_aliases(hits, nodes):
    """Keep one entry per equivalence group / canonical resource.

    An alias and its canonical are the same served document. Both scoring would hand that
    document two slots and, after fusion, two votes. The non-alias id is preferred, because
    the canonical name is the one whose permissions and lifecycle the loader checks.
    """
    best = {}
    for hit in hits:
        node = nodes.get(hit['id']) or {}
        group = node.get('g')
        if group:
            key = ('group', group)
        else:
            canonical_name = node.get('can') or node.get('n')
            kind = node.get('t')
            key = ('canonical', kind, canonical_name) if canonical_name else ('id', hit['id'])
        current = best.get(key)
        if current is None:
            best[key] = hit
            continue
        prefer = (1 - int(node.get('al') or 0), hit['raw_score'])
        other_node = nodes.get(current['id']) or {}
        existing = (1 - int(other_node.get('al') or 0), current['raw_score'])
        if prefer > existing:
            best[key] = hit
    return list(best.values())


def retrieve(text, entries, *, projection_path, manifest, limit=250, deadline=None):
    """Rank eligible resources by an authored evidence path from the query.

    Returns the shared EngineResult dict. Status is OK, EMPTY, TIMEOUT, ERROR or UNAVAILABLE;
    rank starts at 1; every hit names the edge path that earned it.
    """
    started = _now()

    def elapsed():
        return (_now() - started) * 1000.0

    eligible = _eligible_ids(entries)
    if not eligible:
        return _result('EMPTY', reason='NO_ENTRIES', elapsed_ms=elapsed(),
                       coverage={'eligible': 0})
    if not isinstance(text, str) or not text.strip():
        return _result('EMPTY', reason='EMPTY_QUERY', elapsed_ms=elapsed(),
                       coverage={'eligible': len(eligible)})
    if _expired(deadline):
        return _result('TIMEOUT', reason='DEADLINE_BEFORE_LOAD', elapsed_ms=elapsed(),
                       coverage={'eligible': len(eligible)})

    try:
        projection = load_projection(projection_path, expected_kind='graph')
    except ProjectionError as error:
        return _result('UNAVAILABLE', reason=f'PROJECTION_UNTRUSTED:{error}',
                       elapsed_ms=elapsed(), coverage={'eligible': len(eligible)})
    except OSError as error:
        return _result('UNAVAILABLE', reason=f'PROJECTION_UNREADABLE:{type(error).__name__}',
                       elapsed_ms=elapsed(), coverage={'eligible': len(eligible)})
    except Exception as error:  # pragma: no cover - defensive, must never look like no-match
        return _result('ERROR', reason=f'PROJECTION_ERROR:{type(error).__name__}',
                       elapsed_ms=elapsed(), coverage={'eligible': len(eligible)})

    pin = projection.get('manifest') or {}
    mismatch = _check_manifest(projection, manifest)
    if mismatch is not None:
        return _result('UNAVAILABLE', reason=mismatch, elapsed_ms=elapsed(),
                       manifest=pin, coverage={'eligible': len(eligible)})

    if _expired(deadline):
        return _result('TIMEOUT', reason='DEADLINE_AFTER_LOAD', elapsed_ms=elapsed(),
                       manifest=pin, coverage={'eligible': len(eligible)})

    nodes = projection.get('nodes') or {}
    concepts = projection.get('concepts') or {}
    edges = projection.get('edges') or []
    if not nodes:
        return _result('EMPTY', reason='NO_PROJECTED_NODES', elapsed_ms=elapsed(),
                       manifest=pin, coverage={'eligible': len(eligible)})
    if not edges:
        return _result('EMPTY', reason='NO_AUTHORED_EDGES', elapsed_ms=elapsed(),
                       manifest=pin,
                       coverage={'eligible': len(eligible), 'edges': 0})

    q_tokens = tokens(text) - STOPWORDS
    if not q_tokens:
        return _result('EMPTY', reason='NO_QUERY_TOKENS', elapsed_ms=elapsed(),
                       manifest=pin, coverage={'eligible': len(eligible)})

    # ---- adjacency, built once per call from the projection (no network work)
    out_edges = {}
    in_edges = {}
    for edge in edges:
        out_edges.setdefault(edge['s'], []).append(edge)
        in_edges.setdefault(edge['d'], []).append(edge)

    # ---- activation sources: matched concepts and name seeds
    matched_concepts = []
    for concept_id, info in concepts.items():
        if _expired(deadline):
            return _result('TIMEOUT', reason='DEADLINE_DURING_CONCEPT_MATCH',
                           elapsed_ms=elapsed(), manifest=pin,
                           coverage={'eligible': len(eligible)})
        score = _concept_match(concept_id, q_tokens, info.get('label') or '')
        if score:
            matched_concepts.append((concept_id, score, info.get('n') or 0))
    matched_concepts.sort(key=lambda item: (-item[1], item[0]))

    seeds = []
    for rid in sorted(eligible):
        node = nodes.get(rid)
        if node is None or not node.get('act', 1):
            continue
        score = _name_seed_score(node.get('n') or rid.split(':', 1)[-1], q_tokens)
        if score:
            seeds.append((rid, score))
    seeds.sort(key=lambda item: (-item[1], item[0]))
    seeds = seeds[:MAX_SEEDS]

    candidates = {}
    truncated_concepts = []
    matched_concept_ids = {c for c, _, _ in matched_concepts}

    def offer(rid, score, path, relation):
        """Record a candidate reachable by an authored path.

        `path` alternates resource, relation, resource. The capability or tool that activated
        the traversal is reported separately in `via`, because mixing a concept node into a
        resource chain makes "how is this skill related to that one" unreadable.
        """
        if rid not in eligible:
            return
        node = nodes.get(rid)
        if node is None or not node.get('act', 1):
            return
        if rid.startswith(('cap:', 'tool:', 'dom:', 'dep:', 'ext:')):
            return
        if not score or score <= 0:
            return
        path = path[:MAX_EVIDENCE_PATH]
        existing = candidates.get(rid)
        if existing is None or score > existing['raw_score']:
            candidates[rid] = {
                'id': rid,
                'raw_score': round(min(score, 1.0), 6),
                'evidence': {'path': path, 'relation': relation,
                             'hops': max(0, (len(path) - 1) // 2),
                             'via': None,
                             'group': node.get('g')},
                'equivalence_group_id': node.get('g'),
            }
            return candidates[rid]
        return existing

    def offer_via(rid, score, relation, concept_id, path=None):
        hit = offer(rid, score, path or [rid], relation)
        if hit is not None and hit['evidence']['via'] is None:
            hit['evidence']['via'] = concept_id
        return hit

    # ---- hop 1a: a concept the query names directly gives every declared member a path
    for concept_id, c_score, fanout in matched_concepts:
        if fanout > MAX_CONCEPT_FANOUT:
            truncated_concepts.append({'concept': concept_id, 'members': fanout,
                                       'kept': MAX_CONCEPT_FANOUT})
        members = 0
        for edge in in_edges.get(concept_id, ()):
            if members >= MAX_CONCEPT_FANOUT:
                break
            members += 1
            relation = edge['r']
            source_id = edge['s']
            source_node = nodes.get(source_id) or {}
            name_bonus = _name_seed_score(source_node.get('n') or '', q_tokens)
            weight = RELATION_WEIGHT.get(relation, 0.5)
            score = (HOP_DECAY + 0.55 * c_score * _concept_specificity(fanout)) * weight
            score += 0.30 * name_bonus
            offer_via(source_id, score, relation, concept_id, path=[source_id])

    # ---- hop 1b: a name seed lends its concept to siblings that declare the same capability
    for rid, seed_score in seeds:
        if _expired(deadline):
            return _result('TIMEOUT', reason='DEADLINE_DURING_SEED_EXPANSION',
                           elapsed_ms=elapsed(), manifest=pin,
                           coverage={'eligible': len(eligible)})
        # The seed itself is a hit only when it also holds an authored edge; its own name
        # match is the Keyword branch's job and duplicating it here would double-count.
        for edge in out_edges.get(rid, ()):
            target = edge['d']
            if target not in matched_concept_ids:
                continue
            relation = edge['r']
            weight = RELATION_WEIGHT.get(relation, 0.5)
            for sibling_edge in in_edges.get(target, ()):
                sibling = sibling_edge['s']
                if sibling == rid:
                    continue
                concepts_info = concepts.get(target) or {}
                score = (seed_score * HOP_DECAY * weight
                         * _concept_specificity(concepts_info.get('n') or 1))
                offer_via(sibling, score, sibling_edge['r'], target, path=[rid, relation, sibling])

    # ---- hop 2: a bounded BFS across resource-to-resource authored edges only.
    # Depth is counted in traversals, so a chain a -> b -> c reaches c and stops. Without the
    # frontier loop, a dependency found during expansion was never itself expandable and a
    # declared chain silently ended one link early.
    frontier = [(rid, hit) for rid, hit in candidates.items()]
    for depth in range(2):
        if _expired(deadline):
            return _result('TIMEOUT', reason='DEADLINE_DURING_EXPANSION',
                           elapsed_ms=elapsed(), manifest=pin,
                           coverage={'eligible': len(eligible)})
        next_frontier = []
        for rid, hit in frontier:
            relation = hit['evidence']['relation']
            if depth == 0:
                # From a resource reached through its own declared capability, follow the
                # dependencies it declares. The reverse direction ("c depends on a") does not
                # make c relevant to a query that reached a.
                directions = ('out', 'in') if relation in SECOND_HOP_RELATIONS else ('out',)
            else:
                directions = ('out',)
            for direction in directions:
                if direction == 'out':
                    nearby = ((edge, edge['d']) for edge in out_edges.get(rid, ()))
                else:
                    nearby = ((edge, edge['s']) for edge in in_edges.get(rid, ()))
                for edge, other in nearby:
                    if edge['r'] not in SECOND_HOP_RELATIONS:
                        continue
                    if other in candidates or other in eligible and other == rid:
                        continue
                    score = hit['raw_score'] * (SECOND_HOP_DECAY ** (depth + 1))
                    path = hit['evidence']['path'] + [edge['r'], other]
                    offer(other, score, path, edge['r'])
                    found = candidates.get(other)
                    if found is not None and found['raw_score'] == round(min(score, 1.0), 6):
                        next_frontier.append((other, found))
        frontier = next_frontier

    if _expired(deadline):
        return _result('TIMEOUT', reason='DEADLINE_BEFORE_RANK',
                       elapsed_ms=elapsed(), manifest=pin,
                       coverage={'eligible': len(eligible)})

    hits = _collapse_aliases(list(candidates.values()), nodes)
    hits.sort(key=lambda hit: (-hit['raw_score'], hit['id']))
    hits = hits[:max(0, int(limit))]
    for index, hit in enumerate(hits, start=1):
        hit['rank'] = index

    coverage = {
        'eligible': len(eligible),
        'nodes': len(nodes),
        'edges': len(edges),
        'concepts': len(concepts),
        'matched_concepts': len(matched_concepts),
        'seeds': len(seeds),
        'candidates': len(candidates),
        'returned_hits': len(hits),
        'truncated_concepts': len(truncated_concepts),
        'max_hops': 2,
    }
    if not hits:
        return _result('EMPTY', reason='NO_GRAPH_PATH', elapsed_ms=elapsed(),
                       manifest=pin, coverage=coverage)
    return _result('OK', hits=hits, elapsed_ms=elapsed(), manifest=pin, coverage=coverage)


def analyze(text, projection_path, *, manifest=None, top=10):
    """Why a query does or does not reach the graph. Evaluation helper, not a runtime path."""
    projection = load_projection(projection_path, expected_kind='graph')
    mismatch = _check_manifest(projection, manifest or projection.get('manifest'))
    q_tokens = tokens(text or '') - STOPWORDS
    concepts = projection.get('concepts') or {}
    matched = []
    for concept_id, info in concepts.items():
        score = _concept_match(concept_id, q_tokens, info.get('label') or '')
        if score:
            matched.append({'concept': concept_id, 'score': round(score, 4),
                            'members': info.get('n')})
    matched.sort(key=lambda item: (-item['score'], item['concept']))
    return {'manifest_ok': mismatch is None, 'mismatch': mismatch,
            'query_tokens': sorted(q_tokens),
            'matched_concepts': matched[:top],
            'concept_count': len(concepts),
            'edge_count': len(projection.get('edges') or [])}
