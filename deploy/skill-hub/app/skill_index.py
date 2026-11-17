#!/usr/bin/env python3
"""Build and query the on-demand skill index for the shared agent hub.

The hub holds ~2000 portable skills. Attaching all of them to a host root makes every
session pay for the full catalog, which is the opposite of on-demand retrieval. This module
indexes the hub once into SQLite FTS5 and serves the two operations a host actually needs:
ranked search over name/description/tags, and retrieval of one skill's full tree.

The hub stays the only source of bytes. Nothing here copies or edits a skill inside the hub.

`get()` returns the body with its frontmatter intact plus the identity a reader needs to join
it to a trace record: `content_sha256` and `bundle_sha256` over the served bytes, the parsed
`metadata`, the `source_root` inside the hub, and the `hub_root` it came from. The FTS `body`
column still holds the body without frontmatter, because that column is for ranking, not for
identity — a caller that only searched never receives a body at all.

Catalog deduplication and validation (B1–B7, D4, D5, D9):
- Content-hash canonical bodies & alias mapping for IDENTICAL content.
- Preservation of divergent same-name versions and provenance (never collapsed by name).
- Frontmatter parsing and normalized risk validation.
- Missing frontmatter fallback title/description recovery.
- Bundle reference validation.
- Rejection of empty bodies on load without arbitrary minimum length constraints.
- Non-destructive quarantine accounting and idempotent ingest.
- Safe opt-in compaction and retention tooling with rollback.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

from ranking_diversity import diversify
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from shared_resources import DEFAULT_HUB
except ImportError:
    DEFAULT_HUB = Path(os.environ.get('SKILL_HUB_ROOT') or Path.home() / '.local/share/skill-hub')

try:
    from src.live import catalog_ingest, catalog_maintenance
except ImportError:
    try:
        import catalog_ingest
        import catalog_maintenance
    except ImportError:
        # Fallback local import
        from . import catalog_ingest, catalog_maintenance

try:
    import retrieval_metadata
except ImportError:  # pragma: no cover - package-relative hosts
    try:
        from . import retrieval_metadata
    except ImportError:
        # Classification is additive: an install that ships only the older modules still
        # serves search/get/stats. Only the metadata build paths require it.
        retrieval_metadata = None


def _require_metadata():
    if retrieval_metadata is None:
        raise RuntimeError(
            'retrieval_metadata.py is required to build classification/equivalence metadata')
    return retrieval_metadata

from portable_paths import portable_path

HUB = DEFAULT_HUB
SKILLS = 'skills'
PROMPTS = 'prompts'
DB_NAME = 'skill-index.db'

DEFAULT_MATERIALIZE_ROOT = Path(os.environ.get('SKILL_HUB_MATERIALIZE_ROOT')
                                or HUB / 'materialized')
MATERIALIZE_ROOT = DEFAULT_MATERIALIZE_ROOT
FRONTMATTER = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.S)
BUNDLE_DIRECTORIES = ('scripts', 'references')
NORMALIZATION = 'CRLF and CR converted to LF; all other characters preserved; UTF-8'

DOMAIN_HINTS = (
    ('cybersecurity', r'\b(security|vulnerab|malware|exploit|threat|pentest|forensic|attack|cve|sast|encrypt|phish)\b'),
    ('ai-ml', r'\b(llm|model|training|fine-?tun|embedding|prompt|agent|inference|transformer|rag|dataset)\b'),
    ('data', r'\b(sql|database|pipeline|etl|warehouse|analytics|schema|query|bigquery|postgres|pandas)\b'),
    ('devops', r'\b(kubernetes|docker|deploy|ci/cd|terraform|cloud|aws|azure|gcp|infrastructure|monitor|helm)\b'),
    ('software-engineering', r'\b(code|refactor|test|debug|api|module|typescript|python|rust|compile|review|function)\b'),
    ('frontend', r'\b(react|css|html|ui|component|accessib|design system|browser|dom)\b'),
    ('business', r'\b(business|market|sales|crm|finance|accounting|invoice|customer|revenue|kpi|strategy)\b'),
    ('research', r'\b(research|paper|literature|academic|study|scientist|hypothes|experiment|statistic)\b'),
    ('creative', r'\b(writ|design|story|content|brand|visual|video|image|art|copy)\b'),
    ('operations', r'\b(workflow|process|automat|task|manage|plan|project|schedule|onboard)\b'),
)
_DOMAIN_RX = tuple((name, re.compile(rx, re.I)) for name, rx in DOMAIN_HINTS)


def _derive_tags(text: str) -> str:
    """Return a JSON list of domain hints for text that declares no tags of its own."""
    probe = text[:4000].lower()
    return json.dumps([name for name, rx in _DOMAIN_RX if rx.search(probe)] or ['general'])


def db_path(hub=None) -> Path:
    return Path(os.environ['SKILL_HUB_INDEX_PATH']) if os.environ.get('SKILL_HUB_INDEX_PATH') else Path(hub or HUB) / 'state' / DB_NAME


DISABLED_MARKER = 'DISABLED'


def is_disabled(tree: Path | str) -> bool:
    return (Path(tree) / DISABLED_MARKER).exists()


def _frontmatter(text: str) -> Dict[str, Any]:
    """Parse frontmatter with fallback and normalized risk."""
    meta, _, _, _ = catalog_ingest.parse_frontmatter(text)
    return meta


def _body(text: str) -> str:
    match = FRONTMATTER.match(text)
    return text[match.end():] if match else text


def content_digest(text: str) -> str:
    """Identity of a body under the hub normalization: CRLF/CR to LF, all else kept."""
    return catalog_ingest.content_digest(text)


def _bundle_files(tree: Path | str) -> List[tuple[str, Path]]:
    """Use the same complete served-tree identity at ingestion and load time."""
    return catalog_ingest._bundle_files(Path(tree))


def bundle_digest(tree: Path | str) -> str:
    """Digest over every served file, including resources and assets."""
    rows = [{'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for relative, path in _bundle_files(tree)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _rubric(meta: Dict[str, Any], tree: Path | str) -> Optional[Dict[str, Any]]:
    criterion = meta.get('rubric_criterion')
    if not isinstance(criterion, str) or not criterion.strip():
        return None
    rubric = {'criterion': criterion.strip()}
    evidence = meta.get('rubric_evidence')
    if evidence is None or evidence == '':
        rubric['evidence'] = None
        return rubric
    inside = not Path(evidence).is_absolute()
    if inside:
        try:
            inside = (Path(tree) / evidence).resolve().is_relative_to(Path(tree).resolve())
        except OSError:
            inside = False
    if inside:
        rubric['evidence'] = Path(evidence).as_posix()
    else:
        rubric['rejected_evidence'] = str(evidence)
        rubric['evidence'] = None
    return rubric


def connect(hub=None) -> sqlite3.Connection:
    path = db_path(hub)
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def build(hub=None, verbose: bool = False, *, output_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Index hub sources, optionally writing an isolated candidate DB without source mutation."""
    metadata_module = _require_metadata()
    hub = Path(hub or HUB)
    root = hub / SKILLS
    if not root.is_dir():
        raise SystemExit(f'no skill tree at {root}')
    path = Path(output_path).resolve() if output_path is not None else db_path(hub)
    if output_path is not None:
        if path == db_path(hub).resolve():
            raise ValueError('output_path must differ from the source index')
        if path.exists():
            raise FileExistsError(f'candidate output already exists: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.build-' + uuid.uuid4().hex)
    con = sqlite3.connect(tmp)
    con.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE resource_bodies(
            content_sha256 TEXT PRIMARY KEY, body TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE skill_resources(
            name TEXT PRIMARY KEY, description TEXT, tags TEXT, category TEXT,
            body_sha256 TEXT NOT NULL REFERENCES resource_bodies(content_sha256),
            files INTEGER, bytes INTEGER, active INTEGER NOT NULL DEFAULT 1,
            content_sha256 TEXT, canonical_name TEXT, is_alias INTEGER NOT NULL DEFAULT 0,
            risk TEXT DEFAULT 'unknown', bundle_sha256 TEXT,
            category_canonical TEXT, category_list TEXT, category_state TEXT,
            category_reason TEXT, category_provenance TEXT, category_review_status TEXT,
            tags_source TEXT, equivalence_group_id TEXT, family_id TEXT);
        CREATE VIEW skills AS
            SELECT r.rowid AS rowid, r.name, r.description, r.tags, r.category,
                   b.body, r.files, r.bytes, r.active, r.content_sha256,
                   r.canonical_name, r.is_alias, r.risk, r.bundle_sha256, r.body_sha256,
                   r.category_canonical, r.category_list, r.category_state,
                   r.category_reason, r.category_provenance, r.category_review_status,
                   r.tags_source, r.equivalence_group_id, r.family_id
            FROM skill_resources r JOIN resource_bodies b ON b.content_sha256=r.body_sha256;
        CREATE TABLE IF NOT EXISTS aliases(
            alias_name TEXT, canonical_name TEXT NOT NULL,
            kind TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            PRIMARY KEY(alias_name, kind));
        CREATE TABLE IF NOT EXISTS quarantine(
            name TEXT, kind TEXT NOT NULL, reason TEXT NOT NULL,
            details TEXT, recorded_at TEXT NOT NULL, PRIMARY KEY(name, kind));
        CREATE VIRTUAL TABLE fts USING fts5(
            name, description, tags, body, content='skills', content_rowid='rowid',
            tokenize='porter unicode61');
        CREATE TABLE prompt_resources(
            name TEXT PRIMARY KEY, description TEXT, tags TEXT, prompt_type TEXT,
            body_sha256 TEXT NOT NULL REFERENCES resource_bodies(content_sha256),
            path TEXT, files INTEGER, bytes INTEGER,
            content_sha256 TEXT, canonical_name TEXT, is_alias INTEGER NOT NULL DEFAULT 0,
            risk TEXT DEFAULT 'unknown', active INTEGER NOT NULL DEFAULT 1,
            category_canonical TEXT, category_list TEXT, category_state TEXT,
            category_reason TEXT, category_provenance TEXT, category_review_status TEXT,
            tags_source TEXT);
        CREATE VIEW prompts AS
            SELECT r.rowid AS rowid, r.name, r.description, r.tags, r.prompt_type,
                   b.body, r.path, r.files, r.bytes, r.content_sha256,
                   r.canonical_name, r.is_alias, r.risk, r.active, r.body_sha256,
                   r.category_canonical, r.category_list, r.category_state,
                   r.category_reason, r.category_provenance, r.category_review_status,
                   r.tags_source
            FROM prompt_resources r JOIN resource_bodies b ON b.content_sha256=r.body_sha256;
        CREATE VIRTUAL TABLE fts_prompts USING fts5(
            name, description, tags, body, content='prompts', content_rowid='rowid',
            tokenize='porter unicode61');
    ''')

    def store_body(body: str) -> str:
        # Resource identity hashes still cover the original document/bundle;
        # only identical normalized instruction bodies share this storage blob.
        normalized = body.replace('\r\n', '\n').replace('\r', '\n')
        digest = content_digest(normalized)
        con.execute('INSERT OR IGNORE INTO resource_bodies VALUES(?,?)', (digest, normalized))
        return digest

    # Step 1: Pre-validate and collect records
    skill_records: List[catalog_ingest.ValidationRecord] = []
    skipped = 0
    now_iso = dt.datetime.now().isoformat()

    for tree in sorted(p for p in root.iterdir() if p.is_dir()):
        doc = tree / 'SKILL.md'
        if not doc.is_file():
            if verbose:
                print(f'skip (no SKILL.md): {tree.name}', file=sys.stderr)
            skipped += 1
            con.execute('INSERT OR REPLACE INTO quarantine VALUES(?,?,?,?,?)',
                        (tree.name, 'skill', 'missing SKILL.md', str(tree), now_iso))
            continue
        record = catalog_ingest.validate_skill_entry(tree)
        if not record.is_valid:
            con.execute('INSERT OR REPLACE INTO quarantine VALUES(?,?,?,?,?)',
                        (tree.name, 'skill', '; '.join(record.errors), str(tree), now_iso))
        skill_records.append(record)

    # Step 2: Content-addressing & alias resolution for identical bundles (D4)
    # Only valid non-empty skills with frontmatter and intact references are candidates for canonicals
    valid_skills = [r for r in skill_records if r.is_valid]
    alias_to_canon, canon_to_aliases, _ = catalog_ingest.group_canonical_and_aliases(valid_skills)

    # Equivalence/family labels: full-bundle identity may dedupe a RRF slot; a shared
    # body or base name is reported, never merged. Aliases keep their own permissions.
    equivalence_input = [{
        'id': f'skill:{r.name}', 'kind': 'skill', 'name': r.name,
        'content_sha256': r.content_sha256, 'bundle_sha256': r.bundle_sha256, 'risk': r.risk,
    } for r in valid_skills]
    equivalence_rows, _, _ = metadata_module.assign_equivalence(equivalence_input)
    equivalence_by_id = {
        row['id'].split(':', 1)[1]: row for row in equivalence_rows
    }

    rows = 0
    for record in skill_records:
        tree = Path(record.path)
        is_empty = record.is_empty
        text = (tree / 'SKILL.md').read_text(encoding='utf-8', errors='replace') if record.is_valid else ''
        meta = record.metadata
        try:
            files = [path for _, path in _bundle_files(tree)] if record.is_valid else []
        except (ValueError, OSError):
            files = []
        
        is_alias = 1 if record.name in alias_to_canon else 0
        canonical_name = alias_to_canon.get(record.name, record.name)

        # Authored category is preserved in `category`; the resolved value adds nested
        # metadata.category and a categories list without inventing a missing label.
        classification = metadata_module.classify(meta)
        tags = meta.get('tags')
        tags_source = (metadata_module.STATE_AUTHORED
                       if isinstance(tags, str) and tags.strip() else 'DERIVED')
        if not (isinstance(tags, str) and tags.strip()):
            tags = _derive_tags(text)
        equivalence = equivalence_by_id.get(record.name) or {}

        # Alias content equivalence does not transfer external per-resource grants.
        # Keep each valid identity visible; routing can dedup only after authorization.
        is_active = 1 if (record.is_valid and not is_disabled(tree)) else 0

        con.execute(
            '''INSERT OR REPLACE INTO skill_resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                record.name, meta.get('description', ''), tags,
                meta.get('category', '') if isinstance(meta.get('category'), str) else '',
                store_body(_body(text)), len(files),
                sum(f.stat().st_size for f in files), is_active,
                record.content_sha256, canonical_name, is_alias,
                record.risk, record.bundle_sha256,
                classification['category'], json.dumps(classification['categories'], ensure_ascii=False),
                classification['state'], classification['reason'],
                json.dumps(classification['provenance'], ensure_ascii=False, sort_keys=True),
                classification['review_status'], tags_source,
                equivalence.get('equivalence_group_id'), equivalence.get('family_id')
            )
        )
        if is_alias:
            con.execute(
                'INSERT OR REPLACE INTO aliases VALUES(?,?,?,?)',
                (record.name, canonical_name, 'skill', record.content_sha256)
            )
        rows += 1

    # Step 3: Prompts
    prompt_records: List[catalog_ingest.ValidationRecord] = []
    prompt_skipped = 0
    prompts_root = hub / PROMPTS
    if prompts_root.is_dir():
        for entry in sorted(p for p in prompts_root.iterdir() if p.is_file()):
            record = catalog_ingest.validate_prompt_entry(entry)
            if not record.is_valid:
                con.execute('INSERT OR REPLACE INTO quarantine VALUES(?,?,?,?,?)',
                            (entry.stem, 'prompt', '; '.join(record.errors), str(entry), now_iso))
            prompt_records.append(record)

    valid_prompts = [r for r in prompt_records if r.is_valid]
    p_alias_to_canon, _, _ = catalog_ingest.group_canonical_and_aliases(valid_prompts)

    prompt_rows = 0
    for record in prompt_records:
        entry = Path(record.path)
        text = entry.read_text(encoding='utf-8', errors='replace') if record.is_valid else ''
        meta = record.metadata
        body = _body(text) if record.has_frontmatter else text
        is_alias = 1 if record.name in p_alias_to_canon else 0
        canonical_name = p_alias_to_canon.get(record.name, record.name)
        is_active = 1 if record.is_valid else 0
        classification = metadata_module.classify(meta)
        tags = meta.get('tags', '')
        tags_source = (metadata_module.STATE_AUTHORED
                       if isinstance(tags, str) and tags.strip() else 'UNCLASSIFIED')

        con.execute(
            '''INSERT OR REPLACE INTO prompt_resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                record.name, meta.get('description', ''), tags,
                meta.get('prompt_type', ''), store_body(body), entry.name, 1, entry.stat().st_size if record.is_valid else 0,
                record.content_sha256, canonical_name, is_alias, record.risk, is_active,
                classification['category'], json.dumps(classification['categories'], ensure_ascii=False),
                classification['state'], classification['reason'],
                json.dumps(classification['provenance'], ensure_ascii=False, sort_keys=True),
                classification['review_status'], tags_source
            )
        )
        if is_alias:
            con.execute(
                'INSERT OR REPLACE INTO aliases VALUES(?,?,?,?)',
                (record.name, canonical_name, 'prompt', record.content_sha256)
            )
        prompt_rows += 1

    con.execute("INSERT INTO fts(fts) VALUES('rebuild')")
    con.execute("INSERT INTO fts_prompts(fts_prompts) VALUES('rebuild')")
    summary = {
        'active_skills': con.execute('SELECT COUNT(*) FROM skills WHERE active=1').fetchone()[0],
        'active_prompts': con.execute('SELECT COUNT(*) FROM prompts WHERE active=1').fetchone()[0],
        'quarantined': con.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0],
        'canonical_skills': con.execute('SELECT COUNT(*) FROM skills WHERE active=1 AND is_alias=0').fetchone()[0],
        'canonical_prompts': con.execute('SELECT COUNT(*) FROM prompts WHERE active=1 AND is_alias=0').fetchone()[0],
        'body_records': con.execute('SELECT COUNT(*) FROM resource_bodies').fetchone()[0],
        'body_bytes': con.execute('SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM resource_bodies').fetchone()[0],
        'resource_body_bytes': con.execute('''
            SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0)
            FROM (SELECT body FROM skills UNION ALL SELECT body FROM prompts)''').fetchone()[0],
        'authored_category_skills': con.execute(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND category_state='AUTHORED'").fetchone()[0],
        'unknown_category_skills': con.execute(
            "SELECT COUNT(*) FROM skills WHERE active=1 AND category_state='UNKNOWN'").fetchone()[0],
    }
    summary['body_bytes_saved'] = summary['resource_body_bytes'] - summary['body_bytes']
    con.commit()
    con.close()
    tmp.replace(path)
    return {
        'indexed': rows,
        'skipped': skipped,
        'prompts': prompt_rows,
        'prompts_skipped': prompt_skipped,
        'identical_aliases': len(alias_to_canon) + len(p_alias_to_canon),
        **summary,
        'db': str(path),
        'size': path.stat().st_size
    }


def build_projection_from_index(hub=None, *, index_path: Optional[Path | str] = None,
                                output_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Read a built catalog DB (source or candidate) and write the metadata projection.

    Read-only toward the DB: the projection captures active skills and every prompt,
    with authored/derived category state and full/body/family equivalence. Deleting or
    mutating nothing here keeps the live hub untouched.
    """
    metadata_module = _require_metadata()
    path = Path(index_path) if index_path is not None else db_path(hub)
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    resources: List[Dict[str, Any]] = []
    try:
        for kind, table in (('skill', 'skills'), ('prompt', 'prompts')):
            columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
            if not {'category_state', 'category_list'} <= columns:
                raise ValueError(
                    f'{table} has no metadata columns; rebuild the index with this module')
            active_guard = ' WHERE active = 1' if 'active' in columns else ''
            bundle_expr = 'bundle_sha256' if 'bundle_sha256' in columns else "''"
            family_expr = 'family_id' if 'family_id' in columns else 'NULL'
            group_expr = 'equivalence_group_id' if 'equivalence_group_id' in columns else 'NULL'
            # Prompts classify by `prompt_type`, which search_prompts already exposes as
            # `category`; keep the projection consistent with that read surface.
            type_expr = 'prompt_type' if kind == 'prompt' else "''"
            rows = con.execute(f'''
                SELECT name, category_canonical AS category, {bundle_expr} AS bundle_sha256, risk,
                       content_sha256, canonical_name, is_alias, active,
                       category_state, category_reason, category_list,
                       category_provenance, category_review_status, tags_source,
                       {group_expr} AS equivalence_group_id, {family_expr} AS family_id,
                       {type_expr} AS prompt_type
                FROM {table}{active_guard} ORDER BY name''')
            for row in rows:
                record = dict(row)
                try:
                    categories = json.loads(record.get('category_list') or '[]')
                except ValueError:
                    categories = []
                try:
                    provenance = json.loads(record.get('category_provenance') or '{}')
                except ValueError:
                    provenance = {}
                if kind == 'prompt' and record.get('prompt_type'):
                    # search_prompts already exposes prompt_type as `category`.
                    classification = metadata_module.classify(
                        {'category': record['prompt_type']})
                    categories = classification['categories']
                    state = classification['state']
                    reason = classification['reason']
                    provenance = classification['provenance']
                    review = classification['review_status']
                else:
                    state = record.get('category_state') or metadata_module.STATE_UNKNOWN
                    reason = record.get('category_reason')
                    review = record.get('category_review_status')
                name = record['name']
                resources.append({
                    'id': f'{kind}:{name}', 'kind': kind, 'name': name,
                    'canonical_id': f"{kind}:{record.get('canonical_name') or name}",
                    'is_alias': bool(record.get('is_alias')),
                    'active': bool(record.get('active', 1)),
                    'category': record.get('category') or record.get('prompt_type') or '',
                    'categories': categories if isinstance(categories, list) else [],
                    'category_state': state,
                    'category_reason': reason,
                    'category_provenance': provenance,
                    'category_review_status': review,
                    'tags_source': record.get('tags_source'),
                    'content_sha256': record.get('content_sha256') or '',
                    'bundle_sha256': record.get('bundle_sha256') or '',
                    'risk': record.get('risk') or 'unknown',
                })
    finally:
        con.close()
    return metadata_module.build_projection(
        resources, source_hub=str(Path(hub or HUB)), output_path=output_path)


def search(query: str, limit: int = 10, hub=None, *, deadline=None) -> List[Dict[str, Any]]:
    """Rank valid active identities, preserving independent alias permissions."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Catalog search deadline exceeded')
    terms = re.findall(r'\w+', query, flags=re.UNICODE)
    if not terms:
        return []
    match = ' AND '.join('"' + term + '"' for term in terms)
    con = connect(hub)
    try:
        if deadline is not None:
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        columns = {row[1] for row in con.execute('PRAGMA table_info(skills)')}
        identity = ('s.content_sha256, s.canonical_name, s.risk, s.is_alias,'
                    if 'canonical_name' in columns else
                    "NULL AS content_sha256, s.name AS canonical_name, 'unknown' AS risk, 0 AS is_alias,")
        identity += 's.bundle_sha256,' if 'bundle_sha256' in columns else 'NULL AS bundle_sha256,'
        sql = '''SELECT s.name, s.description, s.tags, s.category, s.files, ''' + identity + '''
                        bm25(fts, 8.0, 4.0, 3.0, 1.0) AS rank
                 FROM fts JOIN skills s ON s.rowid = fts.rowid
                 WHERE fts MATCH ? AND s.active = 1 ORDER BY rank LIMIT ?'''
        size = max(1, min(int(limit), 50))
        rows = con.execute(sql, (match, min(size + 10, 60))).fetchall()
        rows = diversify(rows, lambda row: row['name'], query)[:size]
    finally:
        con.close()
    return [{**dict(r), 'kind': 'skill'} for r in rows]


def search_prompts(query: str, limit: int = 10, hub=None, prompt_type: Optional[str] = None, *, deadline=None) -> List[Dict[str, Any]]:
    """Rank prompts by FTS relevance. Same shape as search, with kind: prompt."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Catalog search deadline exceeded')
    terms = re.findall(r'\w+', query, flags=re.UNICODE)
    if not terms:
        return []
    match = ' AND '.join('"' + term + '"' for term in terms)
    con = connect(hub)
    try:
        if deadline is not None:
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        columns = {row[1] for row in con.execute('PRAGMA table_info(prompts)')}
        identity = ('p.content_sha256, p.canonical_name, p.risk, p.is_alias,'
                    if 'canonical_name' in columns else
                    "NULL AS content_sha256, p.name AS canonical_name, 'unknown' AS risk, 0 AS is_alias,")
        sql = '''SELECT p.name, p.description, p.tags, p.prompt_type, p.files, p.path, ''' + identity + '''
                        bm25(fts_prompts, 8.0, 4.0, 3.0, 1.0) AS rank
                 FROM fts_prompts JOIN prompts p ON p.rowid = fts_prompts.rowid
                 WHERE fts_prompts MATCH ?'''
        params: List[Any] = [match]
        if 'active' in columns:
            sql += ' AND p.active = 1'
        if prompt_type:
            sql += ' AND p.prompt_type = ?'
            params.append(prompt_type)
        sql += ' ORDER BY rank LIMIT ?'
        params.append(max(1, min(int(limit), 50)))
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [{**dict(r), 'path': str(portable_path(r['path'])) if r['path'] else r['path'],
             'kind': 'prompt', 'category': r['prompt_type']} for r in rows]


def get_prompt(name: str, hub=None, *, require_indexed=False) -> Dict[str, Any]:
    """Retrieve one prompt. Validates body existence and rejects empty bodies (B6/D5)."""
    hub = Path(hub or HUB)
    if Path(name).name != name or name in ('.', '..'):
        raise KeyError(f'unknown prompt: {name}')
    expected = None
    if require_indexed:
        if not db_path(hub).is_file():
            raise KeyError("prompt catalog unavailable")
        con = connect(hub)
        try:
            columns = {row[1] for row in con.execute('PRAGMA table_info(prompts)')}
            active_filter = ' AND active=1' if 'active' in columns else ''
            pin = 'content_sha256' if 'content_sha256' in columns else 'NULL'
            row = con.execute(f'SELECT {pin} FROM prompts WHERE name=?' + active_filter, (name,)).fetchone()
            if row is None:
                raise KeyError(f'prompt is disabled or not indexed: {name}')
            expected = row[0]
        finally:
            con.close()
    root = hub / PROMPTS
    published = os.environ.get('SKILL_HUB_PUBLISHED_ROOT')
    if published and (Path(published) / PROMPTS / (name + '.md')).is_file():
        if any(p.stem.casefold() == name.casefold() for p in root.iterdir()):
            raise ValueError('published prompt conflicts with native source')
        root = Path(published) / PROMPTS
    if not root.is_dir():
        raise KeyError(f'unknown prompt: {name}')
    direct = root / f'{name}.md'
    if direct.is_file():
        doc = direct
    else:
        doc = next((p for p in sorted(root.iterdir()) if p.stem == name and p.is_file()), None)
    if doc is None:
        raise KeyError(f'unknown prompt: {name}')
    if doc.is_symlink() or not doc.resolve().is_relative_to(root.resolve()):
        raise ValueError('prompt is a link or outside root')
    text = doc.read_text(encoding='utf-8', errors='replace')
    if expected and content_digest(text) != expected:
        raise ValueError(f'CONTENT_CHANGED: prompt differs from requested catalog version: {name}')
    meta, body, has_fm, parse_error = catalog_ingest.parse_frontmatter(text, default_name=doc.stem)
    if parse_error:
        raise ValueError(f'malformed frontmatter rejected: prompt {name} ({parse_error})')
    if not has_fm:
        raise ValueError(f'missing frontmatter rejected: prompt {name}')
    
    # Reject empty body on load (B6/D5)
    if not body.strip():
        raise ValueError(f'empty body rejected: prompt {name}')

    return {
        'name': doc.stem,
        'kind': 'prompt',
        'description': meta.get('description', ''),
        'prompt_type': meta.get('prompt_type', ''),
        'body': body,
        'content_sha256': content_digest(text),
        'body_sha256': content_digest(body),
        'file_sha256': hashlib.sha256(doc.read_bytes()).hexdigest(),
        'path': str(doc),
        'source_root': str(doc.resolve()),
        'hub_root': str(hub.resolve()),
        'metadata': meta,
        'limits': [
            'A prompt is instruction text, not a skill bundle: it carries no SKILL.md and no bundle digest.',
            'content_sha256 covers the stored file; body_sha256 covers the text served after the metadata block.'
        ]
    }


def _make_writable(path: Path) -> None:
    """Restore owner write/execute bits so staging or backup trees can be removed."""
    if not path.exists():
        return
    if path.is_dir():
        for current, _dirs, files in os.walk(path):
            os.chmod(current, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            for fname in files:
                os.chmod(Path(current) / fname, stat.S_IWRITE | stat.S_IREAD)
    else:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def _resolve_active_skill(name: str, hub: Path, *, require_indexed=False) -> Tuple[str, bool, Optional[str], Optional[str]]:
    """Resolve an active skill name or alias to its canonical active name and expected pins."""
    if Path(name).name != name or name in ('.', '..'):
        raise KeyError(f'unknown skill: {name}')
    direct_root = hub / SKILLS / name
    if direct_root.is_dir() and is_disabled(direct_root):
        raise KeyError(f'skill is disabled: {name}')
    if not db_path(hub).is_file():
        if require_indexed:
            raise KeyError("skill catalog unavailable")
        return name, False, None, None
    con = connect(hub)
    try:
        row = con.execute(
            'SELECT active, canonical_name, is_alias, content_sha256, bundle_sha256 FROM skills WHERE name = ?',
            (name,),
        ).fetchone()
        if row is not None and not row['active']:
            raise KeyError(f'skill is disabled: {name}')
        alias_row = con.execute(
            "SELECT canonical_name, content_sha256 FROM aliases WHERE alias_name = ? AND kind = 'skill'",
            (name,),
        ).fetchone()
        target = None
        if alias_row:
            target = alias_row['canonical_name']
        elif row is not None and row['is_alias'] and row['canonical_name'] and row['canonical_name'] != name:
            target = row['canonical_name']
        if target:
            t_row = con.execute(
                'SELECT active, content_sha256, bundle_sha256 FROM skills WHERE name = ?',
                (target,),
            ).fetchone()
            if t_row is None:
                raise KeyError(f'unknown alias target: {target}')
            if not t_row['active']:
                raise KeyError(f'skill is disabled: {target}')
            target_root = hub / SKILLS / target
            if target_root.is_dir() and is_disabled(target_root):
                raise KeyError(f'skill is disabled: {target}')
            content_pins = [
                p for p in (
                    alias_row['content_sha256'] if alias_row else None,
                    row['content_sha256'] if row is not None else None,
                    t_row['content_sha256'],
                )
                if p
            ]
            if len(set(content_pins)) > 1:
                raise ValueError(f'CONTENT_CHANGED: alias content pin mismatch for {name} -> {target}')
            req_bundle = row['bundle_sha256'] if row is not None else None
            tgt_bundle = t_row['bundle_sha256']
            if req_bundle and tgt_bundle and req_bundle != tgt_bundle:
                raise ValueError(f'CONTENT_CHANGED: alias bundle pin mismatch for {name} -> {target}')
            expected_csha = content_pins[0] if content_pins else None
            expected_bsha = tgt_bundle or req_bundle
            return target, target != name, expected_csha, expected_bsha
        if row is None and require_indexed:
            raise KeyError(f'skill is not indexed: {name}')
    finally:
        con.close()
    return name, False, None, None


def _resolve_active_skill_name(name: str, hub: Path) -> Tuple[str, bool]:
    """Resolve an active skill name or alias to its canonical active name, never bypassing disabled."""
    resolved, is_alias, _csha, _bsha = _resolve_active_skill(name, hub)
    return resolved, is_alias


def support_bundle_digest(skill_root: Path) -> str:
    """Hash all supporting files in a skill directory excluding SKILL.md."""
    h = hashlib.sha256()
    for p in sorted(skill_root.rglob('*')):
        if p.is_file() and p.name != 'SKILL.md':
            rel = p.relative_to(skill_root).as_posix()
            h.update(rel.encode('utf-8') + b'\0' + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def apply_exact_content_dedup(hub=None) -> Dict[str, Any]:
    """Group active skills by exact full (content_sha256, support_bundle_digest, risk, permissions) while preserving custom metadata."""
    hub_path = Path(hub or HUB)
    db_file = hub_path / 'state' / 'skill-index.db'
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    try:
        disabled_before = {r[0] for r in con.execute('SELECT name FROM skills WHERE active = 0').fetchall()}
        cols = {r[1] for r in con.execute('PRAGMA table_info(skills)').fetchall()}
        perm_expr = "COALESCE(permissions, '')" if 'permissions' in cols else "''"
        rows = con.execute(
            f'SELECT name, content_sha256, risk, {perm_expr} AS permissions, canonical_name, is_alias, equivalence_group_id '
            'FROM skills WHERE active = 1 ORDER BY name'
        ).fetchall()
        existing_aliases = {
            r[0] for r in con.execute("SELECT alias_name FROM aliases WHERE kind = 'skill'").fetchall()
        }
        groups: Dict[Tuple[str, str, str, str], List[sqlite3.Row]] = {}
        for r in rows:
            csha = r['content_sha256'] or ''
            sroot = hub_path / SKILLS / r['name']
            if not csha or not sroot.is_dir() or is_disabled(sroot):
                continue
            key = (csha, support_bundle_digest(sroot), r['risk'] or 'unknown', r['permissions'] or '')
            groups.setdefault(key, []).append(r)

        has_sr = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_resources'"
        ).fetchone() is not None
        target_table = 'skill_resources' if has_sr else 'skills'
        dup_groups = []
        alias_count = 0
        for (csha, ssha, risk, perms), group_rows in sorted(groups.items()):
            eligible = [
                r['name'] for r in group_rows
                if not r['is_alias']
                and not r['equivalence_group_id']
                and (not r['canonical_name'] or r['canonical_name'] == r['name'])
                and r['name'] not in existing_aliases
            ]
            if len(eligible) <= 1:
                continue
            canonical = catalog_ingest.select_canonical_name(eligible)
            eq_id = 'dup:' + hashlib.sha256(f'{csha}:{ssha}:{risk}:{perms}'.encode('utf-8')).hexdigest()[:16]
            aliases = [n for n in eligible if n != canonical]
            for n in eligible:
                is_al = 1 if n != canonical else 0
                con.execute(
                    f'UPDATE {target_table} SET canonical_name = ?, is_alias = ?, equivalence_group_id = ? WHERE name = ? AND active = 1',
                    (canonical, is_al, eq_id, n),
                )
                if is_al:
                    con.execute(
                        'INSERT OR IGNORE INTO aliases(alias_name, canonical_name, kind, content_sha256) VALUES (?, ?, ?, ?)',
                        (n, canonical, 'skill', csha),
                    )
                    alias_count += 1
            dup_groups.append({
                'equivalence_group_id': eq_id,
                'canonical': canonical,
                'aliases': aliases,
                'content_sha256': csha,
                'support_sha256': ssha,
                'risk': risk,
                'permissions': perms,
            })

        disabled_after = {r[0] for r in con.execute('SELECT name FROM skills WHERE active = 0').fetchall()}
        if disabled_before != disabled_after:
            con.rollback()
            raise RuntimeError('disabled skill identity set mutated during dedup')
        if dup_groups:
            con.commit()
        return {
            'duplicate_groups': len(dup_groups),
            'aliases_registered': alias_count,
            'disabled_preserved': len(disabled_after),
            'groups': dup_groups,
        }
    finally:
        con.close()


def get(name: str, hub=None, materialize: bool = True, materialize_root: Optional[Path | str] = None, *, require_indexed=False) -> Dict[str, Any]:
    """Return one skill's body plus identity and metadata. Rejects empty body (B6/D5)."""
    hub = Path(hub or HUB)
    resolved_name, is_alias, expected_csha, expected_bsha = _resolve_active_skill(name, hub, require_indexed=require_indexed)
    root = hub / SKILLS / resolved_name
    published = os.environ.get('SKILL_HUB_PUBLISHED_ROOT')
    if published and (Path(published) / SKILLS / resolved_name).is_dir():
        if any(p.name.casefold() == resolved_name.casefold() for p in (hub / SKILLS).iterdir()):
            raise ValueError('published skill conflicts with native source')
        root = Path(published) / SKILLS / resolved_name
    if Path(resolved_name).name != resolved_name or resolved_name in ('.', '..') or not root.is_dir():
        raise KeyError(f'unknown skill: {name}')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('skill root is a link')
    if is_disabled(root):
        raise KeyError(f'skill is disabled: {name}')
    doc = root / 'SKILL.md'
    if not doc.is_file():
        raise KeyError(f'unknown skill: {name} (missing SKILL.md)')
    if doc.is_symlink():
        raise ValueError('skill document is a link')
    body_raw = doc.read_text(encoding='utf-8', errors='replace')
    actual_csha = content_digest(body_raw)
    actual_bsha = bundle_digest(root)
    if is_alias:
        if expected_csha and actual_csha != expected_csha:
            raise ValueError(f'CONTENT_CHANGED: alias content mismatch for {name} -> {resolved_name}')
        if expected_bsha and actual_bsha != expected_bsha:
            raise ValueError(f'CONTENT_CHANGED: alias bundle mismatch for {name} -> {resolved_name}')
    meta, body, has_fm, parse_error = catalog_ingest.parse_frontmatter(body_raw, default_name=name)
    if parse_error:
        raise ValueError(f'malformed frontmatter rejected: skill {name} ({parse_error})')
    if not has_fm:
        raise ValueError(f'missing frontmatter rejected: skill {name}')

    # Reject empty body on load (B6/D5)
    if not body.strip():
        raise ValueError(f'empty body rejected: skill {name}')

    _existing_refs, missing_refs, escaping_refs = catalog_ingest.check_bundle_references(root, body)
    if escaping_refs:
        raise ValueError(f'bundle references escape root: skill {name} ({escaping_refs})')
    if missing_refs:
        raise ValueError(f'bundle references missing: skill {name} ({missing_refs})')

    result = {
        'name': resolved_name,
        'description': meta.get('description', ''),
        'body': body_raw,
        'path': str(root),
        'files': [],
        'hub_root': str(hub.resolve()),
        'source_root': str(root.resolve()),
        'content_sha256': content_digest(body_raw),
        'bundle_sha256': bundle_digest(root),
        'metadata': meta
    }
    if is_alias:
        result['canonical_name'] = resolved_name
        result['is_alias'] = True
        result['requested_name'] = name
    rubric = _rubric(meta, root)
    if rubric is not None:
        result['rubric'] = rubric
    if materialize:
        destination = Path(materialize_root or MATERIALIZE_ROOT)
        target = destination / resolved_name
        resolved_root = root.resolve()
        resolved_target = target.resolve()
        if (
            resolved_target == resolved_root
            or resolved_target.is_relative_to(resolved_root)
            or resolved_root.is_relative_to(resolved_target)
        ):
            raise ValueError(
                f'materialize target overlaps skill source: {resolved_target} vs {resolved_root}'
            )
        resolved_hub = hub.resolve()
        sanctioned_mat = resolved_hub / 'materialized'
        if (
            resolved_hub.is_relative_to(resolved_target)
            or (
                resolved_target.is_relative_to(resolved_hub)
                and not (
                    resolved_target.is_relative_to(sanctioned_mat)
                    and resolved_target != sanctioned_mat
                )
            )
        ):
            raise ValueError(
                f'materialize target overlaps protected hub path: {resolved_target}'
            )
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
            raise ValueError('materialize target is a link')
        destination.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.materializing-', dir=destination))
        staged_target = staging / 'staged'
        backup = staging / 'backup'
        has_backup = False
        published = False
        rolled_back = False
        try:
            shutil.copytree(root, staged_target)
            if target.exists():
                target.rename(backup)
                has_backup = True
            try:
                staged_target.rename(target)
                published = True
            except OSError as pub_err:
                if has_backup:
                    try:
                        backup.rename(target)
                        rolled_back = True
                    except OSError as rollback_err:
                        raise OSError(
                            f'materialize publish failed ({pub_err}); rollback failed ({rollback_err}); '
                            f'prior target preserved at {backup}'
                        ) from rollback_err
                raise
        finally:
            unresolved_backup = has_backup and not published and not rolled_back
            if staging.exists() and not unresolved_backup:
                try:
                    _make_writable(staging)
                except OSError:
                    pass
                shutil.rmtree(staging, ignore_errors=True)
        result['path'] = str(target)
        result['materialize_root'] = str(destination.resolve())
    result['files'] = sorted(
        p.relative_to(result['path']).as_posix()
        for p in Path(result['path']).rglob('*') if p.is_file()
    )
    return result


def get_canonical(name: str, hub=None, kind: Optional[str] = None) -> Dict[str, Any]:
    """Describe alias metadata, never authorization or automatic load substitution."""
    con = connect(hub)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'aliases' not in tables:
            return {'name': name, 'canonical_name': name, 'is_alias': False,
                    'kind': kind or 'unknown', 'content_sha256': None}
        aliases = con.execute(
            'SELECT canonical_name, kind, content_sha256 FROM aliases WHERE alias_name = ?', (name,)
        ).fetchall()
        if kind is not None:
            aliases = [row for row in aliases if row['kind'] == kind]
        if len(aliases) > 1:
            raise ValueError('ambiguous resource name; specify kind')
        alias_row = aliases[0] if aliases else None
        if alias_row:
            return {
                'name': name,
                'canonical_name': alias_row['canonical_name'],
                'is_alias': True,
                'kind': alias_row['kind'],
                'content_sha256': alias_row['content_sha256']
            }
        
        skill_row = con.execute(
            'SELECT name, canonical_name, is_alias, content_sha256 FROM skills WHERE name = ?', (name,)
        ).fetchone()
        if skill_row and kind in (None, 'skill'):
            return {
                'name': name,
                'canonical_name': skill_row['canonical_name'] or skill_row['name'],
                'is_alias': bool(skill_row['is_alias']),
                'kind': 'skill',
                'content_sha256': skill_row['content_sha256']
            }
        
        prompt_row = con.execute(
            'SELECT name, canonical_name, is_alias, content_sha256 FROM prompts WHERE name = ?', (name,)
        ).fetchone()
        if prompt_row and kind in (None, 'prompt'):
            return {
                'name': name,
                'canonical_name': prompt_row['canonical_name'] or prompt_row['name'],
                'is_alias': bool(prompt_row['is_alias']),
                'kind': 'prompt',
                'content_sha256': prompt_row['content_sha256']
            }
    finally:
        con.close()
    return {'name': name, 'canonical_name': name, 'is_alias': False, 'kind': 'unknown', 'content_sha256': None}


def list_aliases(hub=None) -> List[Dict[str, Any]]:
    """List all registered alias mappings."""
    con = connect(hub)
    try:
        rows = con.execute(
            'SELECT alias_name, canonical_name, kind, content_sha256 FROM aliases ORDER BY alias_name'
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def quarantine_report(hub=None) -> Dict[str, Any]:
    """Summary of quarantined and invalid resources."""
    hub = Path(hub or HUB)
    con = connect(hub)
    try:
        rows = con.execute('SELECT name, kind, reason, details, recorded_at FROM quarantine ORDER BY name').fetchall()
        quarantined = [dict(r) for r in rows]
    finally:
        con.close()
    return {
        'quarantine_total': len(quarantined),
        'records': quarantined,
    }


def compact(hub=None, dry_run: bool = True, retain_backups: int = 2, backup_dir: Optional[Path | str] = None, allow_live: bool = False, prune_created_backups: bool = False) -> Dict[str, Any]:
    """Execute index compaction via catalog_maintenance (D9)."""
    target_db = db_path(hub)
    return catalog_maintenance.compact_index(
        target_db,
        dry_run=dry_run,
        retain_backups=retain_backups,
        backup_dir=backup_dir,
        allow_live=allow_live,
        prune_created_backups=prune_created_backups,
    )


def stats(hub=None) -> Dict[str, Any]:
    """Counts by kind. Includes canonical vs alias counts."""
    con = connect(hub)
    try:
        row = con.execute('SELECT COUNT(*) n, SUM(files) files, SUM(bytes) bytes FROM skills').fetchone()
        result = dict(row) if row else {'n': 0, 'files': 0, 'bytes': 0}
        try:
            disabled = con.execute('SELECT COUNT(*) n FROM skills WHERE active = 0').fetchone()
            result['disabled'] = disabled['n'] if disabled else 0
            result['active'] = result['n'] - result['disabled']
        except sqlite3.Error:
            result['disabled'] = None
            result['active'] = result['n']

        try:
            aliases_count = con.execute('SELECT COUNT(*) n FROM aliases').fetchone()
            result['aliases'] = aliases_count['n'] if aliases_count else 0
        except sqlite3.Error:
            result['aliases'] = 0

        try:
            prompts = con.execute('SELECT COUNT(*) n, COALESCE(SUM(bytes), 0) bytes FROM prompts').fetchone()
            result['prompts'] = prompts['n'] if prompts else 0
            result['prompt_bytes'] = prompts['bytes'] if prompts else 0
        except sqlite3.Error:
            result['prompts'] = None
            result['prompt_bytes'] = None
    finally:
        con.close()
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub', type=Path, default=HUB)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build')
    p = sub.add_parser('search')
    p.add_argument('query')
    p.add_argument('--limit', type=int, default=10)
    g = sub.add_parser('get')
    g.add_argument('name')
    g.add_argument('--no-materialize', action='store_true')
    sub.add_parser('stats')
    sub.add_parser('aliases')
    c = sub.add_parser('compact')
    c.add_argument('--no-dry-run', action='store_true', default=False)
    c.add_argument('--retain-backups', type=int, default=2)
    c.add_argument('--allow-live', action='store_true', default=False)
    c.add_argument('--prune-created-backups', action='store_true', default=False)
    args = parser.parse_args(argv)

    if args.cmd == 'build':
        print(json.dumps(build(args.hub, verbose=True)))
    elif args.cmd == 'search':
        print(json.dumps(search(args.query, args.limit, args.hub), indent=1))
    elif args.cmd == 'get':
        print(json.dumps(get(args.name, args.hub, not args.no_materialize), indent=1))
    elif args.cmd == 'stats':
        print(json.dumps(stats(args.hub)))
    elif args.cmd == 'aliases':
        print(json.dumps(list_aliases(args.hub), indent=2))
    elif args.cmd == 'compact':
        print(json.dumps(compact(args.hub, dry_run=not args.no_dry_run, retain_backups=args.retain_backups, allow_live=args.allow_live, prune_created_backups=args.prune_created_backups), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
