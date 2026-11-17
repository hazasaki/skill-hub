"""The runtime shortlist over the live Skill Hub: who may be chosen, for skills and prompts alike.

`unified_registry.shortlist()` decides eligibility over an in-memory `Registry`. The host does
not serve from one: it serves `skill_index.search()` and `skill_index.search_prompts()` over the
SQLite FTS index of the real catalog. This module is the missing half — the same decision, made
against the catalog the host actually reads — and it returns the exact shape
`r4_pipeline.run()` and `decision_contract.candidate_snapshot()` already consume, so the R4
chain runs unmodified against 9,536 real skills and 1,341 real prompts.

Two permissions are separated, because conflating them is the defect this module exists to fix:

* **catalog access** — `search`/`get` keep serving every resource the index holds, including the
  proprietary skills the user chose to keep. Nothing here deletes or bulk-disables anything.
* **runtime shortlist eligibility** — a resource may be *offered to a Decision Model* when the
  catalog says it exists, is switched on and has a valid identity. An administrative
  `runtime-approvals.json` record is not a prerequisite: requiring one marked every row
  `not_approved` on a hub where no approvals were ever written, so the shortlist returned
  `considered=0` and the Decision Model had nothing to score. A record may still *hold a
  resource back* (`runtime_shortlist: denied|pending`, or a non-active activation state), so
  presence can narrow eligibility; absence never decides it.

The live index enforces the switch for skills: `skills.active` exists and `search()` filters on
it, while the `prompts` table gained its own `active` column in the current catalog and
`search_prompts()` filters on it when present. `_is_disabled` still asks the host's own marker,
so an on-disk DISABLED marker beats a stale `active=1` row.

Ranking is deliberately not re-invented. Candidates keep the FTS order the host's own
`search_skills` tool returns, so a retrieval benchmark measures the shipped ranking with and
without the eligibility filter, and any difference between the two arms *is* the filter.

    shortlist_from_catalog(hub, query) -> CANDIDATES | NO_CANDIDATE | MISSING_RESOURCE | INVALID_REQUEST

`NO_CANDIDATE` is a fact about the catalog and carries `is_selection_error: False`: an empty
shortlist must never be scored as the model choosing badly.
"""
from __future__ import annotations

import copy
import sqlite3
import threading
import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 'openbot.elephant.shortlist.v1'

OUTCOMES = ('CANDIDATES', 'NO_CANDIDATE', 'MISSING_RESOURCE', 'INVALID_REQUEST')

# Eligibility is one record per resource, keyed `type/id`. A record that exists must be coherent:
# it can narrow eligibility (hold a resource back), never widen it. An unrecognised value is
# refused rather than mapped, so a typo cannot read as approval, and an alias name cannot be
# used to admit a resource whose own catalog row is inactive.
ELIGIBILITY_FIELDS = ('activation_status', 'runtime_shortlist', 'authorization_source')
ELIGIBLE = {'activation_status': 'active', 'runtime_shortlist': 'approved',
            'authorization_source': 'host_policy_only'}

# The catalog grammar `unified_registry` and `decision_contract` both accept. A name outside it
# cannot be pinned into a request, so it is reported as ineligible rather than renamed here:
# renaming a catalog resource would change the hub, which is not this module's business.
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', re.ASCII)


class ShortlistError(ValueError):
    """A catalog or approval source this module refuses to read."""


# Words that carry no routing signal. Kept deliberately small and generic: this is a
# function-word list, not a synonym table, so it removes noise without encoding any particular
# expected answer. `jieba`-style language detection is deliberately not used; a query is treated
# as Vietnamese only when it carries no ASCII content word at all.
STOPWORDS = frozenset("""
a an the for with and or of to in on at is are be as by from that this it its
how do does did what when where which who why plus also than then into over under
today good morning evening hello hi hey thanks please your you we i my our
và cho của là các những một khi để trong trên về nhưng hoặc với dùng ngày làm việc chúc bạn buổi sáng
""".split())


def query_terms(text, *, prefer_ascii=True):
    """Content words of a request, longest-signal first, de-duplicated in order.

    The catalog is English, so an ASCII content word is what can match anything. When a query
    mixes scripts — `bảo mật llm chống prompt injection` — the ASCII words are the ones with
    catalogue reach, and using the Vietnamese ones alongside only dilutes the ranking. A query
    with no ASCII content word keeps its own tokens: `triển khai docker` still reaches docker.
    """
    tokens = [t.lower() for t in re.findall(r'\w+', text or '', flags=re.UNICODE)]
    content = [t for t in tokens if len(t) > 1 and t not in STOPWORDS]
    ascii_terms = [t for t in content if t.isascii() and not t.isdigit()]
    chosen = ascii_terms if (prefer_ascii and ascii_terms) else content
    seen, ordered = set(), []
    for term in chosen:
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


# The share of a request's words that must carry meaning for it to be a capability request.
# Chosen on the public validation set, where positive requests measured 0.78-1.00 and off-topic
# conversation measured 0.00-0.44; the floor sits between the two with margin on both sides.
# It is a property of the request, not of any expected answer, so it applies to every query the
# same way and can be tested against the holdout without having been fitted to it.
MIN_CONTENT_RATIO = 0.5


def content_ratio(text):
    """Content words over all words: an information-density check on the request itself.

    `what is the square root of 144 plus 15` is 44% content, `xin chào buổi sáng chúc bạn một
    ngày làm việc vui vẻ` is 33%: both are conversation or trivia, not requests for a capability,
    and the catalog happening to contain a word from them (`square`) is a coincidence rather
    than a match. A routing shortlist that accepts them produces a candidate the Decision Model
    must then reject, which is the wrong layer to spend that decision on.
    """
    tokens = [t.lower() for t in re.findall(r'\w+', text or '', flags=re.UNICODE)]
    if not tokens:
        return 0.0
    content = [t for t in tokens if len(t) > 1 and t not in STOPWORDS]
    return len(content) / len(tokens)


def grounded_enough(terms, grounded, name_matched=0):
    """Whether a resource is anchored by enough of this request, not by one incidental word.

    Rule 2 of the relaxed arm used to accept a single grounded term, measured on a 46-query
    out-of-scope probe set: fluent off-topic requests reached the shortlist through words that
    occur in thousands of descriptions (`can`, `plot`, `square`), giving 15% rejection. Two
    distinct grounded terms brought the same set to 80% rejection. It is a property of the
    request and the resource together; no word list and no per-query rule is involved.

    The half-the-request bar alone is wrong for long requests naming a short resource: a
    nine-word `Accessibility Audit` request is exactly two content words of description, so a
    correct prompt could never clear half of it. A candidate whose name accounts for two
    grounded terms clears that bar instead. Measured at production depth 40 over the frozen
    validation set: out-of-scope rejection stays at 37/46 on the probe set, skill recall holds
    at 15/22, prompt recall goes 3/4 to 4/4, and validation recall@5 goes 18/26 to 19/26. One
    term is never enough, in either branch.
    """
    if not terms:
        return False
    return len(grounded) >= 2 and (name_matched >= 2 or len(grounded) * 2 >= len(terms))


def _name_coverage(name, terms):
    """How many query terms the resource name accounts for.

    A term matches a name part when it is the same word, when the name part is the term plus a
    suffix (`commit`/`commits`), or when the term is long enough to be a real prefix of a longer
    name part (`docker`/`docker-compose`). The last rule needs the length floor: a two-letter
    term would otherwise match almost every name by accident.
    """
    if not terms:
        return 0
    parts = [part for part in re.findall(r'\w+', (name or '').lower()) if part]
    if not parts:
        return 0
    matched = 0
    for term in terms:
        for part in parts:
            if part == term:
                matched += 1
                break
            # A suffix may extend the term, but only when the name part itself is a real word:
            # otherwise a two-letter term would be satisfied by any longer name beginning so.
            if part.startswith(term) and len(part) >= 4 and len(part) - len(term) <= 3:
                matched += 1
                break
            if term.startswith(part) and len(part) >= 4:
                matched += 1
                break
    return matched


def _document_has(token, name, description, tags):
    """Whether one query token literally occurs in the resource's own text.

    Whole-token containment, not substring: `pháp` must not be satisfied by `php`. This is the
    test that keeps a fluent but irrelevant request from producing candidates purely on a shared
    three-letter fragment.

    A two-character token is accepted only against a *whole* name or description part, never by
    prefix: `fp` and `ts` are real technology names (`fp-ts`) and their loss cost a Vietnamese
    recall case, while a two-character prefix match is what made `ph` reach `php-pro`.
    """
    haystack = ' '.join(filter(None, (name or '', description or '', tags or ''))).lower()
    if not haystack:
        return False
    if len(token) < 3:
        return token in [part for part in re.findall(r'\w+', haystack) if part]
    parts = [part for part in re.findall(r'\w+', haystack) if part]
    return any(part == token or part.startswith(token) or (len(part) >= 4 and token.startswith(part))
               for part in parts)


def _name_anchored(name, grounded):
    """Grounded terms that the resource's own name accounts for.

    This is the signal that separates a real match from a coincidence. `accessibility-audit` is
    anchored by `accessibility` and `audit` even in a nine-word request, while `matplotlib` is not
    anchored by `plot` and `novel` from "summarize the plot of the novel pride and prejudice",
    where those words merely happen to appear in a description.
    """
    if not grounded:
        return []
    parts = [part for part in re.findall(r'\w+', (name or '').lower()) if part]
    anchored = []
    for term in grounded:
        for part in parts:
            if (part == term or (part.startswith(term) and len(part) >= 4
                                 and len(part) - len(term) <= 3)
                    or (term.startswith(part) and len(part) >= 4)):
                anchored.append(term)
                break
    return anchored


def relaxed_search(catalog_module, hub, text, *, kind='skill', limit=10, prompt_type=None):
    """Ranked candidates using every content word instead of requiring all of them.

    The shipped `skill_index.search()` builds one `AND` query, so a single word the catalog does
    not contain — which is *every* Vietnamese word — returns nothing at all. That is why the
    offline baseline missed 73% of its positive queries: the Decision Model was never shown the
    right candidate, so its own accuracy could not matter.

    Three rules, each answering a measured failure rather than a guess:

    1. **Any term matches**, so a partly-English request still reaches the catalog.
    2. **A candidate must contain at least one query token verbatim**, which is what stops a
       fluent off-topic request from producing candidates on bm25 noise alone.
    3. **Name coverage breaks near-ties**, so `hardening-docker-containers-for-production` sorts
       above a skill that merely mentions docker once.

    The hub's own bm25 weights and index do the scoring, so a run is reproducible from the index
    digest; nothing here re-scores with a second model.
    """
    terms = query_terms(text)
    if not terms:
        # No content word means no routing signal: an empty shortlist, not a low-confidence one.
        return []
    match = ' OR '.join('"' + term.replace('"', '') + '"' for term in terms)
    con = catalog_module.connect(hub)
    try:
        if kind == 'skill':
            sql = '''SELECT s.name, s.description, s.tags, s.category, s.files,
                            bm25(fts, 8.0, 4.0, 3.0, 1.0) AS rank
                     FROM fts JOIN skills s ON s.rowid = fts.rowid
                     WHERE fts MATCH ? AND s.active = 1 ORDER BY rank LIMIT ?'''
            rows = con.execute(sql, (match, max(1, min(int(limit), 50)))).fetchall()
            found = [{**dict(row), 'kind': 'skill'} for row in rows]
        else:
            sql = '''SELECT p.name, p.description, p.tags, p.prompt_type, p.files, p.path,
                            bm25(fts_prompts, 8.0, 4.0, 3.0, 1.0) AS rank
                     FROM fts_prompts JOIN prompts p ON p.rowid = fts_prompts.rowid
                     WHERE fts_prompts MATCH ?'''
            params = [match]
            columns = {row[1] for row in con.execute('PRAGMA table_info(prompts)')}
            if 'active' in columns:
                # The current catalog carries prompt activation; an inactive prompt must not be
                # offered just because this arm does not go through `search_prompts()`.
                sql += ' AND p.active = 1'
            if prompt_type:
                sql += ' AND p.prompt_type = ?'
                params.append(prompt_type)
            sql += ' ORDER BY rank LIMIT ?'
            params.append(max(1, min(int(limit), 50)))
            rows = con.execute(sql, params).fetchall()
            found = [{**dict(row), 'kind': 'prompt', 'category': row['prompt_type']}
                     for row in rows]
    finally:
        con.close()
    grounded = []
    for row in found:
        matched = [term for term in terms
                   if _document_has(term, row.get('name'), row.get('description'),
                                    row.get('tags'))]
        if grounded_enough(terms, matched, len(_name_anchored(row.get('name'), matched))):
            grounded.append(row)
    # `sort` is stable, so equal coverage keeps the hub's bm25 order rather than inventing one.
    return sorted(grounded, key=lambda row: -_name_coverage(row['name'], terms))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_catalog(agent_memory_root, hub=None):
    """The host's own `skill_index` module, imported from its owning source.

    `shared_resources` sits beside it and is on the same import path, exactly as the live
    server loads it. Nothing is copied or patched: this holds the real module object, so a
    shortlist reads the same index bytes the host serves.
    """
    root = Path(agent_memory_root)
    if not (root / 'skill_index.py').is_file():
        raise ShortlistError(f'no skill_index.py under {root}')
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    module = importlib.import_module('skill_index')
    return module, Path(hub or module.HUB)


# One entry per (index path, mtime, size). The digest is what pins a shortlist, but hashing a
# 100+ MB index on every request measured 105 ms and dominated the whole shortlist. The key
# includes mtime and size, so a rebuilt index invalidates the cache and a stale digest can never
# be reported: the pin still describes exactly the bytes that were queried.
_IDENTITY_CACHE = {}
_IDENTITY_LOCK = threading.RLock()


def index_identity(catalog_module, hub):
    """Pin the index a shortlist was taken against, so a later reader can prove it."""
    path = (Path(catalog_module.db_path(hub)) if callable(getattr(catalog_module, 'db_path', None))
            else Path(hub) / 'state' / 'skill-index.db')
    if not path.is_file():
        if Path(hub).is_file():
            path = Path(hub)
        else:
            raise ShortlistError(f'no skill index at {path}')
    stat = path.stat()
    wal = Path(str(path) + '-wal')
    ws = wal.stat() if wal.exists() else None
    key = (catalog_module, str(Path(hub).resolve()), str(path.resolve()), stat.st_mtime_ns, stat.st_size, (ws.st_mtime_ns, ws.st_size) if ws else None)
    cached = _IDENTITY_CACHE.get(key)
    if cached is None:
        cached = {'index_path': str(path),
                  'index_sha256': hashlib.sha256(path.read_bytes() + (wal.read_bytes() if ws else b'')).hexdigest(),
                  'index_bytes': stat.st_size, 'hub_root': str(Path(hub).resolve())}
        _IDENTITY_CACHE.clear()
        _IDENTITY_CACHE[key] = cached
    return dict(cached)


def load_eligibility(path):
    """Approval records, or none. A missing file is no approvals, never blanket approval."""
    if path is None:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ShortlistError(f'eligibility file unreadable: {type(error).__name__}') from error
    records = payload.get('records') if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        raise ShortlistError('eligibility file must hold a records object')
    return records


def _ineligible_reason(record, activation):
    """Why this resource may not be shortlisted, or None when it may.

    Order matters: a disabled resource is reported as disabled even if a record also withholds
    it, because the operator's first question is whether the switch works.

    A missing approval record is **not** a reason. The hub has no approval step, so requiring one
    excluded every row (`not_approved`) and starved the Decision Model. What still counts:
    the on-disk disable marker, a valid identifier, and a record that positively withholds the
    resource. Applying a record cannot let an alias or a stale name in: the identifier checked
    here is the candidate's own catalog id.
    """
    if activation == 'disabled':
        return 'disabled'
    if _ID.fullmatch(record['id']) is None:
        return 'invalid_identifier'
    entry = record_entry(record)
    if not entry:
        # No record: eligible on the catalog's own terms (active row, readable file, hash pin).
        return None
    if not isinstance(entry, dict):
        return 'not_approved'
    # A field that is present must match exactly; a field that is absent carries no opinion.
    # This is why a typo (`ACTIVE`) is refused while a record that only says
    # `runtime_shortlist: approved` neither grants nor denies.
    for field in ELIGIBILITY_FIELDS:
        value = entry.get(field)
        if value is not None and value != ELIGIBLE[field]:
            return 'not_approved'
    return None


def record_entry(record):
    """The approval record for one candidate, from the `_approval` slot the caller attached."""
    return record.get('_approval') or {}


def _candidate(catalog_module, hub, name, kind, rank, approval, con=None):
    """One pinned candidate: the identity a later reader re-checks, plus its approval.

    The pin reads `SKILL.md` once and takes the tree's size and file count from the index the
    shortlist already queried. The hub's `get()` is not used here on purpose: it computes a
    `bundle_sha256` by walking every file in the tree, which measured 5-156 ms per candidate and
    put a 20-candidate shortlist far past the 50 ms budget. A recorded bundle digest, when
    present, is pinned directly; the loader computes the actual digest once and refuses a
    mismatch. File count and size are index metadata.
    """
    approval = approval or {}
    if kind == 'skill':
        root = Path(hub) / 'skills' / name
        doc = root / 'SKILL.md'
        raw = doc.read_text(encoding='utf-8', errors='replace')
        content = content_digest(raw)
        meta = tree_metadata(catalog_module, hub, kind, name, con)
        return {'type': 'skill', 'id': name, 'version': content,
                'content_sha256': content,
                'bundle_sha256': approval.get('bundle_sha256'),
                'tree_files': meta.get('files'), 'tree_bytes': meta.get('bytes'),
                'prompt': None, 'state': approval.get('activation_status') or 'active',
                'rank': rank, '_approval': approval}
    root = Path(hub) / 'prompts'
    doc = next((p for p in [root / f'{name}.md', *sorted(root.glob(f'{name}.*'))]
                if p.is_file()), None)
    if doc is None:
        raise KeyError(f'unknown prompt: {name}')
    content = content_digest(doc.read_text(encoding='utf-8', errors='replace'))
    return {'type': 'prompt', 'id': name, 'version': content,
            'content_sha256': content,
            # A prompt is one file: it has no tree, so it has no bundle identity to claim.
            'bundle_sha256': None, 'tree_files': 1,
            'tree_bytes': doc.stat().st_size, 'prompt': None,
            'state': approval.get('activation_status') or 'active',
            'rank': rank, '_approval': approval}


def content_digest(raw):
    """The hub's own normalization, so a pin here equals the digest `get()` would report."""
    return hashlib.sha256(raw.replace('\r\n', '\n').replace('\r', '\n')
                          .encode('utf-8')).hexdigest()


def tree_metadata(catalog_module, hub, kind, name, con=None):
    """File count and byte size straight from the index, with no filesystem walk.

    `con` is passed in by a caller pinning several candidates: opening a connection per
    candidate cost roughly 10 ms each and dominated the shortlist at a 20-candidate budget.
    """
    table = 'skills' if kind == 'skill' else 'prompts'
    own = con is None
    if own:
        con = catalog_module.connect(hub)
    try:
        row = con.execute(f'SELECT files, bytes FROM {table} WHERE name=?', (name,)).fetchone()
    finally:
        if own:
            con.close()
    return dict(row) if row else {}


def _pin(candidate):
    """The exportable half of a candidate: what `decision_contract` pins into a request.

    The bundle pin comes from the approval, if available. The load step computes the actual
    digest once; index file count and size do not identify a bundle.
    """
    return {'type': candidate['type'], 'id': candidate['id'], 'version': candidate['version'],
            'content_sha256': candidate['content_sha256'],
            'bundle_sha256': candidate['bundle_sha256'],
            'tree_files': candidate.get('tree_files'), 'tree_bytes': candidate.get('tree_bytes'),
            'prompt': candidate['prompt'],
            'state': candidate['state'], 'rank': candidate['rank']}


def shortlist_from_catalog(catalog_module, hub, query, *, approvals=None, max_candidates=20,
                           include_prompts=True, prompt_type=None, match='relaxed',
                           min_content_ratio=MIN_CONTENT_RATIO):
    """The deterministic eligible candidate set for one request, pinned and explained.

    `query` carries the retrieval text plus optional `kinds` (`skill`/`prompt`). `approvals`,
    keyed `type/id`, can only *hold a resource back*: a missing or partial hub has no approval
    records at all, and requiring one there returned every row as `not_approved`. Every exclusion
    is reported by reason, so a reader can still tell "the catalog lacks it" from "an operator
    withheld it" from "a record's pin disagrees with the file".

    `match` selects the retrieval arm. `'relaxed'` is the runtime arm — the one whose candidates
    the Decision Model actually sees. `'strict'` is the shipped hub `AND` search, kept because a
    benchmark needs both arms to measure what eligibility and matching each cost.

    `min_content_ratio` is a floor on the request's own information density; a request below it
    is `NO_CANDIDATE` with the measured ratio recorded, and the catalog is never queried for it.
    """
    if not isinstance(query, dict):
        return _outcome('INVALID_REQUEST', reason='query must be an object',
                        catalog=_catalog_revision(catalog_module, hub))
    text = query.get('text')
    if not isinstance(text, str) or not text.strip():
        return _outcome('INVALID_REQUEST', reason='query.text must be nonempty',
                        catalog=_catalog_revision(catalog_module, hub))
    kinds = query.get('kinds') or (['skill', 'prompt'] if include_prompts else ['skill'])
    kinds = [kind for kind in kinds if kind in ('skill', 'prompt')]
    if not kinds:
        return _outcome('INVALID_REQUEST', reason='query.kinds must name skill or prompt',
                        catalog=_catalog_revision(catalog_module, hub))
    if match not in ('relaxed', 'strict'):
        return _outcome('INVALID_REQUEST', reason='match must be relaxed or strict',
                        catalog=_catalog_revision(catalog_module, hub))
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or max_candidates < 1:
        return _outcome('INVALID_REQUEST', reason='invalid candidate budget',
                        catalog=_catalog_revision(catalog_module, hub))

    ratio = content_ratio(text)
    if ratio < min_content_ratio:
        result = _outcome(
            'NO_CANDIDATE', catalog=_catalog_revision(catalog_module, hub),
            reason='request carries too little content to be a capability request')
        result['content_ratio'] = round(ratio, 4)
        result['min_content_ratio'] = min_content_ratio
        return result

    approvals = approvals or {}
    # Ask the catalog for more than the budget: eligibility removes candidates, and asking for
    # exactly the budget would report a shortfall that the catalog does not have.
    depth = max(max_candidates * 4, 40)
    rejected, considered = {}, 0
    eligible = []
    for kind in kinds:
        if match == 'relaxed':
            found = relaxed_search(catalog_module, hub, text, kind=kind, limit=depth,
                                   prompt_type=prompt_type)
        elif kind == 'skill':
            found = catalog_module.search(text, depth, hub)
        else:
            found = catalog_module.search_prompts(text, depth, hub, prompt_type)
        for rank, row in enumerate(found, start=1):
            name = row['name']
            considered += 1
            approval = approvals.get(f'{kind}/{name}')
            probe = {'type': kind, 'id': name, '_approval': approval}
            reason = _ineligible_reason(probe, 'disabled' if _is_disabled(catalog_module, hub, kind, name) else 'active')
            if reason is not None:
                rejected.setdefault(reason, []).append(f'{kind}/{name}')
                continue
            eligible.append((kind, name, rank, approval))

    # FTS rank decides order within a kind. A stale approval does not consume a candidate slot.
    eligible.sort(key=lambda item: (item[0], item[2], item[1]))
    pinned, unavailable = [], []
    con = catalog_module.connect(hub)
    try:
        for kind, name, rank, approval in eligible:
            if len(pinned) == max_candidates:
                break
            try:
                candidate = _candidate(catalog_module, hub, name, kind, rank, approval, con)
                record = approval or {}
                if any(pin is not None and pin != candidate['content_sha256']
                       for pin in (record.get('version'), record.get('content_sha256'))):
                    rejected.setdefault('approval_content_mismatch', []).append(f'{kind}/{name}')
                    continue
                pinned.append(candidate)
            except (KeyError, ValueError, OSError) as error:
                # Named by the catalog, unreadable on disk: a catalog defect, not an empty catalog.
                unavailable.append({'type': kind, 'id': name, 'error': type(error).__name__})
    finally:
        con.close()

    catalog = _catalog_revision(catalog_module, hub)
    if not pinned:
        result = _outcome('NO_CANDIDATE', reason='no active resource matched this query',
                          catalog=catalog, excluded=rejected)
        if unavailable:
            result['unavailable'] = unavailable
        return result
    exported = [_pin(candidate) for candidate in pinned]
    return {
        'schema_version': SCHEMA_VERSION, 'outcome': 'CANDIDATES',
        'candidates': exported, 'candidate_count': len(exported),
        'truncated': len(eligible) > max_candidates,
        'query': text,
        'query_sha256': digest({'text': text, 'kinds': sorted(kinds),
                                'prompt_type': prompt_type}),
        'match': match,
        'query_terms': query_terms(text),
        'content_ratio': round(ratio, 4),
        'min_content_ratio': min_content_ratio,
        'catalog_revision': catalog['catalog_revision'],
        'index_sha256': catalog['index_sha256'],
        'pinned_catalog_sha256': digest(exported),
        'excluded_by_reason': rejected, 'considered': considered,
        'unavailable': unavailable,
        'selection': 'NOT_ATTEMPTED',
        'is_selection_error': False,
        'limits': ['The shortlist decides who may be chosen, never who was chosen.',
                   'A stored record can withhold a resource; absence of one never denies it.',
                   'No re-ranking: candidate order is the host FTS order.'],
    }


def _is_disabled(catalog_module, hub, kind, name):
    """The host's own switch, asked rather than reimplemented. Prompts have no such switch."""
    if kind != 'skill':
        return False
    try:
        return catalog_module.is_disabled(Path(hub) / 'skills' / name)
    except (AttributeError, OSError):
        return False


def _catalog_revision(catalog_module, hub):
    """One identity for the whole catalog, computed from the index that will be queried."""
    identity = index_identity(catalog_module, hub)
    # Counts come from the same index, keyed on the same identity, so they cost one query per
    # index version rather than one per request.
    key = ('counts', catalog_module, identity['hub_root'], identity['index_sha256'])
    counts = _IDENTITY_CACHE.get(key)
    if counts is None:
        try:
            con = catalog_module.connect(hub)
            try:
                skills = con.execute('SELECT COUNT(*) FROM skills').fetchone()[0]
                prompts = con.execute('SELECT COUNT(*) FROM prompts').fetchone()[0]
            finally:
                con.close()
        except Exception as error:  # the index is unreadable; report rather than guess
            raise ShortlistError(f'catalog stats unreadable: {type(error).__name__}') from error
        counts = {'skills': skills, 'prompts': prompts}
        _IDENTITY_CACHE[key] = counts
    return {'catalog_revision': digest({'index_sha256': identity['index_sha256'],
                                        'skills': counts['skills'],
                                        'prompts': counts['prompts']}),
            'index_sha256': identity['index_sha256'],
            'skill_count': counts['skills'], 'prompt_count': counts['prompts']}


def _outcome(kind, *, reason, catalog, excluded=None):
    return {'schema_version': SCHEMA_VERSION, 'outcome': kind, 'candidates': [],
            'candidate_count': 0, 'truncated': False, 'reason': reason,
            'query_sha256': None, 'catalog_revision': catalog['catalog_revision'],
            'index_sha256': catalog['index_sha256'],
            'pinned_catalog_sha256': digest([]),
            'excluded_by_reason': excluded or {}, 'considered': 0, 'unavailable': [],
            'selection': 'NOT_ATTEMPTED', 'is_selection_error': False,
            'limits': ['NO_CANDIDATE and MISSING_RESOURCE describe the catalog, not a bad choice.']}


def catalog_search(catalog_module, hub, text, *, limit=10, kinds=('skill', 'prompt'),
                   prompt_type=None, deadline=None):
    """Catalog access, untouched by eligibility: what a host `search_skills` caller still sees.

    Kept a separate function on purpose. If eligibility ever leaked into this path, the
    catalog resources could silently disappear from search despite being active.
    """
    out = {}
    statuses = {}
    for kind, field in (('skill', 'skills'), ('prompt', 'prompts')):
        if kind not in kinds:
            continue
        kwargs = {'deadline': deadline} if deadline is not None else {}
        try:
            if kind == 'skill':
                out[field] = catalog_module.search(text, limit, hub, **kwargs)
            else:
                out[field] = catalog_module.search_prompts(text, limit, hub, prompt_type, **kwargs)
            statuses[kind] = 'OK'
        except (TimeoutError, sqlite3.OperationalError) as error:
            if deadline is None or (not isinstance(error, TimeoutError)
                                    and getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT):
                raise
            out[field] = []
            statuses[kind] = 'TIMEOUT'
    out['eligibility_applied'] = False
    if deadline is not None:
        out['search_status'] = statuses
        out['partial'] = any(status != 'OK' for status in statuses.values())
    return out


# The catalog entry shown to the Decision Model is an identifier plus a trimmed description.
# Bodies are never sent: the model decides from metadata, and only a selected resource is loaded.
VISIBILITY_DESCRIPTION_CHARS = 160


def _catalog_entry(kind, row, description_chars):
    """One scorable catalog entry: qualified id, trimmed description, no approval."""
    description = ' '.join((row.get('description') or '').split())[:description_chars]
    # Sparse titles do not identify a capability. Supply a bounded purpose opening,
    # never the full resource or its instructions to execute.
    opening = row.get('body_opening') or ''
    opening = re.sub(r'\A---\s*\n.*?\n---\s*\n', '', opening, count=1, flags=re.S)
    opening = re.sub(r'<!--.*?-->', '', opening, flags=re.S)
    purpose = (' '.join(opening.split())[:320]
               if len(query_terms(description, prefer_ascii=False)) < 4 else '')
    return {'type': kind, 'id': f'{kind}:{row["name"]}', 'resource_id': row['name'],
            'description': description, 'tags': str(row.get('tags') or '')[:160],
            'purpose_excerpt': purpose, 'version': row.get('content_sha256'),
            'content_sha256': row.get('content_sha256'),
            'bundle_sha256': row.get('bundle_sha256'), 'risk': row.get('risk'),
            'prompt': None, 'state': 'active'}


def whole_catalog_candidates(catalog_module, hub, *, kinds=('skill', 'prompt'),
                             description_chars=VISIBILITY_DESCRIPTION_CHARS):
    """Every active skill and every prompt, as candidates the Decision Model may score.

    Approval is deliberately not consulted here. Candidate visibility does not
    depend on administrative approval records. Load-time automated verification
    and hash checks live in `elephant_runtime.load_verified`.

    Ids are qualified `type:name`, because three names exist as both a skill and a prompt and
    an unqualified id would let one answer stand for two resources.
    """
    catalog = _catalog_revision(catalog_module, hub)
    identity = index_identity(catalog_module, hub)
    cache_key = ('whole_catalog_candidates', catalog_module, identity['hub_root'], identity['index_path'], identity['index_sha256'], tuple(sorted(kinds)), description_chars)
    cached = _IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    entries = []
    con = catalog_module.connect(hub)
    try:
        for kind, table in (('skill', 'skills'), ('prompt', 'prompts')):
            if kind not in kinds:
                continue
            columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
            identity_cols = [c for c in ('content_sha256', 'bundle_sha256', 'risk') if c in columns]
            extra = (['tags'] if 'tags' in columns else [])
            if 'body' in columns:
                extra.append('substr(body, 1, 1600) AS body_opening')
            fields = ', '.join(['name', 'description'] + identity_cols + extra)
            guards = ['active = 1'] if 'active' in columns else []
            where = ' WHERE ' + ' AND '.join(guards) if guards else ''
            for row in con.execute(f'SELECT {fields} FROM {table}{where} ORDER BY name'):
                entries.append(_catalog_entry(kind, dict(row), description_chars))
    finally:
        con.close()
    if not entries:
        return _outcome('NO_CANDIDATE', reason='the catalog holds no active resource',
                        catalog=catalog)
    seen = set()
    for entry in entries:
        if entry['id'] in seen:
            raise ShortlistError(f'duplicate catalog id {entry["id"]}')
        seen.add(entry['id'])
    res = {'schema_version': SCHEMA_VERSION, 'outcome': 'CANDIDATES', 'candidates': entries,
           'candidate_count': len(entries), 'truncated': False,
           'catalog_revision': catalog['catalog_revision'],
           'index_sha256': catalog['index_sha256'],
           'skill_count': catalog['skill_count'], 'prompt_count': catalog['prompt_count'],
           'description_chars': description_chars,
           'pinned_catalog_sha256': digest(entries),
           'selection': 'NOT_ATTEMPTED', 'is_selection_error': False,
           'limits': ['Candidate visibility; loading still needs automated verification and a digest match.']}
    if len(_IDENTITY_CACHE) >= 128:
        _IDENTITY_CACHE.clear()
    _IDENTITY_CACHE[cache_key] = res
    return copy.deepcopy(res)


# --- provenance, canonical naming and language compatibility ------------------------------

_LANG_SUFFIX_PATTERN = re.compile(
    r'(?:^|[_-])(zh-cn|zh-tw|zh|ja-jp|ja|es|fr|de|ru|ko|pt|it)(?:$|[_-])', re.IGNORECASE)


def parse_canonical_name(resource_id: str) -> tuple[str, str, str | None]:
    """Parse a catalog resource identifier into (base_name, vendor_suffix, language_code).

    Splits namespaced mirror extensions while preserving the semantic root capability.
    """
    clean = resource_id.split(':', 1)[-1] if ':' in resource_id else resource_id
    if '--' in clean:
        base, vendor = clean.split('--', 1)
    else:
        base, vendor = clean, ''
    lang_match = _LANG_SUFFIX_PATTERN.search(vendor or clean)
    lang = lang_match.group(1).lower() if lang_match else None
    return base, vendor, lang


def is_language_compatible(resource_lang: str | None, task_lang: str | None) -> bool:
    """Check language compatibility using validated provenance.

    Neutral resources (None) are universally admitted. Mismatched localized mirrors
    are identified so they do not crowd out primary native capability.
    When task language is unknown or unspecified, resources are admitted so specialists
    are not dropped.
    """
    if not resource_lang:
        return True
    t_lang = (task_lang or '').lower().strip()
    if not t_lang or t_lang == 'unknown':
        return True
    r_lang = resource_lang.lower().strip()
    if r_lang == t_lang or r_lang.startswith(t_lang + '-') or t_lang.startswith(r_lang + '-'):
        return True
    if t_lang in ('vi', 'en'):
        if r_lang in ('zh', 'zh-cn', 'zh-tw', 'ja', 'ja-jp', 'ko', 'ru', 'de', 'fr', 'es', 'pt', 'it'):
            return False
    return True



def _serialized_identity(function):
    from functools import wraps
    @wraps(function)
    def locked(*args, **kwargs):
        with _IDENTITY_LOCK:
            return function(*args, **kwargs)
    return locked

index_identity = _serialized_identity(index_identity)
_catalog_revision = _serialized_identity(_catalog_revision)
whole_catalog_candidates = _serialized_identity(whole_catalog_candidates)
