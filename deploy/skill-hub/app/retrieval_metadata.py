"""Classification and duplicate-equivalence projection for the skill catalog.

Two jobs, both additive to the existing catalog:

1. Resolve a resource's category from every place authors actually put it:
   top-level ``category``, nested ``metadata.category``, and a ``categories``
   list. Authored values always win; missing values stay UNKNOWN instead of
   being invented. Enrichment is recorded with its own source and review
   status so a derived label can never look authored.

2. Describe duplicate structure without merging anything. A *full* equivalence
   needs the same bundle digest, the same document digest and the same risk.
   The same body in two different bundles is a *body* equivalence: it is
   reported, never collapsed, because the shipped scripts differ. A shared
   base name is a *family*, which is a ranking hint, not a merge.

The projection is a versioned JSON artifact keyed by qualified id
(``skill:NAME`` / ``prompt:NAME``). Its manifest pins the revision of the
grouping and taxonomy plus a digest of the exact catalog rows it describes, so
a consumer can refuse a stale or mismatched artifact instead of using it
silently.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
GROUPING_REVISION = 'equivalence-v1'
TAXONOMY_REVISION = 'metadata-classification-v1'

UNKNOWN = 'UNKNOWN'
AUTHORED = 'authored'
INFERRED = 'inferred'
UNVERIFIED = 'unverified'

STATE_AUTHORED = 'AUTHORED'
STATE_INFERRED = 'INFERRED'
STATE_UNKNOWN = 'UNKNOWN'

MERGE_FULL = 'FULL'
MERGE_NONE = 'NONE'


class ProjectionError(Exception):
    """The projection is missing, unreadable, or does not match the manifest."""


def _category_list(value: Any) -> Optional[List[str]]:
    """Normalize one authored category field to a list, or None when unusable."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return None
            out.append(item.strip())
        return out
    return None


def normalize_categories(*values: Any) -> List[str]:
    """Union category values in first-seen order, dropping blanks and duplicates."""
    out: List[str] = []
    for value in values:
        for item in _category_list(value) or []:
            if item not in out:
                out.append(item)
    return out


def classify(metadata: Any, enrichment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the authored category set and record how it was obtained.

    ``metadata`` is the parsed frontmatter mapping. Nothing is rewritten: the
    raw inputs are echoed in ``provenance`` so a reviewer can see exactly which
    field supplied the value and whether two fields disagreed.

    A conflict keeps every distinct value (top-level first) and sets
    ``conflict``; it is never resolved by silently dropping the nested value.
    A missing or unusable value is UNKNOWN with a concrete reason. Enrichment
    is only applied to UNKNOWN and is labelled INFERRED/unverified.
    """
    meta = metadata if isinstance(metadata, dict) else {}
    nested_map = meta.get('metadata') if isinstance(meta.get('metadata'), dict) else {}

    raw = {
        'category': meta.get('category'),
        'metadata.category': nested_map.get('category'),
        'categories': meta.get('categories'),
    }
    normalized: Dict[str, List[str]] = {}
    invalid: List[str] = []
    for field, value in raw.items():
        items = _category_list(value)
        if items is None:
            invalid.append(field)
            items = []
        normalized[field] = items

    top = normalized['category']
    nested = normalized['metadata.category']
    listed = normalized['categories']
    conflict = bool(top and nested and top != nested)

    if top:
        primary = 'category'
    elif nested:
        primary = 'metadata.category'
    elif listed:
        primary = 'categories'
    else:
        primary = None

    categories = normalize_categories(top, nested, listed)
    state = STATE_AUTHORED if categories else STATE_UNKNOWN
    review_status = AUTHORED if categories else UNVERIFIED
    reason: Optional[str] = None
    enrichment_used: Optional[Dict[str, Any]] = None
    enrichment_ignored = False

    if not categories:
        if invalid:
            reason = 'invalid category type in: ' + ', '.join(invalid)
        elif any(raw[field] not in (None, '', [], {}) for field in raw):
            reason = 'category present but empty after normalization'
        else:
            reason = 'no authored category in metadata'
        if enrichment:
            extra = _category_list(enrichment.get('categories'))
            if extra:
                categories = extra
                state = STATE_INFERRED
                review_status = INFERRED
                reason = None
                enrichment_used = {
                    'source': enrichment.get('source') or 'unspecified-enrichment',
                    'review_status': INFERRED,
                }
    elif enrichment:
        # Authored classification is preserved; derived labels never overwrite it.
        enrichment_ignored = True

    return {
        'category': '; '.join(categories),
        'categories': categories,
        'state': state,
        'review_status': review_status,
        'reason': reason,
        'provenance': {
            'primary': primary,
            'sources': normalized,
            'conflict': conflict,
            'conflict_values': ({'category': top, 'metadata.category': nested}
                                if conflict else {}),
            'invalid_fields': invalid,
            'enrichment': enrichment_used,
            'enrichment_ignored': enrichment_ignored,
        },
    }


def family_id(name: str) -> str:
    """Stable family id from the base name before any ``--source`` suffix."""
    base = (name or '').split('--')[0].strip()
    digest = hashlib.sha256(base.encode('utf-8')).hexdigest()[:12]
    return 'family-' + digest


def assign_equivalence(resources: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]], Dict[str, List[str]]]:
    """Annotate resources with full/body/family grouping; never merge bodies.

    ``resources`` items need ``id``, ``kind``, ``name``, ``content_sha256``,
    ``bundle_sha256`` and ``risk``. Returns the annotated list plus the
    mergeable group map and the family map.
    """
    full_keys: Dict[Tuple[str, str, str, str], List[str]] = {}
    body_keys: Dict[Tuple[str, str], List[str]] = {}
    family_keys: Dict[str, List[str]] = {}

    for item in resources:
        kind = item.get('kind') or ''
        content = item.get('content_sha256') or ''
        bundle = item.get('bundle_sha256') or ''
        if content and bundle:
            full_keys.setdefault((kind, bundle, content, item.get('risk') or 'unknown'), []).append(item['id'])
        if content:
            body_keys.setdefault((kind, content), []).append(item['id'])
        family_keys.setdefault(family_id(item.get('name') or ''), []).append(item['id'])

    full_group: Dict[str, str] = {}
    body_group: Dict[str, str] = {}
    fam_group: Dict[str, str] = {}
    groups: Dict[str, List[str]] = {}
    families: Dict[str, List[str]] = {}

    for key, ids in full_keys.items():
        if len(ids) < 2:
            continue
        label = 'eq-' + hashlib.sha256(json.dumps(list(key), separators=(',', ':')).encode()).hexdigest()[:16]
        groups[label] = sorted(ids)
        for resource_id in ids:
            full_group[resource_id] = label

    for key, ids in body_keys.items():
        if len(ids) < 2:
            continue
        label = 'body-' + hashlib.sha256(json.dumps(list(key), separators=(',', ':')).encode()).hexdigest()[:16]
        for resource_id in ids:
            body_group[resource_id] = label

    for key, ids in family_keys.items():
        if len(ids) < 2:
            continue
        families[key] = sorted(ids)
        for resource_id in ids:
            fam_group[resource_id] = key

    annotated: List[Dict[str, Any]] = []
    for item in resources:
        resource_id = item['id']
        group = full_group.get(resource_id)
        merge = MERGE_FULL if group else MERGE_NONE
        if group:
            reason = 'identical bundle digest, document digest and risk'
        elif body_group.get(resource_id):
            reason = 'same document body, different bundle or risk; kept separate'
        else:
            reason = 'no full equivalent in this catalog snapshot'
        merged = dict(item)
        merged.update({
            'equivalence_group_id': group,
            'equivalence_kind': merge,
            'equivalence_reason': reason,
            'body_group_id': body_group.get(resource_id),
            'family_id': fam_group.get(resource_id),
            'mergeable': bool(group),
            'permission_transfer': False,
        })
        annotated.append(merged)
    return annotated, groups, families


def catalog_digest(resources: Iterable[Dict[str, Any]]) -> str:
    """Digest of the exact catalog rows described, so a stale projection is detectable."""
    rows = [
        {
            'id': item['id'],
            'content_sha256': item.get('content_sha256') or '',
            'bundle_sha256': item.get('bundle_sha256') or '',
            'active': int(item.get('active', 1)),
        }
        for item in resources
    ]
    rows.sort(key=lambda row: row['id'])
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def build_projection(
    resources: Sequence[Dict[str, Any]],
    *,
    source_hub: Optional[str] = None,
    generated_at: Optional[str] = None,
    output_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Build (and optionally write) the versioned metadata projection.

    Each resource item: ``id`` (qualified), ``kind``, ``name``, ``category``,
    ``categories``, ``category_state``, ``category_reason``,
    ``category_provenance``, ``category_review_status``, ``tags_source``,
    ``content_sha256``, ``bundle_sha256``, ``risk``, ``active``,
    ``canonical_id`` and ``is_alias``. Missing keys degrade to UNKNOWN/NONE.
    """
    annotated, groups, families = assign_equivalence(resources)
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in annotated:
        by_id[item['id']] = {
            'id': item['id'],
            'kind': item.get('kind'),
            'resource_id': item.get('name'),
            'canonical_id': item.get('canonical_id') or item['id'],
            'is_alias': bool(item.get('is_alias')),
            'active': bool(item.get('active', 1)),
            'category': item.get('category') or '',
            'categories': list(item.get('categories') or []),
            'category_state': item.get('category_state') or STATE_UNKNOWN,
            'category_reason': item.get('category_reason'),
            'category_review_status': item.get('category_review_status') or UNVERIFIED,
            'category_provenance': item.get('category_provenance') or {},
            'tags_source': item.get('tags_source') or UNKNOWN,
            'content_sha256': item.get('content_sha256') or '',
            'bundle_sha256': item.get('bundle_sha256') or '',
            'risk': item.get('risk') or 'unknown',
            'equivalence_group_id': item['equivalence_group_id'],
            'equivalence_kind': item['equivalence_kind'],
            'equivalence_reason': item['equivalence_reason'],
            'body_group_id': item['body_group_id'],
            'family_id': item['family_id'],
            'mergeable': item['mergeable'],
            'permission_transfer': False,
        }

    authored = sum(1 for row in by_id.values() if row['category_state'] == STATE_AUTHORED)
    inferred = sum(1 for row in by_id.values() if row['category_state'] == STATE_INFERRED)
    projection = {
        'schema_version': SCHEMA_VERSION,
        'grouping_revision': GROUPING_REVISION,
        'taxonomy_revision': TAXONOMY_REVISION,
        'catalog_sha256': catalog_digest(resources),
        'resource_count': len(by_id),
        'counts': {
            'authored_category': authored,
            'inferred_category': inferred,
            'unknown_category': len(by_id) - authored - inferred,
            'mergeable_groups': len(groups),
            'mergeable_resources': sum(len(ids) for ids in groups.values()),
            'families': len(families),
        },
        'generated_at': generated_at or dt.datetime.now().isoformat(),
        'source_hub': source_hub,
        'resources': by_id,
        'groups': groups,
        'families': families,
    }
    if output_path is not None:
        write_projection(projection, output_path)
    return projection


def write_projection(projection: Dict[str, Any], output_path: Path | str) -> Path:
    """Write atomically so a reader never sees a half-written projection."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid_hex())
    tmp.write_text(json.dumps(projection, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding='utf-8')
    os.replace(tmp, path)
    return path


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex


def projection_filename(catalog_sha256: str) -> str:
    """Versioned file name: schema, taxonomy and catalog digest are visible."""
    return f'metadata-projection-v{SCHEMA_VERSION}-{TAXONOMY_REVISION}-{catalog_sha256[:12]}.json'


class Projection:
    """Loaded projection with the lookups a retrieval fusion step needs."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.resources: Dict[str, Dict[str, Any]] = payload.get('resources') or {}
        self.groups: Dict[str, List[str]] = payload.get('groups') or {}
        self.families: Dict[str, List[str]] = payload.get('families') or {}

    @property
    def manifest(self) -> Dict[str, Any]:
        return {
            'schema_version': self.payload.get('schema_version'),
            'grouping_revision': self.payload.get('grouping_revision'),
            'taxonomy_revision': self.payload.get('taxonomy_revision'),
            'catalog_sha256': self.payload.get('catalog_sha256'),
        }

    def get(self, resource_id: str) -> Optional[Dict[str, Any]]:
        return self.resources.get(resource_id)

    def merge_group(self, resource_id: str) -> Optional[str]:
        row = self.resources.get(resource_id)
        return row.get('equivalence_group_id') if row else None

    def family(self, resource_id: str) -> Optional[str]:
        row = self.resources.get(resource_id)
        return row.get('family_id') if row else None

    def is_mergeable(self, resource_id: str) -> bool:
        row = self.resources.get(resource_id)
        return bool(row and row.get('mergeable'))

    def representatives(self) -> Dict[str, str]:
        """One vote per mergeable group: group id -> canonical resource id.

        A group without a canonical member falls back to the first id, so the
        map always names a resource that exists in the projection.
        """
        out: Dict[str, str] = {}
        for group, ids in self.groups.items():
            canonical = next(
                (rid for rid in ids
                 if not self.resources.get(rid, {}).get('is_alias')),
                ids[0] if ids else None)
            if canonical:
                out[group] = canonical
        return out


def load_projection(
    path: Path | str,
    *,
    manifest: Optional[Dict[str, Any]] = None,
) -> Projection:
    """Load a projection, refusing a missing file, stale schema or manifest mismatch.

    ``manifest`` may carry any subset of schema_version, grouping_revision,
    taxonomy_revision and catalog_sha256. Only supplied keys are compared; a
    mismatch is ProjectionError so the caller reports UNAVAILABLE rather than
    running against the wrong snapshot.
    """
    target = Path(path)
    if not target.is_file():
        raise ProjectionError(f'projection not found: {target}')
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ProjectionError(f'projection unreadable: {target} ({exc})') from exc
    if not isinstance(payload, dict) or 'resources' not in payload:
        raise ProjectionError(f'projection malformed: {target}')
    if payload.get('schema_version') != SCHEMA_VERSION:
        raise ProjectionError(
            f"schema mismatch: found {payload.get('schema_version')}, expected {SCHEMA_VERSION}")
    if payload.get('grouping_revision') != GROUPING_REVISION:
        raise ProjectionError(
            f"grouping revision mismatch: {payload.get('grouping_revision')}")
    if payload.get('taxonomy_revision') != TAXONOMY_REVISION:
        raise ProjectionError(
            f"taxonomy revision mismatch: {payload.get('taxonomy_revision')}")
    for key, expected in (manifest or {}).items():
        if key not in ('schema_version', 'grouping_revision', 'taxonomy_revision', 'catalog_sha256'):
            continue
        actual = payload.get(key)
        if expected is not None and actual != expected:
            raise ProjectionError(f'manifest mismatch on {key}: found {actual}, expected {expected}')
    return Projection(payload)
