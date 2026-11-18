"""Versioned, source-grounded projection for the TEMPR Graph and Temporal engines.

The live catalog index carries identity, description, tags, category and hashes, but no
relations and no timestamps. Those two gaps are exactly what a Graph and a Temporal branch
would otherwise be tempted to invent. This module builds both projections from what the
source actually declares:

    Graph     edges come from authored frontmatter (`category`, `domain`, `subdomain`,
              `tags`, `tools`, `allowed-tools`, `depends-on`, `requires`, `prerequisites`)
              and from the index's own alias table. No edge exists because two resources
              share a word, a description or a body.
    Temporal  only dates that the source authors, mapped to explicit fields with an explicit
              `time_source`. `date_added` is the date the record was added to the catalog, so
              it is reported as such and never as content freshness. Snapshot time and
              filesystem mtime stay labelled as observed-at facts.

Every node is re-checked against the index: the frontmatter file's content digest must equal
the digest the index holds for that name. A file that drifted contributes no authored claim,
and the drift is counted as a gap instead of being projected as true.

Nothing here writes to the Hub. `build()` reads the index read-only and the skill tree, and
returns plain dictionaries the caller serialises.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

SCHEMA_VERSION = 'tempr.projection.v1'

GRAPH_EDGE_TYPES = ('provides', 'specializes', 'uses_tool', 'requires_tool', 'depends_on',
                    'alias_of')
# Edge types whose far side is a declared capability/tool/dependency rather than a resource.
CONCEPT_PREFIXES = ('cap:', 'tool:', 'dom:', 'dep:', 'ext:')

SKILL_TREE_DIRS = ('skills', 'materialized')

FRONTMATTER = re.compile(r'\A---\s*\n(.*?)\n---\s*\n', re.S)
DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?'
                     r'(?:Z|[+-]\d{2}:?\d{2})?)?$')
SLUG_RX = re.compile(r'[^a-z0-9]+')

STOPWORDS = frozenset("""
a an the and or of for to in on with without into from by at as is are be been being this that
these those it its it's i you we they he she my our your their me us them do does did done can
could should would will shall may might must not no nor if then than so such very more most
please help need want use using used make made get got give given how what when where which who
why about over under between within across per via also just only some any all each every
was were has have had having am being been
các và của cho với trong trên dưới từ đến theo về một những này đó kia khi nếu thì là có không
tôi bạn anh chị em chúng ta họ nó giúp cần muốn dùng làm cách nào gì sao đâu đây rồi đã đang sẽ
""".split())

TOKEN_RX = re.compile(r'[0-9a-z\u00c0-\u1ef9]+')


def tokens(text):
    """Lowercased word tokens, Vietnamese diacritics preserved. No stemming by design."""
    if not text:
        return set()
    folded = unicodedata.normalize('NFKC', str(text)).lower()
    return set(TOKEN_RX.findall(folded))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_digest(text):
    """The hub's body identity: CRLF/CR to LF, everything else preserved."""
    return hashlib.sha256(str(text).replace('\r\n', '\n').replace('\r', '\n')
                          .encode('utf-8')).hexdigest()


def slug(text):
    return SLUG_RX.sub('-', unicodedata.normalize('NFKC', str(text)).lower()).strip('-')


def qualified_id(kind, name):
    return f'{kind}:{name}'


# ---------------------------------------------------------------- time parsing

def as_date(value):
    """A calendar date the source actually declared, or None.

    Accepted: a date object (YAML resolves unquoted dates) or a strict ISO-shaped string.
    Anything else -- 'last week', a number, a free-text log line -- is not a date and is
    rejected rather than coerced, because a wrong date is worse than a missing one.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip().strip('\'"')
    if not text:
        return None
    if DATE_RX.match(text):
        return text.replace(' ', 'T') if (' ' in text) else text
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def epoch_day(iso_date):
    """Sortable integer for a date-shaped string, or None."""
    if not iso_date:
        return None
    try:
        return dt.date.fromisoformat(str(iso_date)[:10]).toordinal()
    except ValueError:
        return None


# ---------------------------------------------------------------- source reading

def _frontmatter(text):
    match = FRONTMATTER.match(text)
    if not match:
        return {}, False
    try:
        import yaml
        fields = yaml.safe_load(match.group(1))
    except Exception:  # malformed YAML is a source defect, not a build failure
        return {}, True
    return (fields if isinstance(fields, dict) else {}), True


def _as_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.extend(str(k) for k in item)
            else:
                out.append(str(item))
        return [v.strip() for v in out if v.strip()]
    if isinstance(value, dict):
        return [str(k).strip() for k in value if str(k).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1]
    parts = re.split(r'[,\n;]', text)
    return [p.strip().strip('\'"') for p in parts if p.strip().strip('\'"')]


def skill_tree_paths(hub):
    """name -> SKILL.md path, from the two trees the indexer serves."""
    hub = Path(hub)
    paths = {}
    for directory in SKILL_TREE_DIRS:
        root = hub / directory
        if not root.is_dir():
            continue
        for child in root.iterdir():
            candidate = child / 'SKILL.md'
            if candidate.is_file():
                # `skills/` is the served tree and wins over a materialized copy.
                paths.setdefault(child.name, candidate)
    return paths


def prompt_tree_paths(hub):
    hub = Path(hub)
    root = hub / 'prompts'
    if not root.is_dir():
        return {}
    return {p.stem: p for p in root.iterdir() if p.is_file()}


def read_index(index_path):
    """Read-only catalog rows plus the alias table, in one connection."""
    path = Path(index_path)
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = {'skill': [], 'prompt': []}
        for kind, table in (('skill', 'skill_resources'), ('prompt', 'prompt_resources')):
            for row in con.execute(f'SELECT * FROM {table}'):
                rows[kind].append(dict(row))
        aliases = [dict(r) for r in con.execute('SELECT * FROM aliases')]
        prompts_extra = {r['name']: r.get('path') for r in rows['prompt']}
    finally:
        con.close()
    return rows, aliases, prompts_extra


# ---------------------------------------------------------------- projection build

def build(hub, index_path, *, ontology=None, now=None, kinds=('skill', 'prompt'),
          max_scan=None):
    """Build graph and temporal projections from an index and the tree it indexes.

    Returns ``(document, report)``. ``document`` holds both projections and the shared
    manifest it pins; ``report`` holds coverage counts and named gaps. Neither contains a
    fabricated edge or an invented date: a field that the source does not declare is absent
    or null, and its absence is counted.
    """
    hub = Path(hub)
    source_root = hub.resolve()
    index_path = Path(index_path)
    built_at = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0).isoformat()
    index_bytes = index_path.read_bytes()
    catalog_sha256 = hashlib.sha256(index_bytes).hexdigest()

    rows, aliases, prompt_paths_index = read_index(index_path)
    skills_tree = skill_tree_paths(hub)
    prompts_tree = prompt_tree_paths(hub)

    gaps = []
    nodes = {}
    edges = []
    edge_keys = set()

    def add_edge(src, dst, relation, evidence, hop_class='authored'):
        key = (src, dst, relation)
        if key in edge_keys or src == dst:
            return
        edge_keys.add(key)
        edges.append({'s': src, 'd': dst, 'r': relation, 'src': evidence,
                      'class': hop_class})

    stats = {
        'rows_seen': 0, 'rows_projected': 0, 'rows_unreadable': 0, 'hash_mismatch': 0,
        'missing_tree': 0, 'with_category': 0, 'with_tags': 0, 'with_domain': 0,
        'with_tools': 0, 'with_allowed_tools': 0, 'with_depends_on': 0,
        'with_requires': 0, 'with_version': 0, 'with_date_added': 0,
        'with_source_repo': 0, 'aliases': 0, 'dependency_targets_missing': 0,
    }

    def declare(name, field, value):
        if value in (None, '', [], {}):
            return
        stats[field] = stats.get(field, 0) + 1

    scanned = 0
    for kind in ('skill', 'prompt'):
        if kind not in kinds:
            continue
        tree = skills_tree if kind == 'skill' else prompts_tree
        for row in rows[kind]:
            name = row['name']
            if max_scan is not None and scanned >= max_scan:
                break
            scanned += 1
            stats['rows_seen'] += 1
            rid = qualified_id(kind, name)
            path = tree.get(name)
            if path is None:
                stats['missing_tree'] += 1
                gaps.append({'kind': 'missing_source', 'id': rid,
                             'detail': 'no source file for an indexed row'})
                meta, has_frontmatter, body_sha = {}, False, None
            else:
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(source_root):
                        raise OSError('source outside catalog root or linked')
                    text = path.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    stats['rows_unreadable'] += 1
                    gaps.append({'kind': 'unreadable_source', 'id': rid})
                    meta, has_frontmatter, body_sha = {}, False, None
                else:
                    body_sha = text_digest(text)
                    expected = row.get('content_sha256')
                    if expected and body_sha != expected:
                        # The tree moved under the index; authored claims are unusable.
                        stats['hash_mismatch'] += 1
                        gaps.append({'kind': 'source_index_mismatch', 'id': rid,
                                     'detail': 'frontmatter file digest differs from index'})
                        meta, has_frontmatter = {}, False
                    else:
                        meta, has_frontmatter = _frontmatter(text)

            if not has_frontmatter:
                meta = {}

            declared = sorted(k for k in meta if k in AUTHORED_KEYS)
            category = meta.get('category')
            if not isinstance(category, str):
                category = row.get('category') if isinstance(row.get('category'), str) else None
            domain = meta.get('domain')
            subdomain = meta.get('subdomain')
            tags_authored = _as_list(meta.get('tags')) if has_frontmatter else []
            tools = _as_list(meta.get('tools'))
            allowed = _as_list(meta.get('allowed-tools')) or _as_list(meta.get('allowed_tools'))
            depends = _as_list(meta.get('depends-on')) or _as_list(meta.get('depends_on'))
            requires = _as_list(meta.get('requires')) or _as_list(meta.get('prerequisites'))
            version = meta.get('version')
            added = as_date(meta.get('date_added'))
            # The two other clocks, each from its own authored field. A `version` string is not a
            # date and is never coerced into one; only an explicit recorded/updated timestamp
            # answers a version or freshness question.
            versioned_at = (as_date(meta.get('version_recorded_at'))
                            or as_date(meta.get('versioned_at')))
            verified_at = (as_date(meta.get('last_updated'))
                           or as_date(meta.get('updated_at'))
                           or as_date(meta.get('verified_at')))
            repo = meta.get('source_repo')
            upstream = meta.get('upstream_commit')
            valid_from = as_date(meta.get('valid_from'))
            valid_to = as_date(meta.get('valid_to'))

            declare(rid, 'with_category', category)
            declare(rid, 'with_tags', tags_authored)
            declare(rid, 'with_domain', domain)
            declare(rid, 'with_tools', tools)
            declare(rid, 'with_allowed_tools', allowed)
            declare(rid, 'with_depends_on', depends)
            declare(rid, 'with_requires', requires)
            declare(rid, 'with_version', version)
            declare(rid, 'with_date_added', added)
            declare(rid, 'with_source_repo', repo)

            caps, cap_sources = [], []
            for value, source in ((category, 'category'), (domain, 'domain'),
                                  (subdomain, 'subdomain')):
                if isinstance(value, str) and value.strip():
                    caps.append(slug(value))
                    cap_sources.append(source)
            for value in _as_list(meta.get('categories')):
                caps.append(slug(value))
                cap_sources.append('categories')
            for value in tags_authored:
                caps.append(slug(value))
                cap_sources.append('tags')

            node = {
                'id': rid, 't': kind, 'n': name,
                'g': row.get('equivalence_group_id') or None,
                'f': row.get('family_id') or None,
                'act': 1 if row.get('active', 1) else 0,
                'al': 1 if row.get('is_alias') else 0,
                'can': row.get('canonical_name') or name,
                'ch': row.get('content_sha256'),
                'bt': row.get('bundle_sha256'),
                'risk': row.get('risk'),
                'ver': str(version) if version not in (None, '') else None,
                'src': meta.get('source') if isinstance(meta.get('source'), str) else None,
                'repo': str(repo) if repo else None,
                'up': str(upstream) if upstream else None,
                'caps': sorted(set(c for c in caps if c)),
                'csrc': sorted(set(cap_sources)),
                'tools': sorted({slug(t) for t in tools if slug(t)}),
                'req': sorted({slug(t) for t in allowed if slug(t)}),
                'dep': sorted({d for d in depends if d}),
                'pre': sorted({d for d in requires if d}),
                'decl': declared,
                # Temporal: only what the source says, each with where it came from.
                'obs': built_at,
                'added': added,
                'ver_at': versioned_at,
                'verified_at': verified_at,
                'vf': valid_from,
                'vt': valid_to,
                'tsrc': ('frontmatter:date_added' if added
                         else ('frontmatter:validity' if (valid_from or valid_to) else None)),
            }
            nodes[rid] = node
            stats['rows_projected'] += 1

            for cap, source in zip(caps, cap_sources):
                if not cap:
                    continue
                relation = 'specializes' if source in ('category', 'domain', 'subdomain') \
                    else 'provides'
                add_edge(rid, f'cap:{cap}', relation, f'authored:{source}')
            if isinstance(domain, str) and domain.strip():
                add_edge(rid, f'dom:{slug(domain)}', 'specializes', 'authored:domain')
            for tool in tools:
                add_edge(rid, f'tool:{slug(tool)}', 'uses_tool', 'authored:tools')
            for tool in allowed:
                add_edge(rid, f'tool:{slug(tool)}', 'requires_tool', 'authored:allowed-tools')
            for dep in depends:
                add_edge(rid, f'dep:{slug(dep)}', 'depends_on', 'authored:depends-on')
            for dep in requires:
                add_edge(rid, f'ext:{slug(dep)}', 'depends_on', 'authored:requires')

    # Alias edges come from the index's own alias table, not from name similarity.
    for record in aliases:
        kind = record.get('kind')
        if kind not in kinds:
            continue
        alias = qualified_id(kind, record['alias_name'])
        target = qualified_id(kind, record['canonical_name'])
        if alias in nodes and target in nodes:
            add_edge(alias, target, 'alias_of', 'index:aliases')
            stats['aliases'] += 1

    # Resolve authored dependency names onto real resources when the name exists.
    resolved, unresolved = [], []
    for edge in edges:
        if edge['r'] != 'depends_on' or not edge['d'].startswith('dep:'):
            continue
        name = edge['d'][4:]
        for kind in ('skill', 'prompt'):
            candidate = qualified_id(kind, name)
            if candidate in nodes:
                edge['d'] = candidate
                edge['r'] = 'depends_on'
                resolved.append(candidate)
                break
        else:
            unresolved.append(name)
            stats['dependency_targets_missing'] += 1
    # `depends-on: mason, aria` names a squad sibling, not a resolvable id.
    for name in sorted(set(unresolved)):
        gaps.append({'kind': 'unresolved_dependency', 'target': name,
                     'detail': 'authored dependency names no indexed resource'})

    if not any(n['added'] for n in nodes.values()):
        gaps.append({'kind': 'no_source_timestamps',
                     'detail': 'no projected row declares a source-authored date'})
    if not edges:
        gaps.append({'kind': 'no_edges', 'detail': 'source declares no relations'})

    # Concept postings: the fanout and rarity signal a traversal needs.
    postings = {}
    for edge in edges:
        if edge['d'].startswith(CONCEPT_PREFIXES):
            postings.setdefault(edge['d'], []).append(edge['s'])
    concepts = {}
    for concept, sources in postings.items():
        concepts[concept] = {
            'n': len(sources),
            'label': concept.split(':', 1)[1].replace('-', ' '),
        }

    taxonomy = sorted({c for c in concepts if c.startswith(('cap:', 'dom:'))})
    temporal_sources = sorted({n['tsrc'] for n in nodes.values() if n['tsrc']})

    ontology_block = ontology or {}
    revision = {
        'catalog_sha256': catalog_sha256,
        'taxonomy_revision': digest({'concepts': taxonomy,
                                     'edge_types': list(GRAPH_EDGE_TYPES)}),
        'ontology_revision': digest(ontology_block),
        'temporal_revision': digest({'time_sources': temporal_sources,
                                     'fields': ['observed_at', 'source_added_at',
                                                'version_recorded_at', 'verified_at',
                                                'last_updated', 'valid_from', 'valid_to']}),
        'config_revision': digest({'schema': SCHEMA_VERSION, 'hub': str(hub)}),
    }

    document = {
        'schema_version': SCHEMA_VERSION,
        'built_at': built_at,
        'manifest': revision,
        'source_index': {'path': str(index_path), 'sha256': catalog_sha256,
                         'bytes': len(index_bytes)},
        'hub': str(hub),
        'ontology': ontology_block,
        'graph': {'nodes': nodes, 'edges': edges, 'concepts': concepts},
        'temporal': {
            'nodes': {rid: {'added': n['added'], 'ver': n['ver'], 'ver_at': n['ver_at'],
                            'verified_at': n['verified_at'], 'obs': n['obs'],
                            'tsrc': n['tsrc'], 'ch': n['ch'], 'vf': n['vf'],
                            'vt': n['vt'], 'n': n['n'], 'caps': n['caps']}
                      for rid, n in nodes.items()},
            'snapshot_at': (ontology_block.get('snapshot_at')
                            if isinstance(ontology_block, dict) else None),
            'time_sources': temporal_sources,
        },
        'coverage': stats,
        'gaps': gaps,
    }
    report = {
        'schema_version': SCHEMA_VERSION,
        'built_at': built_at,
        'manifest': revision,
        'coverage': stats,
        'edges': len(edges),
        'concepts': len(concepts),
        'edge_types': sorted({e['r'] for e in edges}),
        'gaps': gaps,
        'limits': [
            'Authored frontmatter only; a name, description or body similarity creates no edge.',
            'date_added is the catalog-add date, not content freshness.',
            'Snapshot time from Auto Ontology is stored apart and is never a source update.',
        ],
    }
    return document, report


AUTHORED_KEYS = frozenset({
    'category', 'domain', 'subdomain', 'tags', 'tools', 'allowed-tools', 'allowed_tools',
    'depends-on', 'depends_on', 'requires', 'prerequisites', 'version', 'date_added',
    'version_recorded_at', 'versioned_at', 'last_updated', 'updated_at', 'verified_at',
    'source', 'source_repo', 'upstream_commit', 'license', 'author', 'platforms',
    'compatibility', 'risk', 'name', 'description',
})


# ---------------------------------------------------------------- persistence

def write_projection(document, graph_path, temporal_path):
    """Write the two projections with an integrity hash beside each."""
    graph_path = Path(graph_path)
    temporal_path = Path(temporal_path)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    temporal_path.parent.mkdir(parents=True, exist_ok=True)

    graph_doc = {
        'schema_version': document['schema_version'],
        'kind': 'graph',
        'built_at': document['built_at'],
        'manifest': document['manifest'],
        'depends_on': ['catalog_sha256', 'taxonomy_revision', 'ontology_revision'],
        'source_index': document['source_index'],
        'ontology': document['ontology'],
        'nodes': document['graph']['nodes'],
        'edges': document['graph']['edges'],
        'concepts': document['graph']['concepts'],
        'coverage': document['coverage'],
        'gaps': document['gaps'],
    }
    temporal_doc = {
        'schema_version': document['schema_version'],
        'kind': 'temporal',
        'built_at': document['built_at'],
        'manifest': document['manifest'],
        'depends_on': ['catalog_sha256', 'temporal_revision'],
        'source_index': document['source_index'],
        'nodes': document['temporal']['nodes'],
        'snapshot_at': document['temporal']['snapshot_at'],
        'time_sources': document['temporal']['time_sources'],
        'coverage': document['coverage'],
        'gaps': [g for g in document['gaps']
                 if g.get('kind') in ('no_source_timestamps', 'source_index_mismatch',
                                      'missing_source', 'unreadable_source')],
    }

    written = {}
    for path, payload in ((graph_path, graph_doc), (temporal_path, temporal_doc)):
        blob = canonical(payload)
        path.write_bytes(blob)
        sidecar = path.with_suffix(path.suffix + '.sha256')
        sidecar.write_text(hashlib.sha256(blob).hexdigest() + '\n', encoding='utf-8')
        written[str(path)] = {'sha256': hashlib.sha256(blob).hexdigest(),
                              'bytes': len(blob)}
    return written


def load_projection(path, *, expected_kind=None):
    """Read a projection and check the bytes against its sidecar hash.

    A projection without a readable sidecar hash is refused: an unverifiable artefact must
    not be scored as if it were the snapshot the caller pinned.
    """
    path = Path(path)
    blob = path.read_bytes()
    actual = hashlib.sha256(blob).hexdigest()
    sidecar = path.with_suffix(path.suffix + '.sha256')
    if sidecar.is_file():
        declared = sidecar.read_text(encoding='utf-8').strip()
        if declared != actual:
            raise ProjectionError('projection hash mismatch')
    else:
        raise ProjectionError('projection has no integrity sidecar')
    try:
        document = json.loads(blob)
    except ValueError as error:
        raise ProjectionError('projection is not valid JSON') from error
    if not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION:
        raise ProjectionError('projection schema is not the supported version')
    if expected_kind and document.get('kind') != expected_kind:
        raise ProjectionError(f'projection is {document.get("kind")}, not {expected_kind}')
    document['_loaded'] = {'path': str(path), 'sha256': actual, 'bytes': len(blob)}
    return document


class ProjectionError(RuntimeError):
    """The projection cannot be trusted for this call."""
