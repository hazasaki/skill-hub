"""TEMPR Temporal candidate engine: ranks only resources with a source-authored date.

A temporal branch is the easiest place to lie. Three specific lies are refused here:

    mtime is not an update   A file's filesystem timestamp records when this machine wrote it,
                             not when the skill's author changed it. The projection never
                             carries mtime, so the engine cannot rank by it.
    snapshot is not a date    Auto Ontology's `snapshot_at` records when the catalog was
                             captured. It is identical for every row and says nothing about
                             which skill is newer. It is kept in a separate field and never
                             used as a ranking date.
    absence is not zero       A resource with no declared date is excluded and counted, not
                             assigned "now", not assigned the epoch, not silently dropped from
                             the report.

Four clocks are distinguished, because they answer four different questions:

    added     "what was added to the catalog most recently"  -> authored `date_added`
    version   "what is the latest version"                   -> authored version-recorded date
    updated   "which one was updated / is freshest"          -> authored last-updated date
    validity  "which version applied at time T"              -> authored `valid_from`/`valid_to`

`date_added` answers only the first. It is the date this hub ingested a resource, so ranking
"latest version" or "most recently updated" by it is a false answer dressed as a recency
answer. This module refuses that substitution: a version or update question with no authored
version/update date returns EMPTY with `NO_VERSION_DATA` or `NO_UPDATED_TIME`, and says the
catalog carries no such evidence. The current catalog's authored frontmatter has no
version-recorded or last-updated timestamp at all (it carries a version *string*, which is not
a date and is never coerced into one), so those two clocks abstain today by design, not by
accident.

Validity windows are supported and, in the current catalog, absent -- so a period question
returns EMPTY with `NO_VALIDITY_DATA` rather than being answered from `date_added`.

A query with no temporal intent returns EMPTY / NOT_TEMPORAL. That is the contract: the
fusion weight is zero without intent, and an engine that invents staleness is worse than one
that abstains.
"""
from __future__ import annotations

import re
import time

from retrieval_projection import (ProjectionError, STOPWORDS, epoch_day, load_projection,
                                  tokens)

ENGINE = 'temporal'

# Each phrase names the clock it is asking about. "latest version" and "last updated" are not
# "recently added": answering them from `date_added` reports when this hub ingested a file and
# calls it the newest practice. The classes are separate so the ranking can only use the clock
# the question named.
CLOCK_TERMS = (
    ('added', ('newly added', 'recently added', 'added to the catalog', 'added to catalog',
               'date_added', 'added', 'mới thêm', 'vừa thêm', 'thêm vào', 'được thêm')),
    ('version', ('latest version', 'newest version', 'current version', 'version',
                 'changelog', 'release notes', 'bản phát hành', 'phiên bản')),
    ('updated', ('last updated', 'updated', 'update', 'up to date', 'up-to-date', 'freshness',
                 'fresh', 'most recent', 'recently', 'latest', 'newest', 'current', 'nowadays',
                 'stale', 'outdated', 'out of date', 'deprecated', 'legacy', 'mới nhất', 'mới',
                 'gần đây', 'cập nhật', 'lỗi thời', 'cũ', 'hiện hành', 'hiện tại',
                 'thay đổi gần nhất')),
)
# Every phrase, for stripping time words out of the relevance subject. Order is the precedence
# used when a query names more than one clock ("latest version" -> version, not updated).
STRONG_INTENT = tuple(term for _, terms in CLOCK_TERMS for term in terms)
CLOCK_PRECEDENCE = tuple(name for name, _ in CLOCK_TERMS)
TERM_RX = {term: re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)') for term in STRONG_INTENT}
# The authored fields each clock may rank by. `date_added` appears in exactly one list, so no
# version/update question can reach it. `ver` is a version *string* and never a date.
CLOCK_FIELDS = {'added': ('added',), 'version': ('ver_at',), 'updated': ('ver_at', 'verified_at')}
# The authored field each projection key came from, so the evidence names what was actually
# read. `tsrc` describes the catalog-add date only; reusing it for a version rank would report
# `frontmatter:date_added` beside a date taken from `version_recorded_at`.
FIELD_SOURCE = {'added': 'frontmatter:date_added',
                'ver_at': 'frontmatter:version_recorded_at',
                'verified_at': 'frontmatter:last_updated'}
CLOCK_EMPTY_REASON = {'added': 'NO_ADDED_DATE', 'version': 'NO_VERSION_DATA',
                      'updated': 'NO_UPDATED_TIME'}
# Deliberately absent: bare "new", bare "v2", bare "next". They name products and versions in
# ordinary queries ("new project", "nextjs 15") and would make every request look temporal.
YEAR_RX = re.compile(r'(?<!\d)((?:19|20)\d{2})(?:-\d{2}(?:-\d{2})?)?(?!\d)')
BEFORE_RX = re.compile(r'\b(?:before|prior to|older than|trước|trước ngày|trước năm)\b', re.I)
AFTER_RX = re.compile(r'\b(?:after|later than|since|from|sau|sau ngày|từ năm|kể từ)\b', re.I)
BETWEEN_RX = re.compile(r'\b(?:between|from|từ)\b.*\b(?:and|to|đến)\b', re.I)

# The projection carries `added`, `ver_at` and `verified_at`; only the clocks in CLOCK_FIELDS
# above may rank. `ver_at`/`verified_at` are null for every row in the current catalog because
# the authored frontmatter declares no version-recorded or last-updated date, so those two
# clocks abstain explicitly (`NO_VERSION_DATA` / `NO_UPDATED_TIME`) instead of borrowing
# `date_added`. A projection that ever carries those fields ranks from them with no other change.

# Words that name the request itself rather than its subject. They match nearly every
# description, so counting them as relevance let "latest xlsx skill" rank an unrelated film
# skill on the strength of the word "skill" alone.
GENERIC = frozenset("""
skill skills prompt prompts resource resources item items entry entries tool tools agent agents
plugin plugins library libraries file files doc docs document documents thing things stuff
best good great top recommended recommend suggest suggestion
""".split())


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


def detect_intent(text):
    """Classify a query's time intent, with the words and the clock that produced the verdict.

    Returns ``{'intent': bool, 'mode': 'recency'|'period'|None, 'clock': 'added'|'version'|
    'updated'|None, 'period': (from, to)|None, 'terms': [...]}``. A period bound wins over a
    clock word, because "before 2025" names an interval rather than a freshness question.

    ``clock`` is what the ranking is allowed to use. "latest version" is a version question and
    must never be answered from `date_added`; "recently added" is the one question that may be.
    """
    if not isinstance(text, str) or not text.strip():
        return {'intent': False, 'mode': None, 'clock': None, 'period': None, 'terms': []}
    lowered = ' '.join(text.lower().split())
    terms = [term for term in STRONG_INTENT if TERM_RX[term].search(lowered)]
    years = [m.group(1) for m in YEAR_RX.finditer(lowered)]
    bounded = bool(BEFORE_RX.search(lowered) or AFTER_RX.search(lowered)
                   or BETWEEN_RX.search(lowered))
    if years and bounded:
        low, high = min(years), max(years)
        period = (f'{low}-01-01', f'{high}-12-31')
        return {'intent': True, 'mode': 'period', 'clock': None, 'period': period,
                'terms': terms + [f'year:{low}']}
    if years:
        # A bare year is a topical qualifier ("ES2020", "2024 tax rules") as often as a time
        # bound. It counts only together with an explicit recency or bound word.
        clock = _clock_of(terms)
        return {'intent': bool(terms), 'mode': 'recency' if terms else None,
                'clock': clock, 'period': None, 'terms': terms}
    if terms:
        return {'intent': True, 'mode': 'recency', 'clock': _clock_of(terms),
                'period': None, 'terms': terms}
    return {'intent': False, 'mode': None, 'clock': None, 'period': None, 'terms': []}


def _clock_of(terms):
    """The clock a matched phrase set names, in precedence order, or None when it names none."""
    matched = set(terms)
    for clock, phrases in CLOCK_TERMS:
        if any(phrase in matched for phrase in phrases):
            return clock
    return None


def _eligible_ids(entries):
    ids = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        rid = entry.get('id')
        if not rid and entry.get('resource_id') and entry.get('type'):
            rid = f'{entry["type"]}:{entry["resource_id"]}'
        if rid:
            ids.add(str(rid))
    return ids


def _check_manifest(projection, manifest):
    if not isinstance(manifest, dict) or not manifest:
        return 'MANIFEST_MISSING'
    pinned = projection.get('manifest') or {}
    if 'catalog_sha256' not in manifest:
        return 'MANIFEST_INCOMPLETE'
    for key in projection.get('depends_on') or ['catalog_sha256']:
        if key in manifest and manifest[key] != pinned.get(key):
            return f'MANIFEST_MISMATCH:{key}'
    if manifest.get('catalog_sha256') != pinned.get('catalog_sha256'):
        return 'MANIFEST_MISMATCH:catalog_sha256'
    return None


def _relevance(query_tokens, entry, node):
    """Metadata relevance, so a date alone cannot drag an unrelated resource in.

    Uses only the caller's own shortlist entry and the projection's name: the temporal branch
    is not allowed to become a second semantic engine. The query's own time words are removed
    first, because "latest" says how to rank, not what to rank.
    """
    c_tokens = tokens(' '.join(str(part) for part in (
        node.get('n') or ' '.join(node.get('caps') or ()).replace('-', ' '),
        entry.get('description') or '', entry.get('tags') or '',
        entry.get('purpose_excerpt') or '') if part)) - GENERIC
    if not c_tokens or not query_tokens:
        return 0.0
    overlap = c_tokens & query_tokens
    if not overlap:
        return 0.0
    return len(overlap) / max(1, len(query_tokens))


def _within(day, valid_from, valid_to):
    start = epoch_day(valid_from)
    end = epoch_day(valid_to)
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def retrieve(text, entries, *, projection_path, manifest, limit=250, deadline=None):
    """Rank temporal candidates, or abstain explicitly when the query has no time intent."""
    started = _now()

    def elapsed():
        return (_now() - started) * 1000.0

    intent = detect_intent(text)
    eligible = _eligible_ids(entries)
    base_coverage = {'eligible': len(eligible), 'intent': intent['mode'],
                     'clock': intent['clock'],
                     'intent_terms': intent['terms'], 'mode': intent['mode'],
                     'period': intent['period']}
    if not eligible:
        return _result('EMPTY', reason='NO_ENTRIES', elapsed_ms=elapsed(),
                       coverage={**base_coverage, 'eligible': 0})
    if not intent['intent']:
        # The contract, stated in one place: no time intent, no temporal vote.
        return _result('EMPTY', reason='NOT_TEMPORAL', elapsed_ms=elapsed(),
                       coverage=base_coverage)
    if _expired(deadline):
        return _result('TIMEOUT', reason='DEADLINE_BEFORE_LOAD', elapsed_ms=elapsed(),
                       coverage=base_coverage)

    try:
        projection = load_projection(projection_path, expected_kind='temporal')
    except ProjectionError as error:
        return _result('UNAVAILABLE', reason=f'PROJECTION_UNTRUSTED:{error}',
                       elapsed_ms=elapsed(), coverage=base_coverage)
    except OSError as error:
        return _result('UNAVAILABLE', reason=f'PROJECTION_UNREADABLE:{type(error).__name__}',
                       elapsed_ms=elapsed(), coverage=base_coverage)
    except Exception as error:  # pragma: no cover - defensive
        return _result('ERROR', reason=f'PROJECTION_ERROR:{type(error).__name__}',
                       elapsed_ms=elapsed(), coverage=base_coverage)

    pin = projection.get('manifest') or {}
    mismatch = _check_manifest(projection, manifest)
    if mismatch is not None:
        return _result('UNAVAILABLE', reason=mismatch, elapsed_ms=elapsed(),
                       manifest=pin, coverage=base_coverage)

    nodes = projection.get('nodes') or {}
    # The ranking words are removed from the subject before relevance is measured.
    intent_tokens = set()
    for phrase in STRONG_INTENT:
        intent_tokens |= tokens(phrase)
    intent_tokens |= {'year', 'since', 'before', 'after', 'between', 'prior'}
    q_tokens = (tokens(text) - STOPWORDS) - intent_tokens - GENERIC
    if not q_tokens:
        # Nothing but time words: there is no subject to rank, so the branch abstains.
        return _result('EMPTY', reason='NO_QUERY_SUBJECT', elapsed_ms=elapsed(),
                       manifest=pin, coverage={**base_coverage, 'dated': len(nodes)})
    entries_by_id = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        rid = entry.get('id') or (f'{entry.get("type")}:{entry.get("resource_id")}'
                                  if entry.get('resource_id') else None)
        if rid:
            entries_by_id[str(rid)] = entry

    undated = []
    irrelevant = 0
    invalid = 0
    candidates = []
    mode = intent['mode']
    period = intent['period']
    # Only the named clock's own authored fields. A version or update question has no path to
    # `date_added`: the substitution is refused at this point, not filtered out afterwards.
    fields = CLOCK_FIELDS.get(intent['clock'], ())
    newest = 0
    for node in nodes.values():
        for field in fields:
            value = epoch_day(node.get(field))
            if value and value > newest:
                newest = value

    for rid in sorted(eligible):
        if _expired(deadline):
            return _result('TIMEOUT', reason='DEADLINE_DURING_RANK', elapsed_ms=elapsed(),
                           manifest=pin, coverage=base_coverage)
        node = nodes.get(rid)
        if node is None:
            undated.append(rid)
            continue
        relevance = _relevance(q_tokens, entries_by_id.get(rid) or {}, node)
        if relevance <= 0:
            irrelevant += 1
            continue

        if mode == 'period':
            valid_from, valid_to = node.get('vf'), node.get('vt')
            if valid_from is None and valid_to is None:
                undated.append(rid)
                continue
            anchor = node.get('added') or valid_from or valid_to
            anchor_day = epoch_day(anchor)
            if anchor_day is None or not _within(anchor_day, valid_from, valid_to):
                invalid += 1
                continue
            # Inside the window: more recently valid is more specific to the request.
            score = 1.0
            source = 'validity'
            date = valid_from or anchor
        else:
            date, field = None, None
            for candidate_field in fields:
                if node.get(candidate_field):
                    date, field = node[candidate_field], candidate_field
                    break
            day = epoch_day(date)
            if day is None:
                undated.append(rid)
                continue
            if not _within(day, node.get('vf'), node.get('vt')):
                invalid += 1
                continue
            age_years = max(0, newest - day) / 365.25 if newest else 0.0
            recency = 1.0 / (1.0 + age_years)
            score = 0.5 * relevance + 0.5 * recency
            source = FIELD_SOURCE.get(field, f'frontmatter:{field}')

        candidates.append({
            'id': rid,
            'raw_score': round(min(score, 1.0), 6),
            'evidence': {'date': date, 'date_field': source, 'relevance': round(relevance, 4),
                         'time_source': source, 'clock': intent['clock'],
                         'valid_from': node.get('vf'),
                         'valid_to': node.get('vt'),
                         'observed_at': node.get('obs')},
            'equivalence_group_id': None,
        })

    coverage = {**base_coverage, 'dated': len(nodes), 'undated_eligible': len(undated),
                'irrelevant': irrelevant, 'out_of_validity': invalid,
                'candidates': len(candidates), 'returned_hits': 0,
                'time_sources': sorted({c['evidence']['time_source'] for c in candidates})}
    if not candidates:
        if mode == 'period':
            return _result('EMPTY', reason='NO_VALIDITY_DATA', elapsed_ms=elapsed(),
                           manifest=pin, coverage=coverage)
        # The named clock had no authored evidence. `date_added` exists in this catalog but is
        # not an answer to "latest version" or "last updated", so it is not substituted.
        clock = intent['clock'] or 'updated'
        return _result('EMPTY', reason=CLOCK_EMPTY_REASON.get(clock, 'NO_SOURCE_TIMESTAMPS'),
                       elapsed_ms=elapsed(), manifest=pin,
                       coverage={**coverage, 'missing_clock': clock,
                                 'searchable_fields': list(fields)})

    candidates.sort(key=lambda hit: (-hit['raw_score'], hit['id']))
    hits = candidates[:max(0, int(limit))]
    for index, hit in enumerate(hits, start=1):
        hit['rank'] = index
    coverage['returned_hits'] = len(hits)
    return _result('OK', hits=hits, elapsed_ms=elapsed(), manifest=pin, coverage=coverage)
