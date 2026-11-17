"""Non-destructive cleanup layer for the skill catalog.

This module never deletes a skill and never rewrites the source hub. It reads a
built catalog DB (source or candidate), and writes a *clean candidate* copy with
three additive things:

1. ``category_norm`` / ``category_key`` on every resource: a normalized form of
   the authored category only. Case and whitespace are normalized; synonyms are
   NOT merged and nothing is invented. Missing stays UNKNOWN with a reason.
2. ``duplicate_groups`` + ``duplicate_members``: equivalence evidence separated
   into verified full duplicates (same bundle digest, document digest and risk),
   body-only repeats, description repeats and name families. Only the verified
   full group is alias-eligible; the rest are review groups kept distinct.
3. ``cleanup_ledger`` and ``quarantine_class``: a row-level audit trail and a
   reason class for every quarantined entry, including whether a dangling
   reference target exists elsewhere in the hub.

Everything is deterministic: same input DB + same source tree gives the same
output rows, so a reviewer can diff two runs.
"""
from __future__ import annotations

import rust_near

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CLEANUP_REVISION = 'catalog-cleanup-v1'
NEAR_DUPLICATE_THRESHOLD = 0.80
NEAR_SHINGLE = 5
NEAR_MAX_PAIRS_PER_FAMILY = 50
DESCRIPTION_MIN_CHARS = 40

QUARANTINE_CLASSES = (
    'missing_skill_md',
    'empty_body',
    'malformed_frontmatter',
    'missing_frontmatter',
    'escaping_reference',
    'dangling_reference',
    'unsafe_bundle',
    'other',
)

GROUP_FULL = 'full_duplicate'
GROUP_BODY = 'body_duplicate'
GROUP_DESCRIPTION = 'description_duplicate'
GROUP_FAMILY = 'name_family'
GROUP_NEAR = 'near_duplicate'


def normalize_category(value: Optional[str]) -> Tuple[str, str]:
    """Return (display, key) for an authored category, or ('', '') when absent.

    Normalization is only case and whitespace: ``Engineering / Code Quality`` and
    ``engineering / code quality`` share a key, but ``engineering`` and
    ``code-quality`` do not. Merging different terms is a taxonomy decision and is
    deliberately not made here.
    """
    if not isinstance(value, str):
        return '', ''
    display = ' '.join(value.split())
    if not display:
        return '', ''
    return display, display.casefold()


def classify_quarantine(reason: str) -> str:
    """Map one stored quarantine reason to a stable class."""
    text = reason or ''
    if 'missing SKILL.md' in text:
        return 'missing_skill_md'
    if 'escape bundle root' in text:
        return 'escaping_reference'
    if 'missing referenced file' in text:
        return 'dangling_reference'
    if 'malformed frontmatter' in text:
        return 'malformed_frontmatter'
    if 'missing frontmatter' in text:
        return 'missing_frontmatter'
    if 'empty body' in text:
        return 'empty_body'
    if 'invalid bundle' in text or 'is a link' in text:
        return 'unsafe_bundle'
    return 'other'


def missing_reference_paths(reason: str) -> List[str]:
    """Extract the referenced paths quoted in a dangling-reference reason."""
    marker = 'missing referenced file(s): '
    if marker not in (reason or ''):
        return []
    tail = reason.split(marker, 1)[1]
    try:
        found = json.loads(tail.replace("'", '"'))
    except ValueError:
        return []
    return [p for p in found if isinstance(p, str)] if isinstance(found, list) else []


def hub_relative_paths(hub: Path) -> set:
    """Every relative path that exists inside any skill tree, for reference reconciliation.

    One directory walk, no file reads: this answers "does this referenced file
    exist somewhere in the hub" without hashing or copying anything.
    """
    found = set()
    skills = Path(hub) / 'skills'
    if not skills.is_dir():
        return found
    for tree in sorted(p for p in skills.iterdir() if p.is_dir()):
        try:
            for current, _dirs, files in os.walk(tree):
                for name in files:
                    absolute = Path(current) / name
                    try:
                        found.add(absolute.relative_to(tree).as_posix())
                    except ValueError:
                        continue
        except OSError:
            continue
    return found


def _shingles(text: str) -> set:
    words = text.split()
    if len(words) < NEAR_SHINGLE:
        return {' '.join(words)} if words else set()
    return {' '.join(words[i:i + NEAR_SHINGLE]) for i in range(len(words) - NEAR_SHINGLE + 1)}


def jaccard(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def connect_for_cleanup(database, *, read_only=False):
    target = Path(database).resolve().as_uri() + '?mode=ro' if read_only else database
    con = sqlite3.connect(target, uri=read_only)
    con.row_factory = sqlite3.Row
    # A bounded 256 MiB mapping avoids per-page reads across Docker Desktop mounts.
    # SQLite uses its normal pager for the rest of the file or if mmap is unavailable.
    con.execute('PRAGMA main.mmap_size=268435456')
    if read_only:
        # Keep families and bodies on one snapshot and acquire the read lock once.
        con.execute('BEGIN')
    return con


def open_candidate(source_db: Path | str, output_db: Path | str) -> sqlite3.Connection:
    """Copy the source DB and open the copy for writing; the source stays untouched."""
    source = Path(source_db)
    target = Path(output_db)
    if not source.is_file():
        raise FileNotFoundError(f'catalog DB not found: {source}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.copyfile(source, target)
    con = connect_for_cleanup(target)
    con.execute('PRAGMA journal_mode=MEMORY')
    con.execute('PRAGMA synchronous=OFF')
    return con


SCHEMA_ADDITIONS = '''
CREATE TABLE IF NOT EXISTS duplicate_groups(
    group_id TEXT PRIMARY KEY,
    group_kind TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    alias_eligible INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    decision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS duplicate_members(
    group_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(group_id, resource_id));
CREATE TABLE IF NOT EXISTS cleanup_ledger(
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    field TEXT,
    before_value TEXT,
    after_value TEXT,
    reason TEXT,
    review_status TEXT NOT NULL,
    PRIMARY KEY(resource_id, action, field));
CREATE TABLE IF NOT EXISTS quarantine_class(
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    class TEXT NOT NULL,
    unresolved_paths TEXT,
    target_exists_elsewhere INTEGER,
    review_status TEXT NOT NULL,
    PRIMARY KEY(name, kind));
CREATE TABLE IF NOT EXISTS category_norm(
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    category_norm TEXT NOT NULL,
    category_key TEXT NOT NULL,
    canonical_name TEXT,
    is_alias INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(kind, name));
'''


def normalize_categories_in_db(con: sqlite3.Connection) -> Dict[str, int]:
    """Record a normalized form of the authored category in an additive side table.

    ``skills`` and ``prompts`` are views, so a column cannot be added to them
    without recreating the view callers already use. The side table keeps the
    original columns untouched and is joined by ``(kind, name)``.
    """
    stats = {'skills': 0, 'prompts': 0, 'renormalized': 0, 'unknown': 0,
             'case_collisions': 0, 'prompt_category_from_prompt_type': 0}
    first_display: Dict[str, str] = {}
    for kind, table in (('skill', 'skills'), ('prompt', 'prompts')):
        first_display = {}
        # A prompt's authored category is prompt_type; callers already expose it
        # as the prompt category (shortlist_runtime), so read the same field.
        source = 'category_canonical' if kind == 'skill' else 'prompt_type'
        rows = con.execute(
            f'SELECT name, {source} AS raw_category, category_canonical, category_state, '
            f'canonical_name, is_alias FROM {table}').fetchall()
        for row in rows:
            raw = row['raw_category'] or ''
            if kind == 'prompt' and raw and not (row['category_canonical'] or ''):
                stats['prompt_category_from_prompt_type'] += 1
            display, key = normalize_category(raw)
            if display:
                seen = first_display.get(key)
                if seen is None:
                    first_display[key] = display
                elif seen != display:
                    # Only the lookup key folds case; the stored display keeps the
                    # authored spelling. Record the key collision, not a rewrite.
                    stats['case_collisions'] += 1
                    con.execute(
                        'INSERT OR REPLACE INTO cleanup_ledger VALUES(?,?,?,?,?,?,?,?)',
                        (f'{kind}:{row["name"]}', kind, 'case_collision', 'category_key',
                         raw, key,
                         f'casefold key {key!r} also used by {seen!r}; '
                         f'value kept as authored, lookup collapses', 'REVIEW_REQUIRED'))
            con.execute(
                'INSERT OR REPLACE INTO category_norm VALUES(?,?,?,?,?,?)',
                (kind, row['name'], display, key, row['canonical_name'],
                 1 if row['is_alias'] else 0))
            stats['skills' if kind == 'skill' else 'prompts'] += 1
            if not display:
                stats['unknown'] += 1
                continue
            if display != raw:
                stats['renormalized'] += 1
                con.execute(
                    'INSERT OR REPLACE INTO cleanup_ledger VALUES(?,?,?,?,?,?,?,?)',
                    (f'{kind}:{row["name"]}', kind, 'normalized', 'category',
                     raw, display, 'case/whitespace normalization only', 'AUTHORED'))
    return stats


def quarantined_skill_names(con: sqlite3.Connection) -> set:
    """Skill names the index refused, including disabled ones."""
    return {row[0] for row in con.execute(
        "SELECT name FROM quarantine WHERE kind='skill'")}


def build_full_groups(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Identical bundle bytes, document and risk: verified identity, maybe aliasable.

    Identity is not permission. An identical pair whose canonical member is
    disabled or quarantined proves the two files are the same bytes, but a
    caller must never be able to resolve a name onto a skill the catalog
    refuses to serve. Such a group is kept as evidence with
    ``alias_eligible=0`` and a stated reason.
    """
    blocked = quarantined_skill_names(con)
    skills = con.execute('''SELECT name, content_sha256, bundle_sha256, risk, is_alias,
                                   canonical_name, active
                            FROM skills WHERE content_sha256 IS NOT NULL
                              AND bundle_sha256 IS NOT NULL
                              AND length(bundle_sha256)>0''').fetchall()
    buckets: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}
    for row in skills:
        if not row['content_sha256'] or not row['bundle_sha256']:
            continue
        buckets.setdefault((row['bundle_sha256'], row['content_sha256'],
                            row['risk'] or 'unknown'), []).append(row)
    groups = []
    for (bundle, content, risk), rows in buckets.items():
        if len(rows) < 2:
            continue
        group_id = 'full-' + hashlib.sha256(
            json.dumps([bundle, content, risk]).encode()).hexdigest()[:16]
        # Canonical is the member the index did NOT mark as an alias. A declared
        # alias is never canonical, otherwise the group would alias the real
        # skill onto its own copy.
        non_alias = [r['name'] for r in rows if not r['is_alias']]
        plain = [n for n in non_alias if '--' not in n]
        if plain:
            canonical = plain[0]
        elif non_alias:
            canonical = sorted(non_alias)[0]
        else:
            canonical = sorted(r['name'] for r in rows)[0]
        aliases = [r['name'] for r in rows if r['name'] != canonical]
        by_name = {r['name']: r for r in rows}

        def usable(name: str) -> bool:
            row = by_name[name]
            return bool(row['active']) and name not in blocked

        reasons = []
        if not usable(canonical):
            reasons.append(f'canonical {canonical!r} is inactive or quarantined')
        bad_alias = [a for a in aliases if not usable(a)]
        if bad_alias:
            reasons.append('alias member(s) inactive or quarantined: '
                           + ', '.join(sorted(bad_alias)))
        eligible = bool(aliases) and not reasons
        groups.append({
            'group_id': group_id, 'kind': GROUP_FULL,
            'members': [dict(r) for r in rows],
            'evidence': {
                'bundle_sha256': bundle, 'content_sha256': content, 'risk': risk,
                'canonical': canonical, 'alias_members': aliases,
                'active_members': sorted(r['name'] for r in rows if r['active']),
                'quarantined_members': sorted(r['name'] for r in rows
                                              if r['name'] in blocked),
                'eligibility_reason': ('active canonical and active alias member'
                                       if eligible else '; '.join(reasons)),
            },
            'alias_eligible': eligible,
            'decision': ('canonical+alias: identical bundle bytes, document and risk; '
                         'canonical is active and serves'
                         if eligible else
                         'identity only: identical bytes but NOT alias-eligible - '
                         + '; '.join(reasons)),
        })
    return groups


def build_body_groups(con: sqlite3.Connection, full_ids: set) -> List[Dict[str, Any]]:
    """Same normalized *body* (body_sha256), regardless of frontmatter or scripts.

    Body identity is deliberately narrower than document identity: two skills
    can ship the same instructions with different frontmatter, scripts, risk or
    license, and those must stay separate resources. This builder therefore
    keys on ``body_sha256`` - the normalized body digest - and never on
    ``content_sha256`` (whole document) or ``bundle_sha256`` (served tree).

    Every same-body group of active skills is reported, including a pair whose
    bytes also happen to be fully identical; that pair is marked
    ``resolved_full_duplicate`` so the full group is not silently dropped from
    the body inventory. No body group is ever alias-eligible.
    """
    rows = con.execute('''SELECT name, body_sha256, content_sha256, bundle_sha256, risk
                          FROM skills WHERE active=1
                            AND body_sha256 IS NOT NULL AND length(body_sha256)>0
                          ORDER BY body_sha256, name''').fetchall()
    buckets: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        buckets.setdefault(row['body_sha256'], []).append(row)
    groups = []
    for body, members in buckets.items():
        if len(members) < 2:
            continue
        differences = len({m['content_sha256'] or '' for m in members}) > 1
        group_id = 'body-' + hashlib.sha256(body.encode()).hexdigest()[:16]
        annotated = []
        for m in members:
            item = dict(m)
            # A member already resolved as canonical+alias keeps that fact visible
            # here instead of vanishing from the body inventory.
            item['role'] = ('resolved_full_duplicate'
                            if f'skill:{m["name"]}' in full_ids else 'kept')
            annotated.append(item)
        groups.append({
            'group_id': group_id, 'kind': GROUP_BODY,
            'members': annotated,
            'evidence': {
                'body_sha256': body,
                'content_sha256s': sorted({m['content_sha256'] or '' for m in members}),
                'bundles': sorted({m['bundle_sha256'] or '' for m in members}),
                'risks': sorted({m['risk'] or 'unknown' for m in members}),
                'frontmatter_or_script_differences': differences,
                'resolved_members': sorted(
                    m['name'] for m in annotated
                    if m['role'] == 'resolved_full_duplicate'),
            },
            'alias_eligible': False,
            'decision': ('review only: shared normalized body; '
                         + ('members differ in frontmatter/scripts/risk and stay distinct'
                            if differences else
                            'identical members already resolved as a full duplicate; '
                            'remaining differences none')
                         + '; never alias-eligible'),
        })
    return groups


def build_description_groups(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Identical non-trivial descriptions: a review signal, not a duplicate claim."""
    rows = con.execute('''SELECT name, description, content_sha256 FROM skills
                          WHERE active=1 AND description IS NOT NULL''').fetchall()
    buckets: Dict[str, List[str]] = {}
    for row in rows:
        text = ' '.join((row['description'] or '').split()).casefold()
        if len(text) < DESCRIPTION_MIN_CHARS:
            continue
        buckets.setdefault(text, []).append(row['name'])
    groups = []
    for text, names in buckets.items():
        if len(names) < 2:
            continue
        group_id = 'desc-' + hashlib.sha256(text.encode()).hexdigest()[:16]
        groups.append({
            'group_id': group_id, 'kind': GROUP_DESCRIPTION,
            'members': [{'name': n} for n in sorted(names)],
            'evidence': {'description_sha256': hashlib.sha256(text.encode()).hexdigest(),
                         'description': text[:300]},
            'alias_eligible': False,
            'decision': 'review only: equal description does not prove equal capability',
        })
    return groups


def build_family_groups(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Resources sharing a base name before ``--source``: hints, never merges."""
    rows = con.execute('SELECT name FROM skills WHERE active=1 ORDER BY name').fetchall()
    buckets: Dict[str, List[str]] = {}
    for row in rows:
        buckets.setdefault(row['name'].split('--')[0], []).append(row['name'])
    groups = []
    for base, names in buckets.items():
        if len(names) < 2:
            continue
        group_id = 'family-' + hashlib.sha256(base.encode()).hexdigest()[:12]
        groups.append({
            'group_id': group_id, 'kind': GROUP_FAMILY,
            'members': [{'name': n} for n in names],
            'evidence': {'base_name': base},
            'alias_eligible': False,
            'decision': 'ranking hint only: same base name, independent resources',
        })
    return groups


def build_near_duplicate_pairs(con: sqlite3.Connection,
                               families: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bounded fuzzy pairs *inside* name families only.

    Comparing all 8.4k bodies pairwise is 35M comparisons. Restricting to name
    families keeps the work bounded and the evidence interpretable: a near pair
    is a same-base-name pair with high 5-word shingle overlap.
    """
    out: List[Dict[str, Any]] = []
    for family in families:
        names = [m['name'] for m in family['members']]
        if len(names) < 2 or len(names) > 40:
            continue
        placeholders = ','.join('?' for _ in names)
        rows = con.execute(
            f'''SELECT s.name, b.body FROM skills AS s
                JOIN resource_bodies AS b ON b.content_sha256 = s.body_sha256
                WHERE s.name IN ({placeholders})''', names)
        texts = {row['name']: row['body'] for row in rows if row['body']}
        pairs = 0
        ordered = sorted(texts)
        scores = (rust_near.similarities([texts[name] for name in ordered])
                  if NEAR_SHINGLE == 5 else None)
        if scores is None:
            texts = {name: _shingles(text) for name, text in texts.items()}
        for i, left in enumerate(ordered):
            for j, right in enumerate(ordered[i + 1:], start=i + 1):
                score = (scores[i * len(ordered) + j] if scores is not None
                         else jaccard(texts[left], texts[right]))
                if score < NEAR_DUPLICATE_THRESHOLD:
                    continue
                pairs += 1
                if pairs > NEAR_MAX_PAIRS_PER_FAMILY:
                    break
                group_id = 'near-' + hashlib.sha256(
                    json.dumps(sorted([left, right])).encode()).hexdigest()[:16]
                out.append({
                    'group_id': group_id, 'kind': GROUP_NEAR,
                    'members': [{'name': left}, {'name': right}],
                    'evidence': {'jaccard_5gram': round(score, 4),
                                 'base_name': family['evidence']['base_name']},
                    'alias_eligible': False,
                    'decision': 'review only: high text overlap, bundle identity unverified',
                })
            if pairs > NEAR_MAX_PAIRS_PER_FAMILY:
                break
    return out


def persist_groups(con: sqlite3.Connection, groups: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for group in groups:
        members = group['members']
        # The builder decides canonical/alias from real evidence; persistence only
        # records it. A group is alias-eligible solely when the builder proved an
        # active canonical and an active alias member.
        evidence = group['evidence']
        alias_member = None
        if group['alias_eligible']:
            alias_member = (evidence.get('alias_members') or [None])[0]
            if alias_member is None:
                # Never claim eligibility without a concrete alias target.
                group = {**group, 'alias_eligible': False}
        con.execute('INSERT OR REPLACE INTO duplicate_groups VALUES(?,?,?,?,?,?)', (
            group['group_id'], group['kind'], len(members),
            1 if group['alias_eligible'] else 0,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            group['decision']))
        for member in members:
            name = member['name']
            if group['alias_eligible']:
                role = 'alias' if name == alias_member else 'canonical'
            elif member.get('role'):
                role = member['role']
            else:
                role = 'kept'
            con.execute('INSERT OR REPLACE INTO duplicate_members VALUES(?,?,?,?,?)', (
                group['group_id'], f'skill:{name}', 'skill', name, role))
        counts[group['kind']] = counts.get(group['kind'], 0) + 1
    return counts


def persist_quarantine(con: sqlite3.Connection, hub: Path) -> Dict[str, Any]:
    """Classify every quarantined entry and reconcile its dangling references."""
    existing = hub_relative_paths(hub)
    rows = con.execute('SELECT name, kind, reason FROM quarantine ORDER BY name').fetchall()
    by_class: Dict[str, int] = {}
    resolvable = 0
    for row in rows:
        klass = classify_quarantine(row['reason'])
        paths = missing_reference_paths(row['reason'])
        targets = [p for p in paths if p in existing]
        if targets:
            resolvable += 1
        con.execute('INSERT OR REPLACE INTO quarantine_class VALUES(?,?,?,?,?,?)', (
            row['name'], row['kind'], klass,
            json.dumps(paths, ensure_ascii=False),
            1 if targets else 0,
            'UNRESOLVED'))
        by_class[klass] = by_class.get(klass, 0) + 1
    return {'by_class': by_class, 'total': len(rows),
            'dangling_target_exists_elsewhere': resolvable}


def counts(con: sqlite3.Connection) -> Dict[str, Any]:
    one = lambda sql: con.execute(sql).fetchone()[0]
    # The normalization side table only exists after this module creates it, so
    # the pre-cleanup pass reports None rather than a fabricated zero.
    has_key = bool(con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='category_norm'"
    ).fetchone()[0])
    return {
        'active_skills': one('SELECT COUNT(*) FROM skills WHERE active=1'),
        'inactive_skills': one('SELECT COUNT(*) FROM skills WHERE active=0'),
        'active_prompts': one('SELECT COUNT(*) FROM prompts'),
        'authored_category_skills': one(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND category_state='AUTHORED'"),
        'unknown_category_skills': one(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND category_state='UNKNOWN'"),
        'authored_tags_skills': one(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND tags_source='AUTHORED'"),
        'derived_tags_skills': one(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND tags_source<>'AUTHORED'"),
        'distinct_category_authored': one(
            "SELECT COUNT(DISTINCT category_canonical) FROM skills WHERE active=1 "
            "AND category_state='AUTHORED'"),
        'distinct_category_norm': one(
            "SELECT COUNT(DISTINCT n.category_key) FROM category_norm n "
            "JOIN skills s ON s.name=n.name WHERE n.kind='skill' AND s.active=1 "
            "AND n.category_key<>''") if has_key else None,
        'active_prompts_with_category': one(
            "SELECT COUNT(*) FROM prompts WHERE prompt_type IS NOT NULL "
            "AND length(prompt_type)>0"),
        'unknown_category_prompts': one(
            "SELECT COUNT(*) FROM prompts WHERE prompt_type IS NULL "
            "OR length(prompt_type)=0"),
        'quarantined': one('SELECT COUNT(*) FROM quarantine'),
        'aliases': one('SELECT COUNT(*) FROM aliases'),
    }


def run(source_db: Path | str, output_db: Path | str, hub: Path | str) -> Dict[str, Any]:
    """Run the whole cleanup on a copy and return the ledger summary."""
    hub = Path(hub)
    con = open_candidate(source_db, output_db)
    try:
        con.executescript(SCHEMA_ADDITIONS)
        before = counts(con)
        normalization = normalize_categories_in_db(con)

        full = build_full_groups(con)
        # Only a group that is actually alias-resolved resolves its members'
        # identity; an identity-only group (inactive canonical, quarantined
        # member) must not suppress those skills from the body inventory.
        full_ids = {f'skill:{m["name"]}' for g in full
                    if g['alias_eligible'] for m in g['members']}
        body = build_body_groups(con, full_ids)
        descriptions = build_description_groups(con)
        families = build_family_groups(con)
        near = build_near_duplicate_pairs(con, families)
        group_counts = persist_groups(con, full + body + descriptions + families + near)

        quarantine = persist_quarantine(con, hub)
        after = counts(con)
        # Independent cross-check: recompute same-body groups with a plain SQL
        # aggregate and compare to what the builder wrote. A report that claims
        # 0 while the catalog holds 7 must fail here, not at review time.
        crosscheck = con.execute('''SELECT COUNT(*) groups, COALESCE(SUM(n),0) members
            FROM (SELECT COUNT(*) n FROM skills
                  WHERE active=1 AND body_sha256 IS NOT NULL AND length(body_sha256)>0
                  GROUP BY body_sha256 HAVING COUNT(*)>1)''').fetchone()
        written = con.execute('''SELECT COUNT(*) groups, COALESCE(SUM(member_count),0) members
            FROM duplicate_groups WHERE group_kind='body_duplicate' ''').fetchone()
        if (crosscheck[0], crosscheck[1]) != (written[0], written[1]):
            raise RuntimeError(
                'body duplicate inventory mismatch: SQL says '
                f'{crosscheck[0]} groups/{crosscheck[1]} members, groups table says '
                f'{written[0]}/{written[1]}')
        con.execute('''CREATE TABLE IF NOT EXISTS cleanup_meta(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        meta = {
            'cleanup_revision': CLEANUP_REVISION,
            'near_duplicate_threshold': NEAR_DUPLICATE_THRESHOLD,
            'near_duplicate_scope': 'within name families only',
            'category_normalization': 'case and whitespace only; no synonym merging',
        }
        for key, value in meta.items():
            con.execute('INSERT OR REPLACE INTO cleanup_meta VALUES(?,?)',
                        (key, json.dumps(value) if not isinstance(value, str) else value))
        con.commit()
        summary = {
            'cleanup_revision': CLEANUP_REVISION,
            'source_db': str(Path(source_db)),
            'output_db': str(Path(output_db)),
            'before': before,
            'after': after,
            'normalization': normalization,
            'groups': group_counts,
            'body_duplicate_crosscheck': {
                'sql_groups': crosscheck[0], 'sql_members': crosscheck[1],
                'written_groups': written[0], 'written_members': written[1],
                'matches': True,
            },
            'alias_eligible_groups': con.execute(
                "SELECT COUNT(*) FROM duplicate_groups WHERE alias_eligible=1").fetchone()[0],
            'quarantine': quarantine,
        }
        return summary
    finally:
        con.close()
